"""The arm deadline is parsed from config, not just documented in it.

`schedule.no_entry_after` sat in the events params from the day the book was
written and, until 30 Aug, was read by NOTHING - the same shape of defect as
the `model` and `seed` fields a comment promised and no code consumed. This
file pins the config half; the behaviour half lives in test_events_in_run.py,
where the orchestrator fixtures are.
"""

from __future__ import annotations

import datetime as dt

from oaa.strategies.events.params import load_params


def test_the_deadline_is_parsed_from_the_shipped_config():
    params = load_params("config/strategies/earnings_event.yaml")
    # 15:58 for the 2-3 Sep arms only; restores to 15:55. See the dated note
    # in config/strategies/earnings_event.yaml.
    assert params.no_entry_after_at() == dt.time(15, 58)


def test_the_deadline_sits_after_the_arm_and_before_the_close():
    """A deadline before the arm would cancel the book every night; one after
    16:00 would not be a deadline."""
    params = load_params("config/strategies/earnings_event.yaml")
    assert params.arm_at() < params.no_entry_after_at() <= dt.time(16, 0)
