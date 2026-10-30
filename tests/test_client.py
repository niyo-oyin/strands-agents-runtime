from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from strands_runtime.runtime import Runtime
from strands_runtime.server import create_app
from strands_runtime_client import HttpClient, InvocationError, new_session_id
from strands_runtime_client.base import iter_sse_events
from strands_runtime_client.events import ResultEvent, TextEvent
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


def test_http_client_can_continue_prompt_session_with_one_message(http_client: HttpClient) -> None:
    fakes.script(fakes.text("first answer"), fakes.text("second answer"))
    sid = new_session_id()
    http_client.invoke("first", session_id=sid)
    result = http_client.invoke(messages=[{"role": "user", "content": "second"}], session_id=sid)
    assert result.text == "second answer"
    assert [m["content"][0]["text"] for m in fakes.seen_messages()[-1]] == ["first", "first answer", "second"]


def test_http_client_submits_full_history_job_without_session(http_client: HttpClient) -> None:
    fakes.script(fakes.text("job answer"))
    job = http_client.submit(
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
    )
    result = job.result(wait=True)
    assert result is not None and result.text == "job answer"
    assert [m["role"] for m in fakes.seen_messages()[0]] == ["user", "assistant", "user"]


def test_http_client_raises_on_http_error(http_client: HttpClient) -> None:
    with pytest.raises(InvocationError, match="404"):
        http_client.invoke("hi", agent="ghost")


def test_http_client_raises_on_error_event(http_client: HttpClient) -> None:
    fakes.script()
    with pytest.raises(InvocationError, match="no scripted response"):
        http_client.invoke("hi")


def test_http_client_submits_and_follows_a_job(http_client: HttpClient) -> None:
    fakes.script(fakes.tool_use("echo", {"text": "x"}), fakes.text("finished"))

    job = http_client.submit("go", agent="echoer")
    assert job.job_id and len(job.session_id) >= 33
    result = job.result(wait=True)
    assert result is not None and result.text == "finished" and result.seq == 5

    status = job.status()
    assert status.state == "completed" and status.seq == 5
    tail = list(job.follow(after=3))
    assert [e.type for e in tail] == ["text", "result"]
    assert job.cancel().state == "completed"  # 終端なので記録が返るだけ

    # 別のプロセスからの再取得（job_id 省略は最新）。
    same = http_client.job(job.session_id)
    assert same.job_id == job.job_id and same.result() is not None


def test_http_client_job_failure_raises(http_client: HttpClient) -> None:
    fakes.script()
    job = http_client.submit("go")
    with pytest.raises(InvocationError, match="no scripted response"):
        job.result(wait=True)
    assert job.status().state == "failed"
