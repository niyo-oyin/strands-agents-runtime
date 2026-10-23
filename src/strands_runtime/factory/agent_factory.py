"""Assemble a ``strands.Agent`` from a :class:`ResolvedAgent` for one invocation."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from strands import Agent
from strands.hooks import HookProvider

from strands_runtime.definitions.loader import ResolvedAgent
from strands_runtime.definitions.registry import Registry
from strands_runtime.definitions.schema import RuntimeConfig
from strands_runtime.errors import DefinitionError
from strands_runtime.factory.models import build_model
from strands_runtime.factory.references import resolve_class
from strands_runtime.factory.sessions import SessionContext, SessionManagerFactory, effective_session_spec
from strands_runtime.factory.tools import (
    MCPClientCache,
    build_context_injectors,
    build_interventions,
    build_plugins,
    build_skills,
    context_manager_option,
)

logger = logging.getLogger(__name__)


class AgentFactory:
    """Builds agents per request; caches only session-independent resources.

    A ``strands.Agent`` holds conversation state, so one is created for every invocation and the
    ``SessionManager`` restores history. MCP clients and storage backends are shared across builds.
    """

    def __init__(self, registry: Registry, config: RuntimeConfig, *, base_dir: Path) -> None:
        self._registry = registry
        self._config = config
        self._mcp = MCPClientCache()
        self._sessions = SessionManagerFactory(base_dir)

    @property
    def registry(self) -> Registry:
        return self._registry

    def build(self, name: str, session: SessionContext, *, hooks: Sequence[HookProvider] = ()) -> Agent:
        """Build the published agent ``name`` for ``session``; ``hooks`` are attached to every agent in the tree."""
        return self._build(self._registry.get(name), session, depth=0, persist=True, hooks=list(hooks))

    def _build(
        self, resolved: ResolvedAgent, session: SessionContext, *, depth: int, persist: bool, hooks: list[HookProvider]
    ) -> Agent:
        """``persist=False`` builds a subagent: no session manager, so ``as_tool()`` can reset it per call."""
        d = resolved.definition
        if depth > self._config.runtime.max_subagent_depth:
            raise DefinitionError(
                f"subagent nesting deeper than {self._config.runtime.max_subagent_depth} at {d.name!r}"
            )

        tools: list[Any] = [*resolved.tools, *self._mcp.get(resolved)]
        for sub_name in d.subagents:
            sub = self._registry.dependency(sub_name)
            child = self._build(sub, session.child(sub_name), depth=depth + 1, persist=False, hooks=hooks)
            tools.append(child.as_tool(name=sub.name, description=sub.definition.description or None))

        plugins = [*build_plugins(resolved), *build_context_injectors(resolved, session)]
        skills = build_skills(resolved)
        if skills is not None:
            plugins.append(skills)

        structured_output_model: type[BaseModel] | None = None
        if d.structured_output is not None:
            structured_output_model = resolve_class(d.structured_output.model, BaseModel)

        session_manager = (
            self._sessions.build(effective_session_spec(d.session, self._config.session), session) if persist else None
        )
        trace_attributes: dict[str, str] = {
            "session.id": session.session_id,
            "user.id": session.actor_id,
            "agent.name": d.name,
        }

        return Agent(
            name=d.name,
            agent_id=d.name,
            description=d.description or None,
            model=build_model(d.model, self._config.model),
            system_prompt=resolved.system_prompt,
            tools=tools,
            plugins=plugins or None,
            context_manager=context_manager_option(d.context.manager),
            session_manager=session_manager,
            structured_output_model=structured_output_model,
            interventions=build_interventions(resolved) or None,
            callback_handler=None,
            hooks=list(hooks) or None,
            trace_attributes=trace_attributes,
        )
