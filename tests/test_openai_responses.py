from __future__ import annotations

import json
import os

import httpx
import openai
import pytest
from strands.models import ModelRouter
from strands.models.openai_responses import OpenAIResponsesModel

from strands_runtime.definitions.schema import ModelDefaults, ModelSpec
from strands_runtime.errors import DefinitionError
from strands_runtime.factory.models import build_model
from strands_runtime.invoke import InvocationRequest
from strands_runtime.runtime import Runtime
from strands_runtime_client import Message
from strands_runtime_client.events import ResultEvent, TextEvent
from tests.conftest import write_agent, write_runtime


@pytest.mark.parametrize("mode", ["run", "job"])
async def test_request_model_override_keeps_azure_connection_and_does_not_leak(
    tmp_path, monkeypatch, mock_responses, mode
):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    write_agent(
        tmp_path / "agents",
        "azure",
        with_tool=False,
        model_table="""
[model]
id = "openai/default-deployment"
base_url = "https://resource.openai.azure.com/openai/v1/"
api_key_env = "AZURE_OPENAI_API_KEY"
fallbacks = ["openai/fallback-deployment"]
effort = "low"
[model.config]
context_window_limit = 32000
""",
    )
    runtime = Runtime.load(
        write_runtime(
            tmp_path, 'default_agent = "azure"\n[session]\nbackend = "local"\n[session.history]\nbackend = "local"'
        )
    )
    request = InvocationRequest(
        messages=[
            Message(role="user", content="earlier"),
            Message(role="assistant", content="answer"),
            Message(role="user", content="hello"),
        ],
        model="openai/selected-deployment",
        mode=mode,
    )
    if mode == "job":
        accepted = await runtime.jobs.submit(request)
        events = [event async for event in runtime.jobs.follow(accepted.session_id, accepted.job_id)]
        result = events[-1]
    else:
        result = await runtime.invoker.invoke(request)
    assert isinstance(result, ResultEvent) and result.text == "こんにちは"
    await runtime.invoker.invoke(InvocationRequest(messages=[{"role": "user", "content": "again"}]))
    selected, default = mock_responses
    assert json.loads(selected.content)["model"] == "selected-deployment"
    assert json.loads(default.content)["model"] == "default-deployment"
    assert json.loads(selected.content)["reasoning"]["effort"] == "low"
    for req in mock_responses:
        assert str(req.url) == "https://resource.openai.azure.com/openai/v1/responses"
        assert req.headers["authorization"] == "Bearer azure-key"
    assert runtime.registry.get("azure").definition.model.id == "openai/default-deployment"


def test_override_disables_fallbacks_and_config_model_id_only_for_this_call(monkeypatch):
    from strands_runtime.factory.models import override_model

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    spec = ModelSpec(
        id="openai/original",
        fallbacks=["openai/backup"],
        config={"model_id": "configured", "context_window_limit": 32000},
    )
    selected = build_model(override_model(spec, ModelDefaults(), "openai/selected"), ModelDefaults())
    assert isinstance(selected, OpenAIResponsesModel)
    assert selected.get_config()["model_id"] == "selected"
    assert selected.get_config()["context_window_limit"] == 32000
    original = build_model(spec, ModelDefaults())
    assert isinstance(original, ModelRouter)
    assert original.default_model.get_config()["model_id"] == "configured"


def response_stream(text):
    events = [
        {
            "type": "response.output_text.delta",
            "delta": text,
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "sequence_number": 0,
        },
        {
            "type": "response.completed",
            "sequence_number": 1,
            "response": {
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        },
    ]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(
            f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
        ).encode("utf-8"),
    )


@pytest.fixture
def mock_responses(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return response_stream("こんにちは")

    original = openai.AsyncOpenAI

    def client(**kwargs):
        return original(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    monkeypatch.setattr(openai, "AsyncOpenAI", client)
    return requests


async def test_responses_endpoint_key_and_deployment_reach_http_request(tmp_path, monkeypatch, mock_responses):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("OPENAI_API_KEY", "official-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://official.example/v1/")
    write_agent(
        tmp_path / "agents",
        "azure",
        with_tool=False,
        model_table="""
[model]
id = "openai/my-deployment"
base_url = "https://resource.openai.azure.com/openai/v1/"
api_key_env = "AZURE_OPENAI_API_KEY"
effort = "low"
[model.config]
context_window_limit = 32000
""",
    )
    write_agent(
        tmp_path / "agents",
        "official",
        with_tool=False,
        model_table="""
[model]
id = "openai/official-model"
effort = "off"
[model.config]
context_window_limit = 32000
""",
    )
    runtime = Runtime.load(write_runtime(tmp_path))
    for agent in ("azure", "official"):
        events = [
            event
            async for event in runtime.invoker.stream(
                InvocationRequest(agent=agent, messages=[{"role": "user", "content": "hello"}])
            )
        ]
        assert any(isinstance(event, TextEvent) and event.text == "こんにちは" for event in events)
        assert isinstance(events[-1], ResultEvent) and events[-1].text == "こんにちは"
        assert events[-1].metrics.total_tokens == 5
    azure, official = mock_responses
    assert azure.method == "POST"
    assert str(azure.url) == "https://resource.openai.azure.com/openai/v1/responses"
    assert azure.headers["authorization"] == "Bearer azure-key"
    payload = json.loads(azure.content)
    assert payload["model"] == "my-deployment"
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["stream"] is True
    assert str(official.url) == "https://official.example/v1/responses"
    assert official.headers["authorization"] == "Bearer official-key"
    assert json.loads(official.content)["reasoning"] == {"effort": "none"}
    assert os.environ["OPENAI_API_KEY"] == "official-key"
    assert os.environ["OPENAI_BASE_URL"] == "https://official.example/v1/"


@pytest.mark.parametrize("explicit_id", [True, False])
def test_responses_fallbacks_inherit_and_override_connection(monkeypatch, explicit_id):
    monkeypatch.setenv("AZURE_KEY", "primary-key")
    monkeypatch.setenv("SECONDARY_KEY", "secondary-key")
    spec = ModelSpec.model_validate(
        {
            **({"id": "openai/primary"} if explicit_id else {}),
            "base_url": "https://primary.example/openai/v1/",
            "api_key_env": "AZURE_KEY",
            "effort": "low",
            "fallbacks": [
                "openai/second",
                {
                    "id": "openai/third",
                    "base_url": "https://secondary.example/v1/",
                    "api_key_env": "SECONDARY_KEY",
                },
                "openai-chat/chat",
            ],
            "cache": {"ttl": "1h"},
            "config": {"context_window_limit": 32000},
        }
    )
    router = build_model(spec, ModelDefaults(default_id="openai/primary"))
    assert isinstance(router, ModelRouter)
    models = [candidate.model for candidate in router.candidates]
    for model in models[:2]:
        assert isinstance(model, OpenAIResponsesModel)
        assert model.client_args == {"base_url": "https://primary.example/openai/v1/", "api_key": "primary-key"}
    assert models[2].client_args == {"base_url": "https://secondary.example/v1/", "api_key": "secondary-key"}
    assert models[3].client_args == {}  # 別の API プロバイダーへキーを引き継がない。
    for model in models[:3]:
        config = model.get_config()
        assert config["params"] == {"reasoning": {"effort": "low"}}
        assert config["cache_config"].ttl == "1h"
        assert config["context_window_limit"] == 32000


def test_responses_custom_params_override_harness_defaults():
    model = build_model(ModelSpec(id="openai/deployment", config={"params": {}}), ModelDefaults())
    assert model.get_config()["params"] == {}


def test_responses_missing_key_is_reported(monkeypatch):
    monkeypatch.delenv("MISSING_RESPONSES_KEY", raising=False)
    with pytest.raises(DefinitionError, match="MISSING_RESPONSES_KEY"):
        build_model(ModelSpec(id="openai/deployment", api_key_env="MISSING_RESPONSES_KEY"), ModelDefaults())


def test_default_non_openai_model_rejects_connection_settings():
    with pytest.raises(DefinitionError, match="base_url"):
        build_model(ModelSpec(base_url="https://custom.example/v1"), ModelDefaults(default_id="bedrock/test"))


@pytest.mark.parametrize("provider", ["openai", "openai-chat"])
def test_empty_openai_model_name_is_rejected(provider):
    with pytest.raises(DefinitionError, match="model name"):
        build_model(ModelSpec(id=f"{provider}/"), ModelDefaults())


def test_module_rejects_ignored_connection_settings():
    with pytest.raises(ValueError, match="api_key_env"):
        ModelSpec(module="tests.fakes:make_model", api_key_env="CUSTOM_KEY")
