from __future__ import annotations

from pathlib import Path

import pytest
from strands.session import SnapshotSessionManager

from strands_runtime.errors import ReferenceResolutionError, UnknownAgentError
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
