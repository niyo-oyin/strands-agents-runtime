"""The runtime's public event schema, shared by the server (producer) and the clients (consumers).

Strands' raw ``stream_async`` events are normalized into these so callers never depend on SDK internals.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartEvent(_Event):
    type: Literal["start"] = "start"
    agent: str
    session_id: str


class TextEvent(_Event):
    type: Literal["text"] = "text"
    text: str


class ReasoningEvent(_Event):
    type: Literal["reasoning"] = "reasoning"
    text: str


class ToolUseEvent(_Event):
    type: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    name: str
    input: Any = None


class ToolResultEvent(_Event):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    status: str
    preview: str = ""
    """The first text block of the result, truncated; full results stay inside the agent's history."""


class InterruptInfo(_Event):
    id: str
    name: str
    reason: Any = None


class InterruptEvent(_Event):
    type: Literal["interrupt"] = "interrupt"
    interrupts: list[InterruptInfo]


class Metrics(_Event):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cycles: int = 0
    tool_calls: int = 0


class ResultEvent(_Event):
    type: Literal["result"] = "result"
    agent: str
    session_id: str
    stop_reason: str
    text: str
    structured_output: dict[str, Any] | None = None
    metrics: Metrics = Field(default_factory=Metrics)


class ErrorEvent(_Event):
    type: Literal["error"] = "error"
    error_type: str
    message: str


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
    """Parse one event from JSON text/bytes or an already-decoded mapping."""
    if isinstance(data, dict):
        return runtime_event_adapter.validate_python(data)
    return runtime_event_adapter.validate_json(data)
