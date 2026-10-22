from __future__ import annotations

import json

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
