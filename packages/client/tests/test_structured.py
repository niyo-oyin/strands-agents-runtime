from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from strands_runtime_client import HttpClient
from strands_runtime_client.events import ResultEvent, TextEvent, dump_event


class Detail(BaseModel):
    label: str = Field(alias="display_label")
    score: int | None = None


class Answer(BaseModel):
    answer: str
    details: list[Detail] = []


class Node(BaseModel):
    name: str
    children: list[Node] = []


class DictionaryAnswer(BaseModel):
    values: dict[str, int]


class OpenAnswer(BaseModel):
    model_config = ConfigDict(extra="allow")
    answer: str


def transport_for(
    text='{"answer":"ok","details":[{"display_label":"good","score":null}]}', *, reason="end_turn", pending=False
):
    requests = []
    result = ResultEvent(agent="a", session_id="s", stop_reason=reason, text=text)
    job = {
        "type": "job",
        "agent": "a",
        "session_id": "s",
        "job_id": "j",
        "state": "running",
        "accepted_at": "2026-10-30T00:00:00Z",
        "updated_at": "2026-10-30T00:00:00Z",
    }

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        mode = payload.get("mode")
        if mode in ("job", "status") or (mode == "result" and pending):
            return httpx.Response(200, json=job)
        if payload.get("stream") or mode == "follow":
            events = [TextEvent(text=text), result]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(f"data: {json.dumps(dump_event(e))}\n\n" for e in events),
            )
        return httpx.Response(200, json=dump_event(result))

    return httpx.MockTransport(respond), requests, result


@pytest.mark.parametrize("method", ["invoke", "stream", "submit"])
def test_pydantic_model_sent_as_strict_schema_and_parsed_locally(method):
    transport, requests, wire_result = transport_for()
    original_schema = Answer.model_json_schema()
    with HttpClient("https://runtime.example", transport=transport) as client:
        result = getattr(client, method)("answer", response_format=Answer)
        if method == "stream":
            events = list(result)
            assert isinstance(events[0], TextEvent)
            result = events[-1]
        elif method == "submit":
            result = result.result()
    assert isinstance(result.parsed, Answer)
    assert result.parsed.answer == "ok" and result.parsed.details[0].label == "good"
    assert result.parsed.details[0].score is None
    assert dump_event(result) == dump_event(wire_result)
    fmt = requests[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False and schema["required"] == ["answer", "details"]
    detail = schema["$defs"]["Detail"]
    assert detail["additionalProperties"] is False and detail["required"] == ["display_label", "score"]
    assert "default" not in detail["properties"]["score"]
    assert Answer.model_json_schema() == original_schema


def test_recursive_model_schema_and_result():
    transport, requests, _ = transport_for('{"name":"root","children":[{"name":"child","children":[]}]}')
    with HttpClient("https://runtime.example", transport=transport) as client:
        result = client.invoke("tree", response_format=Node)
    assert isinstance(result.parsed, Node) and result.parsed.children[0].name == "child"
    schema = requests[0]["response_format"]["json_schema"]["schema"]
    assert schema["type"] == "object" and "$ref" not in schema
    assert schema["$defs"]["Node"]["additionalProperties"] is False


@pytest.mark.parametrize("method", ["invoke", "stream", "submit"])
@pytest.mark.parametrize("model", [int, BaseModel, RootModel[list[str]], DictionaryAnswer, OpenAnswer])
def test_invalid_model_fails_before_network_without_consuming_stream(method, model):
    with HttpClient("https://runtime.example") as client:
        client._request = Mock(side_effect=AssertionError("must not send"))
        with pytest.raises((ValueError, TypeError)):
            getattr(client, method)("answer", response_format=model)
        client._request.assert_not_called()


@pytest.mark.parametrize("text", ["not JSON", '{"answer":17}', '{"details":[]}'])
def test_invalid_completed_output_raises_pydantic_validation_error(text):
    transport, _, _ = transport_for(text)
    with HttpClient("https://runtime.example", transport=transport) as client:
        stream = client.stream("answer", response_format=Answer)
        assert isinstance(next(stream), TextEvent)
        with pytest.raises(ValidationError):
            list(stream)


@pytest.mark.parametrize("reason", ["max_tokens", "timeout", "limit_turns", "interrupt"])
def test_incomplete_results_remain_available_without_parsing(reason):
    transport, _, _ = transport_for('{"answer":', reason=reason)
    with HttpClient("https://runtime.example", transport=transport) as client:
        result = client.invoke("answer", response_format=Answer)
    assert result.parsed is None and result.stop_reason == reason


def test_explicit_format_does_not_parse_output():
    transport, _, _ = transport_for()
    with HttpClient("https://runtime.example", transport=transport) as client:
        result = client.invoke("answer", response_format={"type": "json_object"})
    assert result.parsed is None and json.loads(result.text)["answer"] == "ok"


@pytest.mark.parametrize("reattach", [False, True])
@pytest.mark.parametrize("operation", ["result", "wait", "follow"])
def test_job_results_keep_local_model_and_reattachment_can_restore_it(reattach, operation):
    transport, requests, _ = transport_for(pending=operation == "wait")
    with HttpClient("https://runtime.example", transport=transport) as client:
        job = client.job("s", response_format=Answer) if reattach else client.submit("answer", response_format=Answer)
        result = list(job.follow())[-1] if operation == "follow" else job.result(wait=operation == "wait")
    assert isinstance(result.parsed, Answer) and result.parsed.answer == "ok"
    assert all("response_format" not in p for p in requests if p.get("mode") != "job")
