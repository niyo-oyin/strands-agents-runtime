# Strands Agents Runtime Client

起動済みの Strands Agents Runtime を呼び出す独立した Python パッケージです。Python 3.10 以上で利用できます。
Strands SDK、モデルプロバイダー、HTTP サーバー、CDK は不要です。

| 用途 | 必須依存と追加依存 | import |
|---|---|---|
| イベント解析・共通インターフェース | `pydantic` | `strands_runtime_client.events` |
| HTTP / Docker / 任意のコンテナ基盤 | `pydantic` + `[http]` の `httpx` | `from strands_runtime_client import HttpClient` |
| Amazon Bedrock AgentCore | `pydantic` + `[agentcore]` の `boto3` | `from strands_runtime_client import AgentCoreClient` |

HTTP と AgentCore の依存は、それぞれのクライアントを生成するときだけ読み込みます。
必要な extra が未インストールの場合は、追加すべき extra 名を含むエラーになります。

## インストール

リポジトリを取得し、そのルートから対象の仮想環境にインストールします。
`uv sync` はランタイムの開発環境も構築するため、クライアントだけの導入には次のコマンドを使います。

```text
uv venv
uv pip install "./packages/client[http]"
```

AgentCore 用は `./packages/client[agentcore]`、両方を使う場合は `./packages/client[http,agentcore]` に置き換えます。
イベント型だけなら `uv pip install ./packages/client` で十分です。

別のアプリケーションへ配布する wheel は、リポジトリのルートで作成できます。

```text
uv build --package strands-agents-runtime-client --out-dir dist/client
```

配布先では、そのアプリケーションの環境にインストールします。

```text
uv pip install "./strands_agents_runtime_client-0.1.0-py3-none-any.whl[http]"
```

公開レジストリへの登録は別途必要です。上の手順はレジストリでの公開を前提にしません。

## HTTP で呼び出す

```python
from strands_runtime_client import HttpClient, new_session_id

with HttpClient("http://127.0.0.1:8080") as client:
    sid = new_session_id()
    for event in client.stream(messages=[{"role": "user", "content": "こんにちは"}], agent="assistant", session_id=sid):
        if event.type == "text":
            print(event.text, end="")
    result = client.invoke(messages=[{"role": "user", "content": "続きをお願い"}], agent="assistant", session_id=sid)
    print(result.text, result.metrics.total_tokens)
```

## messages とモデル指定

`invoke()`・`stream()`・`submit()` は、キーワード引数 `messages` を必須とします。
`prompt` と位置引数による入力は受け付けません。
`messages` の各要素は `role`（`user` / `assistant`）と `content` を持つ辞書です。
`from strands_runtime_client import Message` で型付きのモデルも使えます。
`content` は文字列、または Strands 形式の content block 配列です。画像等の `source.bytes` は base64 文字列で渡します。
`system` / `developer` は受け付けず、システムプロンプトはサーバーのエージェント定義を使います。

```python
result = client.invoke(
    agent="assistant",
    messages=[
        {"role": "user", "content": "このライブラリの特徴は？"},
        {"role": "assistant", "content": "エージェントをAPIとして公開できます。"},
        {"role": "user", "content": "Azureでも使えますか？"},
    ],
    model="openai/my-deployment",
)
```

`model` はサーバーの設定と同じ `provider/model-id` 形式です。Azure では後半にデプロイ名を指定します。
省略時はエージェントの既定モデル・フォールバックを使います。
指定時は同じプロバイダーのモデルに今回だけ切り替え、フォールバックを使いません。
接続先・認証情報・推論設定などはエージェント定義を引き継ぎます。次の呼び出しやサブエージェントには影響しません。
`model.module` によるカスタムモデル、および Graph / Swarm へのモデル上書きは受け付けません。

### 呼び出しごとのパラメータ

次の項目は `invoke()`・`stream()`・`submit()` のキーワード引数、または HTTP の JSON 本文で指定できます。
すべて任意です。未指定または `None` / `null` の項目は、エージェント定義・ランタイム・プロバイダーの既定値を使います。
指定は今回だけに適用し、次の呼び出しやサブエージェントの設定は変更しません。

| パラメータ | 意味 |
|---|---|
| `max_output_tokens` | モデル呼び出し1回の出力上限。正の整数 |
| `temperature` | サンプリング温度。0〜2の範囲内で、接続モデルが対応する値 |
| `top_p` | サンプリングの確率しきい値。0〜1。通常は `temperature` とどちらかを調整 |
| `reasoning_effort` | 推論の強さ。`auto` / `off` / `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` のうち対応する値 |
| `response_format` | Pydanticモデルクラス、`{"type": "text"}`、`{"type": "json_object"}`、または下記の JSON Schema 指定 |
| `tool_choice` | `auto` / `none` / `required`、または `{"type": "function", "function": {"name": "ツール名"}}` |
| `timeout_seconds` | ツールを含む実行の制限時間（秒）。正の数。超過時に停止を要求し、後処理の猶予を経て打ち切る |
| `max_turns` | エージェント実行1回のモデル呼び出し回数の上限。正の整数 |
| `max_total_tokens` | エージェント実行1回の入力＋出力トークン合計の上限。正の整数 |

```python
result = client.invoke(
    messages=[{"role": "user", "content": "この文章を短く要約して"}],
    max_output_tokens=2048,
    reasoning_effort="low",
    timeout_seconds=60,
    max_turns=5,
    max_total_tokens=20000,
)
```

生成条件は指定した項目だけを上書きし、未指定のモデル設定は維持します。
`model` を指定しなければ、生成条件は定義済みのフォールバック候補にも適用します。
`timeout_seconds`・`max_turns`・`max_total_tokens` は、定義の `[limits]` やジョブの制限とのうち小さい値を使います。
既存の `limits.output_tokens` は引き続き有効です。これは複数回のモデル出力の合計を制限し、1回ごとの `max_output_tokens` とは別です。
トークン数・ターン数の上限はターン境界で判定するため、トークン数は最後のモデル呼び出しで超えることがあります。
同じセッションでも各リクエストで集計をやり直します。サブエージェントの使用量は、ルートエージェントのトークン上限には含みません。
クライアント生成時の `timeout` は HTTP 通信の待ち時間で、サーバーの `timeout_seconds` とは別です。

`tool_choice="none"` は実行中のモデル呼び出しすべてに適用します。
`required` と特定ツールの指定は最初に成功したモデル呼び出しまで適用し、その後は `auto` に戻します。
ツールの定義自体はサーバー側で管理します。未登録のツールは指定できません。

JSON Schema の例:

```python
import json

result = client.invoke(
    messages=[{"role": "user", "content": "回答をJSONで返して"}],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "Answer",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
    },
)
print(json.loads(result.text)["answer"])
```

`response_format` は接続先のネイティブ出力形式に渡し、出力は `result.text` に返します。
定義に `[structured_output]` がある場合、`response_format` による上書きは実行・ジョブ受付前にHTTP 400で拒否します。同じスキーマや `text` を指定しても拒否します。
`model.config.params` の `response_format` または `text.format` に `type="json_schema"` が設定されている場合も同様です。使用するフォールバック候補も検証します。
未指定時は既存の構造化出力を維持します。`[structured_output]` を使う定義では、構造化出力用ツールと競合する `tool_choice` の上書きも拒否します。
`json_object` を使う場合は、メッセージでもJSON出力を指示してください。

Pydanticモデルクラスを直接渡すこともできます。

```python
from pydantic import BaseModel


class Answer(BaseModel):
    answer: str
    confidence: float


result = client.invoke(messages=[{"role": "user", "content": "回答と確信度を返して"}], response_format=Answer)
assert isinstance(result.parsed, Answer)
print(result.parsed.answer, result.parsed.confidence)
```

クライアントがモデルをstrictなJSON Schemaに変換し、正常完了時（`stop_reason="end_turn"`）に `result.text` をPydanticで検証します。
検証済みインスタンスは `result.parsed` に入り、元のテキストも保持します。
JSONや型が不正な場合は `pydantic.ValidationError` を送出します。トークン上限・タイムアウト・中断時は解析せず、`parsed=None` と停止理由を返します。
辞書で `response_format` を指定した場合は自動解析せず、`result.text` を利用します。

`stream(..., response_format=Answer)` の最後の `ResultEvent`、`submit(..., response_format=Answer)` で得たハンドルの `result()` / `follow()` でも同じ検証を行います。
既存ジョブに再接続する場合は `client.job(session_id, job_id, response_format=Answer)` で解析用モデルを指定できます。
モデルクラスと `parsed` はクライアント内だけで保持し、通信やジョブの保存には含めません。

モデルクラスの指定は、ルートがobjectでフィールドが固定されたモデルが対象です。入れ子・配列・再帰モデル・nullableフィールドに対応します。
strict出力では、既定値のあるフィールドもすべて必須にし、各objectの追加キーを禁止します。
任意キーの `dict` フィールド、`extra="allow"`、配列をルートにした `RootModel` は通信前に拒否します。
それ以外の形式が必要ならJSON Schemaを辞書で指定してください。受理されるスキーマの範囲は接続先によります。

対応範囲:

- `openai` / `openai-chat` / `bedrock-mantle` / `litellm` は生成条件6項目を変換して接続先へ渡します。
- `bedrock` / `anthropic` / `google` は出力上限・温度・`top_p`・推論設定、`ollama` は出力上限・温度・`top_p` に対応します。
- `response_format`・`tool_choice` は OpenAI 系APIに対応する上記4プロバイダーのみです。実際に受理される値・スキーマはモデルや互換サーバーによります。
- 生成条件の上書きは `model.id` を使う単体エージェントが対象です。`model.module`（フォールバックを含む）には適用できません。
- Graph / Swarm では今回の追加項目のうち `timeout_seconds` のみ指定できます。

未対応の組み合わせはエラーになり、黙って無視しません。値の範囲や形式はクライアントが通信前に検証し、接続モデル固有の対応可否はサーバー・プロバイダー側で検証します。

### セッション・保存・所有者

| 指定 | 動作 |
|---|---|
| `session_id` なし、`store=False`（省略時も同じ） | 渡したmessagesだけで会話し、履歴を保存しない |
| `session_id` なし、`store=True` | 新しい会話として保存し、応答のsession_idを返す |
| `session_id` あり、`store=True`（省略時も同じ） | 保存済み会話を復元し、今回のやり取りも保存 |
| `session_id` あり、`store=False` | 保存済み会話を復元するが、今回のやり取りは保存しない |

保存にはサーバー側の履歴バックエンドが必要です。履歴がnoneの場合、明示的な `store=True` はエラーです。
`session_id` 指定時のmessagesは1件に限り、2件以上・空配列・不正なroleは通信前にエラーになります。
`stream()` も呼び出し時点で検証します。

保存・履歴の復元・ジョブ操作には `actor_id` が必要です。
省略できるのはサーバーの `[session] default_actor_id` が明示されている場合です。
通常の履歴バックエンドもactorごとに保存領域を分け、所有者が異なる会話・ジョブへのアクセスは403で拒否します。
同じ利用者の複数の会話には同じactorを渡し、別の会話には別のsession_idを使います。
共有サービスでは認証済み情報からactorを設定・検証してください。このフィールド自体は認証機能ではありません。

```python
first = client.invoke(
    messages=[{"role": "user", "content": "こんにちは"}],
    actor_id="user-42",
    store=True,
)
# 保存済み会話を参照するが、この質問と回答は会話に残さない。
result = client.invoke(
    messages=[{"role": "user", "content": "ここまでを要約して"}],
    session_id=first.session_id,
    actor_id="user-42",
    store=False,
)
```

`store` は会話履歴・エージェント状態への書き込みだけを制御します。
AgentCore Memoryの長期記憶取得は `[session.history.retrieval]` で別に設定し、actorが指定されていれば `store=False` でも利用できます。
actor未指定・既定actorなしの単発呼び出しは、匿名actorを作らず、Memoryの取得も行いません。
ログ・トレース・ジョブのジャーナル、ツール自身の外部書き込みは別管理です。

AgentCoreの配送用IDだけでは履歴復元を有効にしません。JSON本文の `session_id` が履歴継続の指定です。
IDなし・保存なしでも応答には実行識別用のIDが返りますが、次の呼び出しには必要なmessagesを渡してください。
ジョブのハンドルは受付時のactorを保持し、観測・キャンセルにも送ります。
再取得は `client.job(session_id, job_id, actor_id="user-42")` の形です。

Graph / Swarmは1件のuserメッセージだけを受け付けます。
中断から再開するには最初の実行から保存を有効にし、同じactor・sessionで次のように送ります。

```python
result = client.invoke(
    messages=[{"role": "user", "content": [{"interruptResponse": {"interruptId": interrupt_id, "response": "yes"}}]}],
    session_id=sid,
    actor_id="user-42",
)
```

## AgentCore で呼び出す

```python
import os

from strands_runtime_client import AgentCoreClient

client = AgentCoreClient(
    runtime_arn=os.environ["AGENT_RUNTIME_ARN"],
    region_name="ap-northeast-1",
)
result = client.invoke(messages=[{"role": "user", "content": "こんにちは"}], agent="assistant")
print(result.text)
```

資格情報は boto3 の通常の解決方法に従います。Lambda では実行ロールを利用できます。
既存の `boto3.Session` は `boto_session=` で渡せます。

## ジョブとエラー

両クライアントで同じ操作を使えます。

```python
job = client.submit(messages=[{"role": "user", "content": "長い文章を要約して"}], agent="assistant", actor_id="user-42")
print(job.session_id, job.job_id)
for event in job.follow():
    print(event.seq, event.type)
result = job.result(wait=True)
```

`job.status()` で状態を確認し、`job.cancel()` で停止を要求できます。
別プロセスからは `client.job(session_id, job_id, actor_id="user-42")` で操作を再開できます。
実行が失敗した場合、`invoke()` と `job.result()` は `InvocationError` を送出します。
`stream()` では `error` イベントも含めて呼び出し側が処理します。

イベント型は `strands_runtime_client.events` から import できます。サーバーも同じ定義を使用します。
