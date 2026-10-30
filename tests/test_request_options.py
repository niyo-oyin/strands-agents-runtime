from __future__ import annotations

import json

import httpx
import openai
import pytest

from strands_runtime.definitions.schema import ModelDefaults, ModelSpec
from strands_runtime.factory.models import build_model
from strands_runtime.invoke import InvocationRequest
from strands_runtime.runtime import Runtime
from strands_runtime_client.events import ResultEvent
from strands_runtime_client.inputs import GenerationOptions
from tests import fakes
from tests.conftest import write_agent, write_runtime
from tests.test_openai_responses import response_stream


def chat_stream(*, tool=False):
    delta = {"role": "assistant", "content": '{"answer":"ok"}'}
    if tool:
        delta = {
            "role": "assistant",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text":"hello"}'},
                }
            ],
        }
    chunks = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        },
    ]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n",
    )


def tool_response_stream():
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "sequence_number": 0,
            "item": {"type": "function_call", "name": "echo", "call_id": "call-1", "id": "item-1", "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item-1",
            "output_index": 0,
            "sequence_number": 1,
            "arguments": '{"text":"hello"}',
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {
                "id": "r",
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 4, "total_tokens": 9},
            },
        },
    ]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events),
    )


@pytest.fixture
def openai_requests(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "testing")
    captured = []
    reply_with_tool = []

    def respond(request):
        captured.append(json.loads(request.content))
        tool = bool(reply_with_tool and reply_with_tool.pop(0))
        if request.url.path.endswith("/responses"):
            return tool_response_stream() if tool else response_stream('{"answer":"ok"}')
        return chat_stream(tool=tool)

    original = openai.AsyncOpenAI
    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        lambda **kwargs: original(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))),
    )
    return captured, reply_with_tool


def configured_runtime(tmp_path, provider, *, structured=False):
    params = (
        'max_tokens = 99\nreasoning_effort = "high"'
        if provider == "openai-chat"
        else """
max_output_tokens = 99
reasoning = { effort = "high", summary = "auto" }
text = { verbosity = "low" }
"""
    )
    write_agent(
        tmp_path / "agents",
        "a",
        toml_extra='[structured_output]\nmodel = "tests.schemas:Answer"' if structured else "",
        model_table=f'''
[model]
id = "{provider}/test"
base_url = "https://api.example/v1/"
[model.config]
context_window_limit = 32000
[model.config.params]
temperature = 0.7
top_p = 0.9
{params}
''',
    )
    return Runtime.load(
        write_runtime(
            tmp_path, 'default_agent = "a"\n[session]\nbackend = "local"\n[session.history]\nbackend = "local"'
        )
    )


@pytest.mark.parametrize("provider", ["openai", "openai-chat"])
@pytest.mark.parametrize("mode", ["run", "job"])
async def test_generation_overrides_reach_provider_and_omission_restores_definition(
    tmp_path, openai_requests, provider, mode
):
    captured, _ = openai_requests
    runtime = configured_runtime(tmp_path, provider)
    request = InvocationRequest(
        messages=[{"role": "user", "content": "Answer as JSON"}],
        mode=mode,
        max_output_tokens=123,
        temperature=0,
        top_p=0.5,
        reasoning_effort="low",
        response_format={"type": "json_object"},
        tool_choice="none",
        max_turns=3,
        max_total_tokens=1000,
        timeout_seconds=20,
    )
    if mode == "job":
        accepted = await runtime.jobs.submit(request)
        result = [event async for event in runtime.jobs.follow(accepted.session_id, accepted.job_id)][-1]
    else:
        result = await runtime.invoker.invoke(request)
    assert isinstance(result, ResultEvent) and json.loads(result.text) == {"answer": "ok"}
    await runtime.invoker.invoke(InvocationRequest(messages=[{"role": "user", "content": "default"}]))
    override, default = captured
    assert override["temperature"] == 0 and override["top_p"] == 0.5
    assert default["temperature"] == 0.7 and default["top_p"] == 0.9
    assert override["tool_choice"] == "none" and "tool_choice" not in default
    assert "timeout_seconds" not in override and "max_total_tokens" not in override
    if provider == "openai":
        assert override["max_output_tokens"] == 123 and default["max_output_tokens"] == 99
        assert override["reasoning"] == {"effort": "low", "summary": "auto"}
        assert default["reasoning"]["effort"] == "high"
        assert override["text"] == {"verbosity": "low", "format": {"type": "json_object"}}
    else:
        assert override["max_completion_tokens"] == 123 and "max_tokens" not in override
        assert default["max_tokens"] == 99
        assert override["reasoning_effort"] == "low" and default["reasoning_effort"] == "high"
        assert override["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("provider", ["openai", "openai-chat"])
@pytest.mark.parametrize("format_type", ["text", "json_schema"])
async def test_response_format_reaches_provider(tmp_path, openai_requests, provider, format_type):
    captured, _ = openai_requests
    runtime = configured_runtime(tmp_path, provider)
    fmt = {"type": format_type}
    if format_type == "json_schema":
        fmt["json_schema"] = {
            "name": "Answer",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        }
    result = await runtime.invoker.invoke(
        InvocationRequest(messages=[{"role": "user", "content": "answer"}], response_format=fmt)
    )
    assert isinstance(result, ResultEvent) and json.loads(result.text) == {"answer": "ok"}
    assert result.stop_reason == "end_turn"
    body = captured[0]
    if provider == "openai":
        assert body["text"]["format"] == (
            {"type": "json_schema", **fmt["json_schema"]} if format_type == "json_schema" else fmt
        )
    else:
        assert body["response_format"] == fmt
    assert runtime.registry.get("a").definition.structured_output is None


@pytest.mark.parametrize("provider", ["openai", "openai-chat"])
@pytest.mark.parametrize("mode", ["run", "job"])
@pytest.mark.parametrize("format_type", ["text", "json_object", "json_schema"])
def test_schema_defined_agent_rejects_response_format_before_execution(
    tmp_path, openai_requests, provider, mode, format_type
):
    from starlette.testclient import TestClient

    from strands_runtime.server import create_app

    runtime = configured_runtime(tmp_path, provider, structured=True)
    fmt = {"type": format_type}
    if format_type == "json_schema":
        fmt["json_schema"] = {"name": "Other", "schema": {"type": "object"}}
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/invocations", json={"messages": [{"role": "user", "content": "go"}], "mode": mode, "response_format": fmt}
        )
    assert response.status_code == 400
    assert "cannot override an agent with structured_output" in response.json()["error"]
    assert openai_requests[0] == []
    assert runtime.registry.get("a").definition.structured_output is not None


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("provider", ["openai", "openai-chat"])
def test_native_schema_cannot_be_overridden(provider, fallback):
    from strands_runtime.errors import InvalidRequestError

    fmt = {"type": "json_schema", "name": "Answer", "schema": {"type": "object"}}
    params = {"text": {"format": fmt}} if provider == "openai" else {"response_format": fmt}
    candidate = {"id": f"{provider}/test", "config": {"params": params}}
    spec = ModelSpec(id="openai/test", fallbacks=[candidate]) if fallback else ModelSpec(**candidate)
    with pytest.raises(InvalidRequestError, match="cannot override a JSON schema"):
        build_model(spec, ModelDefaults(), GenerationOptions(response_format={"type": "text"}))


@pytest.mark.parametrize("provider", ["openai", "openai-chat"])
@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "echo"}}])
async def test_forced_tool_choice_is_released_after_first_model_response(tmp_path, openai_requests, provider, choice):
    captured, reply_with_tool = openai_requests
    reply_with_tool.extend([True, False])
    runtime = configured_runtime(tmp_path, provider)
    result = await runtime.invoker.invoke(
        InvocationRequest(messages=[{"role": "user", "content": "say hello"}], tool_choice=choice)
    )
    assert isinstance(result, ResultEvent) and json.loads(result.text) == {"answer": "ok"}
    assert len(captured) == 2
    first = {"type": "function", "name": "echo"} if provider == "openai" and isinstance(choice, dict) else choice
    assert captured[0]["tool_choice"] == first
    assert captured[1]["tool_choice"] == "auto"


@pytest.mark.parametrize(
    "field,limit_key,configured,requested,expected_calls",
    [
        ("max_turns", "turns", 2, None, 2),
        ("max_turns", "turns", 2, 1, 1),
        ("max_turns", "turns", 2, 10, 2),
        ("max_total_tokens", "total_tokens", 20, None, 2),
        ("max_total_tokens", "total_tokens", 20, 1, 1),
        ("max_total_tokens", "total_tokens", 20, 1000, 2),
    ],
)
async def test_execution_limits_inherit_and_cannot_exceed_definition(
    tmp_path, field, limit_key, configured, requested, expected_calls
):
    write_agent(tmp_path / "agents", "a", toml_extra=f"[limits]\n{limit_key} = {configured}")
    runtime = Runtime.load(write_runtime(tmp_path, 'default_agent = "a"'))
    fakes.script(*(fakes.tool_use("echo", {"text": "again"}, tool_use_id=f"t{i}") for i in range(3)))
    request = InvocationRequest.model_validate({"messages": [{"role": "user", "content": "go"}], field: requested})
    result = await runtime.invoker.invoke(request)
    assert isinstance(result, ResultEvent) and result.stop_reason == f"limit_{limit_key}"
    assert len(fakes.seen_messages()) == expected_calls


@pytest.mark.parametrize("requested,configured", [(0.02, None), (10, 0.02), (None, 0.02)])
async def test_timeout_uses_request_or_existing_smaller_limit(tmp_path, requested, configured):
    write_agent(tmp_path / "agents", "a", toml_extra=f"[limits]\ntimeout_seconds = {configured}" if configured else "")
    runtime = Runtime.load(write_runtime(tmp_path, 'default_agent = "a"'))
    fakes.script(fakes.text("long answer" * 20))
    fakes.set_chunk_delay(0.01)
    try:
        result = await runtime.invoker.invoke(
            InvocationRequest(messages=[{"role": "user", "content": "go"}], timeout_seconds=requested)
        )
    finally:
        fakes.set_chunk_delay(0)
    assert isinstance(result, ResultEvent) and result.stop_reason == "timeout"


def test_generation_options_apply_to_every_fallback_without_changing_other_params(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "testing")
    spec = ModelSpec(id="openai/a", fallbacks=["openai-chat/b"], config={"params": {"temperature": 0.8}})
    router = build_model(spec, ModelDefaults(), GenerationOptions(temperature=0, max_output_tokens=12))
    configs = [candidate.model.get_config()["params"] for candidate in router.candidates]
    assert configs[0]["max_output_tokens"] == 12 and configs[1]["max_completion_tokens"] == 12
    assert all(c["temperature"] == 0 for c in configs)
    assert spec.config == {"params": {"temperature": 0.8}}


@pytest.mark.parametrize("effort,expected", [("none", "none"), ("off", "none"), ("high", "high")])
def test_reasoning_override_preserves_other_responses_options(monkeypatch, effort, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "testing")
    spec = ModelSpec(id="openai/test", config={"params": {"reasoning": {"effort": "low", "summary": "auto"}}})
    model = build_model(spec, ModelDefaults(), GenerationOptions(reasoning_effort=effort))
    assert model.get_config()["params"]["reasoning"] == {"effort": expected, "summary": "auto"}


def test_bedrock_generation_overrides_keep_unrelated_config(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    spec = ModelSpec(
        id="bedrock/global.anthropic.claude-opus-5",
        config={
            "max_tokens": 500,
            "temperature": 0.8,
            "top_p": 0.9,
            "additional_request_fields": {
                "thinking": {"type": "enabled", "budget_tokens": 200},
                "output_config": {"effort": "low", "format": {"type": "json_schema"}},
            },
        },
    )
    selected = build_model(
        spec,
        ModelDefaults(),
        GenerationOptions(max_output_tokens=1000, temperature=0, top_p=0.5, reasoning_effort="off"),
    ).get_config()
    assert selected["max_tokens"] == 1000 and selected["temperature"] == 0 and selected["top_p"] == 0.5
    assert selected["additional_request_fields"] == {"output_config": {"format": {"type": "json_schema"}}}
    original = build_model(spec, ModelDefaults()).get_config()
    assert original["max_tokens"] == 500 and original["temperature"] == 0.8
    assert original["additional_request_fields"]["thinking"]["budget_tokens"] == 200


@pytest.mark.parametrize(
    "options",
    [
        {"response_format": {"type": "json_object"}},
        {"tool_choice": "none"},
    ],
)
def test_unsupported_provider_options_are_reported_before_execution(tmp_path, options):
    from starlette.testclient import TestClient

    from strands_runtime.server import create_app

    write_agent(tmp_path / "agents", "a", model_table='[model]\nid = "bedrock/test"')
    runtime = Runtime.load(write_runtime(tmp_path, 'default_agent = "a"'))
    with TestClient(create_app(runtime)) as client:
        response = client.post("/invocations", json={"messages": [{"role": "user", "content": "go"}], **options})
    assert response.status_code == 400 and "not supported" in response.json()["error"]


def test_raw_http_accepts_options_and_reports_invalid_numbers(tmp_path, openai_requests):
    from starlette.testclient import TestClient

    from strands_runtime.server import create_app

    runtime = configured_runtime(tmp_path, "openai")
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/invocations",
            json={
                "messages": [{"role": "user", "content": "go"}],
                "stream": False,
                "temperature": 0,
                "max_output_tokens": 123,
                "max_turns": 1,
            },
        )
        assert response.status_code == 200 and response.json()["text"] == '{"answer":"ok"}'
        invalid = client.post(
            "/invocations", json={"messages": [{"role": "user", "content": "go"}], "max_total_tokens": -1}
        )
    assert invalid.status_code == 400
    assert len(openai_requests[0]) == 1
    assert openai_requests[0][0]["max_output_tokens"] == 123


async def test_request_turn_limit_and_definition_output_budget_remain_independent(tmp_path):
    write_agent(tmp_path / "agents", "a", toml_extra="[limits]\noutput_tokens = 1")
    runtime = Runtime.load(write_runtime(tmp_path, 'default_agent = "a"'))
    fakes.script(fakes.tool_use("echo", {"text": "again"}))
    result = await runtime.invoker.invoke(
        InvocationRequest(messages=[{"role": "user", "content": "go"}], max_turns=10, max_total_tokens=1000)
    )
    assert isinstance(result, ResultEvent) and result.stop_reason == "limit_output_tokens"


async def test_forced_tool_choice_survives_a_hook_requested_retry(tmp_path, openai_requests):
    from strands.hooks import AfterModelCallEvent, HookProvider

    from strands_runtime.factory import SessionContext

    class RetryFirstResponse(HookProvider):
        def __init__(self):
            self.retried = False

        def register_hooks(self, registry, **kwargs):
            registry.add_callback(AfterModelCallEvent, self.retry)

        def retry(self, event):
            if not self.retried and event.stop_response is not None:
                self.retried = True
                event.retry = True

    captured, reply_with_tool = openai_requests
    reply_with_tool.extend([True, True, False])
    runtime = configured_runtime(tmp_path, "openai-chat")
    agent = runtime.factory.build(
        "a",
        SessionContext("retry", actor_id="test-user"),
        hooks=[RetryFirstResponse()],
        options=GenerationOptions(tool_choice="required"),
    )
    events = [event async for event in agent.stream_async("say hello")]
    assert events[-1]["result"].stop_reason == "end_turn"
    assert [body["tool_choice"] for body in captured] == ["required", "required", "auto"]
