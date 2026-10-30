"""Amazon Bedrock AgentCore Runtime にデプロイしたランタイム向けのクライアント（boto3 による SigV4 認証）。

実行時に必要なのは ``boto3`` と ``pydantic`` だけなので、AWS Lambda からの利用にも向く。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from strands_runtime_client.base import AgentClient, InvocationError, iter_sse_events, new_session_id
from strands_runtime_client.events import parse_event

if TYPE_CHECKING:
    import boto3


class AgentCoreClient(AgentClient):
    def __init__(
        self,
        runtime_arn: str,
        *,
        qualifier: str = "DEFAULT",
        region_name: str | None = None,
        boto_session: boto3.Session | None = None,
    ) -> None:
        try:
            import boto3
        except ModuleNotFoundError as exc:
            if exc.name != "boto3":
                raise
            raise ImportError("AgentCoreClient requires strands-agents-runtime-client[agentcore]") from exc

        self._runtime_arn = runtime_arn
        # ``qualifier`` はエンドポイント名。既定の DEFAULT は常に最新バージョンを指す。
        self._qualifier = qualifier
        session = boto_session or boto3.Session()
        self._client = session.client("bedrock-agentcore", region_name=region_name)

    def _request(self, payload: dict[str, Any], *, session_id: str | None) -> Iterator[Any]:
        # AgentCore はセッション ID を必須とし、API の引数（ヘッダー）で渡した値で microVM を割り当てる。
        # 自動生成した ID はヘッダーだけに渡す。ペイロードの session_id は利用者による履歴継続の指定。
        runtime_session_id = session_id or new_session_id()
        try:
            response = self._client.invoke_agent_runtime(
                agentRuntimeArn=self._runtime_arn,
                qualifier=self._qualifier,
                runtimeSessionId=runtime_session_id,
                contentType="application/json",
                accept="text/event-stream",
                payload=json.dumps(payload).encode("utf-8"),
            )
        except self._client.exceptions.RuntimeClientError as exc:
            # 424: コンテナ側が 4xx / 5xx（ランタイムの 400 や 404 など）を返した。本文は例外メッセージに含まれる。
            raise InvocationError("RuntimeClientError", str(exc)) from exc

        body = response["response"]
        content_type = response.get("contentType", "")
        if "text/event-stream" in content_type:
            yield from iter_sse_events(body.iter_lines())
        else:
            # ストリーミングでない JSON（エラー文書など）も、同じイベント型で呼び出し側に見せる。
            data = json.loads(body.read().decode("utf-8") or "{}")
            if "error" in data and "type" not in data:
                raise InvocationError("RuntimeError", str(data["error"]))
            yield parse_event(data)
