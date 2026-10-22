"""Read ``runtime.toml`` and agent directories from disk into validated, path-resolved objects."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from strands_runtime.definitions.schema import AgentDefinition, RuntimeConfig
from strands_runtime.errors import DefinitionError

AGENT_FILE = "agent.toml"
DEFAULT_SKILLS_DIR = "skills"

_ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def expand_env(value: Any, *, source: str) -> Any:
    """Recursively expand ``${VAR}`` and ``${VAR:-default}`` in every string of a TOML tree.

    A reference to an unset variable without a default is an error: silently keeping the literal
    would surface much later as a confusing auth or path failure.
    """
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            if name in os.environ:
                return os.environ[name]
            default = match.group("default")
            if default is not None:
                return default
            raise DefinitionError(f"environment variable {name!r} is not set", source=source)

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: expand_env(v, source=source) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, source=source) for v in value]
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise DefinitionError("file not found", source=str(path)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise DefinitionError(f"invalid TOML: {exc}", source=str(path)) from exc
    return cast(dict[str, Any], expand_env(data, source=str(path)))


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors(include_url=False):
        loc = ".".join(str(part) for part in item["loc"])
        lines.append(f"{loc}: {item['msg']}" if loc else item["msg"])
    return "; ".join(lines)


def load_runtime_config(path: Path) -> RuntimeConfig:
    """Load and validate ``runtime.toml``. A missing file yields the defaults."""
    if not path.exists():
        return RuntimeConfig()
    data = _read_toml(path)
    try:
        return RuntimeConfig.model_validate(data)
    except ValidationError as exc:
        raise DefinitionError(_describe(exc), source=str(path)) from exc


def is_path_reference(value: str) -> bool:
    """Whether a tool/plugin string denotes a file rather than an importable module.

    Mirrors the heuristic Strands uses for tool strings: explicit relative/absolute prefixes or a
    ``.py`` suffix mean a file; everything else is ``pkg.module[:attr]``.
    """
    head = value.split(":", 1)[0]
    return head.startswith(("./", "../", "/", "~")) or head.endswith(".py")


def _resolve_path_reference(value: str, base_dir: Path) -> str:
    """Absolutize the path part of ``path[:attr]`` against ``base_dir``."""
    path_part, sep, attr = value.partition(":")
    resolved = (base_dir / Path(path_part).expanduser()).resolve()
    return f"{resolved}{sep}{attr}"


@dataclass(frozen=True)
class ResolvedAgent:
    """An :class:`AgentDefinition` plus everything the factory needs from the filesystem.

    All paths are absolute: the list fields below replace the definition's relative tool/skill/plugin
    references, and path references nested in ``definition`` (``model.module``, ``structured_output.model``)
    have been absolutized in place. ``mcp_servers`` is always a mapping (``mcp.json`` has been read).
    """

    definition: AgentDefinition
    directory: Path
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    interventions: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.definition.name


def load_agent(directory: Path) -> ResolvedAgent:
    """Load one agent directory (``agent.toml`` + ``prompt.md`` + optional ``skills/``, ``mcp.json``)."""
    directory = directory.resolve()
    agent_file = directory / AGENT_FILE
    data = _read_toml(agent_file)
    try:
        definition = AgentDefinition.model_validate(data)
    except ValidationError as exc:
        raise DefinitionError(_describe(exc), source=str(agent_file)) from exc

    if definition.name != directory.name:
        raise DefinitionError(
            f"name {definition.name!r} must match the directory name {directory.name!r}", source=str(agent_file)
        )

    prompt_path = directory / definition.prompt
    try:
        system_prompt = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DefinitionError(f"prompt file not found: {definition.prompt}", source=str(agent_file)) from exc
    if not system_prompt.strip():
        raise DefinitionError("prompt file is empty", source=str(prompt_path))

    tools = [_resolve_path_reference(t, directory) if is_path_reference(t) else t for t in definition.tools]
    for tool in tools:
        if is_path_reference(tool) and not Path(tool.split(":", 1)[0]).exists():
            raise DefinitionError(f"tool file not found: {tool}", source=str(agent_file))

    if definition.skills is None:
        default_skills = directory / DEFAULT_SKILLS_DIR
        skills = [str(default_skills)] if default_skills.is_dir() else []
    else:
        skills = [
            s if s.startswith(("http://", "https://")) else str((directory / s).resolve()) for s in definition.skills
        ]
        for skill in skills:
            if not skill.startswith(("http://", "https://")) and not Path(skill).exists():
                raise DefinitionError(f"skills path not found: {skill}", source=str(agent_file))

    mcp_servers = _load_mcp_servers(definition.mcp_servers, directory, agent_file)

    interventions = [
        _resolve_path_reference(i, directory) if is_path_reference(i) or i.endswith(".cedar") else i
        for i in definition.interventions
    ]
    plugins = [_resolve_path_reference(p, directory) if is_path_reference(p) else p for p in definition.plugins]

    # Code references nested inside the definition (model.module, structured_output.model) are resolved in place.
    updates: dict[str, Any] = {}
    if definition.model.module and is_path_reference(definition.model.module):
        module = _resolve_path_reference(definition.model.module, directory)
        updates["model"] = definition.model.model_copy(update={"module": module})
    if definition.structured_output and is_path_reference(definition.structured_output.model):
        model_ref = _resolve_path_reference(definition.structured_output.model, directory)
        updates["structured_output"] = definition.structured_output.model_copy(update={"model": model_ref})
    if updates:
        definition = definition.model_copy(update=updates)

    return ResolvedAgent(
        definition=definition,
        directory=directory,
        system_prompt=system_prompt,
        tools=tools,
        skills=skills,
        mcp_servers=mcp_servers,
        interventions=interventions,
        plugins=plugins,
    )


def _load_mcp_servers(
    value: dict[str, dict[str, Any]] | str, directory: Path, agent_file: Path
) -> dict[str, dict[str, Any]]:
    if isinstance(value, str):
        path = (directory / value).resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DefinitionError(f"mcp_servers file not found: {value}", source=str(agent_file)) from exc
        except json.JSONDecodeError as exc:
            raise DefinitionError(f"invalid JSON: {exc}", source=str(path)) from exc
        raw = expand_env(raw, source=str(path))
        servers = raw.get("mcpServers", raw) if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            raise DefinitionError("expected an object with an 'mcpServers' key", source=str(path))
    else:
        servers = value

    resolved: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            raise DefinitionError(f"mcp_servers.{name} must be a table", source=str(agent_file))
        cfg = dict(cfg)
        if "cwd" in cfg and isinstance(cfg["cwd"], str):
            cfg["cwd"] = str((directory / Path(cfg["cwd"]).expanduser()).resolve())
        resolved[name] = cfg
    return resolved
