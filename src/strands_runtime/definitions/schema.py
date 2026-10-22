"""Pydantic schemas for ``agent.toml`` and ``runtime.toml``.

These models validate the *shape* of the TOML files only. Filesystem resolution (prompt text,
relative paths, ``mcp.json``) happens in :mod:`strands_runtime.definitions.loader`, and turning a
definition into a live ``strands.Agent`` happens in :mod:`strands_runtime.factory`.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Effort = Literal["auto", "off", "minimal", "low", "medium", "high", "xhigh", "max"]
ContextManagerName = Literal["auto", "agentic", "off"]
SessionBackend = Literal["inherit", "local", "s3", "agentcore_memory", "none"]

AGENT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
"""Agent names double as directory names, tool names (for ``subagents``) and ``agent_id`` values."""

NonBlankStr = Annotated[str, Field(min_length=1, pattern=r"\S")]

AGENT_TOP_LEVEL_KEYS = frozenset(
    {"name", "description", "kind", "prompt", "tools", "skills", "subagents", "mcp_servers", "interventions", "plugins"}
)
"""Bare keys of ``agent.toml``. Listed here so ``[model.config]`` can detect keys written after a table header."""


class _Strict(BaseModel):
    """Unknown keys are rejected so typos in TOML fail fast."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ModelSpec(_Strict):
    """``[model]`` table.

    ``id`` is a ``"provider/model-id"`` string resolved by ``strands_harness.models.resolve_model``;
    ``module`` is a module reference to a factory returning a ``strands.models.Model`` or ``ModelRouter``
    for anything the provider table cannot express. ``config`` is passed verbatim to ``Model.update_config``.
    """

    id: NonBlankStr | None = None
    module: NonBlankStr | None = None
    effort: Effort = "auto"
    caching: bool = True
    base_url: NonBlankStr | None = None
    """Endpoint for the ``openai-chat`` provider (OpenAI-compatible Chat Completions); default ``OPENAI_BASE_URL``."""
    api_key_env: NonBlankStr | None = None
    """Name of the environment variable holding the key for ``openai-chat``; default ``OPENAI_API_KEY``.

    Lets one runtime talk to several OpenAI-compatible endpoints with different keys. The value never
    appears in TOML, only the variable name.
    """
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _id_xor_module(self) -> ModelSpec:
        if self.id is not None and self.module is not None:
            raise ValueError("model.id and model.module are mutually exclusive")
        for key in ("base_url", "api_key_env"):
            if getattr(self, key) is not None and not (self.id or "").startswith("openai-chat/"):
                raise ValueError(f"model.{key} is only valid with an 'openai-chat/<model>' id")
        swallowed = sorted(set(self.config) & AGENT_TOP_LEVEL_KEYS)
        if swallowed:
            raise ValueError(
                f"model.config contains agent-level keys {swallowed}; in TOML, top-level keys such as "
                "tools/skills/subagents must be written before the first table header like [model]"
            )
        return self


class ContextSpec(_Strict):
    """``[context]`` table → ``Agent(context_manager=...)`` plus just-in-time context injection."""

    manager: ContextManagerName | dict[str, Any] = "auto"
    inject: list[NonBlankStr] = Field(default_factory=list)
    """Text injected into each user turn without touching history: built-in ``"datetime"`` and ``"actor"``,
    or a module reference to a callable ``(InjectionContext) -> str | None`` (SDK ``ContextInjector``)."""
    timezone: NonBlankStr = "UTC"
    """IANA zone used by the ``datetime`` injector."""


class RetrievalSpec(_Strict):
    """Per-namespace long-term memory retrieval for the ``agentcore_memory`` backend."""

    top_k: int = Field(default=10, gt=0, le=1000)
    relevance_score: float = Field(default=0.2, ge=0.0, le=1.0)
    strategy_id: str | None = None


class SessionSpec(_Strict):
    """``[session]`` table, used both in ``runtime.toml`` (defaults) and ``agent.toml`` (override)."""

    backend: SessionBackend = "inherit"
    # local
    dir: NonBlankStr | None = None
    # s3
    bucket: NonBlankStr | None = None
    prefix: str = ""
    region: NonBlankStr | None = None
    # agentcore_memory
    memory_id: NonBlankStr | None = None
    retrieval: dict[str, RetrievalSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _required_keys(self) -> SessionSpec:
        if self.backend == "s3" and not self.bucket:
            raise ValueError("session.bucket is required when backend = 's3'")
        if self.backend == "agentcore_memory" and not self.memory_id:
            raise ValueError("session.memory_id is required when backend = 'agentcore_memory'")
        return self


class StructuredOutputSpec(_Strict):
    """``[structured_output]`` table: a module reference to a pydantic model class."""

    model: NonBlankStr


class LimitsSpec(_Strict):
    """``[limits]`` table → ``strands.types.agent.Limits`` for every invocation."""

    turns: int | None = Field(default=None, gt=0)
    output_tokens: int | None = Field(default=None, gt=0)
    total_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    """Wall-clock bound for one invocation, enforced by the runtime (the agent is cancelled, then drained)."""

    def to_limits(self) -> dict[str, int]:
        return {k: v for k, v in self.model_dump(exclude={"timeout_seconds"}).items() if v is not None}


class AgentDefinition(_Strict):
    """The validated contents of one ``agent.toml``."""

    name: str = Field(pattern=AGENT_NAME_PATTERN.pattern)
    description: str = ""
    kind: Literal["agent"] = "agent"
    prompt: NonBlankStr = "prompt.md"
    model: ModelSpec = Field(default_factory=ModelSpec)
    tools: list[NonBlankStr] = Field(default_factory=list)
    skills: list[NonBlankStr] | None = None
    """``None`` means "use ``./skills`` if it exists"; an empty list disables skills explicitly."""
    subagents: list[str] = Field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] | NonBlankStr = Field(default_factory=dict)
    """Either an ``mcpServers``-style mapping (server name → MCPServerConfig) or a path to a JSON file."""
    context: ContextSpec = Field(default_factory=ContextSpec)
    session: SessionSpec = Field(default_factory=SessionSpec)
    structured_output: StructuredOutputSpec | None = None
    limits: LimitsSpec | None = None
    interventions: list[NonBlankStr] = Field(default_factory=list)
    plugins: list[NonBlankStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _subagent_names(self) -> AgentDefinition:
        for sub in self.subagents:
            if not AGENT_NAME_PATTERN.match(sub):
                raise ValueError(f"invalid subagent name {sub!r}")
            if sub == self.name:
                raise ValueError("an agent cannot list itself in subagents")
        if len(set(self.subagents)) != len(self.subagents):
            raise ValueError("subagents must be unique")
        return self


class RuntimeSection(_Strict):
    agents_dir: NonBlankStr = "./agents"
    default_agent: NonBlankStr | None = None
    enabled_agents: list[NonBlankStr] | None = None
    """``None`` publishes every agent under ``agents_dir``."""
    max_subagent_depth: int = Field(default=3, ge=1, le=10)


class ModelDefaults(_Strict):
    default_id: NonBlankStr = "bedrock/global.anthropic.claude-opus-5"


class ServerSection(_Strict):
    host: NonBlankStr | None = None
    """``None`` lets ``BedrockAgentCoreApp.run`` choose (0.0.0.0 inside a container, else 127.0.0.1)."""
    port: int = Field(default=8080, ge=1, le=65535)
    log_file: NonBlankStr | None = None
    """Also write the process log (runtime, Strands, uvicorn, warnings) to this file, relative to runtime.toml."""


class RuntimeConfig(_Strict):
    """The validated contents of ``runtime.toml``."""

    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    model: ModelDefaults = Field(default_factory=ModelDefaults)
    session: SessionSpec = Field(default_factory=lambda: SessionSpec(backend="local"))
    server: ServerSection = Field(default_factory=ServerSection)

    @model_validator(mode="after")
    def _session_not_inherit(self) -> RuntimeConfig:
        if self.session.backend == "inherit":
            raise ValueError("runtime.toml [session].backend cannot be 'inherit'")
        return self
