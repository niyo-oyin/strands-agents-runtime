"""ランタイムの公開イベントスキーマ。サーバー（送り手）とクライアント（受け手）が同じ定義を使う。

Strands の ``stream_async`` が出す生イベントをこれらに正規化するので、呼び出し側は SDK の内部表現に依存しない。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartEvent(_Event):
    """呼び出しの開始。解決済みのエージェント名と、以降の継続に使えるセッション ID を伝える。"""

    type: Literal["start"] = "start"
    agent: str
    session_id: str


class TextEvent(_Event):
    """モデルの回答テキストの差分。"""

    type: Literal["text"] = "text"
    text: str


class ReasoningEvent(_Event):
    """モデルの推論（思考）テキストの差分。リクエストで ``include_reasoning`` を有効にしたときだけ流れる。"""

    type: Literal["reasoning"] = "reasoning"
    text: str


class ToolUseEvent(_Event):
    """モデルがツールを呼ぶことを決めた。``input`` は呼び出しの引数。"""

    type: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    name: str
    input: Any = None


class ToolResultEvent(_Event):
    """ツールの実行結果。``tool_use_id`` で対応する :class:`ToolUseEvent` と結び付く。"""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    status: str
    preview: str = ""
    """結果の先頭のテキストブロックを一定文字数で切ったもの。"""
    content: list[Any] | None = None
    """結果の content block 全体。リクエストで ``tool_result_detail = "full"`` を指定したときだけ入る。"""


class InterruptInfo(_Event):
    id: str
    name: str
    reason: Any = None


class InterruptEvent(_Event):
    """ツールが人の判断を求めて中断した。``prompt`` に ``interruptResponse`` を入れて再開する。"""

    type: Literal["interrupt"] = "interrupt"
    interrupts: list[InterruptInfo]


class AgentUsageEvent(_Event):
    """エージェントツリー内の 1 エージェント（ルートまたはサブエージェント）のトークン使用量。"""

    model: str
    invocations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class Metrics(_Event):
    """呼び出し全体（エージェントツリー全体）の合計。``agents`` にエージェント別の内訳を持つ。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    cycles: int = 0
    tool_calls: int = 0
    model_failures: int = 0
    """例外で終わったモデル呼び出しの回数。``[model] fallbacks`` があれば、この回数だけ別候補に切り替わっている。"""
    agents: dict[str, AgentUsageEvent] = Field(default_factory=dict)


class ResultEvent(_Event):
    """最終結果。ストリームでは最後のイベント、非ストリーミングでは応答本体になる。"""

    type: Literal["result"] = "result"
    agent: str
    session_id: str
    stop_reason: str
    text: str
    structured_output: dict[str, Any] | None = None
    metrics: Metrics = Field(default_factory=Metrics)


class ErrorEvent(_Event):
    """呼び出しが結果を出せずに終わった。``error_type`` は例外クラス名か、ランタイムが定める種別（Timeout など）。"""

    type: Literal["error"] = "error"
    error_type: str
    message: str


# ``type`` フィールドを判別子にした直和型。JSON からの復元はこれで行う。
RuntimeEvent = Annotated[
    StartEvent
    | TextEvent
    | ReasoningEvent
    | ToolUseEvent
    | ToolResultEvent
    | InterruptEvent
    | ResultEvent
    | ErrorEvent,
    Field(discriminator="type"),
]

runtime_event_adapter: TypeAdapter[Any] = TypeAdapter(RuntimeEvent)


def parse_event(data: str | bytes | dict[str, Any]) -> Any:
    """JSON テキスト・bytes・復元済みの辞書のいずれかから、イベント 1 つを復元する。"""
    if isinstance(data, dict):
        return runtime_event_adapter.validate_python(data)
    return runtime_event_adapter.validate_json(data)
