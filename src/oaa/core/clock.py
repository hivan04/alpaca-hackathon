"""A freezable wall clock.

Every gate in this system asks a question about *now*: is the entry window
still open, does an earnings date sit inside the expiry window, how many days
of theta are left. In live trading `now` is the machine clock. In a replay of
June it must be the replayed session, or the strategy answers June's questions
with August's calendar and the backtest is silently wrong - trades that could
never have fired, and event exclusions that never fire because the earnings
date is now in the past.

So the strategy, risk and options layers read the clock through this module
rather than calling `datetime.now()` directly. `BacktestEngine` freezes it to
each replayed timestamp; nothing else touches it, and unfrozen it is the system
clock exactly as before.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager

_frozen: dt.datetime | None = None


def _aware(moment: dt.datetime) -> dt.datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def freeze(moment: dt.datetime | dt.date) -> None:
    """Pin the clock. A bare date freezes to 16:00 UTC on that day."""
    global _frozen
    if isinstance(moment, dt.datetime):
        _frozen = _aware(moment)
    else:
        _frozen = dt.datetime.combine(moment, dt.time(16, 0), tzinfo=dt.timezone.utc)


def unfreeze() -> None:
    global _frozen
    _frozen = None


def is_frozen() -> bool:
    return _frozen is not None


def utcnow() -> dt.datetime:
    return _frozen or dt.datetime.now(dt.timezone.utc)


def now(tz: dt.tzinfo | None = None) -> dt.datetime:
    moment = utcnow()
    return moment.astimezone(tz) if tz else moment


def today() -> dt.date:
    return utcnow().date()


@contextmanager
def frozen(moment: dt.datetime | dt.date) -> Iterator[None]:
    previous = _frozen
    freeze(moment)
    try:
        yield
    finally:
        globals()["_frozen"] = previous
