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
    for event in client.stream("こんにちは", agent="assistant", session_id=sid):
        if event.type == "text":
            print(event.text, end="")
    result = client.invoke("続きをお願い", agent="assistant", session_id=sid)
    print(result.text, result.metrics.total_tokens)
```

## AgentCore で呼び出す

```python
import os

from strands_runtime_client import AgentCoreClient

client = AgentCoreClient(
    runtime_arn=os.environ["AGENT_RUNTIME_ARN"],
    region_name="ap-northeast-1",
)
result = client.invoke("こんにちは", agent="assistant")
print(result.text)
```

資格情報は boto3 の通常の解決方法に従います。Lambda では実行ロールを利用できます。
既存の `boto3.Session` は `boto_session=` で渡せます。

## ジョブとエラー

両クライアントで同じ操作を使えます。

```python
job = client.submit("長い文章を要約して", agent="assistant")
print(job.session_id, job.job_id)
for event in job.follow():
    print(event.seq, event.type)
result = job.result(wait=True)
```

`job.status()` で状態を確認し、`job.cancel()` で停止を要求できます。
別プロセスからは `client.job(session_id, job_id)` で操作を再開できます。
実行が失敗した場合、`invoke()` と `job.result()` は `InvocationError` を送出します。
`stream()` では `error` イベントも含めて呼び出し側が処理します。

イベント型は `strands_runtime_client.events` から import できます。サーバーも同じ定義を使用します。
