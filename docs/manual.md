# 利用マニュアル

Strands Agents Runtime は、TOML の設定と Markdown のプロンプトからエージェントを読み込み、CLI または HTTP で実行します。
以下のコマンドはリポジトリのルートで実行してください。

## 1. 最初のエージェントを動かす

uv を用意し、依存パッケージをインストールします。Python は `.python-version` に従って uv が用意します。
Windows の PowerShell、macOS、Linux で共通のコマンドを使えます。

```text
uv sync --locked
```

[.env.example](../.env.example) を `.env` にコピーして編集します。PowerShell では `Copy-Item .env.example .env`、Bash では `cp .env.example .env` を使えます。
付属の [assistant](../agents/assistant/agent.toml) と機能確認用 examples は OpenRouter を使うため、`OPENROUTER_API_KEY` を設定してください。
別の接続先を使う場合は、次節のようにエージェントのモデル設定を変更します。

```text
uv run --env-file .env strands-runtime validate
uv run --env-file .env strands-runtime list
uv run --env-file .env strands-runtime run assistant "今日は何日？"
```

`validate` は定義・コード参照・スキルを検証し、モデルへの推論リクエストは送りません。`validate --mcp` は MCP サーバーへの接続も確認します。
`run` はモデルを呼び出します。イベントを JSON 行で見るには末尾に `--json` を付けます。
ランタイム自身は `.env` を読まないため、利用時は `uv run --env-file .env` で渡します。

## 2. 設定とモデルを変更する

| ファイル | 編集する内容 |
|---|---|
| [runtime.toml](../runtime.toml) | エージェントの置き場、公開範囲、既定モデル、セッション保存先、サーバー設定 |
| [agents/assistant/agent.toml](../agents/assistant/agent.toml) | エージェント名、モデル、ツール、スキル、実行上限 |
| [agents/assistant/prompt.md](../agents/assistant/prompt.md) | システムプロンプト |

`agent.toml` の `name` はそのディレクトリ名と一致させます。
`tools`・`skills`・`subagents` などは、最初の `[model]` などのテーブル見出しより前に書いてください。
相対パスは設定ファイルがあるディレクトリを基準に解決されます。`./tools/example.py` のような `/` 区切りの表記は OS 間で共有できます。

モデルは `[model] id` で選びます。`id` と `module` を省略した場合は、`runtime.toml` の `[model] default_id` を使います。

| `id` の形式 | 接続する API |
|---|---|
| `openai/<model>` | Responses API。OpenAI、Azure OpenAI v1、Responses API 互換サーバー |
| `openai-chat/<model>` | Chat Completions。OpenRouter などの互換サーバー |
| `bedrock/<model-id>` | Amazon Bedrock。AWS の資格情報・リージョンを利用 |

OpenAI 系の接続先は `base_url`、キーを持つ環境変数名は `api_key_env` で指定できます。
省略時は `OPENAI_BASE_URL` / `OPENAI_API_KEY` が使われます。API キーの値は TOML に書かず `.env` などで渡します。

Azure OpenAI v1 の設定例（`agent.toml` のモデル設定を置き換える）:

```toml
[model]
id = "openai/my-deployment"
base_url = "https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1/"
api_key_env = "AZURE_OPENAI_API_KEY"

[model.config]
params = {}
```

`my-deployment` は Azure のデプロイ名、`AZURE_OPENAI_API_KEY` は `.env` に設定する変数名です。
`params = {}` は推論パラメータを送らず、デプロイ側の既定値を使う指定です。
推論の強さやフォールバックの設定は [README のモデル設定](../README.md#openai--openai-互換-api-で動かす) を参照してください。

## 3. examples を選んで試す

### 機能ごとの実行例

[examples/feature-check](../examples/feature-check) は、各機能を小さな定義で試すためのサンプル集です。
`-c` をサブコマンドの前に置くと、このサンプル用の `runtime.toml` を読み込めます。

```text
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml list
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml validate --mcp
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml run skilled "秋の俳句を作って" --json
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml run mcp-user "7 と 8 を掛けて" --json
```

上の `run` のエージェント名とプロンプトを変えると、次の機能を確認できます。

| 試したい機能 | 定義と内容 |
|---|---|
| サブエージェント | [coordinator](../examples/feature-check/agents/coordinator/agent.toml) が summarizer に要約を委譲 |
| Agent Skills | [skilled](../examples/feature-check/agents/skilled/agent.toml) が俳句用スキルを利用 |
| MCP | [mcp-user](../examples/feature-check/agents/mcp-user/agent.toml) が付属のローカル MCP サーバーを利用 |
| 構造化出力 | [extractor](../examples/feature-check/agents/extractor/agent.toml) が文章から連絡先を抽出 |
| SDK 同梱ツール | [fetcher](../examples/feature-check/agents/fetcher/agent.toml) が HTTP リクエストを実行 |
| 呼び出しごとの状態 | [tenant-aware](../examples/feature-check/agents/tenant-aware/agent.toml) のツールが `invocation_state` を参照 |
| モデル切り替え | [fallback](../examples/feature-check/agents/fallback/agent.toml) が意図的に失敗する主モデルから別モデルへ切り替え |
| 順次処理・差し戻し | [writing-flow](../examples/feature-check/agents/writing-flow/agent.toml) が見出し・本文・審査を Graph で実行 |
| 並列処理・合流 | [parallel-flow](../examples/feature-check/agents/parallel-flow/agent.toml) が本文と要約を並列に作成して審査へ合流 |
| 担当への引き継ぎ | [helpdesk](../examples/feature-check/agents/helpdesk/agent.toml) が Swarm で受付から請求・技術担当へ引き継ぎ |
| 入れ子の構成 | [nested-flow](../examples/feature-check/agents/nested-flow/agent.toml) が Swarm を Graph のノードとして利用 |
| 承認・実行上限 | [approval-flow](../examples/feature-check/agents/approval-flow/agent.toml) は人の承認待ち、[limited](../examples/feature-check/agents/limited/agent.toml) と [loop-flow](../examples/feature-check/agents/loop-flow/agent.toml) は上限到達を確認 |

承認待ちや上限到達もサンプルの確認対象です。すべての例が通常の回答で完了するわけではありません。
承認待ちは同じ `session_id` に対して `interruptResponse` を送って再開します。

### 設定を調べるための例

[examples/reference](../examples/reference) は、設定項目を説明コメント付きで確認するためのリファレンスです。
[runtime.toml](../examples/reference/runtime.toml)、[単体エージェント](../examples/reference/agents/reference/agent.toml)、[Graph](../examples/reference/agents/flow/agent.toml)、[Swarm](../examples/reference/agents/crew/agent.toml) から、必要な項目を自分の定義へ取り入れてください。
ツール・MCP・スキル・Plugin・Cedar ポリシーなどの補助ファイルも同梱しています。

```text
uv run --env-file .env strands-runtime -c examples/reference/runtime.toml validate --mcp
```

## 4. HTTP で呼び出す

```text
uv run --env-file .env strands-runtime serve --port 8080
```

`GET /ping` で稼働確認、`POST /invocations` で実行できます。
ここではポートを明示しています。省略すると設定ファイルの値を使い、付属のルート設定は `18080`、feature-check は `18081` です。

Python クライアントだけを使う場合は、呼び出し側の仮想環境へ `uv pip install "./packages/client[http]"` で導入できます（リポジトリのルートで実行）。
AgentCore 用は `[agentcore]` を選びます。ランタイム・Strands SDK・CDK は不要です。
wheel での配布手順と詳しい使い方は [クライアントのREADME](../packages/client/README.md) を参照してください。

Python クライアントの例:

```python
from strands_runtime_client import HttpClient, new_session_id

client = HttpClient("http://127.0.0.1:8080")
sid = new_session_id()
result = client.invoke("こんにちは", agent="assistant", session_id=sid)
print(result.text)
```

同じ `session_id` を指定すると会話を続けられます。既定の付属設定は履歴をローカルに保存します。
`prompt` の代わりに `messages` も使えます。セッションを指定するときは1件だけ渡してください。

```python
result = client.invoke(
    messages=[{"role": "user", "content": "続きを教えて"}],
    agent="assistant",
    session_id=sid,
)
```

`session_id` を省略すると `user` / `assistant` の会話全体を `messages` に渡せます。この場合、保存済み履歴は読み書きしません。
`prompt` と `messages` の同時指定、空配列、セッション指定時の複数メッセージは通信前にエラーになります。
`model="openai/my-deployment"` で、定義と同じプロバイダーのモデルを今回だけ指定できます。接続先や認証設定は定義を引き継ぎます。
システムプロンプトはエージェント定義で管理します。
全履歴入力の例、モデル上書きの範囲、Graph / Swarm の制約は [クライアントの入力形式](../packages/client/README.md#messages-とモデル指定) を参照してください。

生成条件や実行上限もリクエストで指定できます。省略した項目はエージェント定義やランタイムの既定値を使います。

```python
result = client.invoke(
    "要点をまとめて",
    max_output_tokens=2048,
    reasoning_effort="low",
    timeout_seconds=60,
    max_turns=5,
    max_total_tokens=20000,
)
```

`temperature`・`top_p`・`response_format`・`tool_choice` も指定できます。
`response_format=Answer` のようにPydanticモデルクラスを渡すと、正常完了時の検証済み結果を `result.parsed` で取得できます。
スキーマを指定済みのエージェントでは `response_format` の上書きを拒否します。
実行上限は定義側の上限との小さい方を使い、生成条件の指定は今回だけ適用します。
プロバイダー別の対応範囲とJSON出力の例は [呼び出しごとのパラメータ](../packages/client/README.md#呼び出しごとのパラメータ) を参照してください。

逐次出力には `client.stream(...)`、長時間の実行には `client.submit(...)` を使います。
ジョブは `job.status()` で状態、`job.follow()` でイベント、`job.result(wait=True)` で結果を取得し、`job.cancel()` で停止を要求できます。
ジョブにはlocal・s3、または外部実行管理DB（`jobs.storage="execution"`）が必要です。PowerShell・curl・ジョブの呼び出し例は [README の使い方](../README.md#使い方) を参照してください。

## 5. コンテナと開発用コマンド

Docker を使う場合は次のコマンドでビルド・起動できます。Windows では Linux コンテナを使用します。

```text
uv run python scripts/dev.py docker-build
uv run python scripts/dev.py docker-run
```

既定のビルド対象は `linux/arm64` です。x86_64 向けのビルドやトレース設定は [README の Docker 手順](../README.md#docker) を参照してください。
デプロイ用設定の例は [deploy/example/runtime.toml](../deploy/example/runtime.toml)、AgentCore の宣言は [infra/deployments.toml](../infra/deployments.toml) にあります。
このデプロイ用設定では S3 を使い、`SESSION_BUCKET` は CDK が作成して渡します。

変更後の確認には、次の共通コマンドを使います。型検査・テストには CDK 用の Node.js も必要です。

```text
uv run python scripts/dev.py check       # lint・型検査・単体テスト・定義と MCP の検証
uv run python scripts/dev.py client-check # クライアント wheel の独立インストール検証（check にも含まれる）
uv run python scripts/dev.py integ       # .env のキーで実モデルを呼ぶ結合テスト
uv run python scripts/dev.py synth       # AWS アカウントなしで CDK テンプレートを生成
```

ログは端末と設定した `[server] log_file` に出力されます。
環境変数の一覧は [.env.example](../.env.example)、トレースの可視化は [README の観測性](../README.md#観測性opentelemetry) を参照してください。

## 6. 同時実行と負荷の調整

設定は役割ごとに分けます。

| 設定 | 役割 | backend / storage |
|---|---|---|
| `[session]` | 同一セッションの実行中・待機中の予約 | `none`（既定）、`local`、`dynamodb`、`cosmos`、`agentcore` |
| `[session.history]` | 会話履歴・エージェント状態の保存 | `none`（既定）、`local`、`s3`、`dynamodb`、`cosmos`、`agentcore_memory` |
| `[execution]` | ワーカーごとの実行枠・待機枠 | 件数と待機時間を指定 |
| `[jobs]` | バックグラウンド実行の記録 | `inherit`（既定）、`local`、`s3`、`session`、`none` |

`session.backend="none"` はサーバ側の会話管理を無効にし、履歴の保存も無効にします。
同じIDを同時に渡しても409にはならず、毎回 `messages` で必要な履歴を送れます。
実行枠・待機枠の制限は有効です。
それ以外では、同じセッションが実行中・待機中なら、SSE開始・ジョブ受付前にHTTP 409で拒否します。
実行枠の待機上限を超えた場合や待機枠が満杯の場合は、HTTP 503と `Retry-After: 1` を返します。

`agent.toml` では `[session.history]` だけを上書きでき、省略時の `backend="inherit"` はランタイムの履歴設定を継承します。
実行の排他方式はランタイム全体で共通です。
`session.backend="none"` のときにエージェント定義から履歴を有効にする設定も拒否します。

### 配置とバックエンドの選択

配置場所を指定する `environment` 設定はありません。
ローカルからCosmos DBを使うことも、ECSからCosmos DBを使うことも可能です。

| backend | 排他が共有される範囲 | 制約 |
|---|---|---|
| `none` | セッションの排他なし | 履歴は `none` のみ |
| `local` | 同じホストの同じSQLiteファイルを使うプロセス | ローカルディスクを使う。複数ワーカーに対応 |
| `dynamodb` / `cosmos` | 同じDB・namespaceを使う全インスタンス | 履歴・ジョブに `local` は使えない |
| `agentcore` | 同じruntimeSessionIdの実行環境 | 1ワーカー、AgentCoreのセッションヘッダーが必須 |

ローカルのDockerも、1コンテナ内で使うなら `local` で構いません。
複数コンテナ・ホストをまたぐ場合は外部DBを使い、全インスタンスのDBと `namespace` を揃えます。
実際のレプリカ数は自動検出しません。ローリング更新で新旧コンテナが重なる場合も考慮してください。
異なるサービスは別の `namespace` にします。履歴側にも独立した `namespace` があります。
起動は `strands-runtime serve` を使い、外側のUvicorn/Gunicorn等でワーカーを追加しないでください。

AgentCoreは同じ `runtimeSessionId` を同じ実行環境へ配送するため、アプリ内のメモリで排他できます。
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` ヘッダーを必須とし、ペイロードの `session_id` を指定するなら同じ値にします。不一致は400です。
実行中409の判定はこのランタイムが行います。
`backend="agentcore"` だけでは、AgentCore MemoryやSession Storageへの保存は有効になりません。
履歴は `[session.history]` で明示します。
ジョブ実行中はSDKの非同期タスク登録で `/ping` を `HealthyBusy` にします。
他の配置の `/ping` はそのワーカーの状態を返し、全コンテナの使用状況を集計しません。
配置例は [deploy/example/runtime.toml](../deploy/example/runtime.toml) を参照してください。

### ローカルで複数ワーカーを使う

```toml
[server]
workers = 4

[session]
backend = "local"
sqlite_path = "./.runtime/execution.sqlite3"

[session.history]
backend = "local"
dir = "./.runtime/sessions"
```

SQLiteはローカルディスクに置き、全ワーカーで同じパスを共有します。
ネットワーク共有、EFS、Azure Filesなどの共有マウントには置かないでください。UNCパスは拒否します。
短いトランザクションでセッションを予約し、エージェントの実行中はDBの書き込みトランザクションを保持しません。
PIDとプロセス開始時刻で所有者を確認し、所有プロセスが終了済みの場合だけ残った予約を回収します。
生存中の所有者や生死を確認できない所有者から、時間経過だけで予約を奪いません。
同じ設定を使うCLIプロセス間でも排他が効きます。

### DynamoDBで排他し、S3に履歴を保存する

```toml
[server]
workers = 2

[session]
backend = "dynamodb"
namespace = "my-service"

[session.dynamodb]
table = "agent-runtime-sessions"
region = "ap-northeast-1"

[session.history]
backend = "s3"
bucket = "my-agent-history"
prefix = "my-service/"
region = "ap-northeast-1"
```

DynamoDBは、文字列型のパーティションキー `id` のみを持つ専用テーブルを事前に作成します。
ソートキー、追加インデックス、Global Tables、TTLは使いません。
ランタイムはテーブル作成や権限付与を行いません。
boto3の認証チェーン（ECSタスクロール等）を使用します。
`DescribeTable`, `DescribeTimeToLive`, `GetItem`, `PutItem`, `DeleteItem` を許可します。
履歴にもDynamoDBを使う場合は、チェックポイントの列挙用に `Scan` も許可します。
排他DBの構造・TTL・レプリカ設定は起動時に検証します。
条件付きPutで取得し、所有者トークンを条件にしたDeleteで解放します。

履歴もDynamoDBに置く場合は、上の `[session.history]` を次に置き換えます。
排他と履歴でテーブルを共用しても、分けても構いません。

```toml
[session.history]
backend = "dynamodb"
namespace = "my-service"

[session.history.dynamodb]
table = "agent-runtime-history"
region = "ap-northeast-1"
```

### Cosmos DBで排他と履歴を管理する

ランタイムの `cosmos` extraをインストールします（クライアントパッケージには不要です）。
Dockerイメージではビルド引数 `EXTRAS` に `cosmos` を含めてください。

```text
uv sync --extra cosmos
```

```toml
[server]
workers = 2

[session]
backend = "cosmos"
namespace = "my-service"

[session.cosmos]
endpoint = "https://your-account.documents.azure.com:443/"
database = "agents"
container = "sessions"
# key = "${COSMOS_KEY}"  # 省略時はDefaultAzureCredential（Managed Identity等）

[session.history]
backend = "cosmos"
namespace = "my-service"

[session.history.cosmos]
endpoint = "https://your-account.documents.azure.com:443/"
database = "agents"
container = "history"
```

Cosmos DB for NoSQLのコンテナを事前に作成します。
パーティションキーは `/id`、書き込みリージョンは1つ、アカウントの整合性は `Strong`、自動TTL削除は無効にします。
予約の一意な作成と、ETagを使った条件付き削除で競合を検出します。
アカウント・コンテナのメタデータ取得とアイテムの読み書き・削除を許可してください。
履歴のチェックポイント列挙にはクエリの権限も必要です。
排他DBは起動時、履歴DBは初回アクセス時に設定を検証します。

### S3とNoSQLの履歴保存の違い

どちらもStrandsのスナップショットを保存し、会話履歴とGraph / Swarmの途中状態を復元します。
S3はスナップショットをオブジェクトとして保存します。
DynamoDB / Cosmos DBではサイズ制限を超える履歴も保存できるよう、小さなチャンクと索引に分けます。
全チャンクを書いてから索引を切り替えるので、保存途中の履歴は復元しません。
読み込み時はサイズとチェックサムを検証します。
会話のメッセージ単位の検索・部分取得APIを提供するものではありません。

NoSQLでは1回の保存に複数の読み書きが発生します。
通常の復元はキーによる取得ですが、チェックポイント列挙はDynamoDBではScan、Cosmos DBではパーティションをまたぐクエリを使います。
大きな履歴の保存コストを抑えたい場合はS3、既存のDynamoDB / Cosmos DBで運用を揃えたい場合はNoSQLが選択肢になります。
自動でS3へ退避する機能はありません。

NoSQLの旧チャンクは保存成功後に削除します。
書き込み失敗・応答喪失時には、未参照チャンクが残る場合があります。
一律のTTLで削除せず、削除が必要なら書き込みを停止したうえで索引の参照先を確認してください。
履歴DBにも上記と同じ整合性・TTLの制約を適用します。

### ジョブ記録と異常終了時の復旧

`jobs.storage="local"` は指定ディレクトリ内の `jobs.sqlite3` を使います。
`inherit` でローカルの履歴保存先を継承した場合も同じです。会話履歴のファイルとは独立しています。
`jobs.storage="s3"` はS3、`session` は排他用DynamoDB / Cosmos DB、`none` はジョブ無効です。
`inherit` は `[session.history]` のlocal / S3 / NoSQLを使い、Memory / noneではジョブが無効になります。
`storage="session"` はエンコード前の1記録が240 KiB以下という制約があります。
大きなツール結果にはS3、またはNoSQL履歴を継承する `inherit`（チャンク分割対応）を使います。
別ワーカーからのジョブ観測は共有ジャーナルを使い、停止要求は専用の記録を所有者の心拍で拾います（通常は最大30秒＋通信時間）。

外部DBの予約には自動失効を設けません。
通信断や心拍の遅れだけでは、所有コンテナが終了したか、まだツールを実行しているかを判断できないためです。
DB障害や取得結果が不明な場合は新規実行を始めず、503にします。
コンテナの強制終了、取得・解放の応答喪失などで予約が残った場合、以降の同じセッションは409のままになります。
`JobLost` はジョブ観測上の状態であり、外部DBの予約を解除するものではありません。

次の手順で復旧します。

1. 予約を表示し、所有コンテナ・プロセスを確認する。
2. その所有者が停止済みであることを配置基盤で確認する。生存中なら先に停止する。
3. 表示された `owner` を指定して解除する。所有者が変わっていた場合は解除しない。

```text
strands-runtime -c runtime.toml reservation SESSION_ID
strands-runtime -c runtime.toml reservation SESSION_ID --release-owner OWNER_TOKEN
```

これは管理者向けCLIで、HTTPの公開APIには解放操作を設けません。
自動的なジョブ再実行・再開は行いません。実行済みの外部ツールの操作を巻き戻すものでもありません。
保存されたジョブ記録の保持期間・削除は運用側で管理し、予約のレコードを一括削除しないでください。

### 実行枠・スレッド・TCP設定

以下は既定値です。実際のモデル・ツール・メモリ容量に合わせて調整してください。

```toml
[execution]
max_concurrent = 16
max_queued = 64
queue_timeout_seconds = 30

[server]
workers = 1
backlog = 2048
http_threads = 64
io_threads = 32
timeout_keep_alive = 5
timeout_graceful_shutdown = 30
timeout_worker_healthcheck = 30
```

| 設定 | 対象 |
|---|---|
| `workers` | 同一インスタンス内のワーカープロセス数。Windows / Linuxともspawnで起動する |
| `max_concurrent` | ワーカーごとのトップレベル実行数。runとjobで共有し、後始末まで保持する |
| `max_queued` | ワーカーごとの実行待ち件数。0なら満杯時に即503 |
| `queue_timeout_seconds` | 実行枠の待機上限。DBアクセス時間・実行時間は含まない |
| `backlog` | OSのTCP接続受付待ち。エージェントの実行待ちとは別 |
| `http_threads` | HTTP側の同期処理・SSE転送が使うAnyIOスレッド上限 |
| `io_threads` | 実行ループの既定executorの上限。同期ツール・S3・実行管理DBのI/O等 |
| `timeout_keep_alive` | アイドルHTTP接続の維持時間。生成時間は制限しない |
| `timeout_graceful_shutdown` | UvicornがHTTP処理の終了を待つ時間。ジョブ停止には別途猶予がある |
| `timeout_worker_healthcheck` | 親がワーカーの死活確認応答を待つ時間 |

例えば `workers=4`, `max_concurrent=16` なら、そのインスタンスの実行上限は64件です。
各ワーカーは独立したモデル・MCP接続・プールを持ちます。HTTP配送の偏りで一部だけ満杯になる場合があります。
`WEB_CONCURRENCY` より設定ファイルの `workers` を優先します。
実行枠が埋まった場合の別セッションの待機順はワーカー内のFIFOです。同一セッションを順番待ちさせる機能はありません。
`status` / `follow` / `result` / `cancel` と `/ping` は実行枠を消費しません。

キューはプロセス内の一時状態で、永続ジョブキューではありません。
`submit()` も実行枠を取得してから受付結果を返します。クライアントのタイムアウトには待機時間も考慮してください。
待機キャンセル・切断時は後始末して予約を解放します。プロセス停止による外部予約の残留は上記の復旧手順で扱います。
Pythonから同じRuntimeを直接呼ぶ場合も、実行操作は同じイベントループで行ってください。

Graph / Swarm・サブエージェントは親の枠の内側で動くため、`max_concurrent` はLLMへのリクエスト数そのものではありません。
HTTPと実行はSDKの別イベントループで動き、I/O待ちは並行できますが、CPU主体のPython処理はワーカー分割が必要になる場合があります。
独自ツールが作るスレッドや外部ライブラリ独自のプールには、上記の上限は適用されません。

```text
uv run --extra cosmos pytest tests/test_admission.py tests/test_server_concurrency.py tests/test_multiprocess.py tests/test_execution_backends.py tests/test_nosql_history.py -q
```

上記は模擬モデルによる並行HTTP処理、実プロセス間の競合、DynamoDBのエミュレーション、Cosmos SDK境界の模擬応答を検証します。
本番サービスの性能やフェイルオーバーを検証する負荷試験ではありません。
関連仕様: [AgentCoreのセッション](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)、[DynamoDB条件式](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)、[Cosmos DBの並行処理](https://learn.microsoft.com/en-us/azure/cosmos-db/faq)、[Uvicorn](https://uvicorn.dev/settings/)。
