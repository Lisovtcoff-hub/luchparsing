
"""Модуль logging_setup.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


class ManualOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return bool(getattr(record, "manual", False))


def setup_logging() -> None:
    """Функция setup_logging.
    """
    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    manual_only = (os.getenv("LOG_MANUAL_ONLY") or "1").strip().lower() not in ("0", "false", "no", "off")
    root = logging.getLogger()
    root.setLevel(level)

    if root.handlers:
        if manual_only:
            for h in root.handlers:
                h.addFilter(ManualOnlyFilter())
        return

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    if manual_only:
        console.addFilter(ManualOnlyFilter())
    root.addHandler(console)

    log_file = os.getenv("LOG_FILE")
    if log_file:
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
        if manual_only:
            file_handler.addFilter(ManualOnlyFilter())
        root.addHandler(file_handler)


def manual_log(logger: logging.Logger, level: int, msg: str, **extra) -> None:
    logger.log(level, msg, extra={"manual": True, **extra})


def kv(**fields) -> str:

    """Функция kv.

    Возвращает:
        Результат выполнения функции.
    """
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v).replace("\n", "\\n")
        parts.append(f"{k}={s}")
    return " ".join(parts)
