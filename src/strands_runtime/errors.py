"""Runtime-wide exception types."""

from __future__ import annotations


class StrandsRuntimeError(Exception):
    """Base class for errors raised by strands_runtime."""


class DefinitionError(StrandsRuntimeError):
    """An agent definition or runtime config is invalid."""

    def __init__(self, message: str, *, source: str | None = None) -> None:
        self.source = source
        super().__init__(f"{source}: {message}" if source else message)


class UnknownAgentError(StrandsRuntimeError):
    """The requested agent is not published by this runtime."""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(f"unknown agent {name!r}; available: {', '.join(available) or '(none)'}")


class SessionBusyError(StrandsRuntimeError):
    """Another invocation is running for the same session in this process (mirrors AgentCore's 409)."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"session {session_id!r} already has an invocation in progress; retry later")


class ReferenceResolutionError(StrandsRuntimeError):
    """A module/attribute reference in a definition could not be resolved."""
