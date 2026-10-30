""":class:`ResolvedAgent` から、1 回の呼び出しに使う ``strands.Agent``（または Graph / Swarm）を組み立てる。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from strands import Agent
from strands.hooks import HookProvider
from strands.multiagent import GraphBuilder, MultiAgentBase, Swarm
from strands.multiagent.graph import GraphState

from strands_runtime.definitions.loader import ResolvedAgent
from strands_runtime.definitions.registry import Registry
from strands_runtime.definitions.schema import (
    AgentDefinition,
    GraphDefinition,
    HistorySpec,
    RuntimeConfig,
    SwarmDefinition,
)
from strands_runtime.errors import DefinitionError, InvalidRequestError
from strands_runtime.factory.models import build_model, override_model
from strands_runtime.factory.references import load_attribute, resolve_class
from strands_runtime.factory.request_options import configure_tool_choice, validate_generation
from strands_runtime.factory.sessions import SessionContext, SessionManagerFactory, effective_history_spec
from strands_runtime.factory.tools import (
    MCPClientCache,
    build_context_injectors,
    build_interventions,
    build_plugins,
    build_skills,
    context_manager_option,
)
from strands_runtime_client.inputs import GenerationOptions

logger = logging.getLogger(__name__)


def _condition(reference: str) -> Callable[..., bool]:
    """エッジ条件のコード参照を解決する。Strands は引数名で新旧の形（``invocation_state`` の有無）を見分ける。"""
    func = load_attribute(reference)
    if not callable(func):
        raise DefinitionError(f"edge condition {reference!r} is not callable")
    return func  # type: ignore[no-any-return]


def _all_completed(from_ids: list[str]) -> Callable[[GraphState], bool]:
    """``wait_for = "all"`` の合流条件。入ってくるエッジの元ノードがすべて完了しているときだけ進む。"""

    def condition(state: GraphState) -> bool:
        completed = {node.node_id for node in state.completed_nodes}
        return all(node_id in completed for node_id in from_ids)

    return condition


class AgentFactory:
    """リクエストごとに ``Agent`` を作る。プロセス内で共有するのはセッションに依存しない資源だけ。

    ``strands.Agent`` は会話履歴を持つオブジェクトなので毎回新しく作り、履歴の復元は ``SessionManager`` に任せる。
    MCP クライアントとストレージは呼び出しをまたいで使い回す。
    """

    def __init__(self, registry: Registry, config: RuntimeConfig, *, base_dir: Path) -> None:
        self._registry = registry
        self._config = config
        self._mcp = MCPClientCache()
        self._sessions = SessionManagerFactory(base_dir)

    @property
    def sessions(self) -> SessionManagerFactory:
        """セッションストレージの共有元。ジョブのジャーナルも同じ ``Storage`` を使う。"""
        return self._sessions

    @property
    def registry(self) -> Registry:
        return self._registry

    def resolve(
        self, name: str, *, model: str | None = None, options: GenerationOptions | None = None
    ) -> ResolvedAgent:
        """呼び出し単位のモデル指定を適用する。共有の定義とサブエージェントの設定は変更しない。"""
        resolved = self._registry.get(name)
        if model is None and (options is None or not options.generation_overrides()):
            return resolved
        definition = resolved.definition
        if not isinstance(definition, AgentDefinition):
            raise InvalidRequestError("model override / generation overrides are only supported for kind = 'agent'")
        spec = override_model(definition.model, self._config.model, model) if model is not None else definition.model
        updates: dict[str, Any] = {"model": spec}
        if options is not None:
            if definition.structured_output is not None:
                if options.response_format is not None:
                    raise InvalidRequestError("response_format cannot override an agent with structured_output")
                if options.tool_choice is not None:
                    raise InvalidRequestError("tool_choice conflicts with the agent's structured_output")
            validate_generation(spec, self._config.model, options)
        return replace(resolved, definition=definition.model_copy(update=updates))

    def build(
        self,
        name: str,
        session: SessionContext,
        *,
        hooks: Sequence[HookProvider] = (),
        model: str | None = None,
        persist: bool = True,
        options: GenerationOptions | None = None,
    ) -> Agent | MultiAgentBase:
        """公開中の定義 ``name`` を ``session`` 用に組み立てる。``hooks`` はツリー内の全エージェントに付く。

        ``kind = "agent"`` なら ``Agent``、``graph`` / ``swarm`` なら Strands のオーケストレーターを返す。
        どちらも ``stream_async`` を持ち、Invoker はそれだけに依存する。
        """
        resolved = self.resolve(name, model=model, options=options)
        return self._build(
            resolved, session, depth=0, persist=persist, hooks=list(hooks), scope=resolved.name, options=options
        )

    def _build(
        self,
        resolved: ResolvedAgent,
        session: SessionContext,
        *,
        depth: int,
        persist: bool,
        hooks: list[HookProvider],
        scope: str,
        node_id: str | None = None,
        options: GenerationOptions | None = None,
    ) -> Agent | MultiAgentBase:
        """種類で分岐する。``scope`` は入れ子のオーケストレーターの ID を一意にするための名前の連なり。"""
        if depth > self._config.runtime.max_subagent_depth:
            raise DefinitionError(f"nesting deeper than {self._config.runtime.max_subagent_depth} at {resolved.name!r}")
        d = resolved.definition
        if isinstance(d, GraphDefinition):
            return self._build_graph(resolved, d, session, depth=depth, persist=persist, hooks=hooks, scope=scope)
        if isinstance(d, SwarmDefinition):
            return self._build_swarm(resolved, d, session, depth=depth, persist=persist, hooks=hooks, scope=scope)
        return self._build_agent(
            resolved, d, session, depth=depth, persist=persist, hooks=hooks, node_id=node_id, options=options
        )

    def _node(
        self,
        node_agent: str,
        node_id: str,
        session: SessionContext,
        *,
        depth: int,
        hooks: list[HookProvider],
        scope: str,
    ) -> Agent | MultiAgentBase:
        """Graph / Swarm のノード 1 つ。セッションは付けず（Strands の制約）、名前はノード ID にする。"""
        dep = self._registry.dependency(node_agent)
        return self._build(
            dep,
            session.child(node_id),
            depth=depth + 1,
            persist=False,
            hooks=hooks,
            scope=f"{scope}--{node_id}",
            node_id=node_id,
        )

    def _orchestrator_session(self, spec: HistorySpec, session: SessionContext, *, persist: bool) -> Any:
        """オーケストレーターの状態（進んだノード、中断状態）の保存先。``none`` と入れ子では付けない。"""
        if not persist:
            return None
        effective = effective_history_spec(spec, self._config.session.history)
        if effective.backend == "none":
            return None
        if effective.backend == "agentcore_memory":
            raise DefinitionError("graph / swarm cannot persist state to the 'agentcore_memory' backend")
        return self._sessions.build(effective, session)

    def _build_graph(
        self,
        resolved: ResolvedAgent,
        d: GraphDefinition,
        session: SessionContext,
        *,
        depth: int,
        persist: bool,
        hooks: list[HookProvider],
        scope: str,
    ) -> MultiAgentBase:
        builder = GraphBuilder()
        for node in d.node_specs:
            builder.add_node(
                self._node(node.agent, node.node_id, session, depth=depth, hooks=hooks, scope=scope), node.node_id
            )
        # ``wait_for = "all"`` のノードへ向かうエッジには、元ノードが全部完了したことを確かめる条件を合成する
        # （Strands の既定は OR で、1 本でも条件を満たせば実行可能になる）。
        inbound: dict[str, list[str]] = {}
        for edge in d.edges:
            inbound.setdefault(edge.to, []).append(edge.from_)
        wait_all = {n.node_id for n in d.node_specs if n.wait_for == "all"}
        for edge in d.edges:
            condition: Callable[..., bool] | None
            if edge.to in wait_all:
                condition = _all_completed(inbound[edge.to])
            elif edge.condition:
                condition = _condition(edge.condition)
            else:
                condition = None
            builder.add_edge(edge.from_, edge.to, condition=condition)
        for entry in d.entry:
            builder.set_entry_point(entry)
        builder.reset_on_revisit(d.reset_on_revisit)
        if d.limits.node_executions is not None:
            builder.set_max_node_executions(d.limits.node_executions)
        if d.limits.timeout_seconds is not None:
            builder.set_execution_timeout(d.limits.timeout_seconds)
        if d.limits.node_timeout_seconds is not None:
            builder.set_node_timeout(d.limits.node_timeout_seconds)
        builder.set_graph_id(scope)
        session_manager = self._orchestrator_session(d.session.history, session, persist=persist)
        if session_manager is not None:
            builder.set_session_manager(session_manager)
        return builder.build()

    def _build_swarm(
        self,
        resolved: ResolvedAgent,
        d: SwarmDefinition,
        session: SessionContext,
        *,
        depth: int,
        persist: bool,
        hooks: list[HookProvider],
        scope: str,
    ) -> MultiAgentBase:
        agents: list[Agent] = []
        entry: Agent | None = None
        for node in d.node_specs:
            built = self._node(node.agent, node.node_id, session, depth=depth, hooks=hooks, scope=scope)
            if not isinstance(built, Agent):
                raise DefinitionError(f"swarm node {node.node_id!r} must be kind = 'agent'")
            agents.append(built)
            if node.node_id == d.entry:
                entry = built
        limits = d.limits
        return Swarm(
            agents,
            entry_point=entry,
            max_handoffs=limits.handoffs,
            max_iterations=limits.handoffs,
            execution_timeout=limits.timeout_seconds,
            node_timeout=limits.node_timeout_seconds,
            repetitive_handoff_detection_window=limits.repetitive_handoff_window,
            repetitive_handoff_min_unique_agents=limits.repetitive_handoff_min_unique_agents,
            session_manager=self._orchestrator_session(d.session.history, session, persist=persist),
            id=scope,
        )

    def _build_agent(
        self,
        resolved: ResolvedAgent,
        d: AgentDefinition,
        session: SessionContext,
        *,
        depth: int,
        persist: bool,
        hooks: list[HookProvider],
        node_id: str | None,
        options: GenerationOptions | None = None,
    ) -> Agent:
        """``persist=False`` はサブエージェントとノード用。

        セッションマネージャーを付けない。
        ``as_tool()`` は呼び出しごとに子の状態をリセットするため永続化と両立せず、Graph / Swarm はノードに持たせることを禁じている。
        ``node_id`` が与えられたら名前にする（Swarm はノード ID に ``agent.name`` を使う）。
        """
        # ツールは「文字列参照」「MCP クライアント」「ツール化したサブエージェント」を 1 つのリストにして SDK に渡す。
        tools: list[Any] = [*resolved.tools, *self._mcp.get(resolved)]
        for sub_name in d.subagents:
            sub = self._registry.dependency(sub_name)
            child = self._build(
                sub, session.child(sub_name), depth=depth + 1, persist=False, hooks=hooks, scope=f"{d.name}--{sub_name}"
            )
            if not isinstance(child, Agent):
                raise DefinitionError(f"subagent {sub_name!r} must be kind = 'agent'")
            tools.append(child.as_tool(name=sub.name, description=sub.definition.description or None))

        plugins = [*build_plugins(resolved), *build_context_injectors(resolved, session)]
        skills = build_skills(resolved)
        if skills is not None:
            plugins.append(skills)

        structured_output_model: type[BaseModel] | None = None
        if d.structured_output is not None:
            structured_output_model = resolve_class(d.structured_output.model, BaseModel)

        session_manager = (
            self._sessions.build(effective_history_spec(d.session.history, self._config.session.history), session)
            if persist
            else None
        )
        # ``session.id`` と ``user.id`` は可視化ツールがセッション・利用者のグルーピングに使う属性名。
        trace_attributes: dict[str, str] = {
            "session.id": session.session_id,
            **({"user.id": session.actor_id} if session.actor_id is not None else {}),
            "agent.name": d.name,
        }

        model = build_model(d.model, self._config.model, options)
        agent = Agent(
            name=node_id or d.name,
            agent_id=node_id or d.name,
            description=d.description or None,
            model=model,
            system_prompt=resolved.system_prompt,
            tools=tools,
            plugins=plugins or None,
            context_manager=context_manager_option(d.context.manager),
            session_manager=session_manager,
            structured_output_model=structured_output_model,
            interventions=build_interventions(resolved) or None,
            callback_handler=None,  # 出力は stream_async のイベントで扱うので、標準出力への表示は無効にする
            hooks=list(hooks) or None,
            trace_attributes=trace_attributes,
        )
        if options is not None:
            configure_tool_choice(agent, model, options)
        return agent
