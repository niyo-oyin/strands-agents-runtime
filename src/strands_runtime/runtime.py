"""Composition root: load configuration and wire registry → factory → invoker.

Both the HTTP server and the CLI start from :meth:`Runtime.load` so they share one code path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from strands_runtime.definitions import Registry, RuntimeConfig, load_runtime_config
from strands_runtime.factory import AgentFactory
from strands_runtime.invoke import Invoker
from strands_runtime.telemetry import setup_telemetry

CONFIG_ENV_VAR = "STRANDS_RUNTIME_CONFIG"
DEFAULT_CONFIG_FILE = "runtime.toml"


@dataclass(frozen=True)
class Runtime:
    base_dir: Path
    config: RuntimeConfig
    registry: Registry
    factory: AgentFactory
    invoker: Invoker

    @classmethod
    def load(cls, config_path: Path | None = None) -> Runtime:
        """Load ``runtime.toml`` (argument, ``$STRANDS_RUNTIME_CONFIG`` or ``./runtime.toml``) and all agents."""
        path = (config_path or Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_FILE))).resolve()
        base_dir = path.parent
        config = load_runtime_config(path)
        registry = Registry.load(config, base_dir)
        factory = AgentFactory(registry, config, base_dir=base_dir)
        setup_telemetry()
        return cls(
            base_dir=base_dir, config=config, registry=registry, factory=factory, invoker=Invoker(factory, config)
        )
