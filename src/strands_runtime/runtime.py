"""組み立ての根。設定を読み、レジストリ → 工場 → Invoker の順に配線する。

HTTP サーバーも CLI も :meth:`Runtime.load` から始まるので、起動の経路は 1 つに揃う。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from strands_runtime.definitions import Registry, RuntimeConfig, load_runtime_config
from strands_runtime.errors import DefinitionError
from strands_runtime.factory import AgentFactory
from strands_runtime.factory.sessions import DEFAULT_LOCAL_SESSION_DIR, effective_history_spec
from strands_runtime.invoke import Invoker
from strands_runtime.invoke.shared_store import SharedStore
from strands_runtime.jobs import JobJournal, JobManager
from strands_runtime.sqlite import JournalStorage
from strands_runtime.telemetry import setup_telemetry

CONFIG_ENV_VAR = "STRANDS_RUNTIME_CONFIG"
DEFAULT_CONFIG_FILE = "runtime.toml"


@dataclass(frozen=True)
class Runtime:
    """読み込み済みの設定と、配線済みの各層。"""

    base_dir: Path
    config_path: Path
    config: RuntimeConfig
    registry: Registry
    factory: AgentFactory
    invoker: Invoker
    jobs: JobManager
    shared_store: SharedStore | None = None

    @classmethod
    def load(cls, config_path: Path | None = None) -> Runtime:
        """``runtime.toml``（引数、``$STRANDS_RUNTIME_CONFIG``、``./runtime.toml`` の順に探す）と全エージェントを読む。

        ``runtime.toml`` の置き場所が、相対パス（``agents_dir`` やセッションの ``dir``）の基準になる。
        定義に問題があればここで例外になり、サーバーは起動しない。
        """
        path = (config_path or Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_FILE))).resolve()
        base_dir = path.parent
        config = load_runtime_config(path)
        registry = Registry.load(config, base_dir)
        for resolved in registry:
            history = effective_history_spec(resolved.definition.session.history, config.session.history)
            if config.session.backend == "none" and history.backend != "none":
                raise DefinitionError(f"agent {resolved.name!r}: session.backend = 'none' cannot save history")
            if config.session.backend in ("dynamodb", "cosmos") and history.backend == "local":
                raise DefinitionError(f"agent {resolved.name!r}: distributed sessions cannot use local history")
        factory = AgentFactory(registry, config, base_dir=base_dir)
        setup_telemetry()
        sqlite_path = (base_dir / config.session.sqlite_path).resolve() if config.session.backend == "local" else None
        shared_store = SharedStore(config.session) if config.session.backend in ("dynamodb", "cosmos") else None
        invoker = Invoker(factory, config, sqlite_path=sqlite_path, shared_store=shared_store)
        jobs = JobManager(
            invoker,
            _job_journal(config, factory, base_dir, shared_store),
            max_duration_seconds=config.jobs.max_duration_seconds,
        )
        if jobs.enabled and config.session.backend != "none":
            # 別プロセスで走っているジョブとの競合も 409 にするため、Invoker の排他判定にジャーナルを加える。
            invoker.busy_check = jobs.session_busy
        return cls(
            base_dir=base_dir,
            config_path=path,
            config=config,
            registry=registry,
            factory=factory,
            invoker=invoker,
            jobs=jobs,
            shared_store=shared_store,
        )


def _job_journal(
    config: RuntimeConfig, factory: AgentFactory, base_dir: Path, shared_store: SharedStore | None = None
) -> JobJournal | None:
    """``[jobs] storage`` に従ってジャーナルの置き場を決める。置ける Storage がなければジョブ機能は無効。"""
    if config.jobs.storage == "none":
        return None
    if config.jobs.storage == "session":
        return JobJournal(shared_store or SharedStore(config.session))
    spec = config.session.history if config.jobs.storage == "inherit" else config.jobs.as_history_spec()
    if spec.backend == "local":
        return JobJournal(
            JournalStorage((base_dir / (spec.dir or DEFAULT_LOCAL_SESSION_DIR) / "jobs.sqlite3").resolve())
        )
    storage = factory.sessions.storage_for(spec)
    return JobJournal(storage) if storage is not None else None
