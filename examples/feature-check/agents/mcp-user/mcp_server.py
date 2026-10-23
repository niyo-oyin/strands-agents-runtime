"""ランタイムの MCP 連携を確認するための、最小の stdio MCP サーバー。agent.toml の [mcp_servers.demo] から起動される。"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


@mcp.tool()
def reverse_text(text: str) -> str:
    """Reverse a string."""
    return text[::-1]


if __name__ == "__main__":
    mcp.run(transport="stdio")
