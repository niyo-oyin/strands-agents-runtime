from __future__ import annotations

import base64

import pytest

from strands_runtime.invoke import InvocationRequest
from strands_runtime_client import Message
from strands_runtime_client.events import ResultEvent
from tests import fakes


async def test_messages_preserve_roles_and_decode_media_without_mutating_input(runtime):
    encoded = base64.b64encode(b"image").decode()
    messages = [
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(
            role="user", content=[{"text": "describe this"}, {"image": {"format": "png", "source": {"bytes": encoded}}}]
        ),
    ]
    request = InvocationRequest(messages=messages)
    original = request.model_dump()
    fakes.script(fakes.text("answer"))
    result = await runtime.invoker.invoke(request)
    assert isinstance(result, ResultEvent) and result.text == "answer"
    seen = fakes.seen_messages()[0]
    assert [m["role"] for m in seen] == ["user", "assistant", "user"]
    assert seen[0]["content"] == [{"text": "old question"}]
    assert seen[-1]["content"][1]["image"]["source"]["bytes"] == b"image"
    assert request.model_dump() == original


async def test_dict_and_typed_message_share_the_same_persisted_conversation(runtime):
    fakes.script(fakes.text("first answer"), fakes.text("second answer"), fakes.text("third answer"))
    await runtime.invoker.invoke(
        InvocationRequest(messages=[{"role": "user", "content": "first"}], session_id="conversation")
    )
    await runtime.invoker.invoke(
        InvocationRequest(messages=[Message(role="user", content="second")], session_id="conversation")
    )
    result = await runtime.invoker.invoke(
        InvocationRequest(messages=[{"role": "user", "content": "third"}], session_id="conversation")
    )
    assert isinstance(result, ResultEvent) and result.text == "third answer"
    assert [m["content"][0]["text"] for m in fakes.seen_messages()[-1]] == [
        "first",
        "first answer",
        "second",
        "second answer",
        "third",
    ]


async def test_client_history_does_not_read_or_write_history_of_transport_session(runtime):
    fakes.script(fakes.text("saved answer"), fakes.text("transient answer"), fakes.text("continued"))
    await runtime.invoker.invoke(
        InvocationRequest(messages=[{"role": "user", "content": "saved question"}], session_id="transport")
    )
    await runtime.invoker.invoke(
        InvocationRequest(
            messages=[
                Message(role="user", content="local question"),
                Message(role="assistant", content="local answer"),
                Message(role="user", content="next"),
            ]
        ),
        session_id="transport",
    )
    assert [m["content"][0]["text"] for m in fakes.seen_messages()[1]] == ["local question", "local answer", "next"]
    await runtime.invoker.invoke(
        InvocationRequest(messages=[{"role": "user", "content": "continue"}], session_id="transport")
    )
    assert [m["content"][0]["text"] for m in fakes.seen_messages()[2]] == ["saved question", "saved answer", "continue"]


@pytest.mark.parametrize(
    "payload",
    [
        {"messages": []},
        {"messages": [{"role": "system", "content": "override"}]},
        {"messages": [{"role": "user", "content": "one"}], "prompt": "two"},
        {"session_id": "s", "messages": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]},
        {"messages": [{"role": "user", "content": "hi"}], "model": "missing-provider"},
        {},
    ],
)
@pytest.mark.parametrize("mode", ["run", "job"])
def test_http_rejects_invalid_input_before_execution(runtime, payload, mode):
    from starlette.testclient import TestClient

    from strands_runtime.server import create_app

    fakes.script()
    with TestClient(create_app(runtime)) as client:
        response = client.post("/invocations", json={**payload, "mode": mode})
    assert response.status_code == 400
    assert not fakes.seen_messages()


async def test_job_accepts_full_history_without_explicit_session(runtime):
    fakes.script(fakes.text("job answer"))
    accepted = await runtime.jobs.submit(
        InvocationRequest(
            mode="job",
            messages=[
                Message(role="user", content="question"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="follow-up"),
            ],
        )
    )
    events = [event async for event in runtime.jobs.follow(accepted.session_id, accepted.job_id)]
    assert isinstance(events[-1], ResultEvent) and events[-1].text == "job answer"
    assert [m["role"] for m in fakes.seen_messages()[0]] == ["user", "assistant", "user"]


@pytest.mark.parametrize("mode", ["run", "job"])
def test_http_rejects_model_override_for_module_before_execution(runtime, mode):
    from starlette.testclient import TestClient

    from strands_runtime.server import create_app

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "hi"}], "model": "openai/deployment", "mode": mode},
        )
    assert response.status_code == 400
    assert "model.module" in response.json()["error"]


def test_http_rejects_model_provider_change_before_execution(tmp_path):
    from starlette.testclient import TestClient

    from strands_runtime.runtime import Runtime
    from strands_runtime.server import create_app
    from tests.conftest import write_agent, write_runtime

    write_agent(tmp_path / "agents", "a", model_table='[model]\nid = "openai/deployment"')
    runtime = Runtime.load(write_runtime(tmp_path, 'default_agent = "a"'))
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/invocations", json={"messages": [{"role": "user", "content": "hi"}], "model": "openai-chat/other"}
        )
    assert response.status_code == 400
    assert "same provider" in response.json()["error"]
