# Strands Agents Runtime 設計文書

- 状態: フェーズ 1 実装済み（2026-10-22）。フェーズ 2 以降は計画
- 作成日: 2026-10-22
- 対象読者: 本ランタイムの開発者、エージェント定義を書く利用者、クライアント（Lambda 等）の実装者

---

## 1. 目的とスコープ

### 1.1 目的

[Strands Agents SDK](https://strandsagents.com/) を土台に、
**エージェントをコードではなく設定ファイルで宣言し、同一の成果物をローカル・Docker・任意のコンテナ基盤（ECS、Azure Container Apps など）・Amazon Bedrock AgentCore Runtime で動かせる** 汎用エージェントランタイムを提供する。

ランタイムが解決する課題は次の 3 つである。

1. **宣言的なエージェント定義**
   ベンダー、モデル ID、推論パラメータ、ツール、MCP サーバー、スキル、プロンプト、セッション方式を TOML（設定）と Markdown（プロンプト）で記述する。
   Python コードを書くのはカスタムツールを追加するときだけに限定する。
2. **実行環境の可搬性**
   ローカル開発、Docker、汎用のコンテナ基盤、AgentCore Runtime のいずれでも同じ HTTP 契約・同じエージェント定義・同じコンテナイメージを使う。AgentCore はデプロイ先の一つであり、設計の中心には置かない。
3. **シンプルな呼び出し**
   クライアント（ローカルの別プログラム、Lambda 等）はエージェント名とプロンプトを含む JSON ペイロードを送るだけで、ストリーミングまたは一括で結果を受け取れる。

### 1.2 スコープ外（ただし拡張点として設計で考慮する）

- グラフ・スワームなどの複雑なマルチエージェントワークフローの実装（第 3 フェーズで検討。[§8](#8-オーケストレーションとエージェント連携) に拡張点を定義）
- Web UI、チャット UI
- Strands SDK が提供しない独自のモデルプロバイダー実装
- エージェント定義のホットリロード（成果物を不変に保ち、再デプロイを正規の更新手段とする。[§11.6](#116-定義の配置方式焼き込み--マウント--s3-同期)）

### 1.3 設計原則

プロジェクトの `CLAUDE.md` の原則に従う。特に本ランタイムでは次を重視する。

- **SDK に寄せる**: Strands SDK と `bedrock-agentcore` SDK が既に持つ機能（モデル解決、MCP 設定の読み込み、スキル、セッション永続化、OTel 計装、HTTP サーバー）は再実装せず、そのまま使う。
  本ランタイムの固有コードは「TOML/Markdown の読み込み」「定義から `Agent` を組み立てる工場」「ペイロード契約」「クライアント」に限定する。
- **後方互換レイヤーを持たない**: 非推奨化が進む `strands-agents-tools` パッケージや Python 版 starter toolkit には依存しない。
- **1 つの成果物**: 環境ごとに別のエントリポイントを持たない。HTTP 契約（`POST /invocations`、`GET /ping`、SSE）は AgentCore 互換の汎用契約で、どのコンテナ基盤でも成り立つ。サーバー実装には `BedrockAgentCoreApp`（中身は Starlette アプリ）を使う。

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
│   strands_runtime_client.{HttpClient, AgentCoreClient}              │
└───────────────┬─────────────────────────────────────────────────────┘
                │ JSON ペイロード（agent, messages, session_id, ...）
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
| `strands-agents-runtime-client` | `strands_runtime_client` | なし | 必須依存は pydantic。http / agentcore extras でトランスポートを追加 |

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
    sessions.py     # session.history.backend → SessionManager
  invoke/
    invoker.py      # 1 リクエストの実行とイベント正規化
  server/
    app.py          # BedrockAgentCoreApp の組み立て、/invocations エントリポイント
    __main__.py     # python -m strands_runtime.server
  cli.py            # validate / list / run / serve

packages/client/src/strands_runtime_client/
  base.py           # クライアント共通インターフェース、SSE パーサー、ジョブ操作
  http.py           # ローカル / Docker 向け HTTP クライアント
  agentcore.py      # boto3 invoke_agent_runtime ラッパー
  events.py         # サーバー・クライアント共通のイベント型（pydantic）
```

### 3.3 実行時のデータフロー

1. 起動時に `runtime.toml`（環境変数 `STRANDS_RUNTIME_CONFIG` で場所を変更可、既定 `./runtime.toml`）を読み、`Registry` が `runtime.agents_dir` 配下を走査して各 `agent.toml` と `prompt.md` を検証し `AgentDefinition` にする。
   定義エラーは起動失敗にする。壊れた定義を持つ成果物はヘルスチェックを通らず、どの基盤でもロールアウトが止まる（AgentCore V2 では最初の `/ping` が健全になった時点でスナップショットされるため、なおさら重要）。
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
fallbacks = ["bedrock/global.anthropic.claude-sonnet-4-6"]   # 任意: 失敗時に順に試す候補（ModelRouter）

[model.cache]                            # 任意: プロンプトキャッシュの調整（Strands CacheConfig）
ttl = "1h"

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
manager = "off"                          # off|auto|agentic（Strands の context_manager）。auto は大きなツール結果の切り詰め・退避、
                                         # 85% 到達時の要約（エージェント自身のモデルで実行）、retrieve_context ツールの追加を伴う
inject = ["datetime", "actor"]           # 任意: 各ユーザーターンに注入する一時コンテキスト（履歴には残らない）
timezone = "Asia/Tokyo"                  # datetime 注入のタイムゾーン

[session.history]                                # 省略時は runtime.toml の [session.history] を継承
backend = "inherit"

[structured_output]                      # 任意: 最終回答を pydantic モデルで返す
model = "mycompany.schemas:SupportAnswer"

[limits]                                 # 任意: 1 呼び出しの上限
turns = 30                               # Strands Limits（turns / output_tokens / total_tokens）
output_tokens = 32000
timeout_seconds = 120                    # ランタイムが強制する実時間の上限
```

全項目を説明コメント付きで書いた定義は `examples/reference/`（`runtime.toml`、単体の `agents/reference/agent.toml`、Graph の `agents/flow/agent.toml`、Swarm の `agents/crew/agent.toml`）にあり、`make check` の `validate --mcp` の対象にして陳腐化を防ぐ。

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
| `model.fallbacks` | list[str or table] | `[]` | 失敗時に順に試す候補。文字列は `id` で、effort・caching・接続先・キー・config を主から継承。テーブル（`id` / `module`、`effort`、`base_url`、`api_key_env`、`config`）で個別指定。1 つでもあれば `ModelRouter`（`FallbackStrategy`）になる |
| `model.cache` | table | なし | Strands `CacheConfig` のキー（`strategy`、`ttl`、`system_prompt_ttl`、`cache_key`、`tools_ttl`）。`caching = true` が前提。主と全候補に適用 |
| `model.base_url` | str | なし | `openai/` / `openai-chat/` 専用。接続先（省略時 `OPENAI_BASE_URL`） |
| `model.api_key_env` | str | なし | `openai/` / `openai-chat/` 専用。キーを保持する環境変数名（省略時 `OPENAI_API_KEY`） |
| `model.module` | `"pkg.mod:factory"` | なし | 7 プロバイダー以外や `ModelRouter` を使う逃げ道。`id` と排他 |
| `tools` | list[str] | `[]` | `Agent(tools=[...])` に文字列のまま渡す（SDK のローダーを使う）。相対パスは定義ディレクトリ基準で絶対化 |
| `skills` | list[str] | `["./skills"]`（存在時） | `AgentSkills(skills=[...])` を `plugins` に追加 |
| `subagents` | list[str] | `[]` | 定義名を解決して構築した `Agent` を `tools` に追加 |
| `mcp_servers` | table or path | `{}` | `MCPClient.load_servers(dict)` → `ToolProvider` として `tools` に追加 |
| `kind` | `"agent"` / `"graph"` / `"swarm"` | `"agent"` | `graph` / `swarm` のキーは [§8.2](#82-graph--swarm-の定義フォーマット) |
| `context.manager` | enum or table | `"off"` | `Agent(context_manager=)`。`auto` / `agentic` は `retrieve_context` ツールの追加と要約（自身のモデル）を伴うため、必要な定義だけで有効にする |
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
                                         # エージェント単位で別デプロイするときに絞り込む（§11.5）

[model]
default_id = "bedrock/global.anthropic.claude-opus-5"

[session]
backend = "local"                        # none|local|dynamodb|cosmos|agentcore。排他方式

[session.history]
backend = "local"                        # local|s3|dynamodb|cosmos|agentcore_memory|none
dir = "./.runtime/sessions"              # local のとき

# [session.history]
# backend = "s3"
# bucket = "my-agent-sessions"
# prefix = "prod/"

# [session.history]
# backend = "agentcore_memory"
# memory_id = "${AGENTCORE_MEMORY_ID}"
# retrieval = { "/preferences/{actorId}/" = { top_k = 5, relevance_score = 0.7 } }

[jobs]
storage = "inherit"                      # inherit|local|s3|session|none。ジョブのジャーナル置き場（§7.6.7）。inherit は [session.history] の local / s3 / dynamodb / cosmos を使う
max_duration_seconds = 28800             # ジョブ 1 件の上限（既定 8 時間）

[server]
port = 8080
log_file = "./.runtime/server.log"       # 任意: 端末に加えてファイルにも出す（警告も含む）

[telemetry]
# Strands/ADOT は標準の OTEL_* 環境変数で制御する。ここには設定を置かない。
```

環境ごとの差分（セッションバックエンド、既定モデル、公開するエージェント）は `runtime.toml` を環境別に用意し、`STRANDS_RUNTIME_CONFIG` で選ぶ。シークレットやバケット名のように値そのものが環境依存のものだけ、TOML に `${VAR}` を書いて環境変数で与える。
`enabled_agents` は `subagents` で参照される定義も含めて読み込む必要があるため、Registry は「公開する定義」と「読み込む定義」を区別し、後者は前者から到達可能な定義すべてとする。
公開範囲（`enabled_agents`）やセッションバックエンドのようにデプロイごとに変わる値は、`runtime.toml` を分けて表し、どれを読むかだけを `STRANDS_RUNTIME_CONFIG` で選ぶ。環境変数で TOML の値を上書きする仕組みは持たない（検討の上で取り下げた。[ADR-6](#adr-6-環境変数で設定値を上書きしない) を参照）。

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
  `base_url` と `api_key_env` をエージェントごとに持てるため、OpenAI 公式と互換サーバーを 1 つのランタイムで同時に使える。`openai-chat/` の `effort` は明示されたときだけ `reasoning_effort` として送る。
- **`openai/<model>`** も `base_url` / `api_key_env` を受け付ける。ハーネスが生成する `OpenAIResponsesModel` の公開 `client_args` に渡し、effort の変換と config・キャッシュの適用は維持する。
  Azure OpenAI v1 では `base_url = "https://<resource>.openai.azure.com/openai/v1/"` と `id = "openai/<デプロイ名>"` を使う（API キー認証、`api-version` 不要）。
  `id` を省いた場合も既定モデルのプロバイダーを確定してから接続設定を検証する。フォールバック候補への接続設定の継承は、同じ API プロバイダー同士に限定する。
  `openai/` の `effort = "auto"` は `high`、`off` は `none`。推論パラメータを受け付けないデプロイでは `[model.config] params = {}` で上書きできる。
- `model.config` は `Model.update_config(**config)` でプロバイダー固有設定を上書きする。
  キー名は Strands 各プロバイダーの `*Config` TypedDict に従う（例: Bedrock は `max_tokens`、`temperature`、`guardrail_id`、`service_tier`。Anthropic は `max_tokens`、`params`）。
  ランタイムはキーを解釈しない。
- `model.module` は `Model` または `ModelRouter` を返す関数への参照。
- `fallbacks` があると、主と各候補を同じ手順で構築したうえで `ModelRouter([...])` に包む（既定の `FallbackStrategy`: 失敗した候補を避けて次を選び、成功すれば元に戻る）。候補ごとに `effort` を解決し、`[model.cache]` と `config` は全候補に適用する。
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

`[session] backend` は会話の排他方式、`[session.history] backend` は保存先を指定する。
両方ともランタイムの既定は `none`。排他が `none` の場合は履歴保存を有効にできない。
排他は `local`（同じホストのSQLite）、`dynamodb`、`cosmos`、`agentcore`（1ワーカーのメモリ）から選ぶ。
ワーカー数・待機枠・DB要件・復旧手順は [マニュアル](manual.md#6-同時実行と負荷の調整) を参照。

`session.history.backend` で `SessionManager` を選ぶ。

| backend | 実装 | 用途 |
|---|---|---|
| `local` | `SnapshotSessionManager(session_id, storage=LocalFileStorage(dir))` | ローカル開発 |
| `s3` | `SnapshotSessionManager(session_id, storage=S3Storage(bucket, prefix=...))` | Docker/AgentCore で AgentCore Memory を使わない場合 |
| `agentcore_memory` | `AgentCoreMemorySessionManager(AgentCoreMemoryConfig(memory_id, session_id, actor_id, retrieval_config))` | AgentCore 上で短期記憶に加えて長期記憶（戦略）を使う場合 |
| `dynamodb` / `cosmos` | `SnapshotSessionManager(session_id, storage=NoSQLStorage(...))` | スナップショットをチャンク分割して保存 |
| `none` | `session_manager=None` | ステートレスなワンショット呼び出し |

- 定義側の `[session.history]` は `backend = "inherit"`（既定）か、上記のいずれかで上書きできる。
- `agentcore_memory` は `actor_id` が必須なので、ペイロードの `actor_id`（省略時は `[session] default_actor_id`、両方なければ保存・復元は拒否）を使う。
- AgentCore の microVM はセッションに紐づくが、アイドルタイムアウトで破棄されるため、**プロセス内メモリをセッション状態の正とは見なさない**。会話を継続する場合は履歴保存を明示するか、クライアントから `messages` を渡す。

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

契約は AgentCore Runtime の HTTP 契約と互換だが、AgentCore 固有の前提は置かない。`POST /invocations` と `GET /ping` だけで構成されるため、ALB 配下の ECS、Azure Container Apps、Cloud Run など任意のコンテナ基盤でそのまま公開できる。

### 7.1 リクエスト（`POST /invocations`）

```json
{
  "agent": "support-bot",
  "messages": [{"role": "user", "content": "注文 #1234 の状況を教えて"}],
  "session_id": "0d1b2b3a-....",
  "actor_id": "user-42",
  "stream": true,
  "invocation_state": { "tenant": "acme" }
}
```

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `agent` | str | `runtime.default_agent` | エージェント定義名 |
| `messages` | list | 実行時必須 | user / assistantの配列。session_id指定時は1件のみ |
| `store` | bool | session_id指定時true、それ以外false | 会話履歴・状態への書き込み。falseでも既存履歴・長期記憶は取得できる |
| `session_id` | str | なし | [§6.3](#63-セッション-id-の解決) |
| `actor_id` | str | session.default_actor_id | 会話・ジョブの所有者。保存・復元・ジョブ操作で必須。別actorのアクセスは403 |
| `stream` | bool | `true` | `false` で最終結果だけを JSON で返す |
| `invocation_state` | object | `{}` | ツールに渡す任意のコンテキスト（`ToolContext.invocation_state`）。保存されない |
| `include_reasoning` | bool | `false` | モデルの推論差分を `reasoning` イベントとして流す（推論モデルでは量が多いため既定は無効） |
| `tool_result_detail` | `"preview"` or `"full"` | `"preview"` | `"full"` で `tool_result` に結果の content block 全体（`content`）を含める |
| `include_subagents` | bool | `false` | サブエージェント内のイベントを `agent` / `parent` 付きで流す（[§7.6.8](#768-ネストした実行の可視化)） |
| `mode` | `"run"` / `"job"` / `"status"` / `"follow"` / `"result"` / `"cancel"` | `"run"` | `run` 以外はジョブの操作（[§7.6.2](#762-ペイロードの-mode)） |
| `job_id` | str | セッションの最新ジョブ | `status` / `follow` / `result` / `cancel` の対象 |
| `after` | int | `0` | `follow` の再生開始位置（この `seq` の次から） |

messagesの `content` にblock配列を渡す場合、`image` / `document` / `video` の `source.bytes` は base64 文字列で送る。ランタイムが bytes に復号して Strands に渡す。
中断（`interrupt`）した会話は、同じ `session_id` で userメッセージの `content = [{"interruptResponse": {"interruptId": "...", "response": ...}}]` を送ると再開する。

`system_prompt` の上書きは許可しない。モデルと生成条件の上書きは [クライアントのREADME](../packages/client/README.md) を参照。
履歴はactorごとに分離し、保存時にsessionとの所有関係を保持する。store=falseでは今回の変更を一時領域に置き、永続保存しない。

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
| `result` | `stop_reason`, `text`, `structured_output`, `metrics`, `session_id`, `nodes`（Graph / Swarm のノード別結果。単体では `null`） | `result` イベント |
| `error` | `error_type`, `message` | 例外（Strands が `force_stop` の後に再送出する例外を 1 件の `error` にまとめる）。結果もエラーもなく終わった場合は `NoResult` |
| `job` | `job_id`, `state`, `accepted_at`, `updated_at`, `finished_at`, `seq`, `last_event`, `stop_reason`, `cancelled_by`, `error_type` | ジョブの記録（[§7.6.3](#763-状態)）。`mode` が `job` / `status` / `cancel` の応答と、`result` の未完了時 |
| `node_start` / `node_stop` / `handoff` | ノード ID、状態、引き継ぎ元と先 | Graph / Swarm のノード遷移（[§8.5](#85-呼び出し契約の拡張)） |

すべてのイベントは共通の任意フィールドとして `seq`（ジョブのジャーナル内の連番）、`ts`（記録時刻）、`agent`（発生させたエージェント名）、`parent`（囲んでいる親の `tool_use_id` またはノード ID）を持つ。同期ストリームのトップレベルのイベントではすべて `null` で、ジョブと `include_subagents` で値が入る（[§7.6](#76-長時間実行ジョブと進捗)）。

`result.metrics` は呼び出し全体（サブエージェントを含むエージェントツリー）の合計で、`input_tokens` / `output_tokens` / `total_tokens` / `cache_read_tokens` / `cache_write_tokens`、`tool_calls`、`cycles`、`model_failures`（失敗してフォールバックした回数）を持ち、`agents` にエージェント名ごとの内訳（使ったモデル、呼び出し回数、トークン数）を載せる。
Strands はエージェントオブジェクト単位でしか使用量を持たないため、ランタイムがツリー内の全エージェントにフックを付け、呼び出し前後の差分を名前ごとに集計する。同じ値は OpenTelemetry の現在のスパンにも `strands_runtime.usage.*` 属性として付ける。
`latency_ms` はランタイムが計測した呼び出し全体の実時間（プロバイダーによって SDK が報告しないため）。
イベント型は pydantic モデルで定義し、サーバーとクライアントで同じモジュールを使う。

`result.stop_reason` は Strands の値（`end_turn`、`max_tokens`、`interrupt`、`limit_turns` など）をそのまま使い、次の 2 つだけランタイムが置き換える。

| 値 | 意味 |
|---|---|
| `structured_output` | `structured_output` に構造化回答が入っている（SDK ではツール呼び出し扱いで `tool_use` になる） |
| `timeout` | `limits.timeout_seconds` 超過で停止した（SDK では `cancelled`） |

### 7.3 非ストリーミングレスポンス

`stream: false` のとき、`result` イベントと同じ構造の JSON を `application/json` で返す。
同期応答は基盤側のタイムアウトに従う（AgentCore では 15 分、ロードバランサー配下ではそのアイドルタイムアウト）。長時間タスクはストリーミングを使い、それでも収まらないものは [§7.6](#76-長時間実行ジョブと進捗) のジョブで扱う。

### 7.4 ヘルスチェック（`GET /ping`）

`BedrockAgentCoreApp` の既定実装を使い、`{"status": "Healthy"}` を返す。
ジョブ（[§7.6](#76-長時間実行ジョブと進捗)）の実行中は `add_async_task` / `complete_async_task` で `HealthyBusy` を返す。

### 7.5 エラー

- 同じ `session_id` で実行（同期実行またはジョブ）が進行中: HTTP 409（AgentCore の `RetryableConflictException` と同じ扱い）。履歴の競合を避けるため直列化ではなく拒否する。ジョブの `status` / `follow` / `result` / `cancel` は通す（[§7.6.6](#766-排他と基盤ごとの挙動)）
- クライアントが途中で切断した場合: ランタイムは `cancel_signal` を立て、エージェントは次の安全な地点で停止して履歴を保存する
- 定義名不明、ペイロード不正: HTTP 400 と `{"error": ..., "details": ...}`
- 実行時例外: ストリーミング中なら `error` イベント、非ストリーミングなら HTTP 500
- AgentCore はコンテナの 4xx/5xx を `424 RuntimeClientError` としてクライアントに返すため、クライアントライブラリはこれを例外に変換する。

---

### 7.6 長時間実行（ジョブ）と進捗

1 回の実行が数十分から 1 時間以上走る可能性を考慮する。
どの基盤にも接続のタイムアウトがあり（AgentCore では同期 15 分・ストリーミング 60 分、ロードバランサーにはアイドルタイムアウト）、接続を保ち続ける前提は成り立たない。
本ランタイムの HTTP 契約は `/invocations` と `/ping` に限定しているため、非同期の操作もすべて `/invocations` のペイロードで表現する（[ADR-7](#adr-7-非同期実行はペイロードの-mode-で表しジョブの状態はセッションストレージを正とする)）。

契約は 2026-10-23 に確定し、同日に実装した（`strands_runtime.jobs`、`InvocationRequest.mode`、クライアントの `submit()` / `JobHandle`）。OpenRouter の実モデルで、サブエージェント込みのジョブの追従、キャンセル、同一セッションの 409、終端後の同期実行の継続を確認済み。
先行事例（OpenAI Responses の background mode、LangGraph Platform の runs、A2A 1.0 の Task、MCP Tasks 拡張、Claude Managed Agents）に共通する最小の語彙、すなわち「サーバーが生成する推測困難な ID」「受付 / 実行中 / 入力待ち / 完了 / 失敗 / キャンセルの状態」「連番カーソルによるストリームの再開」「終端状態に結果を含める」「ベストエフォートで冪等なキャンセル」を採り、それ以外は足さない。

#### 7.6.1 用語

| 用語 | 意味 |
|---|---|
| 呼び出し | `/invocations` への 1 リクエスト |
| 実行 | エージェントが 1 つのプロンプトを処理し終えるまでの単位。同期実行（`mode = "run"`）では呼び出しと実行が一致する |
| ジョブ | リクエストを超えて生きる実行。受け付けた呼び出しはすぐに返り、実行はサーバープロセス内のバックグラウンドタスクとして続く。`job_id`（サーバーが生成する UUIDv4）で識別し、1 つのセッションに属する |
| ジャーナル | ジョブの進捗イベントと最終結果を順番に記録したもの。セッションストレージに置く |

#### 7.6.2 ペイロードの `mode`

| `mode` | 役割 | 応答 |
|---|---|---|
| `run`（既定） | 同期実行。[§7.1](#71-リクエストpost-invocations)〜[§7.3](#73-非ストリーミングレスポンス) のとおり | SSE または JSON |
| `job` | ジョブを受け付けて即座に返る。`messages` と `agent`、`actor_id`、`invocation_state` は `run` と同じ意味を持つ | JSON の `job` イベント（受付直後の記録） |
| `status` | ジョブの現在の記録 | JSON の `job` イベント |
| `follow` | ジャーナルを `after` の次の `seq` から再生し、実行中ならそのまま終端まで追従する | SSE。終端イベント（`result` または `error`）で閉じる |
| `result` | 終端イベントだけを取り出す | 終端なら `result` または `error` の JSON。実行中なら `job` イベント（`state` で判別する） |
| `cancel` | 停止を要求する。冪等で、終端のジョブに対しては現在の記録を返すだけ | JSON の `job` イベント |

`status` / `follow` / `result` / `cancel` は `session_id`（ヘッダーまたはペイロード）を必須とし、`job_id` を省略するとそのセッションの最新のジョブを指す。
`job` モードでは `stream` と `include_reasoning` を無視する（ジャーナルに書く粒度は [§7.6.4](#764-ジャーナル) で固定する）。`tool_result_detail` と `include_subagents` は投入時の値がジャーナルの内容を決め、`follow` する側は記録されたものをそのまま受け取る。
`job_id` が見つからなければ HTTP 404、ジョブ機能が無効な構成（[§7.6.7](#767-設定)）で `job` を指定したら HTTP 400 を返す。

#### 7.6.3 状態

```
受付 ──▶ running ──▶ completed                 result（end_turn / max_tokens / timeout / limit_* / structured_output）
                ├──▶ input_required            result（interrupt。interrupts を含む）
                ├──▶ failed                    error（プロセス消失の JobLost を含む）
                └──▶ cancelling ──▶ cancelled  result（cancelled）
                                └──▶ failed    猶予内に止まらなかった
```

| `state` | 終端 | 意味 |
|---|---|---|
| `running` | | 実行中。`updated_at` が定期的に更新される |
| `cancelling` | | 停止要求を受け、エージェントが次の安全な地点で止まるのを待っている |
| `completed` | ○ | `result` イベントが記録された。`stop_reason` が `timeout` や `limit_turns` の場合もここに入る（部分的な回答を含む正常終了として扱う） |
| `input_required` | ○ | 人の入力待ちで止まった（`result.stop_reason = "interrupt"`）。同じセッションに `interruptResponse` を送ると新しい実行として再開する。ジョブとしては終端 |
| `cancelled` | ○ | `cancel` または基盤の停止要求で止まった（`result.stop_reason = "cancelled"`）。`cancelled_by` が `client` か `runtime` かを示す |
| `failed` | ○ | `error` イベントが記録された。プロセスが消えて進捗が止まったジョブは `error_type = "JobLost"` で失敗として確定する |

終端状態は変わらない。キャンセルは協調的で、`cancelling` のまま猶予（同期実行のタイムアウトと同じ 15 秒）を過ぎた場合はストリームを打ち切って `failed` にする。

`job` イベントの形:

```json
{
  "type": "job",
  "job_id": "6f1c...", "session_id": "0d1b...", "actor_id": "user-42", "agent": "researcher",
  "state": "running",
  "accepted_at": "2026-10-23T09:00:00Z", "updated_at": "2026-10-23T09:12:30Z", "finished_at": null,
  "seq": 17,
  "last_event": { "seq": 17, "ts": "2026-10-23T09:12:30Z", "type": "tool_use", "summary": "http_request" },
  "stop_reason": null, "cancelled_by": null, "error_type": null
}
```

`seq` は記録済みの最後の連番、`last_event` は最後に記録したイベントの要約（種類と、ツール名または本文の先頭）。進捗の全文は `follow` で取る。

#### 7.6.4 ジャーナル

ジャーナルはlocalの場合SQLite、s3の場合Strands Storage、executionの場合外部実行管理DBに置く。キー配置は次のとおり。

```
jobs/{session_id}/latest                      # 最新の job_id
jobs/{session_id}/{job_id}/job.json           # job イベントと同じ内容（状態、心拍、カーソル）
jobs/{session_id}/{job_id}/events/{seq:06d}   # イベント 1 件ずつ。seq は 1 から始まる連番
```

記録するイベントは同期ストリームと同じ型を使い、粒度だけを粗くする。差分（`text` / `reasoning` の逐次チャンク）は記録しない。

| 記録するイベント | 粒度 |
|---|---|
| `start` | 受付時 |
| `text` | アシスタントのメッセージ 1 件につき 1 件（全文）。同期ストリームの差分と同じ型だが、ジョブでは 1 メッセージ分をまとめる |
| `tool_use` / `tool_result` | ツール呼び出しごと。`tool_result` は既定では `preview` のみで、投入時に `tool_result_detail = "full"` なら `content` も記録する。全文は会話履歴にも入るので容量は重複するが、ジョブの途中でツールの生成物を取り出せる |
| `interrupt` | 人の入力待ちで止まったとき |
| `node_start` / `node_stop` / `handoff` | Graph / Swarm のノード遷移 |
| `result` / `error` | 終端。1 件だけ |

記録されるイベントには `seq`（ジョブ内の連番）と `ts`（記録時刻）が付く。同期ストリームのイベントではこの 2 つは `null` のままにする。
S3 では 1 イベント 1 オブジェクトになるが、記録対象が粗いため件数は実行中のツール呼び出し数の 2 倍程度に収まる。局所的な書き込み失敗はジョブを止めず、警告ログに出して次のイベントで `job.json` を更新する。

#### 7.6.5 生存監視とキャンセルの伝播

- 実行中のプロセスは `job.json` の `updated_at` を、イベントの記録時と、少なくとも 30 秒ごと（心拍）に更新する。
- `status` / `follow` / `result` は、`state = running` のまま `updated_at` が 90 秒（心拍の 3 倍）以上古く、かつそのジョブが自プロセスにない場合、`error_type = "JobLost"` の `error` イベントを書いて `failed` に確定する。プロセス再起動後に走査はせず、観測された時点で確定する（Temporal の heartbeat timeout と同じ考え方）。
- `cancel` は、ジョブが自プロセスにあれば直ちに `cancel_signal` を立て、同時に `cancel.json` に停止要求を書く。別のプロセス（複数ワーカーやレプリカなど）で動いているジョブは、心拍のたびに `cancel.json` を読み直して停止要求を見つけ、自分で `cancel_signal` を立てる。伝播の遅れは最大で心拍 1 回分。
- グレースフルシャットダウン（A8）では、実行中の全ジョブに `cancel_signal` を立てて猶予まで待つ。こうして止まったジョブは `cancelled_by = "runtime"` になり、クライアントは再投入すべき停止だと区別できる。

#### 7.6.6 排他と基盤ごとの挙動

- **排他**: 同じセッションの実行中・待機中は409。AgentCoreは1ワーカー＋メモリ、同一ホストの複数ワーカーはSQLite、複数コンテナはDynamoDB / Cosmos DBの条件付き書き込みで予約する。所有者の後始末まで予約を保持する。外部DBの予約には自動失効を設けず、異常終了時は所有者の停止確認後に管理コマンドで解除する。詳細は[マニュアル](manual.md#6-同時実行と負荷の調整)。
- **`/ping`**: ジョブの受付時に `BedrockAgentCoreApp.add_async_task`、終端で `complete_async_task` を呼び、実行中は `HealthyBusy` を返す。AgentCore ではこれがアイドル終了を止め、上限は `maxLifetime`（microVM は最長 8 時間、Instances 計算タイプは最長 14 日）。他の基盤では情報としての意味しかなく、スケールインからの保護は基盤側の設定（ECS のタスクスケールイン保護など）で行う。
- **AgentCore**: 同じセッション ID の後続呼び出しは `HealthyBusy` 中でも同じ microVM に届く（409 が返るのはセッションのプロビジョニング中とティアダウン中だけ）。microVM が終了すると実行中のジョブは通知なく失われ、次の呼び出しは新しい microVM に届く。ジャーナルがストレージにあるため、その新しいmicroVMでの `status` が心拍の途絶を `JobLost` として報告する。観測側は所有者の記録を上書きしない。
- **複数レプリカ**: `status` / `result` / `follow` はジャーナルだけで答えられるので、どのレプリカが受けてもよい。`follow` はジョブを持つプロセスではプロセス内の通知で即時に、それ以外ではジャーナルの追従（1 秒間隔）で流す。
- **ローカル**: 1 プロセスが複数セッションのジョブを同時に持つ。契約は同じ。

#### 7.6.7 設定

```toml
[jobs]
storage = "inherit"            # inherit | local | s3 | session | none。inherit は [session.history] の local / s3 / dynamodb / cosmos をそのまま使う
# dir = "./.runtime/jobs"      # local のとき
# bucket = "..."; prefix = ""  # s3 のとき
max_duration_seconds = 28800   # ジョブ 1 件の上限。エージェントの limits.timeout_seconds が短ければそちらが効く
```

- `[session.history] backend` が `agentcore_memory` または `none` の構成では、ジャーナルを置く `Storage` がないため `storage` を明示しない限りジョブ機能は無効（`mode = "job"` は HTTP 400）。
- `max_duration_seconds` は基盤の上限がないローカルや ECS でジョブが際限なく走るのを防ぐための安全弁で、同期実行の監視タイマーをそのまま使う。既定は AgentCore の microVM 上限に合わせた 8 時間。
- 結果の保持期間はセッションと同じ（A8 のセッション TTL と掃除コマンドで `jobs/` も対象にする）。

#### 7.6.8 ネストした実行の可視化

サブエージェント（agents-as-tools）の中で起きたツール呼び出しやメッセージは、現在は親の `tool_use` / `tool_result` に畳み込まれて見えない。Strands は子エージェントのイベントを `AgentAsToolStreamEvent`（親の `tool_use` 付き）で親ストリームに転送するため、追加の仕組みなしに取り出せる。

- 全イベントに任意の `agent`（発生させたエージェント名）と `parent`（それを囲む親の `tool_use_id`。Graph / Swarm ではノード ID）を設ける。トップレベルのイベントでは `parent` は `null`。
- ペイロードの `include_subagents = true` で、子の `text`（メッセージ単位）/ `tool_use` / `tool_result` / `interrupt` を `agent` と `parent` 付きで流す。既定は `false` で、親の `text` を連結しているクライアントに子の本文が混ざらないようにする。`run` と `job` の両方で同じ意味を持つ。
- Graph / Swarm（A4）では `node_start` / `node_stop` / `handoff` を同じ語彙に加え、ノード内のイベントは `parent = node_id` を持つ。

#### 7.6.9 同期実行との使い分け

| 状況 | 使うもの |
|---|---|
| クライアントが接続を保てて、所要時間が基盤のストリーミング上限より十分短い | `run`（SSE） |
| 接続を保てない、切断しても追従し直したい、所要時間が読めない | `job` + `follow` |
| 応答を人が待たない（バッチ、夜間処理） | `job` + 後からの `result` |

クライアントライブラリでは `submit()` が `job` を送って `JobHandle` を返し、`handle.follow(after=...)` が `follow`、`handle.result(wait=True)` が終端までの追従、`handle.cancel()` が `cancel` に対応する（[§12](#12-クライアント)）。

#### 7.6.10 採用しなかったもの

- **プロセス再起動後の自動再開**: Strands の checkpoint（experimental）は「エージェントが自発的に一時停止する」仕組みで、障害からの復旧手段ではない。会話履歴は `SessionManager` が終端まで保存しているので、`JobLost` は停止を確認した状態ではない。所有者の停止と、外部DBに残る予約の解除を確認してから新しい実行を送る。保存済み履歴から会話は再開できるが、途中のツール操作を自動復旧するものではない。自動化は要求が出てから判断する。
- **コールバック（Webhook / SNS）**: 基盤ごとに配送手段が異なり、契約が `/invocations` の外に出る。必要なら基盤側（EventBridge など）で `result` をポーリングする側に置く。
- **同一セッションのキューイング**: 409 で拒否し、クライアントに再試行させる方が履歴の整合を保ちやすい。
- **SSE の `id:` / `Last-Event-ID` による再開**: AgentCore がクライアントのヘッダーを素通ししないため、カーソルはペイロードの `after` に置く。

## 8. オーケストレーションとエージェント連携

実装済みなのは **Agents-as-Tools**（`subagents`）と **Graph / Swarm**（ロードマップ A4、2026-10-23 に契約を確定し同日に実装）。実モデル（OpenRouter）で、直列・差し戻しループ・並列と合流・入れ子（graph の中の swarm）・上限超過・キャンセル・ノード内の interrupt と再開・ジョブ経由の追従とキャンセルを確認した（`examples/feature-check` の `writing-flow` / `parallel-flow` / `nested-flow` / `loop-flow` / `approval-flow` / `helpdesk`）。
それ以外の方式は定義フォーマットに拡張点だけを予約する。

### 8.1 方式の一覧

| 方式 | Strands の実装 | 定義フォーマット | 状態 |
|---|---|---|---|
| Agents-as-Tools | `Agent.as_tool()` | `subagents = [...]` | 実装済み。親が LLM でルーティングする |
| Graph | `strands.multiagent.GraphBuilder` | `kind = "graph"` + `nodes` + `[[edges]]` | 実装済み（[§8.2](#82-graph--swarm-の定義フォーマット)〜） |
| Swarm | `strands.multiagent.Swarm` | `kind = "swarm"` + `nodes` | 実装済み（同上） |
| モデル駆動サブエージェント | `strands.vended_tools.make_subagent` | `[subagent_tool]`（presets / max_depth） | 予約。要求が出てから |
| A2A（他ランタイム呼び出し） | `strands.vended_tools.make_a2a_client` | `[a2a_clients]` の `allowed_endpoints` | 予約（ロードマップ C1） |
| A2A（公開側） | `bedrock_agentcore.runtime.serve_a2a` + `StrandsA2AExecutor` | `runtime.toml` の `protocol = "A2A"` | 予約（同上） |
| 同一ランタイム内の別エージェント呼び出し | 本ランタイムの `HttpClient` / `AgentCoreClient` をツール化 | — | 自前実装より A2A を優先する |

Strands 1.61 の確認結果で設計に効くもの:

- Graph / Swarm（`MultiAgentBase`）は `stream_async(task, invocation_state)` だけを持ち、`cancel_signal` と `limits` を受け取らない。
- ノードのエージェントに `SessionManager` を付けると `ValueError` になる。永続化はオーケストレーターが自分の状態（進んだノード、中断状態、Swarm の共有コンテキスト）だけを `SnapshotSessionManager` に書き、**ノードの会話履歴は保存しない**。Swarm はノードを実行するたびに履歴をリセットする。
- Graph はノードの例外をストリームの外へ投げ（`result` が出ない）、Swarm は例外を飲んで `FAILED` の結果を返す。上限超過はどちらも `FAILED` の結果になる。Swarm はノードの `stop_reason = "cancelled"` を `COMPLETED` として扱う。
- 最終テキストを取り出すヘルパーはない。`MultiAgentBase` に `as_tool` はない。ノード単位のスパンはなく、`invoke_graph` / `invoke_swarm` が 1 本だけ。
- Graph の複数の入力エッジは **OR**（1 本でも条件を満たせば実行可能）で、合流（全部を待つ）には条件が要る。

### 8.2 Graph / Swarm の定義フォーマット

Graph / Swarm も `agents_dir` 直下のディレクトリに `agent.toml` を置き、`kind` で種類を宣言する。`prompt.md` は持たない。
同じ Registry に載り、同じ `agent` 名で `/invocations` から呼べる。公開・非公開（`enabled_agents`）と依存の到達（ノードが参照する定義の読み込み）、循環検出も単体エージェントと同じ扱い。

```toml
# agents/research-flow/agent.toml
name = "research-flow"
kind = "graph"                              # agent（既定）| graph | swarm
description = "計画 → 執筆 → レビュー（差し戻しあり）"
entry = ["plan"]                            # 省略時は入ってくるエッジがないノード（全ノードに入力エッジがある循環だけのグラフでは必須）
output = "review"                           # result.text にするノード。省略時は最後に完了したノード
reset_on_revisit = true                     # 差し戻しで再実行するとき、ノードの会話を初期状態に戻す（既定 false）

[[nodes]]                                   # 全ノードが定義名 = ID なら nodes = ["plan", "write", ...] の短縮形でもよい（混在は不可）
agent = "planner"
id = "plan"                                 # 省略時は agent と同じ
[[nodes]]
id = "write"
agent = "writer"                            # 同じ agents_dir の定義名。graph のノードは agent / graph / swarm のどれでもよい
[[nodes]]
id = "review"
agent = "reviewer"
[[nodes]]
id = "publish"
agent = "publisher"
wait_for = "all"                            # any（既定。Strands の OR）| all（入ってくるエッジの元ノードが全部完了してから）

[[edges]]
from = "plan"
to = "write"
[[edges]]
from = "write"
to = "review"
[[edges]]
from = "review"
to = "write"
condition = "./conditions.py:needs_rewrite" # (state: GraphState, *, invocation_state) -> bool。副作用を持たせない（複数回評価される）
[[edges]]
from = "review"
to = "publish"
condition = "./conditions.py:approved"

[session.history]                                   # 省略時は runtime.toml を継承。HITL（interrupt）の再開に使う（§8.4）
[limits]
node_executions = 20                        # ノード実行回数の合計（ループの上限）。省略時は無制限
timeout_seconds = 900                       # ワークフロー全体の実時間
node_timeout_seconds = 300                  # ノード 1 回の実時間
```

```toml
# agents/support-swarm/agent.toml
name = "support-swarm"
kind = "swarm"
description = "一次受付から担当へ引き継ぐ"
nodes = ["triage", "billing", "tech"]       # swarm のノードは kind = "agent" の定義に限る（Strands の制約）
entry = "triage"                            # 省略時は先頭

[limits]
handoffs = 20                               # 引き継ぎ回数の上限（Strands の max_handoffs と max_iterations の両方に渡す）
timeout_seconds = 900
node_timeout_seconds = 300
repetitive_handoff_window = 8               # 直近 N 回の引き継ぎで
repetitive_handoff_min_unique_agents = 3    # 異なるエージェントが M 未満なら堂々巡りとみなして止める（0 で無効）
```

**キーの一覧**

| キー | graph | swarm | 意味 |
|---|---|---|---|
| `name` / `kind` / `description` | 必須 | 必須 | `description` は Swarm で「他に呼べるエージェント」の説明としてもモデルに見せる |
| `nodes` | 必須 | 必須 | 文字列の配列（定義名 = ID）か、`[[nodes]]` テーブルの配列（`id` / `agent` / `wait_for`）。TOML の規則で混在はできない。ID は一意 |
| `entry` | 任意（配列） | 任意（1 つ） | 開始ノード |
| `[[edges]]` | 任意 | なし | `from` / `to` / `condition`（コード参照）。同じ `from`→`to` は 1 本 |
| `output` | 任意 | なし | `result.text` の元。Swarm は常に最後に動いたノード |
| `reset_on_revisit` | 任意 | なし | |
| `[limits]` | `node_executions` / `timeout_seconds` / `node_timeout_seconds` | `handoffs` / `timeout_seconds` / `node_timeout_seconds` / `repetitive_handoff_*` | `timeout_seconds` は Strands の `execution_timeout` に渡し、ランタイムの監視タイマー（[§7.2](#72-ストリーミングレスポンスsse) の `timeout`）も同じ値で動かす |
| `[session.history]` | 任意 | 任意 | 単体エージェントと同じキー。オーケストレーターの状態の保存先 |
| `tools` / `skills` / `mcp_servers` / `subagents` / `[model]` / `[context]` / `[structured_output]` / `plugins` / `interventions` / `prompt` | 不可 | 不可 | ノードのエージェント側で定義する |

`validate` が確認すること: ノード ID の一意性、`entry` / `edges` / `output` が存在するノードを指すこと、全ノードに入力エッジがあるグラフでは `entry` があること、`condition` のコード参照が解決できること、Swarm のノードと `subagents` が `kind = "agent"` であること、入れ子の深さが `runtime.max_subagent_depth` 以内であること、定義間の循環（graph の中に自分自身を含む）がないこと。
`wait_for = "all"` のノードへ向かうエッジには `condition` を書けない（合流条件はランタイムが合成する）。

**条件をコード参照にする理由**: Strands のエッジ条件は呼び出し可能オブジェクトであり、本ランタイムの他の拡張点（ツール、プラグイン、注入関数）もコード参照で統一している。宣言的な式言語（Step Functions の `Choice`、Microsoft Agent Framework の PowerFx）は表現力と引き換えに評価器と文法を抱え込む。よくある合流だけを `wait_for` で宣言できるようにし、それ以外は小さな関数を書いてもらう。同じ判定が定義をまたいで繰り返されるようになったら、構造化テーブル（`{ node, status }` や構造化出力の値の比較）を足す。

### 8.3 組み立て（Factory）

- ノードのエージェントは `subagents` と同じく `persist=False`（`SessionManager` なし）で組み立て、`Agent(name=<ノード ID>)` にする。Swarm はノード ID に `agent.name` を使い、`handoff_to_agent` ツールを `Agent` インスタンスに注入するので、ノードごとに専用のインスタンスを作る（リクエストごとの構築は [ADR-5](#adr-5-agent-はリクエストごとに構築し重い資源はプロセス内でキャッシュする) と同じ）。
- 使用量の集計フック（`UsageCollector`）は全ノードに付け、`result.metrics.agents` にノード単位の内訳を出す。キーはノード ID。
- オーケストレーターの `id` は定義名。入れ子（graph の中の graph / swarm）は `"<親の定義名>--<ノード ID>"` で、セッション内で一意にする（Strands の既定 `default_graph` のままだとスナップショットのキーが衝突する）。
- `[session.history]` が `none` 以外なら、オーケストレーターに `SnapshotSessionManager(session_id, storage=...)` を付ける。保存先のキーは `actors/<actor_idのSHA-256>/session/<session_id>/scopes/multiAgent/<id>/`。`agentcore_memory` バックエンドはオーケストレーターの状態を保存できない（`Storage` ではない）ので、graph / swarm の `[session.history]` では `local` / `s3` / `dynamodb` / `cosmos` / `none` だけを許す。
- `wait_for = "all"` のノードには、入ってくるエッジすべてに「元ノードが `completed_nodes` に含まれる」ことを確かめる合成条件を付ける。
- 入れ子の深さは `subagents` と同じ `runtime.max_subagent_depth` で制限する。

### 8.4 実行の意味論

**呼び出し 1 回 = ワークフロー 1 回**。Graph / Swarm には会話の継続がない。ノードのエージェントは呼び出しごとに新しく作られ、履歴は残らない（Strands の設計どおり）。セッションの役割は次の 2 つに限る。

1. **HITL の再開**: ノードのツールが `interrupt` で止まると、オーケストレーターは進捗（完了したノード、中断したノードとその会話）をスナップショットに保存し、`result.stop_reason = "interrupt"` で返る。同じ `session_id` に userメッセージの `content = [{"interruptResponse": ...}]` を送ると、中断したノードだけが再開し、完了済みのノードは再実行されない。`[session.history] backend = "none"` または `store=false` では今回の中断状態を保存できないので、`interrupt` が起きた時点で `error`（`InterruptNotResumable`）にする。
2. **進捗の観測**: ジョブ（[§7.6](#76-長時間実行ジョブと進捗)）のジャーナルと `status` の `last_event` にノードの開始・終了が載る。

完了済みのスナップショットが残っているセッションに新しいプロンプトを送ると、Strands は全ノードをリセットして最初から実行する（意図した挙動）。Swarm の再開は at-least-once で、ノードは重複実行され得る。

ノードへの入力は Strands が組み立てる。Graph では `Original Task: ...` に続けて上流ノードの結果（`From <node>:`）を並べた文、Swarm では `Context:` に続けて引き継ぎメッセージ、元の依頼、これまでに動いたエージェント、共有された知識、他に呼べるエージェント（`description`）を並べた文になる。定義側はこれを前提にプロンプトを書く。

`reset_on_revisit = true` の差し戻しループでは、再実行されるノードは前回の自分の出力も「何回目か」も知らない（入力は毎回 `Original Task` と上流の結果だけ）。終了条件は上流の結果（審査結果など）に置き、`limits.node_executions` を必ず付けて堂々巡りを止める。実モデルの確認では、審査担当が毎回同じ判定を返して上限で止まるケースが実際に起きた。

Swarm の引き継ぎは、`handoff_to_agent` を呼んだエージェントが**自分のターンを終えてから**起きる。ツール結果の後にもう 1 回モデルが呼ばれ、そのテキストがそのノードの最終テキストになる（実モデルでは「担当に引き継ぎました」のような一文になる）。引き継ぎ先のノードが結果を返したら、`result.text` はそのテキストになる。

### 8.5 呼び出し契約の拡張

**イベント**（[§7.2](#72-ストリーミングレスポンスsse) の表に加える）

| `type` | 内容 | 生成元 |
|---|---|---|
| `node_start` | `node_id`, `node_type`（`agent` / `multiagent`）, `agent`（ノードが使う定義名） | `multiagent_node_start` |
| `node_stop` | `node_id`, `status`（`completed` / `failed` / `interrupted` / `cancelled`）, `latency_ms`, `preview`（そのノードの最終テキストの先頭） | `multiagent_node_stop` / `multiagent_node_cancel` |
| `handoff` | `from_nodes`, `to_nodes`, `message`（Swarm のみ。Graph では `null`） | `multiagent_handoff` |

- ノードの中で起きたイベント（`text`、`tool_use`、`tool_result`、`interrupt`）は `multiagent_node_stream` を解いて流す。`agent` はノードの定義名、`parent` はノード ID。**ノード内のイベントは常に流す**（ワークフローの本体はノードの中にあり、隠すとテキストが何も届かない）。`include_subagents` が制御するのは、ノードのエージェントがさらに `subagents` で呼んだ孫のイベントだけ。
- 入れ子のオーケストレーターのノードイベントは、外側のノード ID を `parent` に持つ。
- Graph が中断したノードには `node_stop` が出ない（Strands の挙動）。`interrupt` イベント（`parent` = ノード ID）と `result`（`stop_reason = "interrupt"`）で表す。
- 実装上の注意: Strands の `multiagent_handoff` は `message` キーを持ち、履歴に追加されたメッセージのイベント（`message` キー）と形が重なる。正規化では `type` が `multiagent_` で始まるイベントを先に振り分ける。
- ジョブでは `node_start` / `node_stop` / `handoff` をジャーナルに記録し、`last_event.summary` にノード ID を入れる（[§7.6.4](#764-ジャーナル) の表の「A4 で追加」の項目）。

**結果**

| 状況 | 返すもの |
|---|---|
| `COMPLETED` | `result`。`stop_reason = "end_turn"`、`text` は Graph なら `output` ノード（省略時は最後に完了したノード）、Swarm なら最後に動いたノードの最終テキスト |
| `INTERRUPTED` | `interrupt` + `result`（`stop_reason = "interrupt"`） |
| ランタイムが停止を要求した（`cancel` / タイムアウト） | `result`（`stop_reason = "cancelled"` / `"timeout"`）。Swarm は `cancelled` を `COMPLETED` と報告するため、ランタイムが自分の要求を覚えていて置き換える |
| `FAILED`（上限超過、ノードの失敗） | `error`。`error_type` は `GraphFailed` / `SwarmFailed`、`message` に理由（どの上限か、どのノードがどう失敗したか）。Graph がノードの例外をストリームの外へ投げる場合も同じ `error` に正規化する |

`result` には `nodes`（ノード ID → `{ status, agent, execution_count, latency_ms, text }`）を加える。単体エージェントでは `null`。`metrics` はこれまでどおりツリー全体の合計と `agents` の内訳（ノード ID ごと）。

**停止**: ランタイムの `cancel_signal`（クライアント切断、`cancel`、タイムアウト）が立ったら、組み立て時に控えた全ノードのエージェントに `Agent.cancel()` を送る。Graph では停止したノードの下流が打ち切られ、兄弟の枝は続く。実行中でないノードは開始されない。猶予（`CANCEL_DRAIN_SECONDS`）の扱いは単体エージェントと同じ。

**制限**: `limits.timeout_seconds` は Strands の `execution_timeout`（バッチ境界でしか検査されない）に渡すと同時に、ランタイムの監視タイマーでも使う。単体エージェント向けの `turns` / `output_tokens` / `total_tokens` は graph / swarm では使えず、ノードのエージェント側の `[limits]` で指定する。

### 8.6 観測性

Strands が作るスパンは `invoke_graph` / `invoke_swarm`（`gen_ai.operation.name` も同じ）が 1 本で、各ノードの `invoke_agent` はその子になる。ランタイムの `strands_runtime.usage.*` はこれまでどおりルートに付ける。ノード単位の記録はスパンではなく `node_start` / `node_stop` イベント（ジョブならジャーナル）で持つ。

### 8.7 採用しなかったもの

- **宣言的な条件式**（[§8.2](#82-graph--swarm-の定義フォーマット) の理由）。
- **Graph / Swarm を `subagents` に書く**: `MultiAgentBase` に `as_tool` がなく、ラッパーが要る。graph のノードに graph / swarm を置けるので、入れ子はそちらで表す。必要になれば `@tool` のラッパーで足す。
- **ノード単位のスパン**: SDK にない。イベントで足りる。
- **`make_subagent`（動的な子生成）**: A4 の後。

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
- スパンの形式は OpenTelemetry の GenAI セマンティック規約（`gen_ai.*`）に加え、Strands が OpenInference 互換の `llm.token_count.*` も併記するため、OTLP を受ける LLM 可視化ツールでそのまま読める。ランタイムが足す合計値は独自の `strands_runtime.usage.*` 名前空間で、二重計上を避けるため `gen_ai.usage.*` は使わない。
- 確認済み（2026-10-23、Arize Phoenix を Docker で起動し OTLP/HTTP で送信）: `invoke_agent <親>` → `execute_event_loop_cycle` → `chat`（モデル呼び出し）→ `execute_tool <子>` → `invoke_agent <子>` → `chat` の階層、各 `chat` の `gen_ai.usage.*`（入出力・キャッシュ読み書き）、`session.id` / `user.id` / `gen_ai.agent.name` の付与、ルートの `invoke_agent` にランタイムの `strands_runtime.usage.*` が載ることを確認。サブエージェントの `session.id` は `<親>--<子>`。`make phoenix-up` で再現できる。
- 構造化ログは標準 `logging` で出し、JSON フォーマッタは導入しない（ADOT のログ連携と CloudWatch で十分）。
- `serve` はランタイム・Strands・uvicorn・Python の警告をすべてルートロガーに集約し、`[server] log_file` が設定されていれば同じ内容をファイルにも書く。端末を見られない作業者（別の運用者、エージェント、CI）がサーバーの観測結果を読めるようにするためのもの。
- OpenAI 互換モデルは SDK がコンテキスト長を知らないため、`[model.config] context_window_limit` を定義側で指定する（未指定なら SDK が 200,000 と仮定して警告する）。
- ジョブ（[§7.6](#76-長時間実行ジョブと進捗)）では、実行の `invoke_agent` スパンは受け付けたリクエストのスパンより長く生きる。受付・`status` / `follow` / `cancel` の各リクエストのスパンには `strands_runtime.job.id` を付け、実行側のルートスパンにも同じ属性を付けて、可視化ツールで突き合わせられるようにする。

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
uv run strands-runtime run support-bot --messages '[{"role": "user", "content": "こんにちは"}]'   # サーバーを立てずに 1 回実行（開発用）
curl -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
     -d '{"agent":"support-bot","messages": [{"role": "user", "content": "こんにちは"}]}'
```

`serve` は `BedrockAgentCoreApp.run()` を呼ぶだけであり、AgentCore 上と同じコードパスを通る。

### 11.2 Docker

- ベースイメージ `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`。既定は `linux/arm64`（AgentCore が要求し、ARM64 Mac でもそのまま動く）。x86_64 の基盤では `--platform` を変える。
- `uv sync --frozen --no-dev` で依存を固定し、非 root ユーザーで実行する。
- `ENV DOCKER_CONTAINER=1` を置き、`app.run()` が `0.0.0.0` に束縛するようにする。
- `CMD ["opentelemetry-instrument", "strands-runtime", "serve", "--port", "8080"]`。`--port` が `runtime.toml` の値より優先されるため、開発用に別ポートを書いていてもコンテナ内は契約どおり 8080 で待ち受ける。
- モデルプロバイダーの extras はビルド引数 `EXTRAS`（既定 `agentcore-otel openai`）で選ぶ。
- `agents/` と `runtime.toml` をイメージに含める。環境差分は環境変数で与える（ローカル実行は `--env-file .env`）。
- ローカルでコンテナを動かすときは OTel コレクターがないため `OTEL_SDK_DISABLED=true` を渡す。イメージには焼き込まない（AgentCore 上では Runtime が注入する ADOT 設定を生かす）。
- 確認済み（2026-10-22）: Apple Silicon 上で linux/arm64 イメージをビルドし、OpenRouter の実モデルでツール呼び出し・セッション継続・非ストリーミング応答が動作。イメージは約 945 MB。

### 11.3 汎用コンテナ基盤（ECS、Azure Container Apps、Cloud Run など）

Docker イメージをそのまま 1 サービスとして動かす。契約が `POST /invocations` と `GET /ping` だけなので、基盤固有の対応は不要で、設定すべきは次の点に限られる。

| 項目 | 方針 |
|---|---|
| ヘルスチェック | `GET /ping` を使う（`{"status": "Healthy"}`） |
| セッション ID | ヘッダー `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` は任意。ペイロードの `session_id` でよい |
| セッションの保存先 | `s3`（AWS）または `local` に永続ボリューム。複数レプリカで同じセッションを扱う場合は永続化が前提になる。S3 以外のオブジェクトストア（Azure Blob など）は Strands の `Storage` 実装をモジュール参照で差し込めるようにする（ロードマップ） |
| 同一セッションの同時実行 | 環境に応じた実行管理で409を返す。設定制約と異常終了時の復旧は[マニュアル](manual.md#6-同時実行と負荷の調整)を参照 |
| タイムアウト | ロードバランサーのアイドルタイムアウトを SSE に合わせて延ばす。それでも足りないものは [§7.6](#76-長時間実行ジョブと進捗) のジョブ |
| 認証 | ランタイムは実装しない。ALB / Front Door / API Gateway などの前段で行う |
| 観測性 | 標準の `OTEL_*` 環境変数でコレクターに送る。`opentelemetry-instrument` は汎用の OpenTelemetry 配布でよく、ADOT は必須ではない |
| アーキテクチャ | 既定は linux/arm64。x86_64 の基盤ではビルド時に `--platform` を変えるだけ |
| シークレット | 基盤のシークレット機構（ECS の Secrets Manager 参照、Azure の Key Vault 参照など）で環境変数に注入する |

### 11.4 AgentCore Runtime

**IaC は CDK（Python、`aws_cdk.aws_bedrockagentcore`）を正とし、`infra/` に置く。**
理由は [ADR-4](#adr-4-デプロイは-cdk-を正とし-python-版-starter-toolkit-には依存しない) に記す。

実装（`infra/`、アカウントなしで `synth` とテストまで確認済み）:

- `infra/deployments.toml` で Runtime を宣言する（`name`、Runtime ごとの `runtime.toml` のパス、`platform_version`、ライフサイクル、追加の環境変数、Secrets Manager から解決するシークレット）。
- `infra/stack.py` は 1 スタックに、共有イメージ（`AgentRuntimeArtifact.from_asset`、linux/arm64）、Runtime ごとの `Runtime`（HTTP、IAM 認証、PUBLIC ネットワーク、トレース有効）、`runtime.toml` の `[session.history] backend` に応じた S3 バケットまたは AgentCore Memory、実行ロールへの権限（S3、`bedrock:InvokeModel*`、Memory）を配線する。L2 コンストラクトは ECR・ログ・X-Ray・ワークロード ID の権限しか付けないため、モデル呼び出しはスタック側で足す。
- シークレットは `SecretValue.secrets_manager(name).unsafe_unwrap()` による CloudFormation 動的参照で環境変数に渡す。テンプレートには `{{resolve:secretsmanager:...}}` が入り、値は入らない（実デプロイでの解決はフェーズ 2 で確認）。
- `cdk.json` は `uv run python -m infra.app` を app に指定しており、`npx aws-cdk deploy` で CLI からも使える。`make synth` はアカウント不要で `cdk.out` を生成する。

- `Runtime`（`AgentRuntimeArtifact.fromAsset("..")` で ARM64 イメージをビルドして ECR に配置）、`protocolConfiguration=HTTP`、`environmentVariables`、`lifecycleConfiguration`（アイドル・最長時間）、`requestHeaderConfiguration`（必要なら）、実行ロール。
- セッションバックエンドが `agentcore_memory` のときは `Memory` リソースと戦略を同じスタックで作り、`AGENTCORE_MEMORY_ID` を環境変数で渡す。`s3` のときはバケットと `s3:GetObject/PutObject/ListBucket` 権限を付与する。
- 観測性のために CloudWatch Transaction Search を有効化する（アカウント単位の 1 回限りの設定。スタック外の手順として README に記す）。
- **Direct Code Deploy**（`AgentRuntimeArtifact.fromCodeAsset(...)`、`entrypoint=["opentelemetry-instrument", "main.py"]`）はコールドスタートとセッション生成レートで有利なため、依存が純 Python のみで 250 MB に収まる構成では選択肢にする。
  `uv pip install --python-platform aarch64-manylinux2014 --only-binary=:all:` で ARM64 向けにビルドする。第 2 フェーズで両方式を検証し、既定を決める。
- Runtime V2（スナップショット方式、コールドスタート約 2 秒）は CloudFormation（`PlatformVersion`）、CDK（`platformVersion: PlatformVersion.V2`）、Terraform（`platform_version`）のいずれも対応済み（開発者ガイドの「未対応」注記は古い）。本ランタイムは定義を起動時に読み込んで焼き込む設計で、V2 の「起動時の状態はスナップショットに凍結される」性質と整合する。最初の `/ping` が 120 秒以内に健全になること、環境変数の合計が 1.5〜2.5 KB に収まることを確認したうえで、フェーズ 2 で V2 を既定にする。
- `@aws/agentcore` CLI（Node）は開発者のローカル検証（`agentcore dev`、`agentcore invoke`）に使ってもよいが、本番の IaC には使わない。

### 11.5 複数エージェントのデプロイ単位

1 つのイメージに全定義を含めたうえで、デプロイ単位（AgentCore の Runtime、ECS のサービスなど）をどう切るかは運用要件で選ぶ。どちらも同じ成果物と同じ CDK コンストラクトで実現し、差は Runtime ごとの `runtime.toml`（`enabled_agents` など）と、それを選ぶ `STRANDS_RUNTIME_CONFIG` だけにする。

| 方式 | 構成 | 向くケース |
|---|---|---|
| 単一 Runtime | 1 つの Runtime に全エージェント。クライアントは `agent` で選ぶ | エージェント数が少ない、権限・ライフサイクル設定が共通、まとめて更新したい |
| エージェント単位の Runtime | 同じイメージから Runtime を複数作り、各 Runtime の `enabled_agents` で 1 体（または 1 グループ）に絞る | IAM 権限やアイドル時間を分けたい、個別にバージョン管理・ロールバックしたい、負荷や課金を分離したい |

CDK スタックは Runtime の一覧（名前、読む `runtime.toml`、環境変数、ライフサイクル設定）をパラメーター化し、単一 Runtime は要素 1 つの特殊形として扱う。
エージェント単位に分けた場合、Runtime 間の連携は `subagents`（同じプロセス内で構築）ではなく A2A または `AgentCoreClient` 経由になる（[§8](#8-オーケストレーションとエージェント連携)）。

### 11.6 定義の配置方式（焼き込み / マウント / S3 同期）

ランタイムは `STRANDS_RUNTIME_CONFIG` が指す `runtime.toml` と、その `agents_dir` 配下を**起動時に**読んで検証する。
配置方式の選択肢は次のとおり。表の「更新の反映」「起動時ロードとの相性」は AgentCore Runtime の仕様（2026-10-22 調査、[付録 C](#付録-c-agentcore-runtime-のファイルシステムに関する調査結果)）を基準に書いているが、汎用コンテナ基盤ではマウントが起動時から見えるため「定義ディレクトリのマウント」も成立する。それでも既定を焼き込みにする理由（成果物とバージョンの対応、`validate` を通ったものだけが出る）は基盤に依らない。

| 方式 | 構成 | 更新の反映 | 不変性 | 起動時ロードとの相性 | 追加コード |
|---|---|---|---|---|---|
| **焼き込み（コンテナ）** | `runtime.toml` と `agents/` をイメージに含める | 再ビルドと再デプロイ（約 30 秒、V2 はスナップショット再作成で数分） | 高い。Runtime バージョン = 定義のスナップショット | 良い | なし |
| **焼き込み（Direct Code Deploy）** | 同じ内容を zip にして S3 へ。`UpdateAgentRuntime` で新バージョン | **約 10 秒**（AWS の公表値）。バージョンは不変のまま | 高い | 良い | なし（CDK の `fromCodeAsset`） |
| **デプロイ差分のマウント** | 定義は焼き込み、Runtime ごとの小さな `runtime.toml` だけを外に置く | — | 定義は不変 | **不可**。マウントは呼び出し時にしか接続されず、起動時に `runtime.toml` を読めない。差分ファイルも焼き込むか環境変数（`STRANDS_RUNTIME_CONFIG` でイメージ内の別ファイルを選ぶ）で表す | なし |
| **定義ディレクトリのマウント（S3 Files / EFS）** | `agents/` をマウント先（`/mnt/<name>`）に置く | S3 の変更が数秒で見える（NFSv4.2、close-to-open 一貫性） | 低い。運用でパスにリリース ID を含める | **不可（現設計）**。起動時には空。最初の呼び出し時に読む遅延ロードへ変更すれば可能。VPC モード必須、実行ロールに `s3files:ClientMount` 等 | 中（遅延ロード、再読込、VPC 構成） |
| **起動時 S3 同期** | `agents_dir = "s3://..."` を起動時に同期 | V1: 新規 microVM ごと。**V2: スナップショットに凍結されるため再デプロイまで変わらない** | V2 では焼き込みと同等 | V1 のみ意味がある | 小 |
| **Configuration Bundles（AWS ネイティブ）** | プロンプトやモデル ID を制御プレーンの不変バンドルに置き、リクエストの `baggage` で版を指定。`BedrockAgentCoreContext.get_config_bundle()` で取得 | バンドルの新版発行。A/B テストは Gateway が注入 | 高い（版が不変） | 良い（リクエスト単位） | 中。TOML 定義の一部キーを上書きする統合層が必要 |

**判断**

- **既定は焼き込み**。AgentCore 上で更新速度が課題になる場合は、コンテナではなく **Direct Code Deploy** を使う。これが「不変性を保ちつつ速く更新する」正解で、ランタイムの変更は要らない。
- **マウント方式は採用しない**。起動時ロードと噛み合わず、遅延ロードへの変更は「起動時に全定義を検証して失敗なら起動しない」安全弁を失う。VPC 必須という運用コストも大きい。
- **同じイメージから複数 Runtime を作る**ときは、Runtime ごとの `runtime.toml` を**すべてイメージに焼き込み**、`STRANDS_RUNTIME_CONFIG=/app/deploy/<name>/runtime.toml` で選ぶ。環境変数は小さなパス 1 つで、V2 の 1.5〜2.5 KB 制限にも収まる。
- **Configuration Bundles** は、運用者がプロンプトやモデルをリクエスト単位で差し替える AWS ネイティブの経路として有望（クライアントではなく運用者の制御チャネルなので、ペイロードでの上書き禁止と矛盾しない）。TOML 定義のどのキーを上書き可能にするかの設計が必要で、フェーズ 3 の検討項目とする。

### 11.7 環境ごとの差分まとめ

| 項目 | ローカル | Docker | 汎用コンテナ基盤 | AgentCore |
|---|---|---|---|---|
| 起動 | `strands-runtime serve` | 同じイメージ | 同じイメージ | 同じイメージ（または code zip） |
| バインド | `127.0.0.1:8080` | `0.0.0.0:8080` | `0.0.0.0:8080` | `0.0.0.0:8080` |
| セッション | `local` | `s3` または `local`（検証用） | `s3`（または永続ボリュームの `local`） | `s3` または `agentcore_memory` |
| 認証 | なし | なし | 前段（LB / API Gateway） | SigV4 / JWT |
| セッション ID | ペイロードまたはヘッダー | 同左 | 同左 | ヘッダー（AgentCore が付与） |
| トレース | `OTEL_TRACES_EXPORTER` で任意 | 同左 | `OTEL_*` でコレクターへ | ADOT 自動 |
| AWS 認証情報 | ローカルプロファイル | 環境変数 / マウント | 基盤のロール / 不要 | 実行ロール |

---

## 12. クライアント

独立配布の `strands-agents-runtime-client`（import 名 `strands_runtime_client`）を提供し、Lambda などから利用する。
ソースは `packages/client` に置き、ランタイムと uv workspace のロックファイルを共有する。
イベント型は `strands_runtime_client.events` に置き、サーバーもこのパッケージに依存する。Python 3.10 以上で動作する。

```python
from strands_runtime_client import AgentCoreClient, HttpClient

client = AgentCoreClient(runtime_arn=os.environ["AGENT_RUNTIME_ARN"])   # boto3 bedrock-agentcore
# client = HttpClient("http://localhost:8080")                           # ローカル / Docker

for event in client.stream(agent="support-bot", messages=[{"role": "user", "content": "..."}], session_id=sid, actor_id="user-42"):
    if event.type == "text":
        print(event.text, end="")

result = client.invoke(agent="support-bot", messages=[{"role": "user", "content": "..."}], session_id=sid, actor_id="user-42")
print(result.text, result.structured_output)
```

ジョブ（[§7.6](#76-長時間実行ジョブと進捗)）は `submit()` で投入し、返る `JobHandle` で操作する。別プロセスからは `client.job(session_id, job_id, actor_id="user-42")` で同じハンドルを得る。

```python
job = client.submit(agent="researcher", messages=[{"role": "user", "content": "..."}], session_id=sid, actor_id="user-42")   # mode="job"。すぐ返る
print(job.status().state)                                                # mode="status"
for event in job.follow(after=last_seq):                                 # mode="follow"。切断したら after を進めて呼び直す
    last_seq = event.seq
result = job.result(wait=True)                                           # 終端まで追従して result / error を返す
job.cancel()                                                             # mode="cancel"。冪等
```

- 2 つのクライアントは同じインターフェース（`stream()` / `invoke()` / `submit()`）と同じイベント型を持つ。
- `AgentCoreClient` は `invoke_agent_runtime(agentRuntimeArn, runtimeSessionId, payload, qualifier="DEFAULT")` を呼び、`text/event-stream` なら `iter_lines()` で `data:` 行を解析する。`424 RuntimeClientError` や `409 RetryableConflictException` は型付き例外にする（後者は boto3 がリトライする）。
- `qualifier` でエンドポイント（バージョン固定）を選べるようにする。
- 必須依存は `pydantic` のみ。HTTP 用の `[http]` は `httpx`、AgentCore 用の `[agentcore]` は `boto3` を追加する。
  各トランスポートの依存はクライアント生成時に読み込むため、片方だけのインストールでも公開 API を import できる。
  `uv run python scripts/dev.py client-check` で wheel を core / http / agentcore の独立環境に導入し、重い依存がないことと基本動作を確認する。

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

### 13.2 ローカル CI

CI サービスや AWS アカウントがなくても進められるよう、検査はすべて `make` の目標として定義し、CI を載せるときはそれを呼ぶだけにする。

- `make check` = `ruff format --check` → `ruff check` → `mypy --strict` → `pytest` → `strands-runtime validate --mcp`（`runtime.toml` と `examples/feature-check`）。
- `make hooks` で `pre-commit` を導入し、コミット時に lint と型、プッシュ時にテストと `validate` を強制する。設定は `.pre-commit-config.yaml` で版管理する。
- `make integ` は実モデルの結合テスト（`.env` のキーが必要、無ければスキップ）。
- S3 セッションバックエンドは `moto` のプロセス内モックで検証する（`tests/test_s3_session.py`）。MinIO は Docker Hub からの配布を終了し、LocalStack はライセンストークンが必須になったため、外部の S3 互換ストアには依存しない。実バケットでの確認はフェーズ 2。

### 13.3 テスト方針

外部から観測可能な振る舞いを検証する。

- **定義ローダー**: 妥当な定義の読み込み、未知キー・型不一致・名前不一致・循環参照の拒否、`${VAR}` 展開。
- **Factory**: 偽の `Model`（`strands.models.Model` のテスト用実装）を注入し、ツール・MCP・スキル・サブエージェント・セッションが `Agent` に正しく配線されることを `Agent` の公開属性（`tool_names`、`system_prompt`、`session_manager` の型）で確認する。
- **サーバー契約**: Starlette の `TestClient` で `/invocations` と `/ping` を叩き、SSE のイベント列と非ストリーミング JSON を検証する。モデルは偽物。
- **クライアント**: SSE パーサーと boto3 レスポンスの解析を、記録済みレスポンスで検証する。
- **結合テスト**（任意実行、`tests_integ/`）: 実モデル（Bedrock）と Docker を使い、ローカルコンテナに対して 1 往復する。

### 13.4 ロードマップ

フェーズの区切りは下表のとおり。機能面の中長期的な候補と優先順位は [docs/roadmap.md](roadmap.md) に分けて記す（同系統ランタイムの比較、Strands の未活用機能、2026 年の潮流、運用要件の 4 観点）。

| フェーズ | 内容 | 完了条件 |
|---|---|---|
| 1（実装済み） | 定義フォーマット、ローダー、Factory（モデル・ツール・MCP・Skills・local/s3/agentcore_memory セッション・subagents・structured_output・limits・interventions・コンテキスト注入）、Invoker（イベント正規化、キャンセル、タイムアウト、同一セッション排他、base64 メディア、HITL 再開）、HTTP サーバー、CLI（`validate --mcp`/`list`/`run`/`serve`）、Dockerfile、`HttpClient`、`AgentCoreClient`、`tests_integ/`（実モデル） | ローカルで実モデル（OpenRouter）によりサブエージェント・Skills・MCP・構造化出力・limits・invocation_state・vended ツールを確認済み。Docker・AWS 上は未確認 |
| 2 | CDK スタック（実装済み、`synth` とテストまで。実デプロイは未実施）、`s3` セッション（moto で検証済み）/`agentcore_memory` セッションと `AgentCoreClient` の実環境検証、Lambda サンプル、観測性の確認、Direct Code Deploy の評価 | AgentCore 上で Lambda から呼び出し、CloudWatch でトレースを確認できる |
| 3 | 長期記憶（`[memory]`）、Graph/Swarm/A2A の `kind` 拡張、AgentCore Configuration Bundles による運用者向けのプロンプト・モデル差し替え | 設計 §8 の拡張点のうち優先度の高いものが利用可能 |

---

## 14. 未決事項

レビューで決めたい点。括弧内は本文書での仮の採用値。

1. **パッケージ名・CLI 名**（`strands-agents-runtime` / `strands_runtime` / `strands-runtime`）。公式の `strands-*` 名との混同を避けるなら別名（例: `agentrt`）も候補。
2. **既定モデル**（`bedrock/global.anthropic.claude-opus-5`、strands-harness の既定と同じ）。利用リージョンで有効な Bedrock モデル ID を確認する必要がある。
3. **コンテナ基盤での既定セッションバックエンド**（`s3`）。AgentCore では AgentCore Memory を標準にするかをコスト（イベント課金）と長期記憶の必要性で判断。
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

### ADR-6: 環境変数で設定値を上書きしない

- **背景**: 「同じイメージから Runtime を複数作る」構成では公開範囲（`enabled_agents`）がデプロイごとに変わる。一度 `STRANDS_RUNTIME_ENABLED_AGENTS` による上書きを実装したが、`runtime.toml` の中でこのキーだけが環境変数で差し替わる例外になった。
- **決定**: 環境変数が決めるのは「どの `runtime.toml` を読むか」（`STRANDS_RUNTIME_CONFIG`）だけとし、値の上書きは持たない。デプロイごとの差分はファイルを分けて表す。シークレットや環境依存の値は、TOML が `${VAR}` で変数名を参照する形に限る。
- **理由**: 設定ファイルは「何を動かすか」、環境変数は「どこで・誰の資格で動かすか」という線引きを一貫させる。上書き可能なキーが 1 つあると「あのキーも」と増え、構成の所在が分散する。ファイルを分ける方式は追加コードなしで既に成立しており、`validate` の対象にもなる。

### ADR-7: 非同期実行はペイロードの `mode` で表し、ジョブの状態はセッションストレージを正とする

- **背景**: 1 時間を超える実行は、どの基盤でも接続を保ったままでは完走できない。一方、本ランタイムの HTTP 契約は `/invocations` と `/ping` だけで、AgentCore ではこれ以外のルートもクライアントのヘッダーも使えない。プロセス内の状態は microVM の終了やレプリカの入れ替えで消える。
- **決定**: ジョブの受付・状態・追従・結果・キャンセルをすべて `/invocations` のペイロードの `mode` で表す。進捗と結果はセッションストレージのジャーナルに書き、プロセス内のタスクは「実行する場所」にすぎないものとする。進捗の語彙は同期ストリームのイベント型をそのまま使い、連番 `seq` で再開できるようにする。再起動後の自動再開、コールバック配送、同一セッションのキューイングは持たない。
- **理由**: エンドポイントを増やさなければ、ローカル・コンテナ基盤・AgentCore で同じクライアントが動く。ジャーナルをストレージに置けば `status` / `result` / `follow` はどのプロセスでも答えられ、プロセスが消えたジョブも心拍の途絶から確定できる。先行事例（OpenAI background mode、LangGraph runs、A2A Task、MCP Tasks）が共有する最小の語彙に合わせることで、将来 A2A の Task に対応づけるときも状態の変換だけで済む。

### ADR-8: Graph / Swarm は呼び出し 1 回で完結するワークフローとして扱い、条件はコード参照で書く

- **背景**: Strands の Graph / Swarm はノードのエージェントに `SessionManager` を付けられず、オーケストレーターの状態（進んだノード、中断状態）だけを保存する。Swarm はノード実行のたびに履歴をリセットする。エッジ条件は呼び出し可能オブジェクトである。
- **決定**: Graph / Swarm を会話を持たないワークフローと位置づけ、セッションは HITL の再開と進捗の観測にだけ使う。定義はノードを定義名で参照し、条件はコード参照、よくある合流だけ `wait_for = "all"` で宣言する。失敗は `error`、停止と中断は `result` の `stop_reason` に正規化し、ノードの遷移は `node_start` / `node_stop` / `handoff` で流す。
- **理由**: SDK が持たない会話の継続をランタイムで模倣すると、ノードの履歴管理という SDK 側の設計判断と衝突する。会話が要るなら単体エージェントが Graph を呼ぶ構成（将来のラッパー）か、クライアント側で前回の結果をプロンプトに含める方が素直。条件に式言語を入れると評価器と文法を抱え込むので、他の拡張点と同じコード参照に揃え、頻出パターンだけ宣言化する。

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

## 付録 C. AgentCore Runtime のファイルシステムに関する調査結果

2026-10-22 時点。公式ドキュメントで確認できた事実（確認できなかった点は明記）。

- `filesystemConfigurations` は Runtime あたり 0〜5 件。各要素は `sessionStorage { mountPath }`、`s3FilesAccessPoint { accessPointArn, mountPath }`、`efsAccessPoint { accessPointArn, mountPath }`、`capacityProviderVolume`（Instances 専用）のいずれか 1 つ。S3 Files は最大 2、EFS は最大 2、セッションストレージは最大 1。`mountPath` は `/mnt/<name>` の 1 階層のみ。読み取り専用の指定はなく、IAM（`ClientWrite` を付けない）かアクセスポイントの UID/GID で制御する。
- **マウントは新しいセッション ID の最初の呼び出し時に接続され、初期化中は利用できない**（公式ガイドの明記）。接続は並列で各 30 秒、1 つでも失敗すると呼び出しは HTTP 424。
- S3 Files はライブビュー（NFSv4.2、close-to-open 一貫性）。バケット側の変更は通常数秒で見え、ファイルシステム側の書き込みは約 60 秒でまとめて S3 へ書き出される。EFS も同様にライブ。
- セッションストレージ（Preview）はセッション単位で空から始まり、14 日のアイドルと**Runtime のバージョン更新ごとに**リセットされる。定義の配置には不向き。
- S3 Files / EFS マウントは **VPC モード必須**。TCP 2049、同一 VPC・同一アカウント、ランタイムのサブネットと重なる AZ にマウントターゲット、DNS 解決が必要。実行ロールには `s3files:ClientMount` / `s3files:ClientWrite` / `s3files:GetAccessPoint`（EFS は `elasticfilesystem:ClientMount` / `ClientWrite`）。
- Runtime V2 はスナップショットを最初の健全な `/ping` 時点で取り、`app.run()` より前にモジュールスコープで行ったことはすべて凍結される。AWS は「再デプロイなしに変えるものを起動時に計算しない」ことを推奨。V2 とマウントの組み合わせについての明文はなく、マウントが呼び出し時接続である以上、起動時に読んだ内容は空か凍結になると推定（未確認）。
- IaC: CloudFormation は `FilesystemConfigurations` と `PlatformVersion` に対応。CDK の L2 `Runtime` は `platformVersion` に対応するがファイルシステム設定は L2 に無く、L1 `CfnRuntime` が必要（推定）。Terraform は `filesystem_configuration` と `platform_version` に対応。
- Direct Code Deploy は zip 250 MB（展開後 750 MB）まで。AWS ブログは更新を約 10 秒（コンテナは約 30 秒）と公表。`UpdateAgentRuntime` ごとに新バージョンが作られ `DEFAULT` エンドポイントが移る。V2 ではスナップショット再作成のため数分。
- Configuration Bundles: Runtime ARN に紐づく不変の版付き key/value（最大 5 MB の JSON）。リクエストの W3C `baggage`（`aws.agentcore.configbundle_arn` / `aws.agentcore.configbundle_version`）で版を指定し、`BedrockAgentCoreContext.get_config_bundle()`（bedrock-agentcore 1.8 以降）で取得・キャッシュ。Strands では `BeforeModelCallEvent` フックでの適用が推奨されている。
- 環境変数: 50 件まで、合計サイズは V1 で 4 KB、V2 は Direct Code Deploy 1.5 KB / コンテナ 2.5 KB（V1 水準へ引き上げ予定と明記）。
- 価格: S3 Files / EFS マウントの AgentCore 固有の料金は見つからず（各サービスの通常料金と推定）。S3 Files / EFS マウントの GA/Preview 表記も確認できず。

参照: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html 、https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_FilesystemConfiguration.html 、https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html 、https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/configuration-bundles.html 、https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/configuration-bundles-runtime.html 、https://aws.amazon.com/blogs/machine-learning/iterate-faster-with-amazon-bedrock-agentcore-runtime-direct-code-deployment 、https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-performance.html
