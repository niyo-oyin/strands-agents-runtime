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
        if payload.get("mode") in {"job", "status", "cancel"}:
            return iter(
                [
                    JobEvent(
                        actor_id=payload.get("actor_id", "test-user"),
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
        ({"messages": []}, "at least 1"),
        ({"messages": [{"role": "system", "content": "override"}]}, "user.*assistant"),
        ({"messages": [{"role": "user", "content": []}]}, "at least 1"),
        ({"messages": [{"role": "user", "content": "one"}], "model": "missing-provider"}, "pattern"),
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
    getattr(client, method)(messages=[{"role": "user", "content": "go"}], **options)
    payload, _ = client.requests[-1]
    assert {key: payload[key] for key in options} == options
    getattr(client, method)(messages=[{"role": "user", "content": "go"}], **dict.fromkeys(options))
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
        client.stream(messages=[{"role": "user", "content": "go"}], **options)
    client._request.assert_not_called()


@pytest.mark.parametrize("method", ["invoke", "stream", "submit"])
def test_prompt_and_positional_input_are_removed(method):
    client = RecordingClient()
    client._request = Mock(side_effect=AssertionError("must not send"))
    with pytest.raises(TypeError):
        getattr(client, method)("old input")
    with pytest.raises(TypeError):
        getattr(client, method)(prompt="old input")
    client._request.assert_not_called()


@pytest.mark.parametrize("options", [{"store": "false"}, {"store": 0}, {"actor_id": ""}, {"actor_id": " "}])
def test_invalid_storage_options_fail_before_network(options):
    client = RecordingClient()
    client._request = Mock(side_effect=AssertionError("must not send"))
    with pytest.raises(ValueError):
        client.stream(messages=[{"role": "user", "content": "hi"}], **options)
    client._request.assert_not_called()


def test_store_false_and_actor_are_preserved_in_payload():
    client = RecordingClient()
    client.invoke(messages=[{"role": "user", "content": "hi"}], actor_id="alice", store=False, session_id="s")
    payload, _ = client.requests[0]
    assert payload["store"] is False and payload["actor_id"] == "alice"


@pytest.mark.parametrize("actor", [None, "alice"])
def test_job_handles_keep_actor_for_all_operations(actor):
    client = RecordingClient()
    submitted = client.submit(messages=[{"role": "user", "content": "hi"}], actor_id=actor)
    reattached = client.job(submitted.session_id, actor_id=actor)
    explicit = client.job(submitted.session_id, submitted.job_id, actor_id=submitted.actor_id)
    client.requests.clear()
    for handle in (submitted, reattached, explicit):
        handle.status()
        list(handle.follow())
        handle.result()
        handle.cancel()
    assert len(client.requests) == 12
    assert all(payload["actor_id"] == (actor or "test-user") for payload, _ in client.requests)
