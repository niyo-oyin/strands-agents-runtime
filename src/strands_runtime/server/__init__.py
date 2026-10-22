"""HTTP surface: the AgentCore Runtime contract (``POST /invocations``, ``GET /ping``), also used locally."""

from strands_runtime.server.app import create_app

__all__ = ["create_app"]
