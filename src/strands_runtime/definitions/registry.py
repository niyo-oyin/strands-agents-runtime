"""The set of agent definitions a runtime process knows about.

A registry distinguishes *published* agents (those clients may invoke by name) from *loaded*
agents (published ones plus everything reachable through ``subagents``). ``enabled_agents`` in
``runtime.toml`` narrows the published set so the same image can back several AgentCore Runtimes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from strands_runtime.definitions.loader import AGENT_FILE, ResolvedAgent, load_agent
from strands_runtime.definitions.schema import RuntimeConfig
from strands_runtime.errors import DefinitionError, UnknownAgentError


class Registry:
    def __init__(self, agents: dict[str, ResolvedAgent], published: list[str]) -> None:
        self._agents = agents
        self._published = published
        self._validate_graph()

    @classmethod
    def load(cls, config: RuntimeConfig, base_dir: Path) -> Registry:
        """Discover every agent directory under ``runtime.agents_dir`` and apply ``enabled_agents``."""
        agents_dir = (base_dir / config.runtime.agents_dir).resolve()
        if not agents_dir.is_dir():
            raise DefinitionError("agents directory not found", source=str(agents_dir))

        agents: dict[str, ResolvedAgent] = {}
        for child in sorted(agents_dir.iterdir()):
            if child.is_dir() and (child / AGENT_FILE).is_file():
                agents[child.name] = load_agent(child)
        if not agents:
            raise DefinitionError(f"no agent definitions ({AGENT_FILE}) found", source=str(agents_dir))

        enabled = config.runtime.enabled_agents
        if enabled is None:
            published = list(agents)
        else:
            missing = [name for name in enabled if name not in agents]
            if missing:
                raise DefinitionError(f"enabled_agents not found under {agents_dir}: {', '.join(missing)}")
            published = list(enabled)

        default = config.runtime.default_agent
        if default is not None and default not in published:
            raise DefinitionError(f"default_agent {default!r} is not a published agent")

        registry = cls(agents, published)
        # Keep only what is reachable so an unrelated broken definition cannot affect this runtime.
        reachable = registry._reachable(published)
        return cls({name: agents[name] for name in reachable}, published)

    @property
    def published(self) -> list[str]:
        return list(self._published)

    def __iter__(self) -> Iterator[ResolvedAgent]:
        return iter(self._agents.values())

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def get(self, name: str) -> ResolvedAgent:
        """A published agent, as requested by a client."""
        if name not in self._published:
            raise UnknownAgentError(name, self._published)
        return self._agents[name]

    def dependency(self, name: str) -> ResolvedAgent:
        """Any loaded agent, for ``subagents`` resolution (may be unpublished)."""
        try:
            return self._agents[name]
        except KeyError as exc:
            raise DefinitionError(f"subagent {name!r} is not defined") from exc

    def _reachable(self, roots: list[str]) -> list[str]:
        seen: list[str] = []
        stack = list(roots)
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.append(name)
            stack.extend(self._agents[name].definition.subagents)
        return seen

    def _validate_graph(self) -> None:
        for agent in self._agents.values():
            for sub in agent.definition.subagents:
                if sub not in self._agents:
                    raise DefinitionError(f"subagent {sub!r} is not defined", source=str(agent.directory / AGENT_FILE))
        for name in self._agents:
            self._check_cycle(name, [])

    def _check_cycle(self, name: str, path: list[str]) -> None:
        if name in path:
            cycle = " -> ".join([*path[path.index(name) :], name])
            raise DefinitionError(f"subagent cycle detected: {cycle}")
        for sub in self._agents[name].definition.subagents:
            self._check_cycle(sub, [*path, name])
