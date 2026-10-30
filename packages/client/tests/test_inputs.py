from __future__ import annotations

from unittest.mock import Mock

import pytest

from strands_runtime_client import AgentClient, Message
from strands_runtime_client.events import JobEvent, ResultEvent


class RecordingClient(AgentClient):
    def __init__(self):
        self.requests = []

    def _request(self, payload, *, session_id):
        self.requests.append((payload, session_id))
        if payload.get("mode") == "job":
            return iter(
                [
                    JobEvent(
                        job_id="job-1",
                        session_id=session_id or "generated",
                        agent="a",
                        state="running",
                        accepted_at="2026-10-30T00:00:00Z",
                        updated_at="2026-10-30T00:00:00Z",
                    )
                ]
            )
        return iter([ResultEvent(agent="a", session_id=session_id or "generated", stop_reason="end_turn", text="ok")])


@pytest.mark.parametrize("method", ["invoke", "stream", "submit"])
@pytest.mark.parametrize(
    "arguments,match",
    [
        (
            {
                "session_id": "s",
                "messages": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
            },
            "exactly one message",
        ),
        ({"prompt": "one", "messages": [{"role": "user", "content": "two"}]}, "mutually exclusive"),
        ({"messages": []}, "at least 1"),
        ({"messages": [{"role": "system", "content": "override"}]}, "user.*assistant"),
        ({"messages": [{"role": "user", "content": []}]}, "at least 1"),
        ({"prompt": "one", "model": "missing-provider"}, "pattern"),
        ({}, "prompt or messages is required"),
    ],
)
def test_invalid_input_fails_before_transport_even_without_iterating_stream(method, arguments, match):
    client = RecordingClient()
    client._request = Mock(side_effect=AssertionError("must not send a request"))
    with pytest.raises(ValueError, match=match):
        getattr(client, method)(**arguments)
    client._request.assert_not_called()


@pytest.mark.parametrize("method", ["invoke", "stream", "submit"])
@pytest.mark.parametrize("role", ["user", "assistant"])
def test_single_message_with_session_and_model_is_sent(method, role):
    client = RecordingClient()
    getattr(client, method)(messages=[Message(role=role, content="hello")], session_id="s", model="openai/deployment")
    payload, sid = client.requests[0]
    assert sid == payload["session_id"] == "s"
    assert payload["messages"] == [{"role": role, "content": "hello"}]
    assert payload["model"] == "openai/deployment" and "prompt" not in payload


@pytest.mark.parametrize("method", ["invoke", "stream", "submit"])
def test_history_without_session_is_sent_without_creating_a_conversation_id(method):
    client = RecordingClient()
    history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": [{"text": "three"}]},
    ]
    result = getattr(client, method)(messages=history)
    payload, sid = client.requests[0]
    assert payload["messages"] == history
    assert "session_id" not in payload and sid is None
    if method == "submit":
        assert result.session_id == "generated"


@pytest.mark.parametrize("method", ["invoke", "stream", "submit"])
def test_optional_request_parameters_are_sent_only_when_specified(method):
    client = RecordingClient()
    options = {
        "max_output_tokens": 123,
        "temperature": 0,
        "top_p": 0.5,
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "Answer",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string", "default": None}},
                },
            },
        },
        "tool_choice": {"type": "function", "function": {"name": "echo"}},
        "timeout_seconds": 30,
        "max_turns": 5,
        "max_total_tokens": 2000,
    }
    getattr(client, method)("go", **options)
    payload, _ = client.requests[-1]
    assert {key: payload[key] for key in options} == options
    getattr(client, method)("go", **dict.fromkeys(options))
    assert not set(options).intersection(client.requests[-1][0])


@pytest.mark.parametrize(
    "options",
    [
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": float("nan")},
        {"top_p": 1.1},
        {"reasoning_effort": "invalid"},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"max_turns": 1.5},
        {"max_total_tokens": -1},
        {"response_format": {"type": "json_schema"}},
        {"response_format": {"type": "other"}},
        {"response_format": {"type": "json_schema", "json_schema": {"name": "a"}}},
        {"tool_choice": "other"},
        {"tool_choice": {"type": "function", "function": {"name": ""}}},
    ],
)
def test_invalid_options_fail_before_network(options):
    client = RecordingClient()
    client._request = Mock(side_effect=AssertionError("must not send"))
    with pytest.raises(ValueError):
        client.stream("go", **options)
    client._request.assert_not_called()
