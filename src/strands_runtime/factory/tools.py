"""Tool-side building blocks: MCP clients (cached per process), skills and interventions."""

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
from strands_runtime.errors import DefinitionError
from strands_runtime.factory.references import load_attribute, resolve_instance
from strands_runtime.factory.sessions import SessionContext

logger = logging.getLogger(__name__)


class MCPClientCache:
    """One set of ``MCPClient`` instances per agent definition, shared by every invocation in the process.

    ``MCPClient`` is a ``ToolProvider``: the SDK connects lazily and keeps the session open, so
    reusing instances avoids respawning stdio servers on every request. On AgentCore a process is
    one session's microVM; locally it is the whole server.
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
    if not agent.skills:
        return None
    sources: list[str | Path | Skill] = list(agent.skills)
    return AgentSkills(skills=sources, strict=strict)


def build_plugins(agent: ResolvedAgent) -> list[Plugin]:
    return [resolve_instance(ref, Plugin) for ref in agent.plugins]  # type: ignore[type-abstract]


def build_context_injectors(agent: ResolvedAgent, session: SessionContext) -> list[Plugin]:
    """``[context] inject`` entries → SDK ``ContextInjector`` plugins (ephemeral text on each user turn)."""
    from strands.vended_plugins.context_injector import ContextInjector, InjectionContext

    spec = agent.definition.context
    plugins: list[Plugin] = []
    for entry in spec.inject:
        if entry == "datetime":
            zone = ZoneInfo(spec.timezone)

            def render_datetime(_: InjectionContext, zone: ZoneInfo = zone) -> str:
                return f"<current_datetime>{datetime.now(zone).isoformat(timespec='seconds')}</current_datetime>"

            plugins.append(ContextInjector(render_datetime, name=f"{agent.name}:inject:datetime"))
        elif entry == "actor":
            actor = session.actor_id

            def render_actor(_: InjectionContext, actor: str = actor) -> str:
                return f"<actor_id>{actor}</actor_id>"

            plugins.append(ContextInjector(render_actor, name=f"{agent.name}:inject:actor"))
        else:
            render = load_attribute(entry)
            if not callable(render):
                raise DefinitionError(f"context.inject entry {entry!r} is not callable")
            plugins.append(ContextInjector(render, name=f"{agent.name}:inject:{entry}"))
    return plugins


def build_interventions(agent: ResolvedAgent) -> list[InterventionHandler]:
    handlers: list[InterventionHandler] = []
    for ref in agent.interventions:
        if ref.endswith(".cedar"):
            from strands.vended_interventions import CedarAuthorization  # optional extra: [cedar]

            handlers.append(CedarAuthorization(policies=ref))
        else:
            handlers.append(resolve_instance(ref, InterventionHandler))  # type: ignore[type-abstract]
    return handlers


def context_manager_option(manager: str | dict[str, Any]) -> Any:
    """Translate the TOML spelling to what ``Agent(context_manager=)`` accepts (``"off"`` → ``False``)."""
    if manager == "off":
        return False
    return manager
