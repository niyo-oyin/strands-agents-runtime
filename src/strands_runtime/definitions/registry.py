"""ランタイムプロセスが知っているエージェント定義の集合。

「公開する」エージェント（クライアントが名前で呼べるもの）と「読み込む」エージェント
（公開するものと、そこから ``subagents`` や Graph / Swarm のノードで到達できるものすべて）を区別する。
``runtime.toml`` の ``enabled_agents`` で公開範囲を絞れば、同じイメージを複数のデプロイで使い分けられる。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from strands_runtime.definitions.loader import AGENT_FILE, ResolvedAgent, load_agent
from strands_runtime.definitions.schema import AgentDefinition, RuntimeConfig, SwarmDefinition, dependencies_of
from strands_runtime.errors import DefinitionError, UnknownAgentError


class Registry:
    def __init__(self, agents: dict[str, ResolvedAgent], published: list[str]) -> None:
        self._agents = agents
        self._published = published
        self._validate_graph()

    @classmethod
    def load(cls, config: RuntimeConfig, base_dir: Path) -> Registry:
        """``runtime.agents_dir`` 配下のエージェントディレクトリをすべて読み、``enabled_agents`` を適用する。"""
        agents_dir = (base_dir / config.runtime.agents_dir).resolve()
        if not agents_dir.is_dir():
            raise DefinitionError("agents directory not found", source=str(agents_dir))

        # ``agent.toml`` を持つサブディレクトリだけをエージェントとみなす（README などの同居を許す）。
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

        # まず全定義で参照の整合性を検証してから、公開対象から到達できる定義だけに絞る。
        # 無関係な定義が壊れていても、このデプロイの起動には影響させない。
        registry = cls(agents, published)
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
        """クライアントが名前で要求した、公開中のエージェント。"""
        if name not in self._published:
            raise UnknownAgentError(name, self._published)
        return self._agents[name]

    def dependency(self, name: str) -> ResolvedAgent:
        """``subagents`` やノードの解決に使う、読み込み済みの定義（公開していなくてもよい）。"""
        try:
            return self._agents[name]
        except KeyError as exc:
            raise DefinitionError(f"referenced definition {name!r} is not defined") from exc

    def _reachable(self, roots: list[str]) -> list[str]:
        """``roots`` から ``subagents`` とノードをたどって到達できる名前（重複なし、深さ優先）。"""
        seen: list[str] = []
        stack = list(roots)
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.append(name)
            stack.extend(dependencies_of(self._agents[name].definition))
        return seen

    def _validate_graph(self) -> None:
        """参照先が存在し、種類の制約を満たし、循環していないことを確認する。

        ``subagents`` と Swarm のノードは ``kind = "agent"`` に限る（Strands の ``as_tool`` と ``Swarm`` の制約）。
        Graph のノードはどの種類でもよい。
        """
        for agent in self._agents.values():
            source = str(agent.directory / AGENT_FILE)
            d = agent.definition
            for dep in dependencies_of(d):
                if dep not in self._agents:
                    raise DefinitionError(f"{d.kind} {d.name!r} references undefined {dep!r}", source=source)
            if isinstance(d, AgentDefinition):
                agents_only, where = d.subagents, "subagents"
            elif isinstance(d, SwarmDefinition):
                agents_only, where = d.dependencies, "swarm nodes"
            else:
                agents_only, where = [], ""
            for dep in agents_only:
                target = self._agents[dep].definition
                if not isinstance(target, AgentDefinition):
                    raise DefinitionError(
                        f"{where} must be kind = 'agent', but {dep!r} is a {target.kind}", source=source
                    )
        for name in self._agents:
            self._check_cycle(name, [])

    def _check_cycle(self, name: str, path: list[str]) -> None:
        if name in path:
            cycle = " -> ".join([*path[path.index(name) :], name])
            raise DefinitionError(f"definition cycle detected: {cycle}")
        for dep in dependencies_of(self._agents[name].definition):
            self._check_cycle(dep, [*path, name])
