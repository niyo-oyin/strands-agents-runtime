"""``runtime.toml`` とエージェントディレクトリを読み、検証とパス解決を済ませたオブジェクトにする。"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from strands_runtime.definitions.schema import BUILTIN_INJECTORS, AgentDefinition, RuntimeConfig
from strands_runtime.errors import DefinitionError

AGENT_FILE = "agent.toml"
DEFAULT_SKILLS_DIR = "skills"

# ``${VAR}`` と ``${VAR:-default}`` に一致する。
_ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def expand_env(value: Any, *, source: str) -> Any:
    """TOML ツリー内のすべての文字列で ``${VAR}`` / ``${VAR:-default}`` を環境変数に展開する。

    既定値のない未設定の変数はエラーにする。
    文字列をそのまま残すと、後で認証失敗やパス不正として現れ、原因が分かりにくくなるため。
    """
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            if name in os.environ:
                return os.environ[name]
            default = match.group("default")
            if default is not None:
                return default
            raise DefinitionError(f"environment variable {name!r} is not set", source=source)

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: expand_env(v, source=source) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, source=source) for v in value]
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    """TOML を読み込み、環境変数を展開して返す。読めない場合はファイル名付きのエラーにする。"""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise DefinitionError("file not found", source=str(path)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise DefinitionError(f"invalid TOML: {exc}", source=str(path)) from exc
    return cast(dict[str, Any], expand_env(data, source=str(path)))


def _describe(error: ValidationError) -> str:
    """pydantic の検証エラーを「キーのパス: 理由」を ``;`` で連ねた 1 行にする。"""
    lines = []
    for item in error.errors(include_url=False):
        loc = ".".join(str(part) for part in item["loc"])
        lines.append(f"{loc}: {item['msg']}" if loc else item["msg"])
    return "; ".join(lines)


def load_runtime_config(path: Path) -> RuntimeConfig:
    """``runtime.toml`` を読み込んで検証する。ファイルがなければ既定値を返す。

    デプロイごとの差分（公開するエージェント、セッションバックエンド、既定モデル）は別の ``runtime.toml`` として
    用意し、``STRANDS_RUNTIME_CONFIG`` でどれを読むかを選ぶ。
    ファイルに書いた ``${VAR}`` 以外に、環境変数で値を上書きする経路はない。
    """
    if not path.exists():
        return RuntimeConfig()
    data = _read_toml(path)
    try:
        return RuntimeConfig.model_validate(data)
    except ValidationError as exc:
        raise DefinitionError(_describe(exc), source=str(path)) from exc


def is_path_reference(value: str) -> bool:
    """ツールやプラグインの参照文字列が、モジュールではなくファイルを指しているかを判定する。

    Strands がツール文字列に使う規則と同じで、``./`` ``../`` ``/`` ``~`` で始まるか ``.py`` で終わればファイル、
    それ以外は ``pkg.module[:attr]`` とみなす。
    """
    head = value.split(":", 1)[0]
    return head.startswith(("./", "../", "/", "~")) or head.endswith(".py")


def _resolve_path_reference(value: str, base_dir: Path) -> str:
    """``path[:attr]`` のパス部分を ``base_dir`` 基準の絶対パスにする。``:attr`` はそのまま残す。"""
    path_part, sep, attr = value.partition(":")
    resolved = (base_dir / Path(path_part).expanduser()).resolve()
    return f"{resolved}{sep}{attr}"


@dataclass(frozen=True)
class ResolvedAgent:
    """:class:`AgentDefinition` に、ファイルシステムから集めた情報を加えたもの。factory はこれを受け取る。

    パスはすべて絶対パス。
    下のリスト項目は定義の相対参照（ツール・スキル・プラグイン）を解決したもので、
    定義の内側にある参照（``model.module``、``structured_output.model``、``context.inject``）は ``definition`` の中で絶対化済み。
    ``mcp_servers`` は常にマッピング（``mcp.json`` 指定の場合は読み込み済み）。
    """

    definition: AgentDefinition
    directory: Path
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    interventions: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.definition.name


def load_agent(directory: Path) -> ResolvedAgent:
    """エージェントディレクトリ（``agent.toml`` + ``prompt.md`` + 任意の ``skills/``、``mcp.json``）を 1 つ読み込む。"""
    directory = directory.resolve()
    agent_file = directory / AGENT_FILE
    data = _read_toml(agent_file)
    try:
        definition = AgentDefinition.model_validate(data)
    except ValidationError as exc:
        raise DefinitionError(_describe(exc), source=str(agent_file)) from exc

    # 名前はディレクトリ名と一致させる。レジストリがディレクトリ名で引き、ツール名にもなるため。
    if definition.name != directory.name:
        raise DefinitionError(
            f"name {definition.name!r} must match the directory name {directory.name!r}", source=str(agent_file)
        )

    prompt_path = directory / definition.prompt
    try:
        system_prompt = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DefinitionError(f"prompt file not found: {definition.prompt}", source=str(agent_file)) from exc
    if not system_prompt.strip():
        raise DefinitionError("prompt file is empty", source=str(prompt_path))

    # ツール文字列は Strands のローダーにそのまま渡すので、相対パスだけを絶対化しておく。
    tools = [_resolve_path_reference(t, directory) if is_path_reference(t) else t for t in definition.tools]
    for tool in tools:
        if is_path_reference(tool) and not Path(tool.split(":", 1)[0]).exists():
            raise DefinitionError(f"tool file not found: {tool}", source=str(agent_file))

    # skills 未指定なら ``./skills`` があれば使う。URL（SKILL.md の生 URL）はそのまま通す。
    if definition.skills is None:
        default_skills = directory / DEFAULT_SKILLS_DIR
        skills = [str(default_skills)] if default_skills.is_dir() else []
    else:
        skills = [
            s if s.startswith(("http://", "https://")) else str((directory / s).resolve()) for s in definition.skills
        ]
        for skill in skills:
            if not skill.startswith(("http://", "https://")) and not Path(skill).exists():
                raise DefinitionError(f"skills path not found: {skill}", source=str(agent_file))

    mcp_servers = _load_mcp_servers(definition.mcp_servers, directory, agent_file)

    # ``.cedar`` はポリシーファイルなので、``./`` が付いていなくてもパスとして扱う。
    interventions = [
        _resolve_path_reference(i, directory) if is_path_reference(i) or i.endswith(".cedar") else i
        for i in definition.interventions
    ]
    plugins = [_resolve_path_reference(p, directory) if is_path_reference(p) else p for p in definition.plugins]

    # 定義の内側にあるコード参照（model.module、structured_output.model、context.inject）も、同じ規則で絶対化してから差し替える。
    updates: dict[str, Any] = {}
    if definition.model.module and is_path_reference(definition.model.module):
        module = _resolve_path_reference(definition.model.module, directory)
        updates["model"] = definition.model.model_copy(update={"module": module})
    if definition.structured_output and is_path_reference(definition.structured_output.model):
        model_ref = _resolve_path_reference(definition.structured_output.model, directory)
        updates["structured_output"] = definition.structured_output.model_copy(update={"model": model_ref})
    inject = [
        _resolve_path_reference(i, directory) if i not in BUILTIN_INJECTORS and is_path_reference(i) else i
        for i in definition.context.inject
    ]
    if inject != definition.context.inject:
        updates["context"] = definition.context.model_copy(update={"inject": inject})
    if updates:
        definition = definition.model_copy(update=updates)

    return ResolvedAgent(
        definition=definition,
        directory=directory,
        system_prompt=system_prompt,
        tools=tools,
        skills=skills,
        mcp_servers=mcp_servers,
        interventions=interventions,
        plugins=plugins,
    )


def _load_mcp_servers(
    value: dict[str, dict[str, Any]] | str, directory: Path, agent_file: Path
) -> dict[str, dict[str, Any]]:
    """``mcp_servers`` をマッピングに正規化する。文字列ならファイルを読み、各サーバーの ``cwd`` を絶対化する。"""
    if isinstance(value, str):
        path = (directory / value).resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DefinitionError(f"mcp_servers file not found: {value}", source=str(agent_file)) from exc
        except json.JSONDecodeError as exc:
            raise DefinitionError(f"invalid JSON: {exc}", source=str(path)) from exc
        raw = expand_env(raw, source=str(path))
        # 標準の ``{"mcpServers": {...}}`` でも、サーバー定義だけのオブジェクトでも受け付ける。
        servers = raw.get("mcpServers", raw) if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            raise DefinitionError("expected an object with an 'mcpServers' key", source=str(path))
    else:
        servers = value

    resolved: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            raise DefinitionError(f"mcp_servers.{name} must be a table", source=str(agent_file))
        cfg = dict(cfg)
        # stdio サーバーの作業ディレクトリは、プロセスのカレントではなくエージェントディレクトリ基準にする。
        if "cwd" in cfg and isinstance(cfg["cwd"], str):
            cfg["cwd"] = str((directory / Path(cfg["cwd"]).expanduser()).resolve())
        resolved[name] = cfg
    return resolved
