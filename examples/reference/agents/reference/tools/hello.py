"""``tools = ["./tools/hello.py"]`` から読み込まれるツール。``@tool`` の docstring がモデルに見せる説明になる。"""

from strands import tool


@tool
def hello(name: str) -> str:
    """Greet someone by name.

    Args:
        name: Who to greet.
    """
    return f"Hello, {name}!"
