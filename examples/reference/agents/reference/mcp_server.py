"""``[mcp_servers.demo]`` が起動する最小の MCP サーバー（stdio）。"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("reference-demo")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    server.run()
