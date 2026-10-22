"""HTTP surface: ``POST /invocations`` + ``GET /ping`` (AgentCore-compatible, works on any container platform)."""

from strands_runtime.server.app import create_app, run_app

__all__ = ["create_app", "run_app"]
