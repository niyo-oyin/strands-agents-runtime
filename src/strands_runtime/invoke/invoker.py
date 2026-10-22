"""The invocation pipeline: request → Agent → normalized event stream."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from strands.agent.agent_result import AgentResult
from strands.types.agent import AgentInput

from strands_runtime.definitions.schema import RuntimeConfig
from strands_runtime.errors import StrandsRuntimeError, UnknownAgentError
from strands_runtime.events import (
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

logger = logging.getLogger(__name__)

TOOL_RESULT_PREVIEW_CHARS = 500


class InvocationRequest(BaseModel):
    """The JSON payload accepted by ``POST /invocations`` (and by the CLI/client libraries)."""

    model_config = ConfigDict(extra="forbid")

    agent: str | None = None
    prompt: str | list[dict[str, Any]] = Field(min_length=1)
    session_id: str | None = None
    actor_id: str = ANONYMOUS_ACTOR
    stream: bool = True
    invocation_state: dict[str, Any] = Field(default_factory=dict)


class Invoker:
    def __init__(self, factory: AgentFactory, config: RuntimeConfig) -> None:
        self._factory = factory
        self._config = config

    def resolve_agent_name(self, request: InvocationRequest) -> str:
        name = request.agent or self._config.runtime.default_agent
        if name is None:
            raise UnknownAgentError("(none)", self._factory.registry.published)
        self._factory.registry.get(name)  # raises UnknownAgentError for unpublished names
        return name

    async def stream(self, request: InvocationRequest, *, session_id: str | None = None) -> AsyncIterator[Any]:
        """Yield normalized events. ``session_id`` (from a transport header) beats the payload's."""
        resolved_session = session_id or request.session_id or str(uuid.uuid4())
        session = SessionContext(session_id=resolved_session, actor_id=request.actor_id)
        name = self.resolve_agent_name(request)
        yield StartEvent(agent=name, session_id=session.session_id)

        terminal = False
        try:
            agent = self._factory.build(name, session)
            limits = agent_limits(self._factory, name)
            async for raw in agent.stream_async(
                cast(AgentInput, request.prompt), invocation_state=dict(request.invocation_state), limits=limits
            ):
                for event in normalize(raw, agent=name, session_id=session.session_id):
                    terminal = terminal or isinstance(event, ResultEvent)
                    yield event
        except StrandsRuntimeError as exc:
            terminal = True
            yield ErrorEvent(error_type=type(exc).__name__, message=str(exc))
        except Exception as exc:
            terminal = True
            logger.exception("invocation failed (agent=%s session=%s)", name, session.session_id)
            yield ErrorEvent(error_type=type(exc).__name__, message=str(exc))
        if not terminal:
            yield ErrorEvent(error_type="NoResult", message="the agent stopped without producing a result")

    async def invoke(self, request: InvocationRequest, *, session_id: str | None = None) -> ResultEvent | ErrorEvent:
        """Run to completion and return the final ``result`` (or the first ``error``)."""
        async for event in self.stream(request, session_id=session_id):
            if isinstance(event, ResultEvent | ErrorEvent):
                return event
        return ErrorEvent(error_type="NoResult", message="the agent produced no result")


def agent_limits(factory: AgentFactory, name: str) -> Any:
    spec = factory.registry.get(name).definition.limits
    return spec.to_limits() if spec is not None else None


def normalize(raw: dict[str, Any], *, agent: str, session_id: str) -> list[Any]:
    """Map one raw Strands stream event to zero or more runtime events."""
    if "data" in raw and isinstance(raw["data"], str) and raw["data"]:
        return [TextEvent(text=raw["data"])]
    if raw.get("reasoningText"):
        return [ReasoningEvent(text=str(raw["reasoningText"]))]
    if "message" in raw:
        return _from_message(raw["message"])
    if "result" in raw and isinstance(raw["result"], AgentResult):
        return _from_result(raw["result"], agent=agent, session_id=session_id)
    # ``force_stop`` precedes an exception the SDK re-raises; the raised error becomes the single ErrorEvent.
    return []


def _from_message(message: Any) -> list[Any]:
    if not isinstance(message, dict):
        return []
    events: list[Any] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if "toolUse" in block:
            tu = block["toolUse"]
            events.append(
                ToolUseEvent(
                    tool_use_id=str(tu.get("toolUseId", "")), name=str(tu.get("name", "")), input=tu.get("input")
                )
            )
        elif "toolResult" in block:
            tr = block["toolResult"]
            events.append(
                ToolResultEvent(
                    tool_use_id=str(tr.get("toolUseId", "")),
                    status=str(tr.get("status", "")),
                    preview=_preview(tr.get("content") or []),
                )
            )
    return events


def _preview(content: list[Any]) -> str:
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            text = str(item["text"])
            return text if len(text) <= TOOL_RESULT_PREVIEW_CHARS else text[:TOOL_RESULT_PREVIEW_CHARS] + "…"
        if isinstance(item, dict) and "json" in item:
            return f"<json {type(item['json']).__name__}>"
    return ""


def _from_result(result: AgentResult, *, agent: str, session_id: str) -> list[Any]:
    events: list[Any] = []
    if result.interrupts:
        events.append(
            InterruptEvent(interrupts=[InterruptInfo(id=i.id, name=i.name, reason=i.reason) for i in result.interrupts])
        )
    usage = result.metrics.accumulated_usage
    perf = result.metrics.accumulated_metrics
    events.append(
        ResultEvent(
            agent=agent,
            session_id=session_id,
            stop_reason=str(result.stop_reason),
            text=_message_text(result.message),
            structured_output=(
                result.structured_output.model_dump(mode="json") if result.structured_output is not None else None
            ),
            metrics=Metrics(
                input_tokens=int(usage.get("inputTokens", 0)),
                output_tokens=int(usage.get("outputTokens", 0)),
                total_tokens=int(usage.get("totalTokens", 0)),
                latency_ms=int(perf.get("latencyMs", 0)),
                cycles=result.metrics.cycle_count,
                tool_calls=sum(m.call_count for m in result.metrics.tool_metrics.values()),
            ),
        )
    )
    return events


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return "".join(
        str(block["text"])
        for block in message.get("content") or []
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
