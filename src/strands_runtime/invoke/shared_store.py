"""コンテナ間の予約とジョブ記録。予約は所有者だけが解放し、TTLで横取りしない。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import socket
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from strands_runtime.definitions.schema import HistorySpec, SessionSection
from strands_runtime.errors import CapacityError, DefinitionError, SessionAccessError, SessionBusyError


async def settled[T](operation: Callable[[], T]) -> T:
    """I/O完了前にキャンセルによる後始末を始めない。"""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class SharedStore:
    def __init__(self, config: HistorySpec | SessionSection) -> None:
        self.config = config
        self._client: Any = None
        self._lock = threading.Lock()

    def connect(self) -> Any:
        with self._lock:
            if self._client is not None:
                return self._client
            if self.config.backend == "dynamodb":
                spec = self.config.dynamodb
                assert spec is not None
                client = boto3.client(
                    "dynamodb",
                    region_name=spec.region,
                    config=Config(connect_timeout=5, read_timeout=10, retries={"mode": "standard", "max_attempts": 2}),
                )
                table = client.describe_table(TableName=spec.table)["Table"]
                if table.get("KeySchema") != [{"AttributeName": "id", "KeyType": "HASH"}] or table.get("Replicas"):
                    raise DefinitionError(
                        "session/history DynamoDB table requires a string hash key 'id', no sort key and no global replicas"
                    )
                if table.get("AttributeDefinitions") != [{"AttributeName": "id", "AttributeType": "S"}]:
                    raise DefinitionError("session/history DynamoDB table requires only the string key 'id'")
                ttl = client.describe_time_to_live(TableName=spec.table)["TimeToLiveDescription"]
                if ttl.get("TimeToLiveStatus") != "DISABLED":
                    raise DefinitionError("session/history DynamoDB table must have TTL disabled")
                self._client = client
            else:
                spec_cosmos = self.config.cosmos
                assert spec_cosmos is not None
                try:
                    from azure.cosmos import CosmosClient
                    from azure.identity import DefaultAzureCredential
                except ImportError as exc:
                    raise DefinitionError("Cosmos storage requires strands-agents-runtime[cosmos]") from exc
                client = CosmosClient(
                    spec_cosmos.endpoint,
                    credential=spec_cosmos.key or DefaultAzureCredential(),
                    connection_timeout=5,
                    read_timeout=10,
                    retry_total=2,
                )
                account = client.get_database_account()
                if len(account.WritableLocations) != 1:
                    raise DefinitionError("session/history Cosmos DB requires a single write region")
                if (account.ConsistencyPolicy or {}).get("defaultConsistencyLevel") != "Strong":
                    raise DefinitionError("session/history Cosmos DB requires Strong account consistency")
                container = client.get_database_client(spec_cosmos.database).get_container_client(spec_cosmos.container)
                properties = container.read()
                if properties.get("partitionKey", {}).get("paths") != ["/id"]:
                    raise DefinitionError("session/history Cosmos container requires partition key /id")
                if properties.get("defaultTtl") not in (None, -1):
                    raise DefinitionError("session/history Cosmos container must have automatic TTL deletion disabled")
                self._client = container
            return self._client

    def key(self, kind: str, key: str) -> str:
        # JSON表現で名前空間とキーを分離し、区切り文字による衝突を防ぐ。
        import json

        return hashlib.sha256(json.dumps([self.config.namespace, kind, key]).encode()).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        client = self.connect()
        if self.config.backend == "dynamodb":
            assert self.config.dynamodb is not None
            row = client.get_item(
                TableName=self.config.dynamodb.table, Key={"id": {"S": key}}, ConsistentRead=True
            ).get("Item")
            return {k: v["S"] for k, v in row.items()} if row else None
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return dict(client.read_item(item=key, partition_key=key))
        except CosmosResourceNotFoundError:
            return None

    def put(self, row: dict[str, str], *, exclusive: bool = False) -> bool:
        client = self.connect()
        if self.config.backend == "dynamodb":
            assert self.config.dynamodb is not None
            try:
                client.put_item(
                    TableName=self.config.dynamodb.table,
                    Item={k: {"S": v} for k, v in row.items()},
                    **({"ConditionExpression": "attribute_not_exists(id)"} if exclusive else {}),
                )
            except ClientError as exc:
                if exclusive and exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    return False
                raise
        else:
            from azure.cosmos.exceptions import CosmosResourceExistsError

            try:
                if exclusive:
                    client.create_item(row)
                else:
                    client.upsert_item(row)
            except CosmosResourceExistsError:
                if exclusive:
                    return False
                raise
        return True

    def delete_owner(self, key: str, owner: str) -> bool:
        client = self.connect()
        if self.config.backend == "dynamodb":
            assert self.config.dynamodb is not None
            try:
                client.delete_item(
                    TableName=self.config.dynamodb.table,
                    Key={"id": {"S": key}},
                    ConditionExpression="#owner = :owner",
                    ExpressionAttributeNames={"#owner": "owner"},
                    ExpressionAttributeValues={":owner": {"S": owner}},
                )
                return True
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    return False
                raise
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosHttpResponseError

        row = self.get(key)
        if row is None or row["owner"] != owner:
            return False
        try:
            client.delete_item(
                item=key, partition_key=key, etag=row["_etag"], match_condition=MatchConditions.IfNotModified
            )
            return True
        except CosmosHttpResponseError as exc:
            if exc.status_code in (404, 412):
                return False
            raise

    def delete(self, key: str) -> None:
        """履歴のチャンクや索引を削除する。実行予約にはdelete_ownerを使う。"""
        client = self.connect()
        if self.config.backend == "dynamodb":
            assert self.config.dynamodb is not None
            client.delete_item(TableName=self.config.dynamodb.table, Key={"id": {"S": key}})
        else:
            from azure.cosmos.exceptions import CosmosResourceNotFoundError

            with suppress(CosmosResourceNotFoundError):
                client.delete_item(item=key, partition_key=key)

    def history_keys(self, prefix: str) -> list[str]:
        """SDKのチェックポイント列挙用。通常の履歴復元はキーによる直接取得だけを使う。"""
        client = self.connect()
        if self.config.backend == "dynamodb":
            assert self.config.dynamodb is not None
            paginator = client.get_paginator("scan")
            pages = paginator.paginate(
                TableName=self.config.dynamodb.table,
                ConsistentRead=True,
                FilterExpression="#kind = :kind AND #ns = :ns AND begins_with(#key, :prefix)",
                ExpressionAttributeNames={"#kind": "kind", "#ns": "namespace", "#key": "key"},
                ExpressionAttributeValues={
                    ":kind": {"S": "history"},
                    ":ns": {"S": self.config.namespace},
                    ":prefix": {"S": prefix},
                },
                ProjectionExpression="#key",
            )
            return sorted(row["key"]["S"] for page in pages for row in page.get("Items", []))
        rows = client.query_items(
            query="SELECT c.key FROM c WHERE c.kind = @kind AND c.namespace = @namespace AND STARTSWITH(c.key, @prefix)",
            parameters=[
                {"name": "@kind", "value": "history"},
                {"name": "@namespace", "value": self.config.namespace},
                {"name": "@prefix", "value": prefix},
            ],
            enable_cross_partition_query=True,
        )
        return sorted(row["key"] for row in rows)

    async def read(self, key: str) -> bytes | None:
        row = await settled(lambda: self.get(self.key("journal", key)))
        return base64.b64decode(row["data"]) if row else None

    async def write(self, key: str, data: bytes) -> None:
        if len(data) > 240 * 1024:
            raise ValueError("external job journal entries must not exceed 240 KiB; use S3 for large events")
        await settled(lambda: self.put({"id": self.key("journal", key), "data": base64.b64encode(data).decode()}))

    async def inspect(self, session_id: str) -> dict[str, Any] | None:
        return await settled(lambda: self.get(self.key("reservation", session_id)))

    async def authorize_owner(self, session_id: str, actor_id: str | None, *, claim: bool) -> None:
        def check() -> None:
            key = self.key("session-owner", session_id)
            if claim:
                assert actor_id is not None
                self.put({"id": key, "actor_id": actor_id}, exclusive=True)
            row = self.get(key)
            if row is not None and row["actor_id"] != actor_id:
                raise SessionAccessError()

        await settled(check)

    async def recover(self, session_id: str, owner: str) -> bool:
        return await settled(lambda: self.delete_owner(self.key("reservation", session_id), owner))

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        key, owner = self.key("reservation", session_id), uuid.uuid4().hex
        row = {
            "id": key,
            "owner": owner,
            "session_id": session_id,
            "process": f"{socket.gethostname()}:{os.getpid()}",
            "created_at": datetime.now(UTC).isoformat(),
        }
        acquired = False
        task = asyncio.create_task(settled(lambda: self.put(row, exclusive=True)))
        try:
            try:
                acquired = await asyncio.shield(task)
            except asyncio.CancelledError:
                acquired = await task
                raise
            except Exception as exc:
                # 応答喪失時は取得の成否が不明。実行を始めず、予約は管理者の確認用に残す。
                raise CapacityError("execution store unavailable; invocation was not started") from exc
            if not acquired:
                raise SessionBusyError(session_id)
            yield
        finally:
            if acquired:
                await settled(lambda: self.delete_owner(key, owner))
