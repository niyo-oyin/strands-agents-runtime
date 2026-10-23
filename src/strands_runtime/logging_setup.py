"""CLI とサーバーで共通のプロセス全体のログ設定。

ランタイム・Strands・uvicorn・Python の ``warnings`` をすべてルートロガーに集め、標準エラー出力と、
``[server] log_file`` が設定されていれば 1 つのファイルにも書く。
ファイルがあれば、端末を見られない人（別の運用者、エージェント、CI）もサーバーの出力を読める。
"""

from __future__ import annotations

import logging
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int, *, log_file: Path | None = None) -> None:
    """ルートロガーのハンドラーを付け直す。何度呼んでもハンドラーが重複しない。"""
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

    # UserWarning など（SDK の context_window_limit の警告を含む）もログレコードにして、同じ出口に流す。
    logging.captureWarnings(True)
