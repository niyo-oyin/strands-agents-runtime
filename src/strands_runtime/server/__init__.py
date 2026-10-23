"""HTTP の入口。``POST /invocations`` と ``GET /ping``（AgentCore 互換。任意のコンテナ基盤で動く）。"""

from strands_runtime.server.app import create_app, run_app

__all__ = ["create_app", "run_app"]
