from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from strands_runtime.client import HttpClient, InvocationError, new_session_id
from strands_runtime.client.base import iter_sse_events
from strands_runtime.events import ResultEvent, TextEvent
from strands_runtime.runtime import Runtime
from strands_runtime.server import create_app
from tests import fakes


def test_iter_sse_events_skips_non_data_lines() -> None:
    lines = [
        b": comment",
        b"event: ignored",
        b'data: {"type": "text", "text": "a"}',
        b"",
        'data: {"type": "result", "agent": "x", "session_id": "s", "stop_reason": "end_turn", "text": "a"}',
    ]
    events = list(iter_sse_events(lines))
    assert events == [TextEvent(text="a"), ResultEvent(agent="x", session_id="s", stop_reason="end_turn", text="a")]


def test_new_session_id_satisfies_agentcore_minimum_length() -> None:
    assert len(new_session_id()) >= 33


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def http_client(runtime: Runtime) -> Iterator[HttpClient]:
    """スレッドで動かす本物の uvicorn サーバー。HttpClient は同期なので、プロセス内の ASGI トランスポートでは代用できない。"""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(runtime), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    with HttpClient(f"http://127.0.0.1:{port}") as client:
        yield client
    server.should_exit = True
    thread.join(timeout=5)


def test_http_client_stream_and_invoke(http_client: HttpClient) -> None:
    fakes.script(fakes.text("streamed"), fakes.text("invoked"))
    sid = new_session_id()

    streamed = list(http_client.stream("hi", agent="echoer", session_id=sid))
    assert "".join(e.text for e in streamed if isinstance(e, TextEvent)) == "streamed"

    result = http_client.invoke("again", agent="echoer", session_id=sid)
    assert result.text == "invoked" and result.session_id == sid


def test_http_client_raises_on_http_error(http_client: HttpClient) -> None:
    with pytest.raises(InvocationError, match="404"):
        http_client.invoke("hi", agent="ghost")


def test_http_client_raises_on_error_event(http_client: HttpClient) -> None:
    fakes.script()
    with pytest.raises(InvocationError, match="no scripted response"):
        http_client.invoke("hi")
