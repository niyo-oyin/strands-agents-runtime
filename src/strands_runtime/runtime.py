"""組み立ての根。設定を読み、レジストリ → 工場 → Invoker の順に配線する。

HTTP サーバーも CLI も :meth:`Runtime.load` から始まるので、起動の経路は 1 つに揃う。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from strands_runtime.definitions import Registry, RuntimeConfig, load_runtime_config
from strands_runtime.factory import AgentFactory
from strands_runtime.invoke import Invoker
from strands_runtime.jobs import JobJournal, JobManager
from strands_runtime.telemetry import setup_telemetry

CONFIG_ENV_VAR = "STRANDS_RUNTIME_CONFIG"
DEFAULT_CONFIG_FILE = "runtime.toml"


@dataclass(frozen=True)
class Runtime:
    """読み込み済みの設定と、配線済みの各層。"""

    base_dir: Path
    config: RuntimeConfig
    registry: Registry
    factory: AgentFactory
    invoker: Invoker
    jobs: JobManager

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
        factory = AgentFactory(registry, config, base_dir=base_dir)
        setup_telemetry()
        invoker = Invoker(factory, config)
        jobs = JobManager(invoker, _job_journal(config, factory), max_duration_seconds=config.jobs.max_duration_seconds)
        if jobs.enabled:
            # 別プロセスで走っているジョブとの競合も 409 にするため、Invoker の排他判定にジャーナルを加える。
            invoker.busy_check = jobs.session_busy
        return cls(base_dir=base_dir, config=config, registry=registry, factory=factory, invoker=invoker, jobs=jobs)


def _job_journal(config: RuntimeConfig, factory: AgentFactory) -> JobJournal | None:
    """``[jobs] storage`` に従ってジャーナルの置き場を決める。置ける Storage がなければジョブ機能は無効。"""
    spec = config.session if config.jobs.storage == "inherit" else config.jobs.as_session_spec()
    storage = factory.sessions.storage_for(spec)
    return JobJournal(storage) if storage is not None else None
