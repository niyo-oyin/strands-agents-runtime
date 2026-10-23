"""呼び出しの流れ。リクエスト → Agent の構築 → Strands のストリームを正規化したイベント列にする。"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext
from typing import Any, Literal, cast

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, model_validator
from strands.agent.agent_result import AgentResult
from strands.types.agent import AgentInput, Limits

from strands_runtime.definitions.schema import RuntimeConfig
from strands_runtime.errors import SessionBusyError, StrandsRuntimeError, UnknownAgentError
from strands_runtime.events import (
    AgentUsageEvent,
    ErrorEvent,
    InterruptEvent,
    InterruptInfo,
    Metrics,
    ReasoningEvent,
    ResultEvent,
    StartEvent,
    TextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from strands_runtime.factory import AgentFactory, SessionContext
from strands_runtime.factory.sessions import ANONYMOUS_ACTOR
from strands_runtime.invoke.usage import UsageCollector

logger = logging.getLogger(__name__)

TOOL_RESULT_PREVIEW_CHARS = 500
CANCEL_DRAIN_SECONDS = 15.0
"""タイムアウトで停止を要求してから、エージェントが片付け（履歴の保存など）を終えるのを待つ猶予。"""

ToolResultDetail = Literal["preview", "full"]
Mode = Literal["run", "job", "status", "follow", "result", "cancel"]
EXECUTING_MODES: frozenset[str] = frozenset({"run", "job"})


class InvocationRequest(BaseModel):
    """``POST /invocations`` が受け付ける JSON ペイロード。CLI とクライアントライブラリも同じ形を使う。"""

    model_config = ConfigDict(extra="forbid")

    agent: str | None = None
    prompt: str | list[dict[str, Any]] | None = Field(default=None, min_length=1)
    """文字列か、Strands の content block の配列。``mode`` が ``run`` / ``job`` のとき必須。
    image / document / video ブロックの ``source.bytes`` は base64 文字列で渡す。
    中断した会話は ``[{"interruptResponse": {"interruptId": ..., "response": ...}}]`` で再開する。"""
    session_id: str | None = None
    actor_id: str = ANONYMOUS_ACTOR
    stream: bool = True
    invocation_state: dict[str, Any] = Field(default_factory=dict)
    include_reasoning: bool = False
    """モデルの推論の差分を ``reasoning`` イベントとして流す。量が多いため既定では流さない。"""
    tool_result_detail: ToolResultDetail = "preview"
    """``"full"`` にすると ``tool_result`` イベントに結果の content block 全体を含める。"""
    include_subagents: bool = False
    """サブエージェント内のイベント（メッセージ単位の text、tool_use、tool_result、interrupt）を
    ``agent`` / ``parent`` 付きで流す。既定では子の実行は親の tool_use / tool_result に畳み込まれる。"""
    mode: Mode = "run"
    """``run`` 以外はジョブの操作（設計文書 §7.6.2）。"""
    job_id: str | None = None
    """``status`` / ``follow`` / ``result`` / ``cancel`` の対象。省略するとセッションの最新のジョブ。"""
    after: int = Field(default=0, ge=0)
    """``follow`` の再生開始位置。この ``seq`` の次のイベントから返す。"""

    @model_validator(mode="after")
    def _prompt_when_executing(self) -> InvocationRequest:
        if self.mode in EXECUTING_MODES and self.prompt is None:
            raise ValueError(f"prompt is required when mode is {self.mode!r}")
        return self


class Invoker:
    def __init__(self, factory: AgentFactory, config: RuntimeConfig) -> None:
        self._factory = factory
        self._config = config
        # 実行中のセッション ID。同じセッションの同時呼び出しを拒否するために使う（プロセス内のみ）。
        self._active_sessions: set[str] = set()
        # プロセスの外（ジョブのジャーナル）でセッションが使用中かを確かめる追加の判定。JobManager が差し込む。
        self.busy_check: Callable[[str], Awaitable[bool]] | None = None

    def resolve_agent_name(self, request: InvocationRequest) -> str:
        """ペイロードの ``agent``（なければ既定エージェント）を、公開中であることを確認したうえで返す。"""
        name = request.agent or self._config.runtime.default_agent
        if name is None:
            raise UnknownAgentError("(none)", self._factory.registry.published)
        self._factory.registry.get(name)  # 公開されていない名前なら UnknownAgentError
        return name

    async def stream(
        self,
        request: InvocationRequest,
        *,
        session_id: str | None = None,
        cancel_signal: threading.Event | None = None,
        coalesce_text: bool = False,
        timeout_cap: float | None = None,
        span_attributes: dict[str, Any] | None = None,
    ) -> AsyncIterator[Any]:
        """正規化したイベントを順に返す。``session_id``（トランスポートのヘッダー由来）はペイロードの値より優先する。

        :class:`UnknownAgentError` と :class:`SessionBusyError` は最初のイベントを返す前に送出するので、
        サーバー層は HTTP のステータスコードで応答できる。
        それ以降の問題はイベントとして報告し、必ず ``result`` か ``error`` で終わる。

        ``cancel_signal`` を渡すと呼び出し側が停止を要求できる（ジョブの ``cancel``）。
        ``coalesce_text`` はテキストの差分を流さず、メッセージ単位の ``text`` にまとめる（ジョブのジャーナル用）。
        ``timeout_cap`` は定義の ``limits.timeout_seconds`` とは別の上限で、短い方が効く。
        ``span_attributes`` は実行の ``invoke_agent`` スパンに足す属性（ジョブ ID など）。
        """
        session = SessionContext(
            session_id=session_id or request.session_id or str(uuid.uuid4()), actor_id=request.actor_id
        )
        name = self.resolve_agent_name(request)
        if session.session_id in self._active_sessions:
            raise SessionBusyError(session.session_id)
        if self.busy_check is not None and await self.busy_check(session.session_id):
            raise SessionBusyError(session.session_id)

        self._active_sessions.add(session.session_id)
        run = _Run(
            request,
            agent_name=name,
            session=session,
            cancel=cancel_signal,
            coalesce_text=coalesce_text,
            timeout_cap=timeout_cap,
            span_attributes=span_attributes,
        )
        try:
            yield StartEvent(agent=name, session_id=session.session_id)
            async for event in self._execute(run):
                yield event
        finally:
            self._active_sessions.discard(session.session_id)

    async def _execute(self, run: _Run) -> AsyncIterator[Any]:
        """Agent を組み立てて実行し、イベントを流す。タイムアウト・キャンセル・後始末もここで扱う。"""
        definition = self._factory.registry.get(run.agent_name).definition
        limits = definition.limits.to_limits() if definition.limits else None
        timeout = _min_timeout(definition.limits.timeout_seconds if definition.limits else None, run.timeout_cap)
        inner: AsyncIterator[dict[str, Any]] | None = None
        try:
            assert run.request.prompt is not None  # mode が run / job なら検証済み
            agent = self._factory.build(run.agent_name, run.session, hooks=[run.usage])
            inner = agent.stream_async(
                decode_prompt(run.request.prompt),
                invocation_state=dict(run.request.invocation_state),
                limits=cast("Limits | None", limits or None),
                cancel_signal=run.cancel,
            )
            # タイムアウトは 2 段構え。
            # 1. 監視タイマーが ``cancel_signal`` を立て、エージェントは次の安全な地点で停止して履歴を保存する
            #    （``stop_reason`` が "timeout" の ``result`` が返る）。
            # 2. 猶予（CANCEL_DRAIN_SECONDS）を過ぎても止まらなければ、ストリームを打ち切って ``error`` にする。
            watchdog = asyncio.get_running_loop().call_later(timeout, run.request_timeout) if timeout else None
            try:
                async with asyncio.timeout(timeout + CANCEL_DRAIN_SECONDS) if timeout else nullcontext():
                    async for raw in inner:
                        for event in run.normalize(raw):
                            yield event
            except TimeoutError:
                yield run.error("Timeout", f"invocation exceeded {timeout}s and did not stop within the grace period")
            finally:
                if watchdog is not None:
                    watchdog.cancel()
        except StrandsRuntimeError as exc:
            yield run.error(type(exc).__name__, str(exc))
        except Exception as exc:
            logger.exception("invocation failed (agent=%s session=%s)", run.agent_name, run.session.session_id)
            yield run.error(type(exc).__name__, str(exc))
        finally:
            # 結果もエラーも出していないのに finally に来るのは、クライアントが切断したか、途中で抜けた場合。
            # エージェントの実行を続けても誰も受け取らないので、停止を要求する。
            if not run.terminal:
                run.cancel.set()
            if inner is not None:
                await _aclose(inner)
        if not run.terminal:
            yield run.error("NoResult", "the agent stopped without producing a result")

    async def invoke(self, request: InvocationRequest, *, session_id: str | None = None) -> ResultEvent | ErrorEvent:
        """最後まで実行し、最終の ``result``（または最初の ``error``）を返す。

        ストリームは必ず最後まで消費する。
        途中で抜けると Agent の非同期ジェネレーターがガベージコレクション時に別のコンテキストで閉じられ、
        SDK のスパン管理が壊れる。
        """
        final: ResultEvent | ErrorEvent | None = None
        async for event in self.stream(request, session_id=session_id):
            if final is None and isinstance(event, ResultEvent | ErrorEvent):
                final = event
        return final if final is not None else ErrorEvent(error_type="NoResult", message="the agent produced no result")


def _min_timeout(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


async def _aclose(iterator: AsyncIterator[Any]) -> None:
    """Agent のストリームを明示的に閉じる。閉じる際の例外は結果に影響しないので記録だけする。"""
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        logger.debug("closing the agent stream raised", exc_info=True)


class _Run:
    """1 回の呼び出しの状態。オプション、経過時間、キャンセル、Strands イベントからランタイムイベントへの変換。"""

    def __init__(
        self,
        request: InvocationRequest,
        *,
        agent_name: str,
        session: SessionContext,
        cancel: threading.Event | None = None,
        coalesce_text: bool = False,
        timeout_cap: float | None = None,
        span_attributes: dict[str, Any] | None = None,
    ) -> None:
        self.request = request
        self.agent_name = agent_name
        self.session = session
        self.cancel = cancel if cancel is not None else threading.Event()
        self.coalesce_text = coalesce_text
        self.timeout_cap = timeout_cap
        self.span_attributes = dict(span_attributes or {})
        self.timed_out = False
        # ``result`` か ``error`` を出したら True。出さずに終わった場合の後始末と ``NoResult`` の判定に使う。
        self.terminal = False
        self.usage = UsageCollector()
        self._started = time.monotonic()

    def request_timeout(self) -> None:
        """監視タイマーから呼ばれる。停止理由を "timeout" にするための印を付けてから停止を要求する。"""
        self.timed_out = True
        self.cancel.set()

    def error(self, error_type: str, message: str) -> ErrorEvent:
        self.terminal = True
        return ErrorEvent(error_type=error_type, message=message)

    def normalize(self, raw: dict[str, Any]) -> list[Any]:
        """Strands の生イベント 1 つを、0 個以上のランタイムイベントにする。"""
        return self._normalize(raw, agent=None, parent=None)

    def _normalize(self, raw: dict[str, Any], *, agent: str | None, parent: str | None) -> list[Any]:
        """``agent`` / ``parent`` が付いているのはサブエージェント内のイベント。結果は親の tool_result に畳まれるので出さない。"""
        nested = parent is not None
        if "data" in raw and isinstance(raw["data"], str) and raw["data"]:
            # 差分は、ジョブ（メッセージ単位にまとめる）とサブエージェント（常にメッセージ単位）では流さない。
            return [] if self.coalesce_text or nested else [TextEvent(text=raw["data"])]
        if raw.get("reasoningText"):
            if nested or not self.request.include_reasoning:
                return []
            return [ReasoningEvent(text=str(raw["reasoningText"]))]
        if "message" in raw:
            return self._from_message(raw["message"], agent=agent, parent=parent, coalesce=self.coalesce_text or nested)
        if "result" in raw and isinstance(raw["result"], AgentResult):
            if nested:
                return []
            self.terminal = True
            return self._from_result(raw["result"])
        if "tool_stream_event" in raw and self.request.include_subagents:
            return self._from_tool_stream(raw["tool_stream_event"])
        # ``force_stop`` の後には SDK が例外を再送出するので、ここでは何も出さず例外側で 1 つの ``error`` にする。
        return []

    def _from_tool_stream(self, stream_event: Any) -> list[Any]:
        """サブエージェントが親のストリームに転送してきたイベントを、親子関係を付けて正規化する。

        Strands はサブエージェント（``as_tool``）の各イベントを ``tool_stream_event`` で包んで親に流す。
        ``tool_use`` の名前がサブエージェント名、``toolUseId`` が親子を結ぶ ``parent`` になる。
        通常のストリーミングツールも同じ包みを使うが、中身がエージェントのイベントでなければ何も出さない。
        """
        if not isinstance(stream_event, dict):
            return []
        tool_use = stream_event.get("tool_use")
        child = stream_event.get("data")
        if not isinstance(tool_use, dict) or not isinstance(child, dict):
            return []
        if not ({"data", "message", "result", "tool_stream_event", "reasoningText"} & child.keys()):
            return []
        return self._normalize(child, agent=str(tool_use.get("name", "")), parent=str(tool_use.get("toolUseId", "")))

    def _from_message(self, message: Any, *, agent: str | None, parent: str | None, coalesce: bool) -> list[Any]:
        """履歴に追加されたメッセージから、ツール呼び出し・ツール結果（と、まとめる場合は本文）のイベントを取り出す。"""
        if not isinstance(message, dict):
            return []
        events: list[Any] = []
        if coalesce and message.get("role") == "assistant":
            text = _message_text(message)
            if text:
                events.append(TextEvent(text=text, agent=agent, parent=parent))
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if "toolUse" in block:
                tu = block["toolUse"]
                events.append(
                    ToolUseEvent(
                        tool_use_id=str(tu.get("toolUseId", "")),
                        name=str(tu.get("name", "")),
                        input=tu.get("input"),
                        agent=agent,
                        parent=parent,
                    )
                )
            elif "toolResult" in block:
                tr = block["toolResult"]
                content = tr.get("content") or []
                events.append(
                    ToolResultEvent(
                        tool_use_id=str(tr.get("toolUseId", "")),
                        status=str(tr.get("status", "")),
                        preview=_preview(content),
                        content=content if self.request.tool_result_detail == "full" else None,
                        agent=agent,
                        parent=parent,
                    )
                )
        return events

    def _from_result(self, result: AgentResult) -> list[Any]:
        """最終結果から ``interrupt``（あれば）と ``result`` を作る。"""
        events: list[Any] = []
        if result.interrupts:
            events.append(
                InterruptEvent(
                    interrupts=[InterruptInfo(id=i.id, name=i.name, reason=i.reason) for i in result.interrupts]
                )
            )
        metrics = self._metrics(result)
        _record_usage_on_span(self.agent_name, self.session, metrics, self.span_attributes)
        events.append(
            ResultEvent(
                agent=self.agent_name,
                session_id=self.session.session_id,
                stop_reason=self._stop_reason(result),
                text=_message_text(result.message),
                structured_output=(
                    result.structured_output.model_dump(mode="json") if result.structured_output is not None else None
                ),
                metrics=metrics,
            )
        )
        return events

    def _metrics(self, result: AgentResult) -> Metrics:
        """エージェントツリー全体の合計は collector から取る。collector が空ならルートの値で代用する。"""
        totals = self.usage.totals()
        if not self.usage.agents:
            totals.add(cast(dict[str, int], dict(result.metrics.accumulated_usage)))
        return Metrics(
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            total_tokens=totals.total_tokens,
            cache_read_tokens=totals.cache_read_tokens,
            cache_write_tokens=totals.cache_write_tokens,
            # レイテンシーは SDK の値ではなく、呼び出し全体の実時間をランタイムで測る（報告しないプロバイダーがある）。
            latency_ms=int((time.monotonic() - self._started) * 1000),
            cycles=result.metrics.cycle_count,
            tool_calls=sum(m.call_count for m in result.metrics.tool_metrics.values()),
            model_failures=self.usage.model_failures,
            agents={
                name: AgentUsageEvent(
                    model=u.model,
                    invocations=u.invocations,
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    total_tokens=u.total_tokens,
                    cache_read_tokens=u.cache_read_tokens,
                    cache_write_tokens=u.cache_write_tokens,
                )
                for name, u in self.usage.agents.items()
            },
        )

    def _stop_reason(self, result: AgentResult) -> str:
        """停止理由は SDK の値を使う。ランタイムが置き換えるのは、構造化出力とタイムアウトの 2 つだけ。"""
        if self.timed_out and result.stop_reason == "cancelled":
            return "timeout"
        # SDK は構造化出力をツール呼び出しとして扱うため ``tool_use`` になる。クライアントには分かりにくいので置き換える。
        if result.structured_output is not None:
            return "structured_output"
        return str(result.stop_reason)


def _record_usage_on_span(agent: str, session: SessionContext, metrics: Metrics, extra: dict[str, Any]) -> None:
    """呼び出し全体の集計を、現在の OpenTelemetry スパンに属性として付ける。

    Strands のエージェントスパンの内側で呼ばれるため、通常はそのスパン（``invoke_agent``）に載る。
    合計値を ``gen_ai.usage.*`` にすると可視化ツールがモデル呼び出しの値と二重に数えるので、独自の名前空間を使う。
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.set_attributes(
        {
            "strands_runtime.agent": agent,
            "strands_runtime.session_id": session.session_id,
            "strands_runtime.actor_id": session.actor_id,
            "strands_runtime.usage.input_tokens": metrics.input_tokens,
            "strands_runtime.usage.output_tokens": metrics.output_tokens,
            "strands_runtime.usage.total_tokens": metrics.total_tokens,
            "strands_runtime.usage.cache_read_tokens": metrics.cache_read_tokens,
            "strands_runtime.usage.cache_write_tokens": metrics.cache_write_tokens,
            "strands_runtime.model_failures": metrics.model_failures,
            "strands_runtime.tool_calls": metrics.tool_calls,
            **extra,
        }
    )


def decode_prompt(prompt: str | list[dict[str, Any]]) -> AgentInput:
    """JSON のプロンプトを Strands の入力にする。メディアブロックの base64 ``source.bytes`` は bytes に戻す。"""
    if isinstance(prompt, str):
        return prompt
    return cast(AgentInput, [_decode_block(block) for block in prompt])


_MEDIA_KEYS = ("image", "document", "video")


def _decode_block(block: dict[str, Any]) -> dict[str, Any]:
    """content block 1 つを復号する。元の辞書は変更せず、変更した部分だけ新しい辞書にする。"""
    for key in _MEDIA_KEYS:
        media = block.get(key)
        if isinstance(media, dict):
            source = media.get("source")
            if isinstance(source, dict) and isinstance(source.get("bytes"), str):
                decoded = dict(source, bytes=base64.b64decode(source["bytes"]))
                return {**block, key: {**media, "source": decoded}}
    return block


def _preview(content: list[Any]) -> str:
    """ツール結果の先頭のテキストを、上限文字数で切って返す。JSON 結果は型名だけを示す。"""
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            text = str(item["text"])
            return text if len(text) <= TOOL_RESULT_PREVIEW_CHARS else text[:TOOL_RESULT_PREVIEW_CHARS] + "…"
        if isinstance(item, dict) and "json" in item:
            return f"<json {type(item['json']).__name__}>"
    return ""


def _message_text(message: Any) -> str:
    """メッセージ内のテキストブロックを連結する（toolUse などテキスト以外は無視）。"""
    if not isinstance(message, dict):
        return ""
    return "".join(
        str(block["text"])
        for block in message.get("content") or []
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
