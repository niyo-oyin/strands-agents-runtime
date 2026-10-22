"""Process-wide logging for the CLI and the server.

Everything (runtime, Strands, uvicorn, Python ``warnings``) goes through the root logger to stderr and,
when ``[server] log_file`` is set, to one file as well. A file is what lets someone who cannot watch the
terminal (another operator, an agent, CI) read what the server saw.
"""

from __future__ import annotations

import logging
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int, *, log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)

    # UserWarning and friends (e.g. the SDK's context_window_limit notice) become log records.
    logging.captureWarnings(True)
