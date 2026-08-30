"""Structured logging.

Two sinks, always: a human-readable stream for the terminal, and an
append-only JSONL journal that becomes the evidence trail for the judges.

**The tape.** `oaa.tape` is a reserved logger for the handful of moments an
operator watching a live terminal actually needs: research finished on a name,
a position opened, a position closed and what it made or lost. Everything else
the process says - every REJECT line, every per-symbol evidence count - is
diagnostic. It matters, but it matters in the journal, where it can be read
after the fact by `oaa gates` and the dashboard.

`console="focused"` acts on that split: the terminal handler then passes the
tape and anything at WARNING or above, and drops the rest. Nothing is
suppressed at source - the level of every logger is unchanged, the JSONL sink
still receives everything, and the journal is untouched. It is a decision about
one screen, not about what the system records. `console="full"` (the default)
is the old behaviour.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_CONFIGURED = False

# The logger name the tape reserves. Anything logged under it survives
# `console="focused"`, so it is deliberately narrow - see tape() below.
TAPE = "oaa.tape"

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


class _FocusedFilter(logging.Filter):
    """Terminal-only: the tape, plus anything that went wrong.

    A filter rather than a level change, deliberately. Raising the root level
    to WARNING would also silence the JSONL sink, and would mean the tape had
    to be logged at a severity it does not have - an opened position is not a
    warning, and a log that says it is trains an operator to ignore orange.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(TAPE) or record.levelno >= logging.WARNING


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
    console: str = "full",
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
    if console == "focused":
        handler.addFilter(_FocusedFilter())
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


def tape() -> logging.Logger:
    """The operator's line: entries, exits and research-complete, nothing else.

    Every message added here is one the focused console cannot hide, so the
    value of the mode is exactly the discipline about what gets put on it.
    """
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(TAPE)


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(f"oaa.{name}" if not name.startswith("oaa") else name)
