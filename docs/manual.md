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

Python クライアントの例:

```python
from strands_runtime.client import HttpClient, new_session_id

client = HttpClient("http://127.0.0.1:8080")
sid = new_session_id()
result = client.invoke("こんにちは", agent="assistant", session_id=sid)
print(result.text)
```

同じ `session_id` を指定すると会話を続けられます。既定の付属設定は履歴をローカルに保存します。
逐次出力には `client.stream(...)`、長時間の実行には `client.submit(...)` を使います。
ジョブは `job.status()` で状態、`job.follow()` でイベント、`job.result(wait=True)` で結果を取得し、`job.cancel()` で停止を要求できます。
ジョブには local または s3 の保存先が必要です。PowerShell・curl・ジョブの呼び出し例は [README の使い方](../README.md#使い方) を参照してください。

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
uv run python scripts/dev.py integ       # .env のキーで実モデルを呼ぶ結合テスト
uv run python scripts/dev.py synth       # AWS アカウントなしで CDK テンプレートを生成
```

ログは端末と設定した `[server] log_file` に出力されます。
環境変数の一覧は [.env.example](../.env.example)、トレースの可視化は [README の観測性](../README.md#観測性opentelemetry) を参照してください。
