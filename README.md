# Strands Agents Runtime

[Strands Agents SDK](https://strandsagents.com/) 上で、エージェントを **TOML（設定）と Markdown（プロンプト）で宣言**し、
同じ成果物をローカル・Docker・任意のコンテナ基盤（ECS、Azure Container Apps など）・Amazon Bedrock AgentCore Runtime で動かすためのランタイムです。
セットアップ、モデル設定、examples の選び方は [利用マニュアル](docs/manual.md) を参照してください。

## 構成

```
runtime.toml            # ランタイム設定（既定モデル、セッション、公開するエージェント）
agents/
  assistant/
    agent.toml          # エージェント定義（モデル、ツール、MCP、スキル、制限 …）
    prompt.md           # システムプロンプト
    tools/              # 任意: @tool で書いた Python ツール
    skills/             # 任意: Agent Skills（<name>/SKILL.md）
src/strands_runtime/
  definitions/          # TOML スキーマ、ローダー、レジストリ
  factory/              # 定義 → strands.Agent
  invoke/               # 実行とイベント正規化
  server/               # HTTP サーバー（POST /invocations, GET /ping。AgentCore 互換の汎用契約）
packages/client/        # 独立配布の軽量クライアント（strands_runtime_client）と共有イベント型
```

## 開発（ローカル CI）

CI サービスがなくても、Windows（PowerShell）・macOS・Linux で同じ検査を手元で回せます。
uv と、CDK の合成・テストに必要な Node.js を用意してください。Python は `.python-version` に従って uv が用意します。

```text
uv sync --locked
uv run python scripts/dev.py check         # ruff → mypy → pytest → validate（コミット前の一式）
uv run python scripts/dev.py hooks         # コミット時に lint/型、プッシュ時に test/validate
uv run python scripts/dev.py integ         # 実モデルの結合テスト（.env の OPENROUTER_API_KEY が必要）
uv run python scripts/dev.py docker-build
uv run python scripts/dev.py docker-run
```

`make` がある環境では `make check` なども同じ Python コマンドを呼びます。
`uv run python scripts/dev.py --help` でタスク一覧を確認できます。
設定ファイルやイメージ名は `--config runtime.toml` / `--image strands-runtime:dev` で指定できます。
Docker 関連タスクには Docker が必要です（Windows では Linux コンテナを使用）。

`[session] backend = "s3"` は moto（S3 のプロセス内モック）で往復を検証しています（`tests/test_s3_session.py`）。実バケットでの確認はフェーズ 2。

## 使い方

```bash
uv sync
uv run strands-runtime validate                     # 定義・コード参照・SKILL.md の検証（--mcp で MCP 接続も）
uv run strands-runtime list                         # 公開エージェント一覧
uv run strands-runtime run assistant "今日は何日？"   # サーバーなしで 1 回実行
uv run strands-runtime serve --port 8080            # http://127.0.0.1:8080
```

### Windows（PowerShell）

上記の `uv` コマンドは PowerShell からそのまま実行できます。Bash や `make`、仮想環境の手動アクティベートは不要です。
API キーは `.env.example` をコピーして編集し、`--env-file` で渡します。

```powershell
Copy-Item .env.example .env
# .env の値を編集してから実行
uv run --env-file .env strands-runtime run assistant "今日は何日？"
uv run --env-file .env strands-runtime serve --port 8080
```

HTTP 呼び出しは、別の PowerShell で次のように実行できます。

```powershell
$body = @{ agent = "assistant"; prompt = "こんにちは"; stream = $false } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:8080/invocations -Method Post `
  -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

TOML のパスは `./agents` のように `/` で書けば OS 間で共有できます。
Windows の絶対パスを書く場合は、`dir = 'C:\work\sessions'` のようにシングルクォートを使うとバックスラッシュのエスケープが不要です。
コード参照も `'C:\work\schemas.py:Answer'` の形式で指定できます。
日本語・絵文字を含む CLI 出力をファイルや他のプロセスに渡す場合は、`$env:PYTHONUTF8 = "1"` を設定してください。

### Bash での HTTP 呼び出し

```bash
curl -N -X POST localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"agent": "assistant", "prompt": "こんにちは", "session_id": "0f1e2d3c-0000-4000-8000-000000000001"}'
```

Python クライアントは [packages/client](packages/client) で独立配布できます。
呼び出し側にランタイムや Strands SDK を入れる必要はありません。
Python 3.10 以上で利用でき、HTTP 用は `pydantic` と `httpx`、AgentCore 用は `pydantic` と `boto3` が依存です。

リポジトリを取得して、呼び出し側の仮想環境にインストールします（ランタイム開発環境用の `uv sync` は不要）。

```text
uv venv
uv pip install "./packages/client[http]"
# AgentCore 用: uv pip install "./packages/client[agentcore]"
```

配布用 wheel の作成と使い方は [クライアントのREADME](packages/client/README.md) を参照してください。
import 先は `strands_runtime_client`、イベント型は `strands_runtime_client.events` です。

Python クライアントの例:

```python
from strands_runtime_client import HttpClient, AgentCoreClient, new_session_id

client = HttpClient("http://127.0.0.1:8080")
# client = AgentCoreClient(runtime_arn="arn:aws:bedrock-agentcore:...:runtime/...")

sid = new_session_id()
for event in client.stream("こんにちは", agent="assistant", session_id=sid):
    if event.type == "text":
        print(event.text, end="")
result = client.invoke("続きをお願い", agent="assistant", session_id=sid)
print(result.text, result.metrics)
```

長時間の実行は接続を保たずにジョブとして投入し、後から進捗と結果を取れます。

```python
job = client.submit("…時間のかかる依頼…", agent="assistant")      # mode="job"。すぐ返る
print(job.status().state)                                          # running / completed / failed …
for event in job.follow(after=0):                                  # ジャーナルを再生し、実行中なら追従する
    print(event.seq, event.type)
result = job.result(wait=True)                                     # 終端まで待って result（失敗なら例外）
job.cancel()                                                       # 冪等
```

```bash
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
  -d '{"agent": "assistant", "prompt": "…", "mode": "job", "session_id": "<36 文字以上>"}'
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
  -d '{"mode": "status", "session_id": "<同じ ID>"}'          # follow / result / cancel も同じ形
```

## OpenAI / OpenAI 互換 API で動かす

モデルは各 `agent.toml` の `[model] id` で選びます（`[model]` を省いたエージェントは `runtime.toml` の `default_id`）。

```toml
# agents/assistant/agent.toml
[model]
id = "openai-chat/gpt-4.1-mini"                 # キーは環境変数 OPENAI_API_KEY（TOML には書かない）
# OpenAI 互換サーバーなら:
# id = "openai-chat/<server が返すモデル名>"
# base_url = "http://localhost:11434/v1"
# api_key_env = "LOCAL_LLM_API_KEY"
```

```bash
uv run --env-file .env strands-runtime run assistant "今日は何日？"
```

| id の形式 | 使う API | 用途 |
|---|---|---|
| `openai/<model>` | OpenAI Responses API（strands-harness 経由、effort 変換あり） | OpenAI、Azure OpenAI v1、Responses API 互換サーバー |
| `openai-chat/<model>` | Chat Completions（`strands.models.OpenAIModel`） | Chat Completions 互換サーバー |

どちらも `[model] base_url` で接続先、`api_key_env` でキーを持つ環境変数名を指定できます。
省略時は OpenAI SDK が `OPENAI_BASE_URL` / `OPENAI_API_KEY` を読みます。
エージェントの `id` を省略して `runtime.toml` の `default_id` を使う場合も、接続先とキーを指定できます。

Azure OpenAI の例:

```toml
[model]
id = "openai/my-deployment"                     # Azure に作成したデプロイ名
base_url = "https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1/"
api_key_env = "AZURE_OPENAI_API_KEY"

[model.config]
params = {}                                    # 推論パラメータを送らず、デプロイ側の既定値を使う
```

`.env` に `AZURE_OPENAI_API_KEY=...` を設定し、`uv run --env-file .env strands-runtime run assistant "こんにちは"` で実行します。
`base_url` は `/responses` を含めず `/openai/v1/` までを指定し、`id` の `openai/` より後ろにはデプロイ名を書きます。
この設定は API キー認証の v1 API 向けで、`api-version` は不要です（[Microsoft の公式手順](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle?tabs=key)）。
Microsoft Entra ID の自動トークン更新が必要な場合は、既存の `model.module` でクライアント引数にトークン取得関数を渡すモデルファクトリを用意します。

`openai/` の `effort = "auto"` は既存のハーネスに従って `high`、`off` は `none` に変換されます。
上の `params = {}` はこの推論設定を上書きします。推論を調整する場合は `params = {}` を外し、`[model] effort = "low"` など、デプロイが対応する値を指定してください。

失敗時に別のモデルへ切り替えるには `fallbacks` を書きます（Strands の `ModelRouter`）。プロンプトキャッシュの調整は `[model.cache]`（Strands の `CacheConfig` のキーそのまま）です。
接続先とキーは同じプロバイダー同士（`openai/` 同士、または `openai-chat/` 同士）の場合だけ主モデルから継承します。
別の接続先へ切り替える候補では、テーブル形式で `base_url` と `api_key_env` をそれぞれ指定してください。

```toml
[model]
id = "openai-chat/deepseek/deepseek-v4.1-flash-1017"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
fallbacks = ["openai-chat/xiaomi/mimo-v2.6-pro"]   # 文字列は接続先とキーを主から継承。テーブルで個別指定も可
[model.cache]
ttl = "1h"
```

`result.metrics` にはサブエージェントを含む合計トークン数と、`agents` にエージェントごとの内訳（モデル、呼び出し回数、トークン数）、`model_failures`（フォールバックした回数）が入ります。

OpenAI 互換モデルはコンテキスト長を SDK が知らないため、`[model.config] context_window_limit` にモデルの値を書いてください（未指定だと SDK が 200,000 と仮定し、呼び出しごとに警告を出します）。

公式と互換サーバーを同時に使うときは、エージェントごとに接続先とキーの環境変数を分けます。

```toml
# agents/local-llm/agent.toml
[model]
id = "openai-chat/meta-llama/Llama-3.1-8B-Instruct"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_LLM_API_KEY"      # 値ではなく変数名。OPENAI_API_KEY は公式側に残せる
```

## 設定項目のリファレンス（examples/reference）

`runtime.toml` と `agent.toml` の全設定項目を、説明コメント付きで書いた定義が [examples/reference](examples/reference) にある（単体エージェントは `agents/reference`、Graph は `agents/flow`、Swarm は `agents/crew`）。
補助ファイル（ツール、スキル、Cedar ポリシー、Plugin、注入関数、構造化出力のスキーマ、MCP サーバー）も揃えてあり、`make check` の `validate --mcp` が毎回通す。
設定を追加するときは、スキーマ（`definitions/schema.py`）とこの定義を一緒に更新する。

## 機能確認用の定義（examples/feature-check）

サブエージェント、Agent Skills、MCP、構造化出力、`limits`、`invocation_state`、SDK 同梱ツール、別モデルを 1 つずつ試せる定義を置いています。
キーは gitignore 済みの `.env` に置き、`uv run --env-file` で子プロセスにだけ渡します。

```bash
echo 'OPENROUTER_API_KEY=...' > .env
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml list
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml run skilled "俳句スタイルで秋について" --json
```

## 呼び出しの契約（要点）

- `POST /invocations` の主なフィールド: `agent`、`prompt` または `messages`、`model`、`session_id`、`actor_id`、`stream`、`invocation_state`、`include_reasoning`、`tool_result_detail`（`preview` / `full`）。
- `prompt` は文字列または content block 配列、`messages` は `role`（`user` / `assistant`）と `content` の配列。`content` は文字列または Strands 形式の content block 配列で、画像等の `source.bytes` は base64。両フィールドの同時指定は不可。
- `session_id` 指定時の `messages` は1件のみ。Python クライアントは通信前に検証する。`messages` の全履歴を送る場合は `session_id` を省略し、保存済み履歴は読み書きしない。履歴継続の指定は JSON 本文の `session_id` で行う。
- `model` は `provider/model-id` 形式で、同じプロバイダーのモデルを今回だけ上書きする。接続設定を引き継ぎ、指定時はフォールバックを使わない。`model.module` と Graph / Swarm のモデル上書きは不可。入力例と詳しい制約は [クライアントのREADME](packages/client/README.md#messages-とモデル指定) を参照。
- 生成条件は `max_output_tokens`・`temperature`・`top_p`・`reasoning_effort`・`response_format`・`tool_choice`、実行上限は `timeout_seconds`・`max_turns`・`max_total_tokens` で指定できる。未指定は定義・ランタイムの既定値を継承し、実行上限は定義側の上限を超えない。対応範囲と使用例は [呼び出しごとのパラメータ](packages/client/README.md#呼び出しごとのパラメータ) を参照。
- イベント: `start` → `text` / `reasoning` / `tool_use` / `tool_result` … → `result`（`stop_reason`、`text`、`structured_output`、`metrics`）または `error`。
- `response_format` にはPydanticモデルクラスも指定でき、正常完了時の検証済み結果を `result.parsed` で取得できる。スキーマ指定済みのエージェントでは出力形式の上書きを拒否する。
- `stop_reason` は Strands の値に加え、`structured_output`（構造化回答）と `timeout`（`[limits] timeout_seconds` 超過）をランタイムが返す。
- 同じ `session_id` を同時に呼ぶと 409。中断（`interrupt`）は `prompt = [{"interruptResponse": {...}}]` で再開。
- `kind = "graph"` / `"swarm"` の定義で Graph / Swarm を宣言し、`prompt` または1件の `user` メッセージで呼べる。ノードの遷移は `node_start` / `node_stop` / `handoff` イベント、ノード別の結果は `result.nodes`。
- `mode`（`run` / `job` / `status` / `follow` / `result` / `cancel`）でジョブを操作する。ジョブのイベントには `seq` / `ts` が付き、`follow` は `after` から再生する。`include_subagents = true` でサブエージェント内のイベントを `agent` / `parent` 付きで流す。
- ジョブのジャーナルは `[session]` の `local` / `s3` に置く（`[jobs] storage` で別の置き場も指定できる）。`agentcore_memory` / `none` の構成では `[jobs]` を明示しない限りジョブは無効。
- 実モデルの結合テスト: `uv run --env-file .env pytest tests_integ -q`

## 観測性（OpenTelemetry）

Strands がモデル呼び出し・ツール呼び出し・エージェント実行を OTel GenAI 規約（`gen_ai.*`）のスパンとして出すので、
OTLP を受ける LLM 可視化ツール（Arize Phoenix、Langfuse、LangSmith、Datadog、CloudWatch GenAI Observability など）でそのまま見られます。
ランタイムはルートのスパンに呼び出し全体の合計（`strands_runtime.usage.*`）と `session.id` / `user.id` を付けます。

```bash
make phoenix-up      # Arize Phoenix を Docker で起動（UI: http://localhost:6006）
OTEL_TRACES_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006 OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
  uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml run coordinator "要約して: ..."
```

PowerShell では環境変数を `$env:` で設定します。

```powershell
uv run python scripts/dev.py phoenix-up
$env:OTEL_TRACES_EXPORTER = "otlp"
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:6006"
$env:OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf"
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml run coordinator "要約して: ..."
uv run python scripts/dev.py phoenix-down
```

## Docker

同じイメージをローカル、任意のコンテナ基盤、Amazon Bedrock AgentCore Runtime で使います（既定 linux/arm64、コンテナ内は `0.0.0.0:8080`）。

```bash
docker buildx build --platform linux/arm64 -t strands-runtime --load .
# エージェントが使うモデルプロバイダーの extras はビルド引数で指定（既定: agentcore-otel openai）
# docker buildx build --platform linux/arm64 --build-arg EXTRAS="agentcore-otel openai anthropic" -t strands-runtime --load .

# ローカル実行: キーは .env から渡し、OTel コレクターがないので SDK を無効化する
docker run --rm -p 8080:8080 --env-file .env -e OTEL_SDK_DISABLED=true strands-runtime
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' -d '{"prompt":"こんにちは","stream":false}'
```

- 起動コマンドは `opentelemetry-instrument strands-runtime serve --port 8080`。AgentCore 上では Runtime が注入する ADOT の環境変数でトレースが CloudWatch に送られるため、`OTEL_SDK_DISABLED` は付けません。
- `runtime.toml` の `[server] port` はコマンドラインの `--port 8080` で上書きされます。`log_file` はコンテナ内の `/app/.runtime/server.log` です。
- Bedrock を使う場合は `-e AWS_REGION=... -v ~/.aws:/home/runtime/.aws:ro` などで資格情報を渡します。

## 他のコンテナ基盤へのデプロイ（ECS、Azure Container Apps、Cloud Run など）

契約が `POST /invocations` と `GET /ping` だけなので、イメージを 1 サービスとして動かすだけです。
ヘルスチェックは `/ping`、セッション ID はペイロードの `session_id` で足ります（AgentCore のヘッダーは任意）。
セッションの保存先は `s3` か永続ボリュームの `local`、認証は前段のロードバランサーや API Gateway、トレースは標準の `OTEL_*` でコレクターへ送ります。
x86_64 の基盤では `--platform linux/amd64` でビルドしてください。

## AgentCore へのデプロイ（CDK）

`infra/deployments.toml` に Runtime を宣言し、CDK（Python）が同じイメージから Runtime を作ります。
合成（`synth`）は AWS アカウントなしで動き、テンプレートはテストで検証しています。

```toml
# infra/deployments.toml
[[runtime]]
name = "assistant"
config = "deploy/example/runtime.toml"     # Runtime ごとの runtime.toml（イメージに焼き込み、STRANDS_RUNTIME_CONFIG で選ぶ）
platform_version = "V2"
[runtime.secrets]
OPENROUTER_API_KEY = "strands-runtime/openrouter"   # Secrets Manager のシークレット名。デプロイ時に解決
```

```bash
make synth                      # cdk.out にテンプレートを出力（アカウント不要）
npx aws-cdk deploy              # 実デプロイ（AWS アカウントと CDK bootstrap が必要）
```

スタックは、Runtime の `runtime.toml` が `backend = "s3"` なら S3 バケットを、`agentcore_memory` なら AgentCore Memory を作成し、
環境変数（`SESSION_BUCKET` / `AGENTCORE_MEMORY_ID`）と実行ロールの権限（S3、Bedrock モデル呼び出し）を配線します。

## 環境変数

一覧と説明は [.env.example](.env.example) にまとめています（`cp .env.example .env` して値を埋める）。
ランタイム自身は `.env` を読まず、`uv run --env-file .env` や `docker run --env-file .env` で渡します。

| 変数 | 用途 |
|---|---|
| `STRANDS_RUNTIME_CONFIG` | 読む `runtime.toml`（既定 `./runtime.toml`）。デプロイごとの差分は `runtime.toml` を分けて、この変数で選ぶ |

サーバーのログは標準エラー出力に加え、`runtime.toml` の `[server] log_file` を設定するとファイルにも書かれます（Python の警告も含む）。
| `OPENROUTER_API_KEY` など | `[model] api_key_env` で名指しした変数 |
| `OTEL_TRACES_EXPORTER` | `otlp` / `console`。未設定ならトレースを送らない。`opentelemetry-instrument` 配下では無視 |
| `AWS_REGION` ほか | Bedrock / S3 / AgentCore Memory の資格情報とリージョン |

TOML 内の文字列では `${VAR}` と `${VAR:-default}` が展開されます。
