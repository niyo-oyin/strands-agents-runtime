"""ランタイムの公開イベントスキーマ。サーバー（送り手）とクライアント（受け手）が同じ定義を使う。

Strands の ``stream_async`` が出す生イベントをこれらに正規化するので、呼び出し側は SDK の内部表現に依存しない。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StreamEvent(_Event):
    """ストリームに流れるイベントの共通フィールド。

    4 つとも任意で、同期ストリームのトップレベルのイベントでは ``None`` のまま。
    ``seq`` / ``ts`` はジョブのジャーナルに記録されたときに、``agent`` / ``parent`` はネストした実行
    （``include_subagents``）のイベントに付く。
    """

    seq: int | None = None
    """ジョブのジャーナル内の連番（1 始まり）。``follow`` の ``after`` に渡して再開位置を指定する。"""
    ts: str | None = None
    """ジャーナルに記録した時刻（ISO 8601、UTC）。"""
    agent: str | None = None
    """このイベントを発生させたエージェント名。"""
    parent: str | None = None
    """囲んでいる親の ``tool_use_id``（サブエージェント）またはノード ID（Graph / Swarm）。"""


ENVELOPE_FIELDS = frozenset({"seq", "ts", "agent", "parent"})


def dump_event(event: BaseModel) -> dict[str, Any]:
    """送信・保存用の辞書にする。共通フィールドは値があるときだけ含め、同期ストリームの出力を増やさない。"""
    data = event.model_dump(mode="json")
    for key in ENVELOPE_FIELDS:
        if key in data and data[key] is None:
            del data[key]
    return data


class StartEvent(_StreamEvent):
    """呼び出しの開始。解決済みのエージェント名と、以降の継続に使えるセッション ID を伝える。"""

    type: Literal["start"] = "start"
    agent: str
    session_id: str


class TextEvent(_StreamEvent):
    """モデルの回答テキストの差分。"""

    type: Literal["text"] = "text"
    text: str


class ReasoningEvent(_StreamEvent):
    """モデルの推論（思考）テキストの差分。リクエストで ``include_reasoning`` を有効にしたときだけ流れる。"""

    type: Literal["reasoning"] = "reasoning"
    text: str


class ToolUseEvent(_StreamEvent):
    """モデルがツールを呼ぶことを決めた。``input`` は呼び出しの引数。"""

    type: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    name: str
    input: Any = None


class ToolResultEvent(_StreamEvent):
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


class InterruptEvent(_StreamEvent):
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


class ResultEvent(_StreamEvent):
    """最終結果。ストリームでは最後のイベント、非ストリーミングでは応答本体になる。"""

    type: Literal["result"] = "result"
    agent: str
    session_id: str
    stop_reason: str
    text: str
    structured_output: dict[str, Any] | None = None
    metrics: Metrics = Field(default_factory=Metrics)


class ErrorEvent(_StreamEvent):
    """呼び出しが結果を出せずに終わった。``error_type`` は例外クラス名か、ランタイムが定める種別（Timeout など）。"""

    type: Literal["error"] = "error"
    error_type: str
    message: str


JobState = Literal["running", "cancelling", "completed", "input_required", "cancelled", "failed"]
TERMINAL_JOB_STATES: frozenset[str] = frozenset({"completed", "input_required", "cancelled", "failed"})
CancelledBy = Literal["client", "runtime"]


class JobLastEvent(_Event):
    """ジョブのジャーナルに最後に記録されたイベントの要約。"""

    seq: int
    ts: str
    type: str
    summary: str = ""
    """ツール名、本文の先頭、停止理由など、種類に応じた短い説明。"""


class JobEvent(_StreamEvent):
    """ジョブの記録。``mode`` が ``job`` / ``status`` / ``cancel`` の応答と、``result`` の未完了時に返る。"""

    type: Literal["job"] = "job"
    job_id: str
    session_id: str
    agent: str
    state: JobState
    accepted_at: str
    updated_at: str
    """最後にジャーナルを更新した時刻。実行中は心拍で定期的に進む。"""
    finished_at: str | None = None
    seq: int = 0
    """記録済みの最後の連番。"""
    last_event: JobLastEvent | None = None
    stop_reason: str | None = None
    """終端が ``result`` のとき、その ``stop_reason``。"""
    cancelled_by: CancelledBy | None = None
    """``cancel`` を要求したのがクライアントか、ランタイム（シャットダウン）か。"""
    error_type: str | None = None
    """終端が ``error`` のとき、その ``error_type``。"""

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_JOB_STATES


# ``type`` フィールドを判別子にした直和型。JSON からの復元はこれで行う。
RuntimeEvent = Annotated[
    StartEvent
    | TextEvent
    | ReasoningEvent
    | ToolUseEvent
    | ToolResultEvent
    | InterruptEvent
    | ResultEvent
    | ErrorEvent
    | JobEvent,
    Field(discriminator="type"),
]

runtime_event_adapter: TypeAdapter[Any] = TypeAdapter(RuntimeEvent)


def parse_event(data: str | bytes | dict[str, Any]) -> Any:
    """JSON テキスト・bytes・復元済みの辞書のいずれかから、イベント 1 つを復元する。"""
    if isinstance(data, dict):
        return runtime_event_adapter.validate_python(data)
    return runtime_event_adapter.validate_json(data)
