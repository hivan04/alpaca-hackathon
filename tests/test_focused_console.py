"""The focused console: what an operator sees, and what it must never hide.

`telemetry.console: focused` is a decision about ONE SCREEN. The risk it
carries is that someone later reads it as a decision about what the system
records - and quietly loses the rejection funnel, which is the highest-value
artefact this repo produces. These tests pin both halves: the screen gets
quieter, and nothing else changes.
"""

from __future__ import annotations

import logging

from oaa.config.loader import load_settings
from oaa.core.logging import TAPE, _FocusedFilter


def _record(name: str, level: int) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, "msg", None, None)


def test_the_tape_survives_and_the_diagnostics_do_not():
    keep = _FocusedFilter()
    assert keep.filter(_record(TAPE, logging.INFO)), "the tape is the point of the mode"
    assert not keep.filter(_record("oaa.risk.engine", logging.INFO)), (
        "a REJECT line is diagnostic - it belongs in the journal, not on the screen"
    )


def test_nothing_that_went_wrong_is_ever_filtered():
    """The mode exists to make errors visible, so it must not eat one."""
    keep = _FocusedFilter()
    for level in (logging.WARNING, logging.ERROR, logging.CRITICAL):
        assert keep.filter(_record("oaa.data.alpaca", level)), (
            f"a {logging.getLevelName(level)} from any logger must reach the screen"
        )


def test_focused_only_touches_the_terminal_handler():
    """The filter is attached to the stream handler, never to the logger.

    Attached to the logger it would also strip the JSONL sink and the journal
    would stop matching the run - the exact confusion this mode must not cause.
    """
    from oaa.core.logging import setup_logging

    setup_logging("INFO", "console", console="focused")
    root = logging.getLogger("oaa")
    assert root.level == logging.INFO, "no logger's level is changed by the mode"
    assert not root.filters, "the filter belongs on the handler, not the logger"
    assert any(
        any(isinstance(f, _FocusedFilter) for f in h.filters) for h in root.handlers
    ), "the terminal handler must carry the filter"
    setup_logging("INFO", "console")  # restore for the rest of the suite


def test_full_is_still_reachable_and_unfiltered():
    from oaa.core.logging import setup_logging

    setup_logging("INFO", "console", console="full")
    root = logging.getLogger("oaa")
    assert not any(
        any(isinstance(f, _FocusedFilter) for f in h.filters) for h in root.handlers
    ), "full must stay exactly what it was before the mode existed"


def test_the_shipped_config_declares_a_console_mode():
    cfg = load_settings(profile="dev").config
    assert cfg.telemetry.console in {"full", "focused"}
