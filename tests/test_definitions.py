from __future__ import annotations

import json
from pathlib import Path

import pytest

from strands_runtime.definitions import Registry, load_agent, load_runtime_config
from strands_runtime.errors import DefinitionError, UnknownAgentError
from tests.conftest import write_agent, write_runtime


def test_load_agent_resolves_prompt_tools_skills_and_mcp(tmp_path: Path) -> None:
    directory = write_agent(
        tmp_path / "agents",
        "alpha",
        toml_extra='mcp_servers = "mcp.json"\n',
        prompt="# Prompt\n\nBe brief.",
    )
    (directory / "skills" / "greeting").mkdir(parents=True)
    (directory / "skills" / "greeting" / "SKILL.md").write_text("---\nname: greeting\ndescription: d\n---\nHi", "utf-8")
    (directory / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fs": {"command": "uvx", "args": ["server"], "cwd": "./data"}}}), "utf-8"
    )

    resolved = load_agent(directory)

    assert resolved.name == "alpha"
    assert resolved.system_prompt.startswith("# Prompt")
    assert resolved.tools == [str(directory / "tools" / "echo.py")]
    assert resolved.skills == [str(directory / "skills")]
    assert resolved.mcp_servers["fs"]["cwd"] == str(directory / "data")


def test_explicit_empty_skills_disables_autodetect(tmp_path: Path) -> None:
    directory = write_agent(tmp_path / "agents", "alpha", toml_extra="skills = []\n")
    (directory / "skills").mkdir()
    assert load_agent(directory).skills == []


def test_name_must_match_directory(tmp_path: Path) -> None:
    directory = write_agent(tmp_path / "agents", "alpha")
    (directory / "agent.toml").write_text((directory / "agent.toml").read_text().replace('"alpha"', '"beta"'))
    with pytest.raises(DefinitionError, match="must match the directory name"):
        load_agent(directory)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    directory = write_agent(tmp_path / "agents", "alpha", toml_extra="temperature = 0.2\n")
    with pytest.raises(DefinitionError, match="temperature"):
        load_agent(directory)


def test_missing_prompt_file(tmp_path: Path) -> None:
    directory = write_agent(tmp_path / "agents", "alpha", toml_extra='prompt = "nope.md"\n')
    with pytest.raises(DefinitionError, match="prompt file not found"):
        load_agent(directory)


def test_missing_tool_file(tmp_path: Path) -> None:
    directory = write_agent(tmp_path / "agents", "alpha", with_tool=False, toml_extra='tools = ["./tools/x.py"]\n')
    with pytest.raises(DefinitionError, match="tool file not found"):
        load_agent(directory)


def test_env_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "secret")
    monkeypatch.delenv("UNSET_VAR", raising=False)
    directory = write_agent(
        tmp_path / "agents",
        "alpha",
        toml_extra=(
            '[mcp_servers.docs]\nurl = "https://x/mcp"\n'
            'headers = { Authorization = "Bearer ${MCP_TOKEN}", X = "${UNSET_VAR:-dflt}" }\n'
        ),
    )
    resolved = load_agent(directory)
    assert resolved.mcp_servers["docs"]["headers"] == {"Authorization": "Bearer secret", "X": "dflt"}


def test_unset_env_without_default_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    directory = write_agent(tmp_path / "agents", "alpha", toml_extra='[mcp_servers.x]\nurl = "${NOPE}"\n')
    with pytest.raises(DefinitionError, match="NOPE"):
        load_agent(directory)


def test_registry_publishes_enabled_agents_and_loads_dependencies(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "parent", toml_extra='subagents = ["child"]\n')
    write_agent(agents, "child")
    write_agent(agents, "other")
    config = load_runtime_config(write_runtime(tmp_path, 'enabled_agents = ["parent"]'))

    registry = Registry.load(config, tmp_path)

    assert registry.published == ["parent"]
    assert "child" in registry and "other" not in registry
    assert registry.dependency("child").name == "child"
    with pytest.raises(UnknownAgentError):
        registry.get("child")


def test_registry_detects_subagent_cycles(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "a", toml_extra='subagents = ["b"]\n')
    write_agent(agents, "b", toml_extra='subagents = ["a"]\n')
    config = load_runtime_config(write_runtime(tmp_path))
    with pytest.raises(DefinitionError, match="cycle"):
        Registry.load(config, tmp_path)


def test_registry_rejects_unpublished_default_agent(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents", "a")
    write_agent(tmp_path / "agents", "b")
    config = load_runtime_config(write_runtime(tmp_path, 'enabled_agents = ["a"]\ndefault_agent = "b"'))
    with pytest.raises(DefinitionError, match="default_agent"):
        Registry.load(config, tmp_path)


def test_runtime_session_cannot_inherit(tmp_path: Path) -> None:
    path = write_runtime(tmp_path, '[session]\nbackend = "inherit"')
    with pytest.raises(DefinitionError, match="inherit"):
        load_runtime_config(path)


def test_missing_runtime_config_uses_defaults(tmp_path: Path) -> None:
    config = load_runtime_config(tmp_path / "runtime.toml")
    assert config.session.backend == "local"
    assert config.server.port == 8080


def test_top_level_keys_after_model_table_are_rejected_with_hint(tmp_path: Path) -> None:
    directory = write_agent(
        tmp_path / "agents", "alpha", with_tool=False, toml_extra='[model.config]\ntools = ["./tools/x.py"]\n'
    )
    with pytest.raises(DefinitionError, match="before the first table header"):
        load_agent(directory)


def test_nested_path_references_are_absolutized(tmp_path: Path) -> None:
    directory = write_agent(
        tmp_path / "agents",
        "alpha",
        toml_extra='[structured_output]\nmodel = "./schemas.py:Answer"\n',
        model_table='[model]\nmodule = "./fake_model.py:make_model"\n',
    )
    (directory / "schemas.py").write_text("", "utf-8")
    (directory / "fake_model.py").write_text("", "utf-8")

    resolved = load_agent(directory)

    assert resolved.definition.structured_output is not None
    assert resolved.definition.structured_output.model == f"{directory / 'schemas.py'}:Answer"
    assert resolved.definition.model.module == f"{directory / 'fake_model.py'}:make_model"


def test_enabled_agents_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = tmp_path / "agents"
    write_agent(agents, "a")
    write_agent(agents, "b")
    monkeypatch.setenv("STRANDS_RUNTIME_ENABLED_AGENTS", "b")
    config = load_runtime_config(write_runtime(tmp_path, 'enabled_agents = ["a"]'))

    assert Registry.load(config, tmp_path).published == ["b"]

    monkeypatch.setenv("STRANDS_RUNTIME_ENABLED_AGENTS", " , ")
    with pytest.raises(DefinitionError, match="names no agents"):
        load_runtime_config(write_runtime(tmp_path))
