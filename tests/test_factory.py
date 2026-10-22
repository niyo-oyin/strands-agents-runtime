from __future__ import annotations

from pathlib import Path

import pytest
from strands.session import SnapshotSessionManager

from strands_runtime.errors import DefinitionError, ReferenceResolutionError, UnknownAgentError
from strands_runtime.factory import SessionContext
from strands_runtime.runtime import Runtime
from tests.conftest import write_agent, write_runtime
from tests.fakes import ScriptedModel

SESSION = SessionContext(session_id="s-1", actor_id="user-1")


def test_build_wires_prompt_tools_model_config_and_session(project: Path) -> None:
    (project / "agents" / "echoer" / "agent.toml").open("a").write("\n[model.config]\nmax_tokens = 123\n")
    runtime = Runtime.load(project / "runtime.toml")

    agent = runtime.factory.build("echoer", SESSION)

    assert agent.system_prompt == "You are a test agent."
    assert "echo" in agent.tool_names
    assert isinstance(agent.model, ScriptedModel)
    assert agent.model.get_config()["max_tokens"] == 123
    assert isinstance(agent._session_manager, SnapshotSessionManager)
    assert (project / "sessions").is_dir()


def test_subagents_become_tools_and_session_none_disables_persistence(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "parent", toml_extra='subagents = ["helper"]\n[session]\nbackend = "none"\n')
    write_agent(agents, "helper")
    runtime = Runtime.load(write_runtime(tmp_path))

    agent = runtime.factory.build("parent", SESSION)

    assert "helper" in agent.tool_names
    assert agent._session_manager is None


def test_structured_output_model_is_resolved(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents", "a", toml_extra='[structured_output]\nmodel = "tests.schemas:Answer"\n')
    runtime = Runtime.load(write_runtime(tmp_path))
    from tests.schemas import Answer

    agent = runtime.factory.build("a", SESSION)
    assert agent._default_structured_output_model is Answer


def test_bad_reference_is_reported(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents", "a", toml_extra='plugins = ["tests.schemas:Missing"]\n')
    runtime = Runtime.load(write_runtime(tmp_path))
    with pytest.raises(ReferenceResolutionError, match="Missing"):
        runtime.factory.build("a", SESSION)


def test_unknown_agent(runtime: Runtime) -> None:
    with pytest.raises(UnknownAgentError):
        runtime.factory.build("nope", SESSION)


def test_openai_chat_provider_builds_chat_completions_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    write_agent(
        tmp_path / "agents",
        "a",
        model_table='[model]\nid = "openai-chat/my-model"\neffort = "low"\nbase_url = "http://localhost:9/v1"\n',
    )
    runtime = Runtime.load(write_runtime(tmp_path))

    agent = runtime.factory.build("a", SESSION)

    from strands.models.openai import OpenAIModel

    assert isinstance(agent.model, OpenAIModel)
    config = agent.model.get_config()
    assert config["model_id"] == "my-model"
    assert config["params"] == {"reasoning_effort": "low"}


def test_openai_chat_api_key_env_selects_a_per_agent_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "official-key")
    monkeypatch.setenv("LOCAL_LLM_KEY", "local-key")
    agents = tmp_path / "agents"
    write_agent(agents, "official", model_table='[model]\nid = "openai-chat/gpt-x"\n')
    write_agent(
        agents,
        "local",
        model_table=(
            '[model]\nid = "openai-chat/org/model:tag"\nbase_url = "http://localhost:9/v1"\n'
            'api_key_env = "LOCAL_LLM_KEY"\n'
        ),
    )
    runtime = Runtime.load(write_runtime(tmp_path))

    from strands.models.openai import OpenAIModel

    official = runtime.factory.build("official", SESSION)
    local = runtime.factory.build("local", SESSION)
    assert isinstance(official.model, OpenAIModel) and isinstance(local.model, OpenAIModel)
    assert official.model.client_args.get("api_key") is None  # openai SDK falls back to OPENAI_API_KEY
    assert local.model.client_args == {"base_url": "http://localhost:9/v1", "api_key": "local-key"}
    assert local.model.get_config()["model_id"] == "org/model:tag"


def test_openai_chat_missing_api_key_env_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    write_agent(tmp_path / "agents", "a", model_table='[model]\nid = "openai-chat/m"\napi_key_env = "NO_SUCH_KEY"\n')
    runtime = Runtime.load(write_runtime(tmp_path))
    with pytest.raises(DefinitionError, match="NO_SUCH_KEY"):
        runtime.factory.build("a", SESSION)


def test_openai_chat_base_url_requires_that_provider(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents", "a", model_table='[model]\nid = "openai/gpt-x"\nbase_url = "http://x"\n')
    with pytest.raises(DefinitionError, match="base_url"):
        Runtime.load(write_runtime(tmp_path))
