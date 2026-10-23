from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from strands_runtime.runtime import Runtime

ECHO_TOOL = '''
from strands import tool


@tool
def echo(text: str) -> str:
    """Echo the given text back.

    Args:
        text: Text to echo.
    """
    return f"echo:{text}"
'''

FAKE_MODEL = """
[model]
module = "tests.fakes:make_model"
"""


def write_agent(
    agents_dir: Path,
    name: str,
    *,
    toml_extra: str = "",
    prompt: str = "You are a test agent.",
    with_tool: bool = True,
    model_table: str = FAKE_MODEL,
) -> Path:
    directory = agents_dir / name
    directory.mkdir(parents=True)
    tools_line = 'tools = ["./tools/echo.py"]\n' if with_tool else ""
    (directory / "agent.toml").write_text(
        textwrap.dedent(f'name = "{name}"\ndescription = "Test agent {name}"\n{tools_line}{toml_extra}\n{model_table}'),
        encoding="utf-8",
    )
    (directory / "prompt.md").write_text(prompt, encoding="utf-8")
    if with_tool:
        (directory / "tools").mkdir()
        (directory / "tools" / "echo.py").write_text(ECHO_TOOL, encoding="utf-8")
    return directory


def write_runtime(base_dir: Path, body: str = "") -> Path:
    path = base_dir / "runtime.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [runtime]
            agents_dir = "./agents"
            {body}
            """
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """偽モデルを使うエージェント ``echoer`` を 1 つ持つプロジェクトディレクトリ。"""
    write_agent(tmp_path / "agents", "echoer")
    write_runtime(tmp_path, 'default_agent = "echoer"\n[session]\nbackend = "local"\ndir = "./sessions"')
    return tmp_path


@pytest.fixture
def runtime(project: Path) -> Runtime:
    return Runtime.load(project / "runtime.toml")
