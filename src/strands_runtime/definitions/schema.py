"""``agent.toml`` と ``runtime.toml`` の pydantic スキーマ。

ここで検証するのは TOML の「形」だけ。
プロンプト本文の読み込みや相対パスの解決、``mcp.json`` の読み込みは :mod:`strands_runtime.definitions.loader` が、
定義から ``strands.Agent`` を組み立てるのは :mod:`strands_runtime.factory` が担当する。
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Effort = Literal["auto", "off", "minimal", "low", "medium", "high", "xhigh", "max"]
ContextManagerName = Literal["auto", "agentic", "off"]
SessionBackend = Literal["inherit", "local", "s3", "agentcore_memory", "none"]

AGENT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
"""エージェント名はディレクトリ名・``subagents`` のツール名・``agent_id`` を兼ねるため、英小文字と数字に限定する。"""

NonBlankStr = Annotated[str, Field(min_length=1, pattern=r"\S")]

AGENT_TOP_LEVEL_KEYS = frozenset(
    {"name", "description", "kind", "prompt", "tools", "skills", "subagents", "mcp_servers", "interventions", "plugins"}
)
"""``agent.toml`` のトップレベルキー。
TOML ではテーブル見出し（``[model]`` など）より後に書いたキーがそのテーブルに属してしまうため、
``[model.config]`` に紛れ込んだトップレベルキーを検出するのに使う。"""


class _Strict(BaseModel):
    """未知のキーを拒否し、TOML の綴り間違いを起動時に止める。"""

    model_config = ConfigDict(extra="forbid", strict=True)


def _check_openai_chat_only(obj: ModelCandidate | ModelSpec, prefix: str) -> None:
    """``base_url`` と ``api_key_env`` は ``openai-chat/`` プロバイダー専用の設定であることを確認する。"""
    for key in ("base_url", "api_key_env"):
        if getattr(obj, key) is not None and obj.id is not None and not obj.id.startswith("openai-chat/"):
            raise ValueError(f"{prefix}{key} is only valid with an 'openai-chat/<model>' id")


class ModelCandidate(_Strict):
    """``[model] fallbacks`` の 1 要素。未指定の項目は主モデルの値を引き継ぐ。"""

    id: NonBlankStr | None = None
    module: NonBlankStr | None = None
    effort: Effort | None = None
    base_url: NonBlankStr | None = None
    api_key_env: NonBlankStr | None = None
    config: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _one_of(self) -> ModelCandidate:
        if (self.id is None) == (self.module is None):
            raise ValueError("a fallback needs exactly one of id or module")
        _check_openai_chat_only(self, "fallbacks[].")
        return self


class CacheSpec(_Strict):
    """``[model.cache]`` テーブル。``strands.models.CacheConfig``（プロンプトキャッシュ）にそのまま渡す。キー名は SDK のもの。"""

    strategy: Literal["auto", "anthropic"] = "auto"
    ttl: NonBlankStr | None = None
    system_prompt_ttl: bool | NonBlankStr = True
    cache_key: NonBlankStr | bool | None = None
    tools_ttl: bool | NonBlankStr | None = None


class ModelSpec(_Strict):
    """``[model]`` テーブル。

    ``id`` は ``"provider/model-id"`` 形式で、``strands_harness.models.resolve_model`` が解決する。
    ``module`` はプロバイダー表で表せないモデル（``Model`` または ``ModelRouter`` を返す関数）へのモジュール参照。
    ``config`` はそのまま ``Model.update_config`` に渡す。
    ``fallbacks`` を書くと、呼び出し失敗時に候補を切り替える ``ModelRouter`` になる。
    """

    id: NonBlankStr | None = None
    module: NonBlankStr | None = None
    effort: Effort = "auto"
    caching: bool = True
    base_url: NonBlankStr | None = None
    """``openai-chat`` プロバイダー（OpenAI 互換 Chat Completions）の接続先。省略時は ``OPENAI_BASE_URL``。"""
    api_key_env: NonBlankStr | None = None
    """``openai-chat`` のキーを保持する環境変数の名前。省略時は ``OPENAI_API_KEY``。

    値ではなく変数名だけを TOML に書く。
    エージェントごとに別の変数を指せるので、公式 OpenAI と互換サーバーを同じランタイムで同時に使える。
    """
    config: dict[str, Any] = Field(default_factory=dict)
    fallbacks: list[NonBlankStr | ModelCandidate] = Field(default_factory=list)
    """前の候補が失敗したときに順に試すモデル（``ModelRouter`` + ``FallbackStrategy``）。
    文字列は ``id`` の指定で、effort・caching・接続先・キー・config を主モデルから引き継ぐ。"""
    cache: CacheSpec | None = None
    """プロンプトキャッシュの調整。``caching = true`` が前提で、主モデルと全候補に適用する。"""

    @model_validator(mode="after")
    def _consistent(self) -> ModelSpec:
        if self.id is not None and self.module is not None:
            raise ValueError("model.id and model.module are mutually exclusive")
        _check_openai_chat_only(self, "model.")
        if self.cache is not None and not self.caching:
            raise ValueError("model.cache needs caching = true")
        # ``[model.config]`` の後に ``tools = [...]`` を書くと TOML 上は ``model.config.tools`` になる。
        # 黙って無視すると「ツールが登録されない」という分かりにくい事象になるので、助言付きで拒否する。
        swallowed = sorted(set(self.config) & AGENT_TOP_LEVEL_KEYS)
        if swallowed:
            raise ValueError(
                f"model.config contains agent-level keys {swallowed}; in TOML, top-level keys such as "
                "tools/skills/subagents must be written before the first table header like [model]"
            )
        return self

    def candidates(self) -> list[ModelCandidate]:
        """主モデルを先頭に、継承を解決済みのフォールバック候補を並べて返す。"""
        # ``id`` も ``module`` もない場合は runtime.toml の既定モデルを使う。番兵 ``__default__`` を factory 側が解釈する。
        primary = (
            ModelCandidate(
                id=self.id,
                module=self.module,
                effort=self.effort,
                base_url=self.base_url,
                api_key_env=self.api_key_env,
                config=self.config,
            )
            if (self.id or self.module)
            else ModelCandidate(id="__default__", effort=self.effort, config=self.config)
        )
        resolved = [primary]
        for entry in self.fallbacks:
            candidate = ModelCandidate(id=entry) if isinstance(entry, str) else entry
            # 接続先とキーは、主も候補も ``openai-chat/`` のときだけ引き継ぐ（他プロバイダーでは意味を持たない）。
            inherit_openai_chat = (candidate.id or "").startswith("openai-chat/") and (self.id or "").startswith(
                "openai-chat/"
            )
            resolved.append(
                ModelCandidate(
                    id=candidate.id,
                    module=candidate.module,
                    effort=candidate.effort if candidate.effort is not None else self.effort,
                    base_url=candidate.base_url
                    if candidate.base_url is not None or not inherit_openai_chat
                    else self.base_url,
                    api_key_env=(
                        candidate.api_key_env
                        if candidate.api_key_env is not None or not inherit_openai_chat
                        else self.api_key_env
                    ),
                    config=candidate.config if candidate.config is not None else self.config,
                )
            )
        return resolved


class ContextSpec(_Strict):
    """``[context]`` テーブル。``Agent(context_manager=...)`` と、各ターンへの一時的なコンテキスト注入。"""

    manager: ContextManagerName | dict[str, Any] = "auto"
    inject: list[NonBlankStr] = Field(default_factory=list)
    """ユーザーターンごとに注入する文（会話履歴には残らない）。
    組み込みは ``"datetime"`` と ``"actor"``。それ以外は ``(InjectionContext) -> str | None`` を返す呼び出し可能オブジェクトへの
    モジュール参照で、SDK の ``ContextInjector`` に渡す。"""
    timezone: NonBlankStr = "UTC"
    """``datetime`` 注入が使う IANA タイムゾーン名。"""


class RetrievalSpec(_Strict):
    """``agentcore_memory`` バックエンドで、名前空間ごとに長期記憶を取り込む設定。"""

    top_k: int = Field(default=10, gt=0, le=1000)
    relevance_score: float = Field(default=0.2, ge=0.0, le=1.0)
    strategy_id: str | None = None


class SessionSpec(_Strict):
    """``[session]`` テーブル。``runtime.toml`` では既定、``agent.toml`` では上書きとして使う。"""

    backend: SessionBackend = "inherit"
    # local 用
    dir: NonBlankStr | None = None
    # s3 用
    bucket: NonBlankStr | None = None
    prefix: str = ""
    region: NonBlankStr | None = None
    # agentcore_memory 用
    memory_id: NonBlankStr | None = None
    retrieval: dict[str, RetrievalSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _required_keys(self) -> SessionSpec:
        if self.backend == "s3" and not self.bucket:
            raise ValueError("session.bucket is required when backend = 's3'")
        if self.backend == "agentcore_memory" and not self.memory_id:
            raise ValueError("session.memory_id is required when backend = 'agentcore_memory'")
        return self


class StructuredOutputSpec(_Strict):
    """``[structured_output]`` テーブル。pydantic モデルクラスへのモジュール参照。"""

    model: NonBlankStr


class LimitsSpec(_Strict):
    """``[limits]`` テーブル。毎回の呼び出しに適用する ``strands.types.agent.Limits``。"""

    turns: int | None = Field(default=None, gt=0)
    output_tokens: int | None = Field(default=None, gt=0)
    total_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    """1 回の呼び出しの実時間の上限。SDK ではなくランタイムが監視し、超過時はエージェントに停止を要求する。"""

    def to_limits(self) -> dict[str, int]:
        """SDK の ``Limits`` に渡す辞書（``timeout_seconds`` はランタイム側で扱うので除く）。"""
        return {k: v for k, v in self.model_dump(exclude={"timeout_seconds"}).items() if v is not None}


class AgentDefinition(_Strict):
    """1 つの ``agent.toml`` を検証した結果。"""

    name: str = Field(pattern=AGENT_NAME_PATTERN.pattern)
    description: str = ""
    kind: Literal["agent"] = "agent"
    prompt: NonBlankStr = "prompt.md"
    model: ModelSpec = Field(default_factory=ModelSpec)
    tools: list[NonBlankStr] = Field(default_factory=list)
    skills: list[NonBlankStr] | None = None
    """``None`` は「``./skills`` があれば使う」。空リストはスキルを明示的に無効化する。"""
    subagents: list[str] = Field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] | NonBlankStr = Field(default_factory=dict)
    """``mcpServers`` 形式のマッピング（サーバー名 → MCPServerConfig）か、その JSON ファイルへのパス。"""
    context: ContextSpec = Field(default_factory=ContextSpec)
    session: SessionSpec = Field(default_factory=SessionSpec)
    structured_output: StructuredOutputSpec | None = None
    limits: LimitsSpec | None = None
    interventions: list[NonBlankStr] = Field(default_factory=list)
    plugins: list[NonBlankStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _subagent_names(self) -> AgentDefinition:
        for sub in self.subagents:
            if not AGENT_NAME_PATTERN.match(sub):
                raise ValueError(f"invalid subagent name {sub!r}")
            if sub == self.name:
                raise ValueError("an agent cannot list itself in subagents")
        if len(set(self.subagents)) != len(self.subagents):
            raise ValueError("subagents must be unique")
        return self


class RuntimeSection(_Strict):
    """``[runtime]`` テーブル。"""

    agents_dir: NonBlankStr = "./agents"
    default_agent: NonBlankStr | None = None
    enabled_agents: list[NonBlankStr] | None = None
    """``None`` なら ``agents_dir`` 配下の全エージェントを公開する。"""
    max_subagent_depth: int = Field(default=3, ge=1, le=10)


class ModelDefaults(_Strict):
    """``[model]`` テーブル（runtime.toml 側）。``[model]`` を持たないエージェントが使う既定。"""

    default_id: NonBlankStr = "bedrock/global.anthropic.claude-opus-5"


class ServerSection(_Strict):
    """``[server]`` テーブル。"""

    host: NonBlankStr | None = None
    """``None`` なら ``BedrockAgentCoreApp.run`` に任せる（コンテナ内は 0.0.0.0、それ以外は 127.0.0.1）。"""
    port: int = Field(default=8080, ge=1, le=65535)
    log_file: NonBlankStr | None = None
    """プロセスのログ（ランタイム・Strands・uvicorn・警告）をこのファイルにも書く。runtime.toml からの相対パス。"""


class RuntimeConfig(_Strict):
    """1 つの ``runtime.toml`` を検証した結果。"""

    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    model: ModelDefaults = Field(default_factory=ModelDefaults)
    session: SessionSpec = Field(default_factory=lambda: SessionSpec(backend="local"))
    server: ServerSection = Field(default_factory=ServerSection)

    @model_validator(mode="after")
    def _session_not_inherit(self) -> RuntimeConfig:
        # ``inherit`` は agent.toml が runtime.toml を参照するための値なので、参照先である runtime.toml には書けない。
        if self.session.backend == "inherit":
            raise ValueError("runtime.toml [session].backend cannot be 'inherit'")
        return self
