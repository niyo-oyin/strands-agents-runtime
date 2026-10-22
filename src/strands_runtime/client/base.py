"""Shared client interface and SSE decoding."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Any

from strands_runtime.events import ErrorEvent, ResultEvent, parse_event


class InvocationError(Exception):
    """The runtime reported an error (an ``error`` event or a non-2xx response)."""

    def __init__(self, error_type: str, message: str) -> None:
        self.error_type = error_type
        super().__init__(f"{error_type}: {message}")


def new_session_id() -> str:
    """A fresh session id. UUID4 text is 36 characters, satisfying AgentCore's 33-character minimum."""
    return str(uuid.uuid4())


def iter_sse_events(lines: Iterable[str | bytes]) -> Iterator[Any]:
    """Decode ``data: {...}`` frames into runtime events; other SSE fields and blank lines are skipped."""
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line.startswith("data:"):
            body = line[5:].strip()
            if body:
                yield parse_event(body)


class AgentClient(ABC):
    """Synchronous client; the same shape for every transport."""

    @abstractmethod
    def stream(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """Yield runtime events as they arrive."""

    def invoke(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
    ) -> ResultEvent:
        """Run to completion; raise :class:`InvocationError` if the runtime reports an error."""
        for event in self.stream(
            prompt, agent=agent, session_id=session_id, actor_id=actor_id, invocation_state=invocation_state
        ):
            if isinstance(event, ResultEvent):
                return event
            if isinstance(event, ErrorEvent):
                raise InvocationError(event.error_type, event.message)
        raise InvocationError("NoResult", "the stream ended without a result event")

    @staticmethod
    def _payload(
        prompt: str | list[dict[str, Any]],
        agent: str | None,
        session_id: str | None,
        actor_id: str | None,
        invocation_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt, "stream": True}
        if agent is not None:
            payload["agent"] = agent
        if session_id is not None:
            payload["session_id"] = session_id
        if actor_id is not None:
            payload["actor_id"] = actor_id
        if invocation_state:
            payload["invocation_state"] = invocation_state
        return payload
