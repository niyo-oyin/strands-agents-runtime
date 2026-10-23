"""``infra/deployments.toml``。1 つのイメージからどの AgentCore Runtime を作るかの宣言。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# AgentRuntimeName の制約（英字で始まり、英数字と _、48 文字まで）。
RUNTIME_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StackSpec(_Strict):
    name: str = Field(min_length=1)


class RuntimeSpec(_Strict):
    """``[[runtime]]`` 1 つ分。"""

    name: str = Field(pattern=RUNTIME_NAME.pattern)
    config: str = Field(min_length=1)
    """この Runtime が読む ``runtime.toml`` のリポジトリ相対パス。イメージ内では ``/app/<config>`` にある。"""
    platform_version: Literal["V1", "V2"] = "V2"
    idle_timeout_seconds: int = Field(default=900, ge=60, le=28800)
    max_lifetime_seconds: int = Field(default=28800, ge=60, le=28800)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    """環境変数名 → Secrets Manager のシークレット名。値はデプロイ時に CloudFormation が解決する。"""

    @model_validator(mode="after")
    def _idle_within_lifetime(self) -> RuntimeSpec:
        if self.idle_timeout_seconds > self.max_lifetime_seconds:
            raise ValueError("idle_timeout_seconds must not exceed max_lifetime_seconds")
        if set(self.env) & set(self.secrets):
            raise ValueError("an environment variable cannot be both in env and secrets")
        return self


class Deployments(_Strict):
    stack: StackSpec
    runtime: list[RuntimeSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> Deployments:
        names = [r.name for r in self.runtime]
        if len(set(names)) != len(names):
            raise ValueError("runtime names must be unique")
        return self


def load_deployments(path: Path) -> Deployments:
    with path.open("rb") as fh:
        return Deployments.model_validate(tomllib.load(fh))


def session_backend(repo_root: Path, runtime_config: str) -> str:
    """Runtime の ``runtime.toml`` から ``[session] backend`` だけを読む。

    ランタイム本体のローダーは ``${VAR}`` を環境変数に展開するが、合成（synth）時にはシークレットが揃っていないので、
    展開せずに TOML を直接読む。
    """
    with (repo_root / runtime_config).open("rb") as fh:
        data = tomllib.load(fh)
    backend = data.get("session", {}).get("backend", "local")
    return str(backend)
