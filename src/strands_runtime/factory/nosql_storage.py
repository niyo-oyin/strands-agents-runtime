"""Strandsのスナップショットを、世代ごとのチャンクと小さな索引でNoSQLへ保存する。"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from typing import Any

from strands.storage import Storage
from strands.types.exceptions import StorageError

from strands_runtime.invoke.shared_store import SharedStore, settled

logger = logging.getLogger(__name__)
CHUNK_BYTES = 128 * 1024  # base64後もDynamoDBの400 KB制限を下回る。


class NoSQLStorage(Storage[str, str]):
    def __init__(self, store: SharedStore) -> None:
        self.store = store

    def _chunk_key(self, key: str, generation: str, index: int) -> str:
        return self.store.key("history-chunk", f"{self.store.key('history', key)}:{generation}:{index}")

    async def write(self, key: str, data: bytes) -> None:
        await settled(lambda: self._write(key, data))

    def _write(self, key: str, data: bytes) -> None:
        head_key = self.store.key("history", key)
        previous = self.store.get(head_key)
        generation = uuid.uuid4().hex
        count = (len(data) + CHUNK_BYTES - 1) // CHUNK_BYTES
        # 索引を更新するまで以前の世代は読める。応答喪失時の成否は不明なので、失敗した世代は消さない。
        for index in range(count):
            chunk = data[index * CHUNK_BYTES : (index + 1) * CHUNK_BYTES]
            self.store.put({"id": self._chunk_key(key, generation, index), "data": base64.b64encode(chunk).decode()})
        self.store.put(
            {
                "id": head_key,
                "kind": "history",
                "namespace": self.store.config.namespace,
                "key": key,
                "generation": generation,
                "count": str(count),
                "size": str(len(data)),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        # 公開済み索引の参照先は削除しない。旧世代を読む途中だった読者はread側で再取得する。
        if previous is not None:
            try:
                self._delete_chunks(key, previous)
            except Exception:
                logger.warning("old history chunks could not be removed (key=%s)", key, exc_info=True)

    async def read(self, key: str) -> bytes | None:
        return await settled(lambda: self._read(key))

    def _read(self, key: str) -> bytes | None:
        head_key = self.store.key("history", key)
        for _ in range(3):
            head = self.store.get(head_key)
            if head is None:
                return None
            chunks = []
            for index in range(int(head["count"])):
                row = self.store.get(self._chunk_key(key, head["generation"], index))
                if row is None:
                    break
                chunks.append(base64.b64decode(row["data"], validate=True))
            else:
                data = b"".join(chunks)
                if len(data) != int(head["size"]) or hashlib.sha256(data).hexdigest() != head["sha256"]:
                    raise StorageError(f"history snapshot checksum mismatch: {key}")
                return data
            current = self.store.get(head_key)
            if current is not None and current["generation"] == head["generation"]:
                raise StorageError(f"history snapshot is incomplete: {key}")
        raise StorageError(f"history snapshot changed repeatedly during read: {key}")

    async def delete(self, key: str) -> None:
        def delete() -> None:
            head_key = self.store.key("history", key)
            head = self.store.get(head_key)
            self.store.delete(head_key)
            if head is not None:
                self._delete_chunks(key, head)

        await settled(delete)

    def _delete_chunks(self, key: str, head: dict[str, Any]) -> None:
        for index in range(int(head["count"])):
            self.store.delete(self._chunk_key(key, head["generation"], index))

    async def list(self, query: str) -> list[str]:
        return await settled(lambda: self.store.history_keys(query))
