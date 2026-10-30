from __future__ import annotations

import io
import json

import boto3
import httpx
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from strands_runtime_client import AgentCoreClient, HttpClient, InvocationError
from strands_runtime_client.events import ResultEvent, TextEvent, parse_event

RESULT = {"type": "result", "agent": "assistant", "session_id": "s", "stop_reason": "end_turn", "text": "こんにちは"}


@pytest.mark.parametrize("streaming", [False, True])
def test_http_without_runtime(streaming):
    def respond(request):
        assert request.url.path == "/invocations"
        assert request.headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] == "s"
        assert json.loads(request.content)["prompt"] == "hello"
        if streaming:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=(
                    f'data: {{"type":"text","text":"こんにちは"}}\n\ndata: {json.dumps(RESULT, ensure_ascii=False)}\n\n'
                ),
            )
        return httpx.Response(200, json=RESULT)

    with HttpClient("https://runtime.example", transport=httpx.MockTransport(respond)) as client:
        assert client.invoke("hello", session_id="s") == ResultEvent(**RESULT)


@pytest.mark.parametrize(
    "status,body,match",
    [
        (409, {"error": "busy"}, "HTTP 409"),
        (200, {"type": "error", "error_type": "Failed", "message": "failed"}, "Failed"),
    ],
)
def test_http_errors_without_runtime(status, body, match):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    with (
        HttpClient("https://runtime.example", transport=transport) as client,
        pytest.raises(InvocationError, match=match),
    ):
        client.invoke("hello")


@pytest.mark.parametrize("streaming", [False, True])
def test_agentcore_without_runtime(streaming):
    session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")
    client = AgentCoreClient(
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test-1234567890", boto_session=session
    )
    sid = "00000000-0000-4000-8000-000000000001"
    result = {**RESULT, "session_id": sid}
    payload = json.dumps(result).encode("utf-8")
    if streaming:
        payload = b"data: " + payload + b"\n\n"
    with Stubber(client._client) as stub:
        stub.add_response(
            "invoke_agent_runtime",
            {
                "contentType": "text/event-stream" if streaming else "application/json",
                "response": StreamingBody(io.BytesIO(payload), len(payload)),
            },
            {
                "agentRuntimeArn": client._runtime_arn,
                "qualifier": "DEFAULT",
                "runtimeSessionId": sid,
                "contentType": "application/json",
                "accept": "text/event-stream",
                "payload": json.dumps({"prompt": "hello", "stream": True, "session_id": sid}).encode("utf-8"),
            },
        )
        assert client.invoke("hello", session_id=sid) == ResultEvent(**result)
        stub.assert_no_pending_responses()


def test_events_parse_without_runtime():
    assert parse_event(b'{"type":"text","text":"hello"}') == TextEvent(text="hello")
