"""同一ホストの実行予約。所有プロセスが生存する限り予約を失効させない。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psutil

from strands_runtime.errors import SessionAccessError, SessionBusyError
from strands_runtime.sqlite import Database


class SqliteReservations:
    def __init__(self, path: Path) -> None:
        self._pid = os.getpid()
        self._created = float(psutil.Process(self._pid).create_time())
        self._db = Database(
            path,
            "CREATE TABLE IF NOT EXISTS reservations "
            "(session TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER NOT NULL, created REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS session_owners (session TEXT PRIMARY KEY, actor TEXT NOT NULL)",
        )

    async def authorize_owner(self, session_id: str, actor_id: str | None, *, claim: bool) -> None:
        def check(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT actor FROM session_owners WHERE session = ?", (session_id,)).fetchone()
            if row is not None and row[0] != actor_id:
                raise SessionAccessError()
            if row is None and claim:
                assert actor_id is not None
                conn.execute("INSERT INTO session_owners VALUES (?, ?)", (session_id, actor_id))

        await self._db.call(check)

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        owner = uuid.uuid4().hex

        def reserve(conn: sqlite3.Connection) -> bool:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT pid, created FROM reservations WHERE session = ?", (session_id,)).fetchone()
            if row and _alive(row[0], row[1]):
                return False
            conn.execute(
                "INSERT OR REPLACE INTO reservations VALUES (?, ?, ?, ?)",
                (session_id, owner, self._pid, self._created),
            )
            return True

        def release(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM reservations WHERE session = ? AND owner = ?", (session_id, owner))

        acquired = False
        task = asyncio.create_task(self._db.call(reserve))
        try:
            try:
                acquired = await asyncio.shield(task)
            except asyncio.CancelledError:
                acquired = await task
                raise
            if not acquired:
                raise SessionBusyError(session_id)
            yield
        finally:
            if acquired:
                await self._db.call(release)


def _alive(pid: int, created: float) -> bool:
    try:
        process = psutil.Process(pid)
        # PID再利用とOS再起動を識別する。時間経過だけでは生存中の所有者から予約を奪わない。
        return bool(
            process.is_running() and process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # 生死を確認できないときは、安全側に倒して重複を拒否する。
