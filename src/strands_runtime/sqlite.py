"""同一ホスト用のSQLite。接続と短いトランザクションは呼び出し内で閉じる。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


class Database:
    def __init__(self, path: Path, schema: str) -> None:
        if str(path).startswith("\\\\"):
            raise ValueError("SQLite requires a local disk, not a UNC network share")
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run(lambda conn: conn.executescript(schema))

    def run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        # rollback journalを使用する。ネットワークI/Oやエージェント実行をこの中に入れない。
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            return operation(conn)

    async def call(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        task = asyncio.create_task(asyncio.to_thread(self.run, operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # キャンセル後に書き込みが追い越して完了することを防ぐ。
            await task
            raise


class JournalStorage:
    """ジョブ記録専用のローカルKV。履歴用のStrands Storageとは独立している。"""

    def __init__(self, path: Path) -> None:
        self._db = Database(path, "CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, value BLOB NOT NULL)")

    async def read(self, key: str) -> bytes | None:
        def read(conn: sqlite3.Connection) -> bytes | None:
            row = conn.execute("SELECT value FROM entries WHERE key = ?", (key,)).fetchone()
            return bytes(row[0]) if row else None

        return await self._db.call(read)

    async def write(self, key: str, data: bytes) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT OR REPLACE INTO entries VALUES (?, ?)", (key, data))

        await self._db.call(write)
