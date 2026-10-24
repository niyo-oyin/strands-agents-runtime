"""人の承認を interrupt で求めるツール。``interruptResponse`` で再開すると ``answer`` に応答が入る。"""

from strands import ToolContext, tool


@tool(context=True)
def request_approval(summary: str, tool_context: ToolContext) -> str:
    """Ask a human to approve publishing. Returns the human's answer.

    Args:
        summary: One-line summary of what will be published.
    """
    answer = tool_context.interrupt("publish-approval", reason={"summary": summary})
    return f"human answered: {answer}"
