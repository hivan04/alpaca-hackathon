"""Structured logging.

Two sinks, always: a human-readable stream for the terminal, and an
append-only JSONL journal that becomes the evidence trail for the judges.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_CONFIGURED = False

_COLORS = {
    "DEBUG": "\033[38;5;244m",
    "INFO": "\033[38;5;39m",
    "WARNING": "\033[38;5;214m",
    "ERROR": "\033[38;5;196m",
    "CRITICAL": "\033[48;5;196m\033[38;5;231m",
}
_RESET = "\033[0m"


class _ConsoleFormatter(logging.Formatter):
    def __init__(self, color: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.color:
            return text
        return f"{_COLORS.get(record.levelname, '')}{text}{_RESET}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "__dict__", {}).items():
            if key.startswith("oaa_"):
                payload[key[4:]] = value
        return json.dumps(payload, default=str)


def setup_logging(
    level: str = "INFO",
    fmt: str = "console",
    logfile: str | Path | None = None,
) -> None:
    global _CONFIGURED
    root = logging.getLogger("oaa")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _JsonFormatter() if fmt == "json" else _ConsoleFormatter(color=sys.stderr.isatty())
    )
    root.addHandler(handler)

    if logfile:
        path = Path(logfile)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(_JsonFormatter())
        root.addHandler(file_handler)

    # Third-party noise we do not want in the trade log.
    for noisy in ("httpx", "httpcore", "urllib3", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(f"oaa.{name}" if not name.startswith("oaa") else name)
