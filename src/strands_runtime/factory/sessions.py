"""セッションの識別と、設定されたバックエンドに応じた ``SessionManager`` の構築。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from strands.session import SessionManager, SnapshotSessionManager
from strands.storage import LocalFileStorage, S3Storage, Storage

from strands_runtime.definitions.schema import HistorySpec
from strands_runtime.errors import DefinitionError
from strands_runtime.factory.history_access import ActorStorage, InvocationStorage, authorize_memory, authorize_storage

DEFAULT_LOCAL_SESSION_DIR = "./.runtime/sessions"


@dataclass(frozen=True)
class SessionContext:
    """「誰が」「どの会話で」呼んでいるか。サーバー層がヘッダーとペイロードから決める。"""

    session_id: str
    actor_id: str | None = None
    store: bool = True
    restore: bool = True

    def child(self, name: str) -> SessionContext:
        """サブエージェント用のセッション。親から導出するが、履歴は親と混ざらないよう別の ID にする。"""
        return SessionContext(
            session_id=f"{self.session_id}--{name}", actor_id=self.actor_id, store=False, restore=False
        )


def effective_history_spec(agent_spec: HistorySpec, runtime_spec: HistorySpec) -> HistorySpec:
    """``agent.toml`` の ``[session.history]`` が ``inherit`` なら ``runtime.toml`` の設定を使う。"""
    return runtime_spec if agent_spec.backend == "inherit" else agent_spec


class SessionManagerFactory:
    """呼び出しごとに ``SessionManager`` を作る。状態を持たない ``Storage`` はプロセス内で共有する。"""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._storages: dict[str, Storage] = {}
        self._lock = threading.Lock()
        self._memory_clients: dict[str | None, Any] = {}

    async def authorize(self, spec: HistorySpec, ctx: SessionContext) -> bool:
        if spec.backend == "none" or not (ctx.restore or ctx.store):
            return False
        assert ctx.actor_id is not None
        if spec.backend == "agentcore_memory":
            from strands_runtime.invoke.shared_store import settled

            def check() -> bool:
                with self._lock:
                    if spec.region not in self._memory_clients:
                        self._memory_clients[spec.region] = boto3.client("bedrock-agentcore", region_name=spec.region)
                    client = self._memory_clients[spec.region]
                assert spec.memory_id is not None and ctx.actor_id is not None
                return authorize_memory(
                    client, spec.memory_id, ctx.session_id, ctx.actor_id, store=ctx.store, new=not ctx.restore
                )

            return await settled(check)
        else:
            storage = self.storage_for(spec)
            assert storage is not None
            # 予約解放より先に、キャンセル中の書き込みも完了させる。
            task = asyncio.create_task(
                authorize_storage(storage, ctx.session_id, ctx.actor_id, store=ctx.store, new=not ctx.restore)
            )
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def build(self, spec: HistorySpec, ctx: SessionContext) -> SessionManager | None:
        match spec.backend:
            case "none":
                return None
            case "local" | "s3" | "dynamodb" | "cosmos":
                if not (ctx.store or ctx.restore):
                    return None
                storage = self.storage_for(spec)
                assert storage is not None
                if not ctx.store or not ctx.restore:
                    storage = InvocationStorage(storage, store=ctx.store, restore=ctx.restore)
                assert ctx.actor_id is not None
                storage = ActorStorage(storage, ctx.actor_id)
                return SnapshotSessionManager(ctx.session_id, storage=storage)
            case "agentcore_memory":
                if ctx.actor_id is None:
                    return None
                return self._agentcore_memory(spec, ctx)
            case _:
                # ``inherit`` は effective_history_spec で解決済みのはずなので、ここに来るのは配線ミス。
                raise DefinitionError(f"session backend {spec.backend!r} cannot be built (did inheritance resolve?)")

    def storage_for(self, spec: HistorySpec) -> Storage | None:
        """local / S3 / NoSQL の共有Storageを返す。Memory / noneには汎用StorageがないのでNone。"""
        match spec.backend:
            case "local":
                return self._storage(f"local:{spec.dir}", lambda: self._local_storage(spec))
            case "s3":
                return self._storage(f"s3:{spec.bucket}:{spec.prefix}:{spec.region}", lambda: self._s3_storage(spec))
            case "dynamodb" | "cosmos":
                from strands_runtime.factory.nosql_storage import NoSQLStorage
                from strands_runtime.invoke.shared_store import SharedStore

                return self._storage(spec.model_dump_json(), lambda: NoSQLStorage(SharedStore(spec)))
            case _:
                return None

    def _storage(self, key: str, make: Callable[[], Storage]) -> Storage:
        """設定内容をキーにして ``Storage`` を 1 つだけ作り、以降は使い回す。"""
        with self._lock:
            if key not in self._storages:
                self._storages[key] = make()
            return self._storages[key]

    def _local_storage(self, spec: HistorySpec) -> Storage:
        directory = (self._base_dir / (spec.dir or DEFAULT_LOCAL_SESSION_DIR)).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return LocalFileStorage(str(directory))

    @staticmethod
    def _s3_storage(spec: HistorySpec) -> Storage:
        assert spec.bucket is not None  # HistorySpec の検証で保証済み
        return S3Storage(spec.bucket, prefix=spec.prefix, region_name=spec.region)

    @staticmethod
    def _agentcore_memory(spec: HistorySpec, ctx: SessionContext) -> SessionManager:
        # bedrock-agentcore の Strands 連携はこのバックエンドでしか使わないので、ここで初めて読み込む。
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
            PersistenceMode,
            RetrievalConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

        assert spec.memory_id is not None and ctx.actor_id is not None
        config = AgentCoreMemoryConfig(
            memory_id=spec.memory_id,
            session_id=ctx.session_id,
            actor_id=ctx.actor_id,
            retrieval_config={ns: RetrievalConfig(**r.model_dump()) for ns, r in spec.retrieval.items()} or None,
            persistence_mode=PersistenceMode.FULL if ctx.store else PersistenceMode.NONE,
            async_mode=True,
        )

        class MemorySessionManager(AgentCoreMemorySessionManager):
            def read_session(self, session_id: str, **kwargs: Any) -> Any:
                if not ctx.restore:
                    return None
                return super().read_session(session_id, **kwargs)

            def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> Any:
                if not ctx.restore:
                    return None
                return super().read_agent(session_id, agent_id, **kwargs)

        manager: SessionManager = MemorySessionManager(config, region_name=spec.region)
        return manager
