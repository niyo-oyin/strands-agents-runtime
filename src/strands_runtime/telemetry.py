"""環境が求めたときにだけ、Strands の OpenTelemetry スパンをエクスポーターに配線する。

計装そのものは Strands が行う。このモジュールが決めるのはスパンの送り先だけ。

- ``opentelemetry-instrument`` 配下（AgentCore の ADOT など）では既にグローバルな ``TracerProvider`` があり、
  Strands はそれを自動で使う。``OTEL_TRACES_EXPORTER`` の値に関係なく、ここでは何もしない。
- それ以外では ``OTEL_TRACES_EXPORTER=otlp|console``（カンマ区切り可）でエクスポーターを選ぶ。未設定なら送らない。
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.trace import ProxyTracerProvider

logger = logging.getLogger(__name__)

_configured = False


def setup_telemetry() -> None:
    """プロセスで 1 度だけ実行する。2 度目以降は何もしない。"""
    global _configured
    if _configured:
        return
    _configured = True

    # ``ProxyTracerProvider`` は「まだ誰も設定していない」ことを表す既定の実装。
    # それ以外が入っていれば外部（ADOT など）が設定済みなので、二重にエクスポーターを付けない。
    if not isinstance(trace.get_tracer_provider(), ProxyTracerProvider):
        logger.debug("global TracerProvider already set; leaving telemetry to it")
        return

    selected = {name.strip().lower() for name in os.environ.get("OTEL_TRACES_EXPORTER", "").split(",") if name.strip()}
    if not selected or "none" in selected:
        return

    from strands.telemetry import StrandsTelemetry

    telemetry = StrandsTelemetry()
    if "otlp" in selected:
        telemetry.setup_otlp_exporter()
    if "console" in selected:
        telemetry.setup_console_exporter()
    unsupported = selected - {"otlp", "console"}
    if unsupported:
        logger.warning("OTEL_TRACES_EXPORTER values not supported here: %s", ", ".join(sorted(unsupported)))
