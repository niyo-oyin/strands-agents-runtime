"""Client for a runtime reachable over plain HTTP (``strands-runtime serve`` or the Docker image)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from strands_runtime.client.base import AgentClient, InvocationError, iter_sse_events

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


class HttpClient(AgentClient):
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 600.0,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """``transport`` lets tests (or embedders) hand in e.g. ``httpx.ASGITransport(app)``."""
        self._client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def stream(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        agent: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        invocation_state: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        payload = self._payload(prompt, agent, session_id, actor_id, invocation_state)
        headers = {SESSION_HEADER: session_id} if session_id else {}
        with self._client.stream("POST", "/invocations", json=payload, headers=headers) as response:
            if response.status_code >= 400:
                body = response.read().decode("utf-8", errors="replace")
                raise InvocationError(f"HTTP {response.status_code}", body)
            yield from iter_sse_events(response.iter_lines())
