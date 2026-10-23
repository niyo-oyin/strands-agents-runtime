"""The invocation pipeline: request → Agent → normalized event stream."""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import nullcontext
from typing import Any, Literal, cast

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field
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
"""After a timeout the agent is asked to stop; this is how long it gets to wind down and persist."""

ToolResultDetail = Literal["preview", "full"]


class InvocationRequest(BaseModel):
    """The JSON payload accepted by ``POST /invocations`` (and by the CLI/client libraries)."""

    model_config = ConfigDict(extra="forbid")

    agent: str | None = None
    prompt: str | list[dict[str, Any]] = Field(min_length=1)
    """Text, or Strands content blocks. Binary ``source.bytes`` in image/document/video blocks is base64 text.
    An interrupted session is resumed with ``[{"interruptResponse": {"interruptId": ..., "response": ...}}]``."""
    session_id: str | None = None
    actor_id: str = ANONYMOUS_ACTOR
    stream: bool = True
    invocation_state: dict[str, Any] = Field(default_factory=dict)
    include_reasoning: bool = False
    """Emit the model's reasoning deltas as ``reasoning`` events (off by default: they are bulky)."""
    tool_result_detail: ToolResultDetail = "preview"
    """``"full"`` adds the complete content blocks to ``tool_result`` events."""


class Invoker:
    def __init__(self, factory: AgentFactory, config: RuntimeConfig) -> None:
        self._factory = factory
        self._config = config
        self._active_sessions: set[str] = set()

    def resolve_agent_name(self, request: InvocationRequest) -> str:
        name = request.agent or self._config.runtime.default_agent
        if name is None:
            raise UnknownAgentError("(none)", self._factory.registry.published)
        self._factory.registry.get(name)  # raises UnknownAgentError for unpublished names
        return name

    async def stream(self, request: InvocationRequest, *, session_id: str | None = None) -> AsyncIterator[Any]:
        """Yield normalized events. ``session_id`` (from a transport header) beats the payload's.

        Raises :class:`UnknownAgentError` / :class:`SessionBusyError` before the first event so transports can
        answer with a status code. Everything after that is reported as events, ending in ``result`` or ``error``.
        """
        session = SessionContext(
            session_id=session_id or request.session_id or str(uuid.uuid4()), actor_id=request.actor_id
        )
        name = self.resolve_agent_name(request)
        if session.session_id in self._active_sessions:
            raise SessionBusyError(session.session_id)

        self._active_sessions.add(session.session_id)
        run = _Run(request, agent_name=name, session=session)
        try:
            yield StartEvent(agent=name, session_id=session.session_id)
            async for event in self._execute(run):
                yield event
        finally:
            self._active_sessions.discard(session.session_id)

    async def _execute(self, run: _Run) -> AsyncIterator[Any]:
        definition = self._factory.registry.get(run.agent_name).definition
        limits = definition.limits.to_limits() if definition.limits else None
        timeout = definition.limits.timeout_seconds if definition.limits else None
        inner: AsyncIterator[dict[str, Any]] | None = None
        try:
            agent = self._factory.build(run.agent_name, run.session, hooks=[run.usage])
            inner = agent.stream_async(
                decode_prompt(run.request.prompt),
                invocation_state=dict(run.request.invocation_state),
                limits=cast("Limits | None", limits or None),
                cancel_signal=run.cancel,
            )
            # On timeout the agent is first asked to stop at its next safe point (so history is persisted and a
            # ``result`` with stop_reason "timeout" comes back); only if it ignores that for the grace period is
            # the stream torn down with an ``error``.
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
            if not run.terminal:
                # The consumer went away (client disconnect) or we are bailing out: stop the agent loop.
                run.cancel.set()
            if inner is not None:
                await _aclose(inner)
        if not run.terminal:
            yield run.error("NoResult", "the agent stopped without producing a result")

    async def invoke(self, request: InvocationRequest, *, session_id: str | None = None) -> ResultEvent | ErrorEvent:
        """Run to completion and return the final ``result`` (or the first ``error``).

        The stream is always consumed to its end: breaking out early leaves the agent's async generator
        to be closed by garbage collection from another context, which breaks the SDK's span bookkeeping.
        """
        final: ResultEvent | ErrorEvent | None = None
        async for event in self.stream(request, session_id=session_id):
            if final is None and isinstance(event, ResultEvent | ErrorEvent):
                final = event
        return final if final is not None else ErrorEvent(error_type="NoResult", message="the agent produced no result")


async def _aclose(iterator: AsyncIterator[Any]) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # closing is best effort; the invocation outcome was already reported
        logger.debug("closing the agent stream raised", exc_info=True)


class _Run:
    """Per-invocation state: options, timing, cancellation and the raw→runtime event mapping."""

    def __init__(self, request: InvocationRequest, *, agent_name: str, session: SessionContext) -> None:
        self.request = request
        self.agent_name = agent_name
        self.session = session
        self.cancel = threading.Event()
        self.timed_out = False
        self.terminal = False
        self.usage = UsageCollector()
        self._started = time.monotonic()

    def request_timeout(self) -> None:
        self.timed_out = True
        self.cancel.set()

    def error(self, error_type: str, message: str) -> ErrorEvent:
        self.terminal = True
        return ErrorEvent(error_type=error_type, message=message)

    def normalize(self, raw: dict[str, Any]) -> list[Any]:
        """Map one raw Strands stream event to zero or more runtime events."""
        if "data" in raw and isinstance(raw["data"], str) and raw["data"]:
            return [TextEvent(text=raw["data"])]
        if raw.get("reasoningText"):
            return [ReasoningEvent(text=str(raw["reasoningText"]))] if self.request.include_reasoning else []
        if "message" in raw:
            return self._from_message(raw["message"])
        if "result" in raw and isinstance(raw["result"], AgentResult):
            self.terminal = True
            return self._from_result(raw["result"])
        # ``force_stop`` precedes an exception the SDK re-raises; the raised error becomes the single ErrorEvent.
        return []

    def _from_message(self, message: Any) -> list[Any]:
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
                content = tr.get("content") or []
                events.append(
                    ToolResultEvent(
                        tool_use_id=str(tr.get("toolUseId", "")),
                        status=str(tr.get("status", "")),
                        preview=_preview(content),
                        content=content if self.request.tool_result_detail == "full" else None,
                    )
                )
        return events

    def _from_result(self, result: AgentResult) -> list[Any]:
        events: list[Any] = []
        if result.interrupts:
            events.append(
                InterruptEvent(
                    interrupts=[InterruptInfo(id=i.id, name=i.name, reason=i.reason) for i in result.interrupts]
                )
            )
        metrics = self._metrics(result)
        _record_usage_on_span(self.agent_name, self.session, metrics)
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
        """Totals across the agent tree from the collector; the root's own metrics are the fallback."""
        totals = self.usage.totals()
        if not self.usage.agents:
            totals.add(cast(dict[str, int], dict(result.metrics.accumulated_usage)))
        return Metrics(
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            total_tokens=totals.total_tokens,
            cache_read_tokens=totals.cache_read_tokens,
            cache_write_tokens=totals.cache_write_tokens,
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
        """Runtime stop reasons: the SDK's, except that a structured answer and a runtime timeout get their own."""
        if self.timed_out and result.stop_reason == "cancelled":
            return "timeout"
        if result.structured_output is not None:
            return "structured_output"
        return str(result.stop_reason)


def _record_usage_on_span(agent: str, session: SessionContext, metrics: Metrics) -> None:
    """Put the invocation's totals on the current OpenTelemetry span (the server request span when instrumented)."""
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
        }
    )


def decode_prompt(prompt: str | list[dict[str, Any]]) -> AgentInput:
    """Turn the JSON prompt into Strands input: base64 ``source.bytes`` in media blocks become real bytes."""
    if isinstance(prompt, str):
        return prompt
    return cast(AgentInput, [_decode_block(block) for block in prompt])


_MEDIA_KEYS = ("image", "document", "video")


def _decode_block(block: dict[str, Any]) -> dict[str, Any]:
    for key in _MEDIA_KEYS:
        media = block.get(key)
        if isinstance(media, dict):
            source = media.get("source")
            if isinstance(source, dict) and isinstance(source.get("bytes"), str):
                decoded = dict(source, bytes=base64.b64decode(source["bytes"]))
                return {**block, key: {**media, "source": decoded}}
    return block


def _preview(content: list[Any]) -> str:
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            text = str(item["text"])
            return text if len(text) <= TOOL_RESULT_PREVIEW_CHARS else text[:TOOL_RESULT_PREVIEW_CHARS] + "…"
        if isinstance(item, dict) and "json" in item:
            return f"<json {type(item['json']).__name__}>"
    return ""


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return "".join(
        str(block["text"])
        for block in message.get("content") or []
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
