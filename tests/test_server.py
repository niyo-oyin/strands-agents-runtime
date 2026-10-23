from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from strands_runtime.client.base import iter_sse_events
from strands_runtime.events import ResultEvent, StartEvent
from strands_runtime.runtime import Runtime
from strands_runtime.server import create_app
from tests import fakes

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


@pytest.fixture
def client(runtime: Runtime) -> TestClient:
    return TestClient(create_app(runtime))


def test_ping(client: TestClient) -> None:
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json()["status"] == "Healthy"


def test_streaming_invocation_uses_header_session(client: TestClient) -> None:
    fakes.script(fakes.text("hello"))
    with client.stream(
        "POST", "/invocations", json={"agent": "echoer", "prompt": "hi"}, headers={SESSION_HEADER: "hdr-session-000001"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = list(iter_sse_events(response.iter_lines()))

    assert isinstance(events[0], StartEvent) and events[0].session_id == "hdr-session-000001"
    result = events[-1]
    assert isinstance(result, ResultEvent) and result.text == "hello" and result.session_id == "hdr-session-000001"


def test_non_streaming_invocation_returns_json_result(client: TestClient) -> None:
    fakes.script(fakes.text("sync answer"))
    response = client.post("/invocations", json={"prompt": "hi", "stream": False, "session_id": "s-json"})
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "result" and body["text"] == "sync answer" and body["agent"] == "echoer"


def test_unknown_agent_is_404(client: TestClient) -> None:
    response = client.post("/invocations", json={"agent": "ghost", "prompt": "hi"})
    assert response.status_code == 404
    assert "ghost" in response.json()["error"]


def test_invalid_payload_is_400(client: TestClient) -> None:
    response = client.post("/invocations", json={"prompt": "hi", "system_prompt": "override"})
    assert response.status_code == 400
    assert "system_prompt" in response.json()["error"]

    response = client.post("/invocations", content=b"not json", headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_non_streaming_error_is_500(client: TestClient) -> None:
    fakes.script()
    response = client.post("/invocations", json={"prompt": "hi", "stream": False})
    assert response.status_code == 500
    assert response.json()["type"] == "error"


def test_sse_frames_are_json_per_line(client: TestClient) -> None:
    fakes.script(fakes.text("x"))
    with client.stream("POST", "/invocations", json={"prompt": "hi"}) as response:
        frames = [line for line in response.iter_lines() if line.startswith("data: ")]
    for frame in frames:
        json.loads(frame[len("data: ") :])
    assert frames


def test_log_file_receives_server_logs_and_warnings(tmp_path) -> None:
    import logging
    import warnings

    from strands_runtime.logging_setup import configure_logging

    log_file = tmp_path / "logs" / "server.log"
    configure_logging(logging.INFO, log_file=log_file)
    try:
        logging.getLogger("strands_runtime.test").info("hello from the server")
        warnings.warn("context_window_limit is not set", UserWarning, stacklevel=1)
    finally:
        configure_logging(logging.WARNING)
    text = log_file.read_text(encoding="utf-8")
    assert "hello from the server" in text and "context_window_limit is not set" in text


def test_busy_session_is_409(client: TestClient, runtime: Runtime) -> None:
    runtime.invoker._active_sessions.add("busy-session-000000000000000000000000")
    try:
        response = client.post(
            "/invocations", json={"prompt": "hi", "session_id": "busy-session-000000000000000000000000"}
        )
    finally:
        runtime.invoker._active_sessions.discard("busy-session-000000000000000000000000")
    assert response.status_code == 409
    assert "in progress" in response.json()["error"]


@pytest.fixture
def job_client(runtime: Runtime) -> Iterator[TestClient]:
    """``with`` で使うと 1 つのイベントループが保たれ、バックグラウンドのジョブがリクエストをまたいで動く。"""
    with TestClient(create_app(runtime)) as client:
        yield client


def test_job_mode_lifecycle_over_http(job_client: TestClient) -> None:
    fakes.script(fakes.text("x" * 300), fakes.text("later"))
    fakes.set_chunk_delay(0.02)
    sid = "http-job-000000000000000000000000000"
    try:
        accepted = job_client.post("/invocations", json={"prompt": "go", "mode": "job"}, headers={SESSION_HEADER: sid})
        assert accepted.status_code == 200
        job = accepted.json()
        assert job["type"] == "job" and job["state"] == "running" and job["session_id"] == sid
        assert "parent" not in job  # 値のない共通フィールドは出さない

        # 実行中は /ping が HealthyBusy、同じセッションの run は 409、status は通る。
        assert job_client.get("/ping").json()["status"] == "HealthyBusy"
        assert job_client.post("/invocations", json={"prompt": "again", "session_id": sid}).status_code == 409
        status = job_client.post("/invocations", json={"mode": "status", "session_id": sid}).json()
        assert status["state"] == "running" and status["job_id"] == job["job_id"]
        pending = job_client.post("/invocations", json={"mode": "result", "session_id": sid}).json()
        assert pending["type"] == "job"

        cancelled = job_client.post("/invocations", json={"mode": "cancel", "session_id": sid}).json()
        assert cancelled["state"] == "cancelling"

        with job_client.stream("POST", "/invocations", json={"mode": "follow", "session_id": sid}) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            events = list(iter_sse_events(response.iter_lines()))
    finally:
        fakes.set_chunk_delay(0.0)
    assert isinstance(events[0], StartEvent) and events[0].seq == 1
    assert isinstance(events[-1], ResultEvent) and events[-1].stop_reason == "cancelled"

    final = job_client.post("/invocations", json={"mode": "result", "session_id": sid}).json()
    assert final["type"] == "result" and final["stop_reason"] == "cancelled"
    assert job_client.get("/ping").json()["status"] == "Healthy"
    # セッションは空いたので、次の同期実行が通る。
    assert (
        job_client.post("/invocations", json={"prompt": "again", "session_id": sid, "stream": False}).json()["text"]
        == "later"
    )


def test_job_observation_errors(job_client: TestClient) -> None:
    assert job_client.post("/invocations", json={"mode": "status"}).status_code == 400  # session_id なし
    assert (
        job_client.post(
            "/invocations", json={"mode": "status", "session_id": "nobody-000000000000000000000000"}
        ).status_code
        == 404
    )
    assert job_client.post("/invocations", json={"mode": "job"}).status_code == 400  # prompt なし
