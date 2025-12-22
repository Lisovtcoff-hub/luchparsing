
"""Модуль logging_setup.

Содержит прикладную логику и точки входа проекта.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging() -> None:
    """Функция setup_logging.
    """
    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(level)


    if root.handlers:
        return

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    root.addHandler(console)

    log_file = os.getenv("LOG_FILE")
    if log_file:
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
        root.addHandler(file_handler)


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
