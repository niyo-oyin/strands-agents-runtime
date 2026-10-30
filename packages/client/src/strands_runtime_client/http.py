"""HTTP で直接到達できるランタイム（``strands-runtime serve`` や Docker イメージ）向けのクライアント。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from strands_runtime_client.base import AgentClient, InvocationError, iter_sse_events
from strands_runtime_client.events import parse_event

if TYPE_CHECKING:
    import httpx

# AgentCore がセッションの識別に使うヘッダー。他の基盤では任意で、ペイロードの session_id と同じ値を入れる。
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
        """``transport`` には ``httpx.MockTransport`` などの同期トランスポートを指定できる。"""
        try:
            import httpx
        except ModuleNotFoundError as exc:
            if exc.name != "httpx":
                raise
            raise ImportError("HttpClient requires strands-agents-runtime-client[http]") from exc

        self._client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, payload: dict[str, Any], *, session_id: str | None) -> Iterator[Any]:
        headers = {SESSION_HEADER: session_id} if session_id else {}
        with self._client.stream("POST", "/invocations", json=payload, headers=headers) as response:
            # 400 / 404 / 409 などはイベントではなく JSON のエラー本文で返るので、本文ごと例外にする。
            if response.status_code >= 400:
                body = response.read().decode("utf-8", errors="replace")
                raise InvocationError(f"HTTP {response.status_code}", body)
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                yield from iter_sse_events(response.iter_lines())
            else:
                # 非ストリーミングの応答（ジョブの記録、result イベント）は JSON 1 つ。
                yield parse_event(response.read())
