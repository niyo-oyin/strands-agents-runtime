"""``invocation_state`` の値をツールが読めることを確認するためのツール。``context=True`` で ToolContext を受け取る。"""

from strands import ToolContext, tool


@tool(context=True)
def whoami(tool_context: ToolContext) -> str:
    """Return the tenant this request belongs to (from the request's invocation_state)."""
    tenant = tool_context.invocation_state.get("tenant", "(unknown)")
    return f"tenant={tenant}"
