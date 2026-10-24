"""ツール側の部品。MCP クライアント（プロセス内で共有）、スキル、コンテキスト注入、介入ハンドラー。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from strands.interventions import InterventionHandler
from strands.plugins import Plugin
from strands.tools.mcp import MCPClient
from strands.vended_plugins.skills import AgentSkills, Skill

from strands_runtime.definitions.loader import ResolvedAgent
from strands_runtime.definitions.schema import AgentDefinition
from strands_runtime.errors import DefinitionError
from strands_runtime.factory.references import load_attribute, resolve_instance
from strands_runtime.factory.sessions import SessionContext

logger = logging.getLogger(__name__)


class MCPClientCache:
    """エージェント定義ごとに ``MCPClient`` の組を 1 つだけ作り、プロセス内の全呼び出しで共有する。

    ``MCPClient`` は ``ToolProvider`` で、SDK が初回利用時に接続してセッションを保つ。
    インスタンスを使い回せば、リクエストのたびに stdio サーバーを起動し直さずに済む。
    """

    def __init__(self) -> None:
        self._clients: dict[str, list[MCPClient]] = {}
        self._lock = threading.Lock()

    def get(self, agent: ResolvedAgent) -> list[MCPClient]:
        if not agent.mcp_servers:
            return []
        with self._lock:
            if agent.name not in self._clients:
                self._clients[agent.name] = MCPClient.load_servers({"mcpServers": agent.mcp_servers})
            return self._clients[agent.name]


def build_skills(agent: ResolvedAgent, *, strict: bool = False) -> AgentSkills | None:
    """``skills`` の参照先から SDK の ``AgentSkills`` プラグインを作る。スキルがなければ ``None``。"""
    if not agent.skills:
        return None
    sources: list[str | Path | Skill] = list(agent.skills)
    return AgentSkills(skills=sources, strict=strict)


def build_plugins(agent: ResolvedAgent) -> list[Plugin]:
    """``plugins`` のモジュール参照を ``Plugin`` インスタンスに解決する。"""
    return [resolve_instance(ref, Plugin) for ref in agent.plugins]  # type: ignore[type-abstract]


def build_context_injectors(agent: ResolvedAgent, session: SessionContext) -> list[Plugin]:
    """``[context] inject`` の各項目を SDK の ``ContextInjector`` にする（ユーザーターンごとの一時的な注入）。"""
    from strands.vended_plugins.context_injector import ContextInjector, InjectionContext

    if not isinstance(agent.definition, AgentDefinition):
        return []
    spec = agent.definition.context
    plugins: list[Plugin] = []
    for entry in spec.inject:
        # 組み込み名は BUILTIN_INJECTORS と一致させる（ローダーと validate が同じ集合でコード参照と区別する）。
        if entry == "datetime":
            zone = ZoneInfo(spec.timezone)

            # 注入関数は呼び出しのたびに実行されるので、現在時刻はそのとき取得する。
            def render_datetime(_: InjectionContext, zone: ZoneInfo = zone) -> str:
                return f"<current_datetime>{datetime.now(zone).isoformat(timespec='seconds')}</current_datetime>"

            plugins.append(ContextInjector(render_datetime, name=f"{agent.name}:inject:datetime"))
        elif entry == "actor":
            actor = session.actor_id

            def render_actor(_: InjectionContext, actor: str = actor) -> str:
                return f"<actor_id>{actor}</actor_id>"

            plugins.append(ContextInjector(render_actor, name=f"{agent.name}:inject:actor"))
        else:
            # 組み込み以外は ``(InjectionContext) -> str | None`` の呼び出し可能オブジェクトへの参照。
            render = load_attribute(entry)
            if not callable(render):
                raise DefinitionError(f"context.inject entry {entry!r} is not callable")
            plugins.append(ContextInjector(render, name=f"{agent.name}:inject:{entry}"))
    return plugins


def build_interventions(agent: ResolvedAgent) -> list[InterventionHandler]:
    """``interventions`` を解決する。``.cedar`` ファイルは Cedar ポリシー、それ以外はハンドラーへのモジュール参照。"""
    handlers: list[InterventionHandler] = []
    for ref in agent.interventions:
        if ref.endswith(".cedar"):
            from strands.vended_interventions import CedarAuthorization  # extras の [cedar] が必要

            handlers.append(CedarAuthorization(policies=ref))
        else:
            handlers.append(resolve_instance(ref, InterventionHandler))  # type: ignore[type-abstract]
    return handlers


def context_manager_option(manager: str | dict[str, Any]) -> Any:
    """TOML の表記を ``Agent(context_manager=)`` が受け付ける値にする（``"off"`` は ``False``）。"""
    if manager == "off":
        return False
    return manager
