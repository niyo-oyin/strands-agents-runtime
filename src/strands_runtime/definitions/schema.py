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


def _check_openai_connection(obj: ModelCandidate | ModelSpec, prefix: str) -> None:
    """接続先とキーの指定を、OpenAI の Responses / Chat Completions プロバイダーに限定する。"""
    for key in ("base_url", "api_key_env"):
        if getattr(obj, key) is not None and (
            obj.module is not None or (obj.id is not None and not obj.id.startswith(("openai/", "openai-chat/")))
        ):
            raise ValueError(f"{prefix}{key} is only valid with an 'openai/<model>' or 'openai-chat/<model>' id")


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
        _check_openai_connection(self, "fallbacks[].")
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
    """``openai`` / ``openai-chat`` の接続先。省略時は ``OPENAI_BASE_URL``。"""
    api_key_env: NonBlankStr | None = None
    """``openai`` / ``openai-chat`` のキーを保持する環境変数の名前。省略時は ``OPENAI_API_KEY``。

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
        _check_openai_connection(self, "model.")
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

    def candidates(self, *, default_id: str) -> list[ModelCandidate]:
        """主モデルを先頭に、継承を解決済みのフォールバック候補を並べて返す。"""
        # 既定モデルのプロバイダーを確定してから、接続設定の検証と継承を行う。
        primary = ModelCandidate(
            id=self.id or (default_id if self.module is None else None),
            module=self.module,
            effort=self.effort,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            config=self.config,
        )
        resolved = [primary]
        for entry in self.fallbacks:
            candidate = ModelCandidate(id=entry) if isinstance(entry, str) else entry
            # 同じ API プロバイダーの候補だけに接続先とキーを引き継ぐ。
            inherit_connection = any(
                (candidate.id or "").startswith(prefix) and (primary.id or "").startswith(prefix)
                for prefix in ("openai/", "openai-chat/")
            )
            resolved.append(
                ModelCandidate(
                    id=candidate.id,
                    module=candidate.module,
                    effort=candidate.effort if candidate.effort is not None else self.effort,
                    base_url=candidate.base_url
                    if candidate.base_url is not None or not inherit_connection
                    else self.base_url,
                    api_key_env=(
                        candidate.api_key_env
                        if candidate.api_key_env is not None or not inherit_connection
                        else self.api_key_env
                    ),
                    config=candidate.config if candidate.config is not None else self.config,
                )
            )
        return resolved


BUILTIN_INJECTORS = frozenset({"datetime", "actor"})
"""``[context] inject`` の組み込み項目。これ以外はコード参照として扱う。"""


class ContextSpec(_Strict):
    """``[context]`` テーブル。``Agent(context_manager=...)`` と、各ターンへの一時的なコンテキスト注入。"""

    manager: ContextManagerName | dict[str, Any] = "off"
    """Strands の文脈管理。既定の ``off`` は SDK 標準のスライディングウィンドウだけを使う。
    ``auto`` は大きなツール結果の切り詰め・退避と使用率 85% での要約を行い、全エージェントに
    ``retrieve_context`` ツールを足す。動作が定義から読み取りにくくなるので、必要な定義だけで有効にする。"""
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


class NodeSpec(_Strict):
    """Graph / Swarm のノード 1 つ。``agent`` は同じ ``agents_dir`` の定義名、``id`` は省略時 ``agent`` と同じ。"""

    agent: str = Field(pattern=AGENT_NAME_PATTERN.pattern)
    id: str | None = Field(default=None, pattern=AGENT_NAME_PATTERN.pattern)
    wait_for: Literal["any", "all"] = "any"
    """``all`` は、入ってくるエッジの元ノードがすべて完了してから実行する（合流）。Strands の既定は ``any``（OR）。"""

    @property
    def node_id(self) -> str:
        return self.id or self.agent


class EdgeSpec(_Strict):
    """Graph のエッジ。``condition`` は ``(GraphState, *, invocation_state) -> bool`` へのコード参照。"""

    from_: str = Field(alias="from", pattern=AGENT_NAME_PATTERN.pattern)
    to: str = Field(pattern=AGENT_NAME_PATTERN.pattern)
    condition: NonBlankStr | None = None


class GraphLimitsSpec(_Strict):
    """Graph の ``[limits]``。Strands の ``max_node_executions`` / ``execution_timeout`` / ``node_timeout``。"""

    node_executions: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    node_timeout_seconds: float | None = Field(default=None, gt=0)


class SwarmLimitsSpec(_Strict):
    """Swarm の ``[limits]``。既定値は Strands の ``Swarm`` と同じ。"""

    handoffs: int = Field(default=20, gt=0)
    """``max_handoffs`` と ``max_iterations`` の両方に渡す（Strands ではどちらも実行回数と比較される）。"""
    timeout_seconds: float = Field(default=900, gt=0)
    node_timeout_seconds: float = Field(default=300, gt=0)
    repetitive_handoff_window: int = Field(default=0, ge=0)
    repetitive_handoff_min_unique_agents: int = Field(default=0, ge=0)


class _MultiAgentDefinition(_Strict):
    """Graph / Swarm 定義の共通部分。ノードは定義名で参照し、ノード自身の設定は参照先の ``agent.toml`` に置く。"""

    name: str = Field(pattern=AGENT_NAME_PATTERN.pattern)
    description: str = ""
    nodes: list[str] | list[NodeSpec] = Field(min_length=1)
    """文字列（定義名 = ノード ID）の配列か、``[[nodes]]`` テーブルの配列。TOML の規則で混在はできない。"""
    session: SessionSpec = Field(default_factory=SessionSpec)
    """オーケストレーターの状態（進んだノード、中断状態）の保存先。HITL の再開に使う。"""

    @property
    def node_specs(self) -> list[NodeSpec]:
        return [NodeSpec(agent=n) if isinstance(n, str) else n for n in self.nodes]

    @property
    def node_ids(self) -> list[str]:
        return [n.node_id for n in self.node_specs]

    @property
    def dependencies(self) -> list[str]:
        """この定義が参照する他の定義名（重複なし、順序維持）。"""
        return list(dict.fromkeys(n.agent for n in self.node_specs))

    @model_validator(mode="after")
    def _nodes_consistent(self) -> _MultiAgentDefinition:
        ids = self.node_ids
        if len(set(ids)) != len(ids):
            raise ValueError("node ids must be unique")
        if self.name in self.dependencies:
            raise ValueError("a graph or swarm cannot contain itself as a node")
        if self.session.backend == "agentcore_memory":
            raise ValueError("graph / swarm cannot use session backend 'agentcore_memory' (no generic storage)")
        return self


class GraphDefinition(_MultiAgentDefinition):
    """``kind = "graph"``。決定的なワークフロー（``strands.multiagent.GraphBuilder``）。"""

    kind: Literal["graph"]
    entry: list[str] = Field(default_factory=list)
    """開始ノード。省略時は入ってくるエッジがないノード。"""
    edges: list[EdgeSpec] = Field(default_factory=list)
    output: str | None = None
    """``result.text`` にするノード。省略時は最後に完了したノード。"""
    reset_on_revisit: bool = False
    limits: GraphLimitsSpec = Field(default_factory=GraphLimitsSpec)

    @model_validator(mode="after")
    def _edges_consistent(self) -> GraphDefinition:
        ids = set(self.node_ids)
        for ref in [*self.entry, *([self.output] if self.output else [])]:
            if ref not in ids:
                raise ValueError(f"unknown node {ref!r} in entry/output")
        seen: set[tuple[str, str]] = set()
        wait_all = {n.node_id for n in self.node_specs if n.wait_for == "all"}
        for edge in self.edges:
            if edge.from_ not in ids or edge.to not in ids:
                raise ValueError(f"edge {edge.from_!r} -> {edge.to!r} refers to an unknown node")
            if (edge.from_, edge.to) in seen:
                raise ValueError(f"duplicate edge {edge.from_!r} -> {edge.to!r}")
            seen.add((edge.from_, edge.to))
            if edge.to in wait_all and edge.condition is not None:
                raise ValueError(f"edges into {edge.to!r} (wait_for = 'all') cannot have a condition")
        # 入ってくるエッジのないノードがなければ Strands は開始点を決められない（循環だけのグラフ）。
        if not self.entry and self.edges and not (ids - {e.to for e in self.edges}):
            raise ValueError("entry is required when every node has an incoming edge")
        return self


class SwarmDefinition(_MultiAgentDefinition):
    """``kind = "swarm"``。自律的なハンドオフ（``strands.multiagent.Swarm``）。ノードは ``kind = "agent"`` の定義に限る。"""

    kind: Literal["swarm"]
    entry: str | None = None
    limits: SwarmLimitsSpec = Field(default_factory=SwarmLimitsSpec)

    @model_validator(mode="after")
    def _entry_exists(self) -> SwarmDefinition:
        if self.entry is not None and self.entry not in self.node_ids:
            raise ValueError(f"unknown entry node {self.entry!r}")
        return self


Definition = AgentDefinition | GraphDefinition | SwarmDefinition
MultiAgentDefinition = GraphDefinition | SwarmDefinition
DEFINITION_KINDS: dict[str, type[AgentDefinition] | type[GraphDefinition] | type[SwarmDefinition]] = {
    "agent": AgentDefinition,
    "graph": GraphDefinition,
    "swarm": SwarmDefinition,
}


def parse_definition(data: dict[str, Any]) -> Definition:
    """``kind``（省略時 ``agent``）で定義の型を選んで検証する。"""
    kind = data.get("kind", "agent")
    model = DEFINITION_KINDS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise ValueError(f"kind must be one of {sorted(DEFINITION_KINDS)}, got {kind!r}")
    return model.model_validate(data)


def dependencies_of(definition: Definition) -> list[str]:
    """定義が参照する他の定義名（単体は ``subagents``、Graph / Swarm はノード）。"""
    if isinstance(definition, AgentDefinition):
        return list(definition.subagents)
    return definition.dependencies


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


class JobsSection(_Strict):
    """``runtime.toml`` の ``[jobs]``。ジョブのジャーナルを置く場所と、ジョブ 1 件の上限時間。"""

    storage: Literal["inherit", "local", "s3"] = "inherit"
    """``inherit`` は ``[session]`` が ``local`` / ``s3`` ならその Storage を使う。
    それ以外（``agentcore_memory`` / ``none``）ではジャーナルを置けないので、明示しない限りジョブは無効になる。"""
    dir: NonBlankStr | None = None
    bucket: NonBlankStr | None = None
    prefix: str = ""
    region: NonBlankStr | None = None
    max_duration_seconds: float = Field(default=28800, gt=0)
    """基盤の上限がないローカルや ECS でジョブが際限なく走らないための安全弁。既定は 8 時間。"""

    @model_validator(mode="after")
    def _required_keys(self) -> JobsSection:
        if self.storage == "s3" and not self.bucket:
            raise ValueError("[jobs] storage = 's3' requires 'bucket'")
        return self

    def as_session_spec(self) -> SessionSpec:
        """``local`` / ``s3`` の Storage は ``[session]`` と同じ作り方なので、同じ構築経路に乗せる。"""
        return SessionSpec(
            backend=self.storage, dir=self.dir, bucket=self.bucket, prefix=self.prefix, region=self.region
        )


class RuntimeConfig(_Strict):
    """1 つの ``runtime.toml`` を検証した結果。"""

    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    model: ModelDefaults = Field(default_factory=ModelDefaults)
    session: SessionSpec = Field(default_factory=lambda: SessionSpec(backend="local"))
    jobs: JobsSection = Field(default_factory=JobsSection)
    server: ServerSection = Field(default_factory=ServerSection)

    @model_validator(mode="after")
    def _session_not_inherit(self) -> RuntimeConfig:
        # ``inherit`` は agent.toml が runtime.toml を参照するための値なので、参照先である runtime.toml には書けない。
        if self.session.backend == "inherit":
            raise ValueError("runtime.toml [session].backend cannot be 'inherit'")
        return self
