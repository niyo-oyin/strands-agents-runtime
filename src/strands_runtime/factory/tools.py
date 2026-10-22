"""Tool-side building blocks: MCP clients (cached per process), skills and interventions."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from strands.interventions import InterventionHandler
from strands.plugins import Plugin
from strands.tools.mcp import MCPClient
from strands.vended_plugins.skills import AgentSkills, Skill

from strands_runtime.definitions.loader import ResolvedAgent
from strands_runtime.factory.references import resolve_instance

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
