"""履歴の所有者と、保存しない呼び出しの一時的な書き込み。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from strands.storage import Storage

from strands_runtime.errors import InvalidRequestError, SessionAccessError

MEMORY_OWNER_ACTOR = "strands-runtime-session-owners"


def actor_scope(actor_id: str) -> str:
    return hashlib.sha256(actor_id.encode()).hexdigest()


class ActorStorage(Storage[str, str]):
    """各actorの履歴を独立したキー空間に置く。"""

    def __init__(self, source: Storage, actor_id: str) -> None:
        self.source = source
        self.prefix = f"actors/{actor_scope(actor_id)}/"

    async def read(self, key: str) -> bytes | None:
        return await self.source.read(self.prefix + key)

    async def write(self, key: str, data: bytes) -> None:
        await self.source.write(self.prefix + key, data)

    async def delete(self, key: str) -> None:
        await self.source.delete(self.prefix + key)

    async def list(self, query: str) -> list[str]:
        return [key[len(self.prefix) :] for key in await self.source.list(self.prefix + query)]


class InvocationStorage(Storage[str, str]):
    """復元と保存を独立して制御し、今回の変更は呼び出し内でも参照できる。"""

    def __init__(self, source: Storage, *, store: bool, restore: bool) -> None:
        self.source = source
        self.store = store
        self.restore = restore
        self.changes: dict[str, bytes | None] = {}

    async def read(self, key: str) -> bytes | None:
        if key in self.changes:
            return self.changes[key]
        return await self.source.read(key) if self.restore else None

    async def write(self, key: str, data: bytes) -> None:
        if self.store:
            await self.source.write(key, data)
        self.changes[key] = data

    async def delete(self, key: str) -> None:
        if self.store:
            await self.source.delete(key)
        self.changes[key] = None

    async def list(self, query: str) -> list[str]:
        keys = set(await self.source.list(query)) if self.restore else set()
        for key, value in self.changes.items():
            if key.startswith(query):
                if value is None:
                    keys.discard(key)
                else:
                    keys.add(key)
        return sorted(keys)


async def authorize_storage(
    storage: Storage, session_id: str, actor_id: str, *, store: bool, new: bool = False
) -> bool:
    # 呼び出し元は同一sessionの実行予約を保持する。書き込み失敗を無視して実行を始めない。
    key = f"session-owners/{hashlib.sha256(session_id.encode()).hexdigest()}"
    owner = await storage.read(key)
    if owner is not None and owner.decode() != actor_id:
        raise SessionAccessError()
    if owner is not None and store and new:
        raise InvalidRequestError("session already exists; specify session_id to continue it")
    if owner is None and store:
        await storage.write(key, actor_id.encode())
    return owner is not None


def authorize_memory(
    client: Any, memory_id: str, session_id: str, actor_id: str, *, store: bool, new: bool = False
) -> bool:
    # 所有者は抽出対象の会話と分け、専用actorのblobイベントに保存する。
    params = {"memoryId": memory_id, "actorId": MEMORY_OWNER_ACTOR, "sessionId": session_id}
    response = client.list_events(**params, includePayloads=True, maxResults=1)
    events = response.get("events", [])
    if events:
        owner = json.loads(events[0]["payload"][0]["blob"])["actor_id"]
        if owner != actor_id:
            raise SessionAccessError()
        if store and new:
            raise InvalidRequestError("session already exists; specify session_id to continue it")
    elif store:
        client.create_event(
            **params, eventTimestamp=datetime.now(UTC), payload=[{"blob": json.dumps({"actor_id": actor_id})}]
        )
    return bool(events)
