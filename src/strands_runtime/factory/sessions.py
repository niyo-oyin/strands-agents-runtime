"""セッションの識別と、設定されたバックエンドに応じた ``SessionManager`` の構築。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from strands.session import SessionManager, SnapshotSessionManager
from strands.storage import LocalFileStorage, S3Storage, Storage

from strands_runtime.definitions.schema import SessionSpec
from strands_runtime.errors import DefinitionError

DEFAULT_LOCAL_SESSION_DIR = "./.runtime/sessions"
ANONYMOUS_ACTOR = "anonymous"


@dataclass(frozen=True)
class SessionContext:
    """「誰が」「どの会話で」呼んでいるか。サーバー層がヘッダーとペイロードから決める。"""

    session_id: str
    actor_id: str = ANONYMOUS_ACTOR

    def child(self, name: str) -> SessionContext:
        """サブエージェント用のセッション。親から導出するが、履歴は親と混ざらないよう別の ID にする。"""
        return SessionContext(session_id=f"{self.session_id}--{name}", actor_id=self.actor_id)


def effective_session_spec(agent_spec: SessionSpec, runtime_spec: SessionSpec) -> SessionSpec:
    """``agent.toml`` の ``[session]`` が ``inherit`` なら ``runtime.toml`` の設定を使う。"""
    return runtime_spec if agent_spec.backend == "inherit" else agent_spec


class SessionManagerFactory:
    """呼び出しごとに ``SessionManager`` を作る。状態を持たない ``Storage`` はプロセス内で共有する。"""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._storages: dict[str, Storage] = {}
        self._lock = threading.Lock()

    def build(self, spec: SessionSpec, ctx: SessionContext) -> SessionManager | None:
        match spec.backend:
            case "none":
                return None
            case "local":
                storage = self._storage(f"local:{spec.dir}", lambda: self._local_storage(spec))
                return SnapshotSessionManager(ctx.session_id, storage=storage)
            case "s3":
                storage = self._storage(f"s3:{spec.bucket}:{spec.prefix}:{spec.region}", lambda: self._s3_storage(spec))
                return SnapshotSessionManager(ctx.session_id, storage=storage)
            case "agentcore_memory":
                return self._agentcore_memory(spec, ctx)
            case _:
                # ``inherit`` は effective_session_spec で解決済みのはずなので、ここに来るのは配線ミス。
                raise DefinitionError(f"session backend {spec.backend!r} cannot be built (did inheritance resolve?)")

    def _storage(self, key: str, make: Callable[[], Storage]) -> Storage:
        """設定内容をキーにして ``Storage`` を 1 つだけ作り、以降は使い回す。"""
        with self._lock:
            if key not in self._storages:
                self._storages[key] = make()
            return self._storages[key]

    def _local_storage(self, spec: SessionSpec) -> Storage:
        directory = (self._base_dir / (spec.dir or DEFAULT_LOCAL_SESSION_DIR)).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return LocalFileStorage(str(directory))

    @staticmethod
    def _s3_storage(spec: SessionSpec) -> Storage:
        assert spec.bucket is not None  # SessionSpec の検証で保証済み
        return S3Storage(spec.bucket, prefix=spec.prefix, region_name=spec.region)

    @staticmethod
    def _agentcore_memory(spec: SessionSpec, ctx: SessionContext) -> SessionManager:
        # bedrock-agentcore の Strands 連携はこのバックエンドでしか使わないので、ここで初めて読み込む。
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

        assert spec.memory_id is not None  # SessionSpec の検証で保証済み
        config = AgentCoreMemoryConfig(
            memory_id=spec.memory_id,
            session_id=ctx.session_id,
            actor_id=ctx.actor_id,
            retrieval_config={ns: RetrievalConfig(**r.model_dump()) for ns, r in spec.retrieval.items()} or None,
        )
        manager: SessionManager = AgentCoreMemorySessionManager(config, region_name=spec.region)
        return manager
