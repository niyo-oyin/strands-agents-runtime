"""実行枠と有限の待機枠。同じ実行イベントループから利用する。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from strands_runtime.definitions.schema import ExecutionSection
from strands_runtime.errors import CapacityError, SessionAccessError, SessionBusyError
from strands_runtime.invoke.reservations import SqliteReservations
from strands_runtime.invoke.shared_store import SharedStore


class AdmissionController:
    """セッションを待機前に予約し、実行完了・キャンセル時に必ず解放する。"""

    def __init__(
        self,
        config: ExecutionSection,
        *,
        sqlite_path: Path | None = None,
        shared_store: SharedStore | None = None,
        session_enabled: bool = True,
    ) -> None:
        self._config = config
        self._session_enabled = session_enabled
        self._slots = asyncio.Semaphore(config.max_concurrent)
        self._sessions: set[str] = set()
        self._owners: dict[str, str] = {}
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reservations = shared_store or (SqliteReservations(sqlite_path) if sqlite_path is not None else None)

    async def authorize_owner(self, session_id: str, actor_id: str | None, *, claim: bool) -> None:
        if not self._session_enabled:
            return
        if self._reservations is not None:
            await self._reservations.authorize_owner(session_id, actor_id, claim=claim)
        else:
            if session_id in self._owners and self._owners[session_id] != actor_id:
                raise SessionAccessError()
            if claim:
                assert actor_id is not None
                self._owners[session_id] = actor_id

    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncIterator[None]:
        if not self._session_enabled:
            session_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("invocations on one runtime must use the same event loop")
        self._loop = loop
        # この区間に await を入れない。ジャーナルを確認する前から同じセッションを拒否する。
        if self._closed:
            raise CapacityError("runtime is shutting down")
        if session_id in self._sessions:
            raise SessionBusyError(session_id)
        acquired = False
        reserved = False
        try:
            full = len(self._sessions) >= self._config.max_concurrent + self._config.max_queued
            self._sessions.add(session_id)
            reserved = True
            async with self._reservations.hold(session_id) if self._reservations else nullcontext():
                if full:
                    raise CapacityError("invocation queue is full; retry later")
                try:
                    await asyncio.wait_for(self._slots.acquire(), self._config.queue_timeout_seconds)
                except TimeoutError as exc:
                    raise CapacityError("invocation queue wait timed out; retry later") from exc
                acquired = True
                if self._closed:
                    raise CapacityError("runtime is shutting down")
                yield
        finally:
            if acquired:
                self._slots.release()
            if reserved:
                self._sessions.remove(session_id)

    def close(self) -> None:
        """新規受付を止め、実行枠を待つリクエストも503で終了させる。再開しない。"""
        if self._closed:
            return
        self._closed = True
        for _ in self._sessions:
            self._slots.release()
