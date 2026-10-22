# Strands Agents Runtime 設計文書

- 状態: フェーズ 1 実装済み（2026-10-22）。フェーズ 2 以降は計画
- 作成日: 2026-10-22
- 対象読者: 本ランタイムの開発者、エージェント定義を書く利用者、クライアント（Lambda 等）の実装者

---

## 1. 目的とスコープ

### 1.1 目的

[Strands Agents SDK](https://strandsagents.com/) を土台に、
**エージェントをコードではなく設定ファイルで宣言し、同一の成果物をローカル・Docker・Amazon Bedrock AgentCore Runtime で動かせる** 汎用エージェントランタイムを提供する。

ランタイムが解決する課題は次の 3 つである。

1. **宣言的なエージェント定義**
   ベンダー、モデル ID、推論パラメータ、ツール、MCP サーバー、スキル、プロンプト、セッション方式を TOML（設定）と Markdown（プロンプト）で記述する。
   Python コードを書くのはカスタムツールを追加するときだけに限定する。
2. **実行環境の可搬性**
   ローカル開発、Docker、AgentCore Runtime の 3 環境で同じ HTTP 契約・同じエージェント定義・同じコンテナイメージを使う。
3. **シンプルな呼び出し**
   クライアント（ローカルの別プログラム、Lambda 等）はエージェント名とプロンプトを含む JSON ペイロードを送るだけで、ストリーミングまたは一括で結果を受け取れる。

### 1.2 スコープ外（ただし拡張点として設計で考慮する）

- グラフ・スワームなどの複雑なマルチエージェントワークフローの実装（第 3 フェーズで検討。[§8](#8-オーケストレーションとエージェント連携) に拡張点を定義）
- Web UI、チャット UI
- Strands SDK が提供しない独自のモデルプロバイダー実装
- エージェント定義のホットリロード（AgentCore Runtime のバージョンは不変であり、再デプロイが正規の更新手段）

### 1.3 設計原則

プロジェクトの `CLAUDE.md` の原則に従う。特に本ランタイムでは次を重視する。

- **SDK に寄せる**: Strands SDK と `bedrock-agentcore` SDK が既に持つ機能（モデル解決、MCP 設定の読み込み、スキル、セッション永続化、OTel 計装、HTTP サーバー）は再実装せず、そのまま使う。
  本ランタイムの固有コードは「TOML/Markdown の読み込み」「定義から `Agent` を組み立てる工場」「ペイロード契約」「クライアント」に限定する。
- **後方互換レイヤーを持たない**: 非推奨化が進む `strands-agents-tools` パッケージや Python 版 starter toolkit には依存しない。
- **1 つの成果物**: ローカルと AgentCore で別のエントリポイントを持たない。`BedrockAgentCoreApp` をそのままローカルサーバーとしても使う。

---

## 2. 前提調査の要点（2026-10-22 時点）

設計判断に直接影響する事実を記す。
詳細な API 名は各節で参照する。

### 2.1 Strands Agents SDK（`strands-agents` 1.61.1、Python 3.10 以上）

- リポジトリは [`strands-agents/harness-sdk`](https://github.com/strands-agents/harness-sdk) モノレポに移行した。
  Python SDK（`strands-agents`）に加え、**`strands-harness`**（0.3.1）というパッケージが同梱されており、`"provider/name"` 形式のモデル文字列解決、推論強度（effort）のプロバイダー別マッピング、JSON ベースの可搬設定を提供する。
- **Agent Skills はネイティブ対応**（`strands.AgentSkills`、`SKILL.md` + YAML frontmatter の [agentskills.io](https://agentskills.io) 仕様）。
- **`strands-agents-tools` は非推奨化が進行中**。多くのツールが SDK 同梱の `strands.vended_tools`（`shell`、`file_editor`、`http_request`、`web_fetch`、`python_repl` 等）と MCP サーバーへ移行し、README は「将来アーカイブする」と明記している。
- **MCP の宣言的設定は SDK が持つ**: `MCPClient.load_servers(config)` が標準の `mcpServers` JSON（`command`/`args`/`env`/`url`/`headers`/`transport`/`auth`/`tool_filters`/`prefix`/`disabled` 等）を読み、`${VAR}` 展開も行う。
- **実験的な JSON エージェント設定** `strands.experimental.config_to_agent` が存在するが、`name`/`model`/`prompt`/`tools` のみで TOML・Markdown には非対応。TOML/Markdown を読むものは SDK・ハーネス・公式ツールのいずれにも存在しない。
- `Agent` は多数のオプションを持つ（`model`、`tools`、`system_prompt`、`session_manager`、`context_manager`、`plugins`、`hooks`、`interventions`、`structured_output_model`、`trace_attributes`、`tool_executor` 等）。`Agent(tools=[...])` は文字列（モジュールパス、`pkg.mod:func`、ファイルパス）、`ToolProvider`（`MCPClient` を含む）、`Agent` インスタンス（自動で `as_tool()`）を受け付ける。
- セッション永続化は `strands.storage`（`LocalFileStorage`/`S3Storage`/`InMemoryStorage`）上の `SnapshotSessionManager` が最新の方式。AgentCore Memory 連携は `bedrock-agentcore[strands-agents]` の `AgentCoreMemorySessionManager`。
- `strands-agents-builder` はアーカイブ済み（2026-06-03）。

### 2.2 Amazon Bedrock AgentCore

- **Python 版 `bedrock-agentcore-starter-toolkit`（`agentcore configure/launch`）は非推奨**。後継は Node.js 版 CLI [`@aws/agentcore`](https://github.com/aws/agentcore-cli)（`create/dev/deploy/invoke`、内部は AWS CDK）。IaC は CloudFormation `AWS::BedrockAgentCore::Runtime`、CDK `aws_cdk.aws_bedrockagentcore`（stable）、Terraform が揃っている。
- **Runtime の HTTP 契約**: ポート 8080、`POST /invocations`（JSON 入力、JSON または SSE 出力）、`GET /ping`（`{"status": "Healthy" | "HealthyBusy"}`）、ホスト `0.0.0.0`、**ARM64**。セッション ID はヘッダー `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`（33 文字以上）で渡され、セッション単位に microVM が割り当てられる（アイドル 15 分・最長 8 時間がデフォルト）。
- **Direct Code Deploy**（zip を S3 に置く、Docker 不要）が追加され、CLI のデフォルトになった。コンテナと比べて新規セッション生成レートが高く（25/s 対 1.6/s）、更新が速い。
- プロトコルは HTTP / MCP / A2A / AG-UI の 4 種。A2A は `bedrock_agentcore.runtime.serve_a2a` と Strands の `StrandsA2AExecutor` で提供できる。
- `bedrock-agentcore` SDK（1.24.2）の `BedrockAgentCoreApp` は Starlette ベースで、`@app.entrypoint` に非同期ジェネレーターを渡すと SSE（`data: {...}\n\n`）でストリーミングする。`RequestContext` から `session_id` と許可済みリクエストヘッダーが取れる。
- 観測性は OpenTelemetry。コンテナ起動コマンドを `opentelemetry-instrument` で包み、`aws-opentelemetry-distro` を依存に含めれば Strands の組み込みスパンが CloudWatch GenAI Observability に流れる。

---

## 3. 全体アーキテクチャ

### 3.1 レイヤー構成

```
┌─────────────────────────────────────────────────────────────────────┐
│ クライアント                                                        │
│   ローカルプログラム / Lambda / 他エージェント                       │
│   strands_runtime.client.{HttpClient, AgentCoreClient}              │
└───────────────┬─────────────────────────────────────────────────────┘
                │ JSON ペイロード（agent, prompt, session_id, ...）
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ サーバー層  strands_runtime.server                                   │
│   BedrockAgentCoreApp  POST /invocations  GET /ping                  │
│   ペイロード検証 → セッション ID 解決 → Invoker 呼び出し → SSE/JSON  │
└───────────────┬─────────────────────────────────────────────────────┘
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 実行層  strands_runtime.invoke                                       │
│   Invoker: Agent 構築（Factory）→ stream_async → イベント正規化       │
└──────┬──────────────────────────────┬───────────────────────────────┘
       ▼                              ▼
┌──────────────────────┐   ┌──────────────────────────────────────────┐
│ 定義層               │   │ 工場  strands_runtime.factory              │
│ strands_runtime.     │   │   AgentDefinition → strands.Agent          │
│ definitions          │──▶│   モデル解決 / ツール読込 / MCP / Skills   │
│   agent.toml         │   │   セッション / 構造化出力 / サブエージェント│
│   prompt.md          │   │   プロセス内キャッシュ（MCPClient 等）     │
│   Registry           │   └──────────────────────────────────────────┘
└──────────────────────┘                     │
                                             ▼
                    Strands Agents SDK / strands-harness / bedrock-agentcore
                    （モデルプロバイダー、ツールループ、Storage、OTel）
```

### 3.2 パッケージ構成

| 配布名 | Python パッケージ | CLI | 備考 |
|---|---|---|---|
| `strands-agents-runtime` | `strands_runtime` | `strands-runtime` | 名称は [§14](#14-未決事項) で確認 |

```
strands_runtime/
  definitions/      # TOML/Markdown の読み込みと pydantic モデル（AgentDefinition, RuntimeConfig）
    schema.py
    loader.py
    registry.py
  factory/          # AgentDefinition → strands.Agent
    agent_factory.py
    models.py       # resolve_model のラッパー、model.config の適用
    tools.py        # ツール文字列・MCP・スキルの解決、プロセス内キャッシュ
    sessions.py     # session.backend → SessionManager
  invoke/
    invoker.py      # 1 リクエストの実行とイベント正規化
    events.py       # ランタイムイベントスキーマ（pydantic）
  server/
    app.py          # BedrockAgentCoreApp の組み立て、/invocations エントリポイント
    __main__.py     # python -m strands_runtime.server
  client/
    http.py         # ローカル / Docker 向け HTTP クライアント
    agentcore.py    # boto3 invoke_agent_runtime ラッパー
    events.py       # SSE パーサー（client と server で共有するイベント型を再利用）
  cli.py            # validate / list / run / serve
```

### 3.3 実行時のデータフロー

1. 起動時に `runtime.toml`（環境変数 `STRANDS_RUNTIME_CONFIG` で場所を変更可、既定 `./runtime.toml`）を読み、`Registry` が `runtime.agents_dir` 配下を走査して各 `agent.toml` と `prompt.md` を検証し `AgentDefinition` にする。
   定義エラーは起動失敗にする（AgentCore V2 では最初の `/ping` が健全になった時点でスナップショットされるため、起動時検証が重要）。
2. リクエスト到着時、サーバー層がペイロードを検証し、セッション ID を解決する（[§6.3](#63-セッション-id-の解決)）。
3. `Invoker` が `AgentFactory` に定義名とセッション情報を渡し、**リクエストごとに `strands.Agent` を構築**する。
   Strands の `Agent` は会話履歴を保持するステートフルなオブジェクトであり、セッション状態の復元は `SessionManager` が担うため、毎回構築するのが SDK の想定パターンである。
   MCP 接続やツールモジュールなどセッションに依存しない重い資源はプロセス内でキャッシュする。
4. `agent.stream_async(prompt, invocation_state=...)` のイベントを正規化し、SSE として返す。非ストリーミング指定なら最終結果だけを JSON で返す。

---

## 4. エージェント定義フォーマット

### 4.1 ディレクトリ構造

```
agents/
  support-bot/
    agent.toml          # 必須: エージェント定義
    prompt.md           # 既定のシステムプロンプト（agent.toml の prompt で変更可）
    tools/              # 任意: @tool で定義した Python ツール
      lookup_order.py
    skills/             # 任意: Agent Skills（<skill-name>/SKILL.md）
      refund-policy/
        SKILL.md
        references/
    mcp.json            # 任意: mcpServers 形式（agent.toml 内のテーブルでも記述可）
  researcher/
    agent.toml
    prompt.md
runtime.toml            # ランタイム全体の設定
```

1 ディレクトリ = 1 エージェント。ディレクトリ名がエージェント名であり、`agent.toml` の `name` と一致しなければならない（スキルの `name` とディレクトリ名の一致規則と同じ発想）。

### 4.2 `agent.toml`

```toml
# トップレベルのキーは最初のテーブル（[model] など）より前に書く（TOML の規則）
name = "support-bot"
description = "注文状況の照会と返金ポリシーの案内を行うサポートエージェント"
prompt = "prompt.md"                     # 省略時は prompt.md

tools = [
  "strands.vended_tools:http_request",   # SDK 同梱ツール
  "./tools/lookup_order.py",             # ディレクトリ内の @tool
  "mycompany.tools.crm",                 # インストール済みパッケージのモジュール
  "mycompany.tools.crm:escalate",        # モジュール内の特定関数
]

skills = ["./skills"]                    # 省略時: skills/ があれば自動で有効

subagents = ["researcher"]               # 他のエージェント定義名。as_tool() でツール化

interventions = ["./policy.cedar"]       # 任意: Cedar ポリシー、またはモジュール参照
plugins = ["mycompany.plugins:AuditLog"] # 任意: strands.plugins.Plugin のモジュール参照

[model]
id = "bedrock/global.anthropic.claude-opus-5"   # "provider/model-id"
effort = "medium"                               # auto|off|minimal|low|medium|high|xhigh|max
caching = true                                  # プロンプトキャッシュ

[model.config]                           # プロバイダー固有の設定（Model.update_config に透過）
max_tokens = 8192
temperature = 0.2
# guardrail_id = "abc123"               # Bedrock の場合
# guardrail_version = "1"

[mcp_servers.docs]                       # キー名がサーバー名。値は Strands MCPServerConfig と同じ
url = "https://mcp.example.com/mcp"
headers = { Authorization = "Bearer ${DOCS_MCP_TOKEN}" }
tool_filters = { allowed = ["search_docs", "get_page"] }

[mcp_servers.fs]
command = "uvx"
args = ["mcp-server-filesystem", "/data"]
disabled = false

# mcp_servers = "mcp.json"               # テーブルの代わりにファイルを指すこともできる

[context]
manager = "auto"                         # auto|agentic|off（Strands の context_manager）
inject = ["datetime", "actor"]           # 任意: 各ユーザーターンに注入する一時コンテキスト（履歴には残らない）
timezone = "Asia/Tokyo"                  # datetime 注入のタイムゾーン

[session]                                # 省略時は runtime.toml の [session] を継承
backend = "inherit"

[structured_output]                      # 任意: 最終回答を pydantic モデルで返す
model = "mycompany.schemas:SupportAnswer"

[limits]                                 # 任意: 1 呼び出しの上限
turns = 30                               # Strands Limits（turns / output_tokens / total_tokens）
output_tokens = 32000
timeout_seconds = 120                    # ランタイムが強制する実時間の上限
```

#### スキーマ（主要キー）

| キー | 型 | 既定 | Strands への対応 |
|---|---|---|---|
| `name` | str | 必須 | `Agent(name=)`、`agent_id` |
| `description` | str | `""` | `Agent(description=)`。サブエージェント化時のツール説明にも使う |
| `prompt` | path | `prompt.md` | 読み込んだ本文を `Agent(system_prompt=)` |
| `model.id` | `"provider/id"` | `runtime.toml` の既定 | `strands_harness.models.resolve_model` |
| `model.effort` | enum | `"auto"` | 同上（プロバイダー別の thinking/reasoning 設定に変換） |
| `model.caching` | bool | `true` | 同上（`CacheConfig`） |
| `model.config` | table | `{}` | `model.update_config(**config)`。キーはプロバイダーの `*Config` TypedDict |
| `model.base_url` | str | なし | `openai-chat/` 専用。接続先（省略時 `OPENAI_BASE_URL`） |
| `model.api_key_env` | str | なし | `openai-chat/` 専用。キーを保持する環境変数名（省略時 `OPENAI_API_KEY`） |
| `model.module` | `"pkg.mod:factory"` | なし | 7 プロバイダー以外や `ModelRouter` を使う逃げ道。`id` と排他 |
| `tools` | list[str] | `[]` | `Agent(tools=[...])` に文字列のまま渡す（SDK のローダーを使う）。相対パスは定義ディレクトリ基準で絶対化 |
| `skills` | list[str] | `["./skills"]`（存在時） | `AgentSkills(skills=[...])` を `plugins` に追加 |
| `subagents` | list[str] | `[]` | 定義名を解決して構築した `Agent` を `tools` に追加 |
| `mcp_servers` | table or path | `{}` | `MCPClient.load_servers(dict)` → `ToolProvider` として `tools` に追加 |
| `context.manager` | enum or table | `"auto"` | `Agent(context_manager=)` |
| `context.inject` | list[str] | `[]` | SDK の `ContextInjector`。組み込みは `datetime`（`context.timezone` で時刻帯指定）と `actor`（`actor_id`）。それ以外はモジュール参照で `(InjectionContext) -> str` を指す |
| `limits.timeout_seconds` | float | なし | ランタイムの監視タイマー。超過時は `cancel_signal` で穏やかに停止させ `stop_reason = "timeout"` の `result` を返す。猶予（15 秒）内に止まらなければ `error`（`Timeout`） |
| `session` | table | `inherit` | [§6](#6-セッションとメモリ) |
| `structured_output.model` | module ref | なし | `Agent(structured_output_model=)` |
| `limits` | table | なし | `agent.stream_async(limits=)` |
| `interventions` | list[str] | `[]` | `.cedar` は `CedarAuthorization`、それ以外はモジュール参照 |
| `plugins` | list[module ref] | `[]` | `Agent(plugins=)` |

モジュール参照は Strands のツール文字列と同じ `pkg.module` / `pkg.module:attr` / 相対ファイルパスの 3 形式に統一する。

### 4.3 `prompt.md`

- ファイル全体をシステムプロンプトとして渡す。Markdown は LLM への指示として自然であり、レビューしやすい。
- 最初の段階ではテンプレート機能を持たない。
  日付や実行環境などの動的コンテキストは `strands.vended_plugins.context_injector` のような SDK のプラグインで注入する方針とし、独自のテンプレートエンジンは導入しない。
- 後から必要になった場合の拡張点として、frontmatter（YAML）でメタデータを持てる余地を残す（現時点では未使用）。

### 4.4 `runtime.toml`

```toml
[runtime]
agents_dir = "./agents"
default_agent = "support-bot"            # payload.agent 省略時に使う
# enabled_agents = ["support-bot"]       # 省略時は agents_dir 配下の全定義を公開。
                                         # エージェント単位で別デプロイするときに絞り込む（§11.4）

[model]
default_id = "bedrock/global.anthropic.claude-opus-5"

[session]
backend = "local"                        # local|s3|agentcore_memory|none
dir = "./.runtime/sessions"              # local のとき

# [session]
# backend = "s3"
# bucket = "my-agent-sessions"
# prefix = "prod/"

# [session]
# backend = "agentcore_memory"
# memory_id = "${AGENTCORE_MEMORY_ID}"
# retrieval = { "/preferences/{actorId}/" = { top_k = 5, relevance_score = 0.7 } }

[server]
port = 8080
log_file = "./.runtime/server.log"       # 任意: 端末に加えてファイルにも出す（警告も含む）

[telemetry]
# Strands/ADOT は標準の OTEL_* 環境変数で制御する。ここには設定を置かない。
```

環境ごとの差分（セッションバックエンド、既定モデル、公開するエージェント）は `runtime.toml` を環境別に用意するか、値に `${VAR}` を書いて環境変数で与える。
`enabled_agents` は `subagents` で参照される定義も含めて読み込む必要があるため、Registry は「公開する定義」と「読み込む定義」を区別し、後者は前者から到達可能な定義すべてとする。
`enabled_agents` は環境変数 `STRANDS_RUNTIME_ENABLED_AGENTS`（カンマ区切り）で上書きできる。TOML のリストは `${VAR}` 展開の対象外であり、「同じイメージから Runtime を複数作る」構成では公開範囲が環境差分になるため、この 1 項目だけ環境変数での上書きを持つ。

**環境変数と設定ファイルの線引き**: 設定ファイルは「何を動かすか」（版管理され、イメージに焼かれ、環境間で同一）、環境変数は「どこで・誰の資格で動かすか」とシークレット（デプロイ基盤が注入）。TOML は `${VAR}` で変数名だけを参照し、値を持たない。観測性の送信先は OpenTelemetry 標準の環境変数に委ね、TOML では扱わない。ランタイムが参照する環境変数の一覧は `.env.example` に保つ。
`${VAR}` 展開は `runtime.toml` と `agent.toml` の文字列値すべてに適用する（MCP の値は SDK 側でも展開されるが、二重展開は無害）。

### 4.5 検証

- TOML は標準ライブラリ `tomllib` で読み、pydantic v2 モデルで検証する（`extra="forbid"`、未知キーはエラー）。
- `strands-runtime validate` コマンドで、全定義の読み込み、モジュール参照の解決、MCP 設定の静的検証、スキルの `SKILL.md` 検証（`AgentSkills(..., strict=True)` で読み込む）、サブエージェント参照の循環検出を行う。
  CI での実行を想定する。

---

## 5. Agent の組み立て（Factory）

### 5.1 基本方針

`AgentFactory.build(definition, session) -> strands.Agent` は `strands.Agent` を直接構築する。
`strands_harness.create_harness` は使わない。理由は [ADR-2](#adr-2-create_harness-ではなく-agent-を直接構築し-モデル解決だけハーネスを使う) に記す。

```python
# 概念コード（実装ではない）
def build(self, d: AgentDefinition, session: SessionContext) -> Agent:
    model = resolve_model(d.model.id, default=self.cfg.model.default_id,
                          effort=d.model.effort, caching=d.model.caching)
    if d.model.config:
        model.update_config(**d.model.config)

    tools: list = [*d.tools]                              # 文字列のまま SDK に委ねる
    tools += self._mcp_clients(d)                         # プロセス内キャッシュ済み MCPClient
    tools += [self.build(self.registry[name], session.child()).as_tool()
              for name in d.subagents]

    plugins = [*self._plugins(d)]
    if d.skills:
        plugins.append(AgentSkills(skills=d.skills))

    return Agent(
        name=d.name, agent_id=d.name, description=d.description,
        model=model, system_prompt=d.prompt_text, tools=tools, plugins=plugins,
        context_manager=d.context.manager,
        session_manager=self._session_manager(d, session),
        structured_output_model=d.structured_output_model,
        interventions=self._interventions(d),
        callback_handler=None,                             # 出力は stream_async で扱う
        trace_attributes={"session.id": session.id, "user.id": session.actor_id,
                          "agent.name": d.name},
    )
```

### 5.2 モデル解決

- `"provider/id"` 文字列は `strands_harness.models.resolve_model` に委ねる。
  対応プロバイダーは `bedrock`、`bedrock-mantle`、`anthropic`、`openai`（Responses API）、`google`、`ollama`、`litellm`。
  `effort` はプロバイダーとモデルファミリーに対して検証され、Bedrock の Claude なら `additional_request_fields.thinking`、Anthropic なら `params.thinking` などに変換される。
- 例外として **`openai-chat/<model>`** は本ランタイム固有のプロバイダーで、`strands.models.OpenAIModel`（Chat Completions）を直接構築する。
  ハーネスの `openai/` は Responses API を使うため、Chat Completions しか実装しない OpenAI 互換サーバー（vLLM、Ollama、LiteLLM プロキシ等）に接続できない。
  エンドポイントは `model.base_url` または環境変数 `OPENAI_BASE_URL`、キーは `model.api_key_env` で名指しした環境変数（既定 `OPENAI_API_KEY`）。
  `base_url` と `api_key_env` をエージェントごとに持てるため、OpenAI 公式と互換サーバーを 1 つのランタイムで同時に使える。`effort` は明示されたときだけ `reasoning_effort` として送る。
- `model.config` は `Model.update_config(**config)` でプロバイダー固有設定を上書きする。
  キー名は Strands 各プロバイダーの `*Config` TypedDict に従う（例: Bedrock は `max_tokens`、`temperature`、`guardrail_id`、`service_tier`。Anthropic は `max_tokens`、`params`）。
  ランタイムはキーを解釈しない。
- `model.module` は `Model` または `ModelRouter` を返す関数への参照。フォールバック戦略（`FallbackStrategy`）などはこの経路で行う。
- プロバイダーの追加依存（`strands-agents[anthropic]` 等）は本ランタイムの extras として同名で公開する（`strands-agents-runtime[anthropic]`）。

### 5.3 ツール

- `tools` の文字列は `Agent(tools=[...])` にそのまま渡す。
  Strands の `ToolRegistry` がモジュールの `@tool` 関数、`TOOL_SPEC` モジュール、ファイルパスを解決する。
  相対パス（`./tools/...`）だけは定義ディレクトリ基準の絶対パスに変換してから渡す。
- `Agent(load_tools_from_directory=True)` はカレントディレクトリの `./tools/` しか見ず、ホットリロード（watchdog）を伴うため使わない。
- SDK 同梱ツールは `strands.vended_tools:<name>` で参照する。
  サンドボックス系（`shell`、`file_editor`、`python_repl`）は `Agent(sandbox=)` の既定（`PosixShellSandbox`）で動く。AgentCore 上ではセッション単位の microVM 内で実行されるので隔離は満たされるが、ファイルシステムは一時的である。

### 5.4 MCP

- `mcp_servers` テーブル（または `mcp.json`）を `MCPClient.load_servers(config)` に渡し、返った `MCPClient` 群を `tools` に加える。`MCPClient` は `ToolProvider` なのでツール一覧は初回利用時に取得される。
- **プロセス内キャッシュ**: `MCPClient` は定義名 + サーバー名でキャッシュし、同一プロセス内の複数リクエストで再利用する。
  AgentCore ではプロセス = microVM = セッションなので、セッション中の stdio サーバーの再起動を避けられる。
  ローカルでも同様に効く。
- 接続失敗時の挙動は `continue_on_error` で定義側が選ぶ（既定は失敗）。
- AgentCore Gateway に接続する場合は `url` に Gateway の `/mcp` を、`headers.Authorization` にトークンを指定する。トークン取得は [§10](#10-セキュリティ) の方式に従う。

### 5.5 Skills

- `AgentSkills(skills=[...])` を `plugins` に追加する。SDK がシステムプロンプトに `<available_skills>` を注入し、`skills` ツールで有効化する。
- スキルが `scripts/` を持つ場合、エージェントがそれを実行するには `strands.vended_tools:shell` 等が `tools` に必要である。
  `validate` コマンドは「スクリプトを含むスキルがあるのに shell/file 系ツールがない」場合に警告を出す。
- 有効化済みスキルは `agent.state` に保存されるため、セッション永続化の対象になる。

### 5.6 サブエージェント

- `subagents = ["researcher"]` は `Registry` から定義を取り出し、同じ Factory で構築した `Agent` を `as_tool()` でツール化して親に渡す。
  ツール名は定義名、説明は `description` になる。
- サブエージェントは**セッションマネージャーを持たない**（呼び出しごとに独立して動く）。
  Strands の `as_tool(preserve_context=False)` はセッションマネージャー付きの `Agent` を拒否するため、永続化は親だけが担う。
  サブエージェントのトレース属性には `"{parent_session}--{name}"` を `session.id` として付け、親と区別できるようにする。
- 循環参照は `validate` で検出し、実行時にも深さ上限（既定 3）で防ぐ。

---

## 6. セッションとメモリ

### 6.1 セッション（短期記憶 = 会話履歴）

`session.backend` で `SessionManager` を選ぶ。

| backend | 実装 | 用途 |
|---|---|---|
| `local` | `SnapshotSessionManager(session_id, storage=LocalFileStorage(dir))` | ローカル開発 |
| `s3` | `SnapshotSessionManager(session_id, storage=S3Storage(bucket, prefix=...))` | Docker/AgentCore で AgentCore Memory を使わない場合 |
| `agentcore_memory` | `AgentCoreMemorySessionManager(AgentCoreMemoryConfig(memory_id, session_id, actor_id, retrieval_config))` | AgentCore 上で短期記憶に加えて長期記憶（戦略）を使う場合 |
| `none` | `session_manager=None` | ステートレスなワンショット呼び出し |

- 定義側の `[session]` は `backend = "inherit"`（既定）か、上記のいずれかで上書きできる。
- `agentcore_memory` は `actor_id` が必須なので、ペイロードの `actor_id`（省略時は `"anonymous"`）を使う。
- AgentCore の microVM はセッションに紐づくが、アイドルタイムアウトで破棄されるため、**プロセス内メモリをセッション状態の正とは見なさない**。常に `SessionManager` で永続化する。

### 6.2 長期記憶

- `agentcore_memory` バックエンドでは AgentCore Memory の戦略（semantic / summary / user preference）を `retrieval` 設定で取り込む。
- それ以外の環境では Strands の `MemoryManager`（`FileMemoryStore`、`BedrockKnowledgeBaseStore`）を `[memory]` テーブルで有効化できるようにする。第 3 フェーズ。

### 6.3 セッション ID の解決

優先順位は次のとおり。

1. HTTP ヘッダー `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`（AgentCore が付与。ローカルでもクライアントが付けてよい）
2. ペイロードの `session_id`
3. どちらもなければ UUID を生成し、結果イベントで返す（次回の継続に使える）

AgentCore はセッション ID を 33 文字以上に制約するため、クライアントライブラリは UUIDv4（36 文字）を生成する。

---

## 7. 呼び出しプロトコル

### 7.1 リクエスト（`POST /invocations`）

```json
{
  "agent": "support-bot",
  "prompt": "注文 #1234 の状況を教えて",
  "session_id": "0d1b2b3a-....",
  "actor_id": "user-42",
  "stream": true,
  "invocation_state": { "tenant": "acme" }
}
```

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `agent` | str | `runtime.default_agent` | エージェント定義名 |
| `prompt` | str または content block の配列 | 必須 | Strands の `AgentInput` と同じ。画像などのマルチモーダル入力は配列形式で渡す |
| `session_id` | str | なし | [§6.3](#63-セッション-id-の解決) |
| `actor_id` | str | `"anonymous"` | 利用者識別子。メモリの名前空間とトレースの `user.id` に使う |
| `stream` | bool | `true` | `false` で最終結果だけを JSON で返す |
| `invocation_state` | object | `{}` | ツールに渡す任意のコンテキスト（`ToolContext.invocation_state`）。保存されない |
| `include_reasoning` | bool | `false` | モデルの推論差分を `reasoning` イベントとして流す（推論モデルでは量が多いため既定は無効） |
| `tool_result_detail` | `"preview"` or `"full"` | `"preview"` | `"full"` で `tool_result` に結果の content block 全体（`content`）を含める |

`prompt` に content block の配列を渡す場合、`image` / `document` / `video` の `source.bytes` は base64 文字列で送る。ランタイムが bytes に復号して Strands に渡す。
中断（`interrupt`）した会話は、同じ `session_id` で `prompt = [{"interruptResponse": {"interruptId": "...", "response": ...}}]` を送ると再開する。

ペイロードの `system_prompt` や `model` の上書きは**許可しない**。
エージェントの振る舞いは定義ファイルで確定させ、クライアントはそれを選ぶだけとする。

### 7.2 ストリーミングレスポンス（SSE）

`BedrockAgentCoreApp` の仕組みに従い、`Content-Type: text/event-stream` で `data: <JSON>\n\n` を送る。
Strands の生イベント（`stream_async` の dict）は量が多く、シリアライズ不能な値も含むため、**ランタイム独自のイベントスキーマに正規化**する。

| `type` | 内容 | 生成元 |
|---|---|---|
| `start` | `agent`, `session_id` | 実行開始時 |
| `text` | `text`（差分） | `data` イベント |
| `reasoning` | `text`（差分） | `reasoningText` イベント |
| `tool_use` | `tool_use_id`, `name`, `input` | `message`（assistant、toolUse ブロック） |
| `tool_result` | `tool_use_id`, `status`, `preview`（500 文字まで）、`tool_result_detail = "full"` のときは `content` も | `message`（user、toolResult ブロック） |
| `interrupt` | `interrupts`（HITL 再開用）| `stop_reason == "interrupt"` |
| `result` | `stop_reason`, `text`, `structured_output`, `metrics`, `session_id` | `result` イベント |
| `error` | `error_type`, `message` | 例外（Strands が `force_stop` の後に再送出する例外を 1 件の `error` にまとめる）。結果もエラーもなく終わった場合は `NoResult` |

`result.metrics` は `EventLoopMetrics` から入出力トークン数とツール呼び出し回数を抜き出し、`latency_ms` はランタイムが計測した呼び出し全体の実時間とする（プロバイダーによって SDK が報告しないため）。
イベント型は pydantic モデルで定義し、サーバーとクライアントで同じモジュールを使う。

`result.stop_reason` は Strands の値（`end_turn`、`max_tokens`、`interrupt`、`limit_turns` など）をそのまま使い、次の 2 つだけランタイムが置き換える。

| 値 | 意味 |
|---|---|
| `structured_output` | `structured_output` に構造化回答が入っている（SDK ではツール呼び出し扱いで `tool_use` になる） |
| `timeout` | `limits.timeout_seconds` 超過で停止した（SDK では `cancelled`） |

### 7.3 非ストリーミングレスポンス

`stream: false` のとき、`result` イベントと同じ構造の JSON を `application/json` で返す。
AgentCore の同期リクエストは 15 分で打ち切られるため、長時間タスクはストリーミングを使う（ストリーミングは最長 60 分）。

### 7.4 ヘルスチェック（`GET /ping`）

`BedrockAgentCoreApp` の既定実装を使い、`{"status": "Healthy"}` を返す。
長時間のバックグラウンド処理を導入する場合は `@app.async_task` で `HealthyBusy` を自動化する（現時点では不要）。

### 7.5 エラー

- 同じ `session_id` の呼び出しが同一プロセスで実行中: HTTP 409（AgentCore の `RetryableConflictException` と同じ扱い）。履歴の競合を避けるため直列化ではなく拒否する
- クライアントが途中で切断した場合: ランタイムは `cancel_signal` を立て、エージェントは次の安全な地点で停止して履歴を保存する
- 定義名不明、ペイロード不正: HTTP 400 と `{"error": ..., "details": ...}`
- 実行時例外: ストリーミング中なら `error` イベント、非ストリーミングなら HTTP 500
- AgentCore はコンテナの 4xx/5xx を `424 RuntimeClientError` としてクライアントに返すため、クライアントライブラリはこれを例外に変換する。

---

## 8. オーケストレーションとエージェント連携

第 1・2 フェーズで実装するのは **Agents-as-Tools**（`subagents`）のみとする。
Strands が提供する他の方式に対しては、定義フォーマットに次の拡張点を予約する。

| 方式 | Strands の実装 | 定義フォーマットの拡張点 | 備考 |
|---|---|---|---|
| Agents-as-Tools | `Agent.as_tool()` | `subagents = [...]`（実装済みとする） | 最も単純。親が LLM でルーティング |
| モデル駆動サブエージェント | `strands.vended_tools.make_subagent` | `[subagent_tool]` テーブル（presets / max_depth） | 親が動的にサブエージェントを生成 |
| Graph | `strands.multiagent.GraphBuilder` | `kind = "graph"` の定義 + `[[nodes]]` `[[edges]]` | 決定的なワークフロー |
| Swarm | `strands.multiagent.Swarm` / `make_swarm` | `kind = "swarm"` の定義 + `nodes = [...]` | 自律的なハンドオフ |
| A2A（他ランタイム呼び出し） | `strands.vended_tools.make_a2a_client` | `[a2a_clients]` の `allowed_endpoints` | 別デプロイのエージェントをツール化 |
| A2A（公開側） | `bedrock_agentcore.runtime.serve_a2a` + `StrandsA2AExecutor` | `runtime.toml` の `protocol = "A2A"` | AgentCore の A2A プロトコルモードでデプロイ |
| 同一ランタイム内の別エージェント呼び出し | 本ランタイムの `HttpClient`/`AgentCoreClient` をツール化 | — | 自前実装より A2A を優先する |

`agent.toml` に `kind`（既定 `"agent"`）を予約し、Graph/Swarm は別 `kind` として同じ Registry と呼び出し契約で扱う。
`Invoker` は `AgentBase`/`MultiAgentBase` のどちらも `stream_async` を持つことに依存し、イベント正規化で `multiagent_*` イベントを扱う。

---

## 9. 観測性

- Strands は `Agent` の実行、モデル呼び出し、ツール呼び出しを OpenTelemetry スパンとして自動計装する。本ランタイムはエクスポーターの配線だけを行う。
- **AgentCore**: コンテナの `CMD` を `opentelemetry-instrument` で包み、`aws-opentelemetry-distro` を依存に含める。
  Runtime が ADOT 用の環境変数を注入し、トレース・ログ・メトリクスが CloudWatch GenAI Observability に届く。
  Strands は既存のグローバル TracerProvider を自動で使うため、ランタイム側で `StrandsTelemetry` を呼ばない。
- **ローカル / Docker**: `OTEL_TRACES_EXPORTER=console|otlp` が設定されているときだけ `StrandsTelemetry().setup_otlp_exporter()` / `setup_console_exporter()` を一度呼ぶ。
  未設定なら何もしない（localhost:4318 への無駄な接続を避ける）。
  ただし、グローバルな TracerProvider が既に設定されている場合（`opentelemetry-instrument` 配下で起動した場合）は、環境変数の有無にかかわらず何もしない。AgentCore 上では ADOT が `OTEL_TRACES_EXPORTER=otlp` を注入するため、このガードがないとエクスポーターが二重になる。
- `Agent(trace_attributes=...)` に `session.id`、`user.id`、`agent.name` を付与する。AgentCore のセッションヘッダーは ADOT が自動で `session.id` に紐づける。
- 構造化ログは標準 `logging` で出し、JSON フォーマッタは導入しない（ADOT のログ連携と CloudWatch で十分）。
- `serve` はランタイム・Strands・uvicorn・Python の警告をすべてルートロガーに集約し、`[server] log_file` が設定されていれば同じ内容をファイルにも書く。端末を見られない作業者（別の運用者、エージェント、CI）がサーバーの観測結果を読めるようにするためのもの。
- OpenAI 互換モデルは SDK がコンテキスト長を知らないため、`[model.config] context_window_limit` を定義側で指定する（未指定なら SDK が 200,000 と仮定して警告する）。

---

## 10. セキュリティ

- **エージェント定義ディレクトリは信頼済みコード**として扱う。`tools/*.py`、モジュール参照、MCP の `command` は任意コード実行に等しい。定義はリポジトリ管理し、イメージに焼き込む。ペイロードから定義を変更する経路は設けない。
- **シークレット**は環境変数で与え、TOML には `${VAR}` のみ書く。
  AgentCore では環境変数サイズに上限（V1 で 4 KB 合計、V2 はさらに小さい）があるため、大きなシークレットは AgentCore Identity の `@requires_api_key` / `@requires_access_token` または Secrets Manager で取得する。
  MCP の `headers.Authorization` に動的トークンが必要な場合は、`MCPClient(auth=MCPClientCredentials(...))`（OAuth client credentials）を `auth` キーで設定する。
- **インバウンド認証**は AgentCore の責務（SigV4 または JWT）。ランタイム自体は認証を実装しない。ローカルでは認証なしで `127.0.0.1` に束縛する。
- **ツール実行の制御**は Strands の `interventions`（Cedar ポリシー、`InterventionHandler`）に委ねる。本ランタイムは `.cedar` ファイルとモジュール参照を渡すだけにする。
- **サプライチェーン**: 依存は `uv` と `pyproject.toml` の `[[tool.uv.index]]` で `https://pypi.flatt.tech/simple/` を既定ソースにする。コンテナビルドでも同じ設定を使う。

---

## 11. デプロイ

### 11.1 ローカル

```bash
uv run strands-runtime serve            # BedrockAgentCoreApp を 127.0.0.1:8080 で起動
uv run strands-runtime run support-bot "こんにちは"   # サーバーを立てずに 1 回実行（開発用）
curl -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
     -d '{"agent":"support-bot","prompt":"こんにちは"}'
```

`serve` は `BedrockAgentCoreApp.run()` を呼ぶだけであり、AgentCore 上と同じコードパスを通る。

### 11.2 Docker

- ベースイメージ `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`、`linux/arm64` でビルドする（AgentCore の要件に合わせ、ローカルも ARM64 Mac で同じイメージを使う）。
- `uv sync --frozen --no-dev` で依存を固定し、非 root ユーザーで実行する。
- `ENV DOCKER_CONTAINER=1` を置き、`app.run()` が `0.0.0.0` に束縛するようにする。
- `CMD ["opentelemetry-instrument", "strands-runtime", "serve", "--port", "8080"]`。`--port` が `runtime.toml` の値より優先されるため、開発用に別ポートを書いていてもコンテナ内は AgentCore 契約の 8080 で待ち受ける。
- モデルプロバイダーの extras はビルド引数 `EXTRAS`（既定 `agentcore-otel openai`）で選ぶ。
- `agents/` と `runtime.toml` をイメージに含める。環境差分は環境変数で与える（ローカル実行は `--env-file .env`）。
- ローカルでコンテナを動かすときは OTel コレクターがないため `OTEL_SDK_DISABLED=true` を渡す。イメージには焼き込まない（AgentCore 上では Runtime が注入する ADOT 設定を生かす）。
- 確認済み（2026-10-22）: Apple Silicon 上で linux/arm64 イメージをビルドし、OpenRouter の実モデルでツール呼び出し・セッション継続・非ストリーミング応答が動作。イメージは約 945 MB。

### 11.3 AgentCore Runtime

**IaC は CDK（Python、`aws_cdk.aws_bedrockagentcore`）を正とし、`infra/` に置く。**
理由は [ADR-4](#adr-4-デプロイは-cdk-を正とし-python-版-starter-toolkit-には依存しない) に記す。

- `Runtime`（`AgentRuntimeArtifact.fromAsset("..")` で ARM64 イメージをビルドして ECR に配置）、`protocolConfiguration=HTTP`、`environmentVariables`、`lifecycleConfiguration`（アイドル・最長時間）、`requestHeaderConfiguration`（必要なら）、実行ロール。
- セッションバックエンドが `agentcore_memory` のときは `Memory` リソースと戦略を同じスタックで作り、`AGENTCORE_MEMORY_ID` を環境変数で渡す。`s3` のときはバケットと `s3:GetObject/PutObject/ListBucket` 権限を付与する。
- 観測性のために CloudWatch Transaction Search を有効化する（アカウント単位の 1 回限りの設定。スタック外の手順として README に記す）。
- **Direct Code Deploy**（`AgentRuntimeArtifact.fromCodeAsset(...)`、`entrypoint=["opentelemetry-instrument", "main.py"]`）はコールドスタートとセッション生成レートで有利なため、依存が純 Python のみで 250 MB に収まる構成では選択肢にする。
  `uv pip install --python-platform aarch64-manylinux2014 --only-binary=:all:` で ARM64 向けにビルドする。第 2 フェーズで両方式を検証し、既定を決める。
- Runtime V2（スナップショット方式、コールドスタート約 2 秒）は CDK/CloudFormation が `platformVersion` に未対応のため、現時点では採用しない。対応後に切り替える。
- `@aws/agentcore` CLI（Node）は開発者のローカル検証（`agentcore dev`、`agentcore invoke`）に使ってもよいが、本番の IaC には使わない。

### 11.4 複数エージェントのデプロイ単位

1 つのイメージに全定義を含めたうえで、Runtime リソースをどう切るかは運用要件で選ぶ。どちらも同じ成果物と同じ CDK コンストラクトで実現し、差は `runtime.toml`（または環境変数）の `enabled_agents` だけにする。

| 方式 | 構成 | 向くケース |
|---|---|---|
| 単一 Runtime | 1 つの Runtime に全エージェント。クライアントは `agent` で選ぶ | エージェント数が少ない、権限・ライフサイクル設定が共通、まとめて更新したい |
| エージェント単位の Runtime | 同じイメージから Runtime を複数作り、各 Runtime の `enabled_agents` で 1 体（または 1 グループ）に絞る | IAM 権限やアイドル時間を分けたい、個別にバージョン管理・ロールバックしたい、負荷や課金を分離したい |

CDK スタックは Runtime の一覧（名前、`enabled_agents`、環境変数、ライフサイクル設定）をパラメーター化し、単一 Runtime は要素 1 つの特殊形として扱う。
エージェント単位に分けた場合、Runtime 間の連携は `subagents`（同じプロセス内で構築）ではなく A2A または `AgentCoreClient` 経由になる（[§8](#8-オーケストレーションとエージェント連携)）。

### 11.5 環境ごとの差分まとめ

| 項目 | ローカル | Docker | AgentCore |
|---|---|---|---|
| 起動 | `strands-runtime serve` | 同じイメージ | 同じイメージ（または code zip） |
| バインド | `127.0.0.1:8080` | `0.0.0.0:8080` | `0.0.0.0:8080` |
| セッション | `local` | `s3` または `local`（検証用） | `s3` または `agentcore_memory` |
| 認証 | なし | なし | SigV4 / JWT |
| セッション ID | ペイロードまたはヘッダー | 同左 | ヘッダー（AgentCore が付与） |
| トレース | `OTEL_TRACES_EXPORTER` で任意 | 同左 | ADOT 自動 |
| AWS 認証情報 | ローカルプロファイル | 環境変数 / マウント | 実行ロール |

---

## 12. クライアント

Python パッケージ `strands_runtime.client` を提供し、Lambda などから利用する。

```python
from strands_runtime.client import AgentCoreClient, HttpClient

client = AgentCoreClient(runtime_arn=os.environ["AGENT_RUNTIME_ARN"])   # boto3 bedrock-agentcore
# client = HttpClient("http://localhost:8080")                           # ローカル / Docker

for event in client.stream(agent="support-bot", prompt="...", session_id=sid, actor_id="user-42"):
    if event.type == "text":
        print(event.text, end="")

result = client.invoke(agent="support-bot", prompt="...", session_id=sid)   # stream=false
print(result.text, result.structured_output)
```

- 2 つのクライアントは同じインターフェース（`stream()` / `invoke()`）と同じイベント型を持つ。
- `AgentCoreClient` は `invoke_agent_runtime(agentRuntimeArn, runtimeSessionId, payload, qualifier="DEFAULT")` を呼び、`text/event-stream` なら `iter_lines()` で `data:` 行を解析する。`424 RuntimeClientError` や `409 RetryableConflictException` は型付き例外にする（後者は boto3 がリトライする）。
- `qualifier` でエンドポイント（バージョン固定）を選べるようにする。
- クライアントは依存を最小にする（`boto3` と `pydantic` のみ）。Lambda でのパッケージサイズを抑えるため、extras `strands-agents-runtime[client]` を用意するか、別配布にするかは [§14](#14-未決事項) で確認する。

---

## 13. 開発方針

### 13.1 技術スタック

| 項目 | 選択 |
|---|---|
| Python | 3.13（AgentCore の `PYTHON_3_13`、CDK の enum と一致） |
| パッケージ管理 | uv、`pyproject.toml` で flatt レジストリを既定に設定 |
| 主要依存 | `strands-agents[otel]`、`strands-harness`（モデル解決のみ）、`bedrock-agentcore[strands-agents]`、`pydantic>=2`、`aws-opentelemetry-distro` |
| extras | `anthropic`、`openai`、`gemini`、`ollama`、`litellm`、`a2a`、`cedar`、`client` |
| CLI | `argparse`（標準ライブラリ。コマンドは 4 つなので追加依存を避ける） |
| 設定読み込み | `tomllib`（標準ライブラリ） |
| Lint / 型 | ruff、mypy（strict） |
| テスト | pytest、pytest-asyncio |

### 13.2 テスト方針

外部から観測可能な振る舞いを検証する。

- **定義ローダー**: 妥当な定義の読み込み、未知キー・型不一致・名前不一致・循環参照の拒否、`${VAR}` 展開。
- **Factory**: 偽の `Model`（`strands.models.Model` のテスト用実装）を注入し、ツール・MCP・スキル・サブエージェント・セッションが `Agent` に正しく配線されることを `Agent` の公開属性（`tool_names`、`system_prompt`、`session_manager` の型）で確認する。
- **サーバー契約**: Starlette の `TestClient` で `/invocations` と `/ping` を叩き、SSE のイベント列と非ストリーミング JSON を検証する。モデルは偽物。
- **クライアント**: SSE パーサーと boto3 レスポンスの解析を、記録済みレスポンスで検証する。
- **結合テスト**（任意実行、`tests_integ/`）: 実モデル（Bedrock）と Docker を使い、ローカルコンテナに対して 1 往復する。

### 13.3 ロードマップ

| フェーズ | 内容 | 完了条件 |
|---|---|---|
| 1（実装済み） | 定義フォーマット、ローダー、Factory（モデル・ツール・MCP・Skills・local/s3/agentcore_memory セッション・subagents・structured_output・limits・interventions・コンテキスト注入）、Invoker（イベント正規化、キャンセル、タイムアウト、同一セッション排他、base64 メディア、HITL 再開）、HTTP サーバー、CLI（`validate --mcp`/`list`/`run`/`serve`）、Dockerfile、`HttpClient`、`AgentCoreClient`、`tests_integ/`（実モデル） | ローカルで実モデル（OpenRouter）によりサブエージェント・Skills・MCP・構造化出力・limits・invocation_state・vended ツールを確認済み。Docker・AWS 上は未確認 |
| 2 | CDK スタック（Runtime、ECR、ロール、Memory または S3）、`s3`/`agentcore_memory` セッションと `AgentCoreClient` の実環境検証、Lambda サンプル、観測性の確認、code zip の評価 | AgentCore 上で Lambda から呼び出し、CloudWatch でトレースを確認できる |
| 3 | 長期記憶（`[memory]`）、Graph/Swarm/A2A の `kind` 拡張、HITL（interrupt 再開）、`prompt.md` の動的コンテキスト注入 | 設計 §8 の拡張点のうち優先度の高いものが利用可能 |

---

## 14. 未決事項

レビューで決めたい点。括弧内は本文書での仮の採用値。

1. **パッケージ名・CLI 名**（`strands-agents-runtime` / `strands_runtime` / `strands-runtime`）。公式の `strands-*` 名との混同を避けるなら別名（例: `agentrt`）も候補。
2. **既定モデル**（`bedrock/global.anthropic.claude-opus-5`、strands-harness の既定と同じ）。利用リージョンで有効な Bedrock モデル ID を確認する必要がある。
3. **AgentCore の既定セッションバックエンド**（`s3`）。AgentCore Memory を標準にするかはコスト（イベント課金）と長期記憶の必要性で判断。
4. **デプロイ方式の既定**（コンテナ）。code zip はフェーズ 2 で評価後に決定。
5. **クライアントの配布形態**（同一パッケージの extras）。Lambda のサイズ要件が厳しければ別配布。
6. **インバウンド認証**（SigV4）。外部 IdP の JWT を使う要件があるか。
7. **`prompt.md` のテンプレート機能**（なし）。日付や利用者情報の埋め込み要求が出た時点で SDK のプラグインで対応する方針でよいか。

---

## 付録 A. 設計決定記録（ADR）

### ADR-1: 定義フォーマットは TOML + Markdown。Strands/ハーネスの JSON 形式は採用しない

- **背景**: `strands.experimental.config_to_agent`（JSON、4 キーのみ）と `strands-harness` の可搬設定（JSON、camelCase、コーディングエージェント向けの既定）が存在する。
- **決定**: 利用者が書くのは TOML と Markdown。内部表現は pydantic モデル。
- **理由**: 要件が TOML/Markdown を明示している。既存 JSON 形式はいずれも表現力か用途が合わず、変換レイヤーを挟むと両形式の制約を同時に受ける。MCP 設定だけは SDK の `MCPServerConfig` とキーを完全に一致させ、`mcp.json` の流用を可能にする。

### ADR-2: `create_harness` ではなく `Agent` を直接構築し、モデル解決だけハーネスを使う

- **背景**: `strands_harness.create_harness` はモデル解決、MCP、Skills、セッション、メモリ、コンテキスト管理を一括配線する。一方で、コーディングエージェント向けの既定（`shell`/`read`/`write`/`edit` ツール、`todos`/`environment` プラグイン、独自のシステムプロンプト契約、ファイルメモリ）が有効になり、`pydantic-monty` を必須依存に持ち、`OTEL_TRACES_EXPORTER` が設定されていると独自にエクスポーターを配線する（AgentCore の ADOT と二重になる可能性）。
- **決定**: `Agent` は本ランタイムの Factory が直接構築する。`"provider/id"` と `effort` の解決は `strands_harness.models.resolve_model` に委ねる。
- **理由**: ハーネスが配線する残りの要素（`MCPClient.load_servers`、`AgentSkills`、`SnapshotSessionManager`、`context_manager` 文字列）は SDK 側でそれぞれ 1 行で済み、既定を打ち消すためのオプション指定の方が多くなる。推論強度（effort）のプロバイダー別マッピングだけは非自明なロジック（約 150 行、モデルファミリーごとの検証を含む）であり、再実装すべきでない。
- **リスク**: `resolve_model` はハーネス 0.x の公開関数であり、シグネチャが変わり得る。バージョンを範囲固定し、Factory 内の 1 箇所に閉じ込める。

### ADR-3: ストリーミングイベントは独自スキーマに正規化する

- **背景**: `stream_async` の生イベントはモデルプロバイダーの生チャンクやシリアライズ不能なオブジェクトを含み、鍵の種類も多い。
- **決定**: `text` / `reasoning` / `tool_use` / `tool_result` / `interrupt` / `result` / `error` に正規化し、pydantic でサーバーとクライアントが共有する。
- **理由**: クライアント（Lambda 等）が Strands の内部構造に依存しないようにする。SDK のイベント名変更（例: ツールフック名の改名）の影響をサーバー側 1 箇所に閉じる。

### ADR-4: デプロイは CDK を正とし、Python 版 starter toolkit には依存しない

- **背景**: `bedrock-agentcore-starter-toolkit` は非推奨。後継の `@aws/agentcore` CLI は Node.js 製で、内部は CDK。
- **決定**: `infra/` に Python CDK スタックを置く。CLI は開発者のローカル検証にのみ任意で使う。
- **理由**: Runtime に加えて Memory、S3、IAM、環境変数を 1 つのスタックで再現可能に管理したい。CLI は独自のプロジェクト配置（`agentcore/agentcore.json`、`main.py`）を前提とし、ランタイムの構成と二重管理になる。

### ADR-5: `Agent` はリクエストごとに構築し、重い資源はプロセス内でキャッシュする

- **背景**: Strands の `Agent` は履歴を持つステートフルなオブジェクト。AgentCore はセッション単位の microVM だが、アイドルで破棄される。
- **決定**: リクエストごとに `Agent` を構築し、`SessionManager` で履歴を復元する。`MCPClient`、読み込んだツールモジュール、検証済み定義はプロセス内でキャッシュする。
- **理由**: 同時実行の `Agent` 共有（`ConcurrentInvocationMode.THROW`）を避け、ローカル（マルチセッション 1 プロセス）と AgentCore（1 セッション 1 プロセス）で同じコードにする。

---

## 付録 B. 参照

- Strands Agents SDK ドキュメント: https://strandsagents.com/docs/
  - Agent / Agent loop: https://strandsagents.com/docs/user-guide/sdk/agents/agent-loop/
  - Skills プラグイン: https://strandsagents.com/docs/user-guide/sdk/plugins/skills/
  - MCP ツール: https://strandsagents.com/docs/user-guide/sdk/tools/mcp-tools/
  - Vended tools: https://strandsagents.com/docs/user-guide/sdk/tools/vended-tools/
  - セッション管理: https://strandsagents.com/docs/user-guide/sdk/agents/session-management/
  - マルチエージェント（Agents-as-Tools / Graph / Swarm / A2A）: https://strandsagents.com/docs/user-guide/sdk/multi-agent/agents-as-tools/
  - 実験的 JSON 設定: https://strandsagents.com/docs/user-guide/sdk/experimental/agent-config/
- strands-harness: https://github.com/strands-agents/harness-sdk/tree/main/harness-py
- Amazon Bedrock AgentCore
  - Runtime サービス契約: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html
  - HTTP プロトコル契約: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html
  - セッション: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html
  - Direct Code Deploy: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html
  - 呼び出し: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html
  - 観測性: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html
  - CloudFormation `AWS::BedrockAgentCore::Runtime`: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-bedrockagentcore-runtime.html
  - CDK `aws_bedrockagentcore`: https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_bedrockagentcore-readme.html
  - `bedrock-agentcore` SDK: https://github.com/aws/bedrock-agentcore-sdk-python
  - `@aws/agentcore` CLI: https://github.com/aws/agentcore-cli
  - 料金: https://aws.amazon.com/bedrock/agentcore/pricing/
