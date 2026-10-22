# Strands Agents Runtime

[Strands Agents SDK](https://strandsagents.com/) 上で、エージェントを **TOML（設定）と Markdown（プロンプト）で宣言**し、
同じ成果物をローカル・Docker・Amazon Bedrock AgentCore Runtime で動かすためのランタイムです。
設計の詳細は [docs/design.md](docs/design.md) を参照してください。

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
  server/               # AgentCore 契約の HTTP サーバー（ローカルでも同じ）
  client/               # HttpClient / AgentCoreClient
  events.py             # サーバーとクライアントで共有するイベントスキーマ
```

## 使い方

```bash
uv sync
uv run strands-runtime validate                     # 定義・コード参照・SKILL.md の検証（--mcp で MCP 接続も）
uv run strands-runtime list                         # 公開エージェント一覧
uv run strands-runtime run assistant "今日は何日？"   # サーバーなしで 1 回実行
uv run strands-runtime serve                        # http://127.0.0.1:8080
```

```bash
curl -N -X POST localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"agent": "assistant", "prompt": "こんにちは", "session_id": "0f1e2d3c-0000-4000-8000-000000000001"}'
```

Python クライアント:

```python
from strands_runtime.client import HttpClient, AgentCoreClient, new_session_id

client = HttpClient("http://127.0.0.1:8080")
# client = AgentCoreClient(runtime_arn="arn:aws:bedrock-agentcore:...:runtime/...")

sid = new_session_id()
for event in client.stream("こんにちは", agent="assistant", session_id=sid):
    if event.type == "text":
        print(event.text, end="")
result = client.invoke("続きをお願い", agent="assistant", session_id=sid)
print(result.text, result.metrics)
```

## OpenAI / OpenAI 互換 API で動かす

モデルは `runtime.toml` の `default_id`（環境変数 `RUNTIME_DEFAULT_MODEL` で上書き可）か、各 `agent.toml` の `[model] id` で選びます。

```bash
export OPENAI_API_KEY=...                      # キーは環境変数で渡す（TOML には書かない）
export RUNTIME_DEFAULT_MODEL=openai-chat/gpt-4.1-mini
# OpenAI 互換サーバーなら加えて:
# export OPENAI_BASE_URL=http://localhost:11434/v1
uv run --extra openai strands-runtime run assistant "今日は何日？"
```

| id の形式 | 使う API | 用途 |
|---|---|---|
| `openai/<model>` | OpenAI Responses API（strands-harness 経由、effort 変換あり） | OpenAI 本家 |
| `openai-chat/<model>` | Chat Completions（`strands.models.OpenAIModel`） | OpenAI 互換サーバー全般。`[model] base_url` で接続先、`api_key_env` でキーの環境変数名を指定可 |

OpenAI 互換モデルはコンテキスト長を SDK が知らないため、`[model.config] context_window_limit` にモデルの値を書いてください（未指定だと SDK が 200,000 と仮定し、呼び出しごとに警告を出します）。

公式と互換サーバーを同時に使うときは、エージェントごとに接続先とキーの環境変数を分けます。

```toml
# agents/local-llm/agent.toml
[model]
id = "openai-chat/meta-llama/Llama-3.1-8B-Instruct"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_LLM_API_KEY"      # 値ではなく変数名。OPENAI_API_KEY は公式側に残せる
```

## 機能確認用の定義（examples/feature-check）

サブエージェント、Agent Skills、MCP、構造化出力、`limits`、`invocation_state`、SDK 同梱ツール、別モデルを 1 つずつ試せる定義を置いています。
キーは gitignore 済みの `.env` に置き、`uv run --env-file` で子プロセスにだけ渡します。

```bash
echo 'OPENROUTER_API_KEY=...' > .env
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml list
uv run --env-file .env strands-runtime -c examples/feature-check/runtime.toml run skilled "俳句スタイルで秋について" --json
```

## 呼び出しの契約（要点）

- `POST /invocations` の主なフィールド: `agent`、`prompt`（文字列または content block 配列。画像等の `source.bytes` は base64）、`session_id`、`actor_id`、`stream`、`invocation_state`、`include_reasoning`、`tool_result_detail`（`preview` / `full`）。
- イベント: `start` → `text` / `reasoning` / `tool_use` / `tool_result` … → `result`（`stop_reason`、`text`、`structured_output`、`metrics`）または `error`。
- `stop_reason` は Strands の値に加え、`structured_output`（構造化回答）と `timeout`（`[limits] timeout_seconds` 超過）をランタイムが返す。
- 同じ `session_id` を同時に呼ぶと 409。中断（`interrupt`）は `prompt = [{"interruptResponse": {...}}]` で再開。
- 実モデルの結合テスト: `uv run --env-file .env pytest tests_integ -q`

## Docker

同じイメージをローカルと Amazon Bedrock AgentCore Runtime で使います（linux/arm64、コンテナ内は `0.0.0.0:8080`）。

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

AgentCore Runtime へは同じイメージを ECR に置いてデプロイします（CDK スタックはフェーズ 2 で追加予定）。

## 環境変数

一覧と説明は [.env.example](.env.example) にまとめています（`cp .env.example .env` して値を埋める）。
ランタイム自身は `.env` を読まず、`uv run --env-file .env` や `docker run --env-file .env` で渡します。

| 変数 | 用途 |
|---|---|
| `STRANDS_RUNTIME_CONFIG` | `runtime.toml` のパス（既定 `./runtime.toml`） |
| `STRANDS_RUNTIME_ENABLED_AGENTS` | 公開エージェントをカンマ区切りで上書き（同じイメージから複数 Runtime を作るとき） |

サーバーのログは標準エラー出力に加え、`runtime.toml` の `[server] log_file` を設定するとファイルにも書かれます（Python の警告も含む）。
| `OPENROUTER_API_KEY` など | `[model] api_key_env` で名指しした変数 |
| `OTEL_TRACES_EXPORTER` | `otlp` / `console`。未設定ならトレースを送らない。`opentelemetry-instrument` 配下では無視 |
| `AWS_REGION` ほか | Bedrock / S3 / AgentCore Memory の資格情報とリージョン |

TOML 内の文字列では `${VAR}` と `${VAR:-default}` が展開されます。
