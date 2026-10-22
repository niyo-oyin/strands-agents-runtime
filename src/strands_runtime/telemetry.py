"""Wire Strands' OpenTelemetry spans to an exporter when, and only when, the environment asks for one.

Strands instruments the agent loop itself; this module only decides where spans go:

- Under ``opentelemetry-instrument`` (AgentCore with ADOT) a global ``TracerProvider`` already exists
  and Strands uses it automatically, so nothing is done here, whatever ``OTEL_TRACES_EXPORTER`` says.
- Otherwise ``OTEL_TRACES_EXPORTER=otlp|console`` (comma-separated) selects exporters; unset means off.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.trace import ProxyTracerProvider

logger = logging.getLogger(__name__)

_configured = False


def setup_telemetry() -> None:
    global _configured
    if _configured:
        return
    _configured = True

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
