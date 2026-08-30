"""The earnings-week universe: a callable list of names that report this week.

The other books trade a fixed universe pinned in `config/default.yaml`. This
one cannot: the whole premise is that the tradable set changes every week,
because the events it trades are dated. So the universe is derived from the
confirmed calendar rather than written down twice - one source of truth, and no
way for the list to drift out of step with the dates it was built from.

Callable three ways, all reading the same file:

    from oaa.strategies.events.universe import earnings_universe, symbols
    book = earnings_universe()                 # {"AVGO": {...}, ...}
    names = symbols()                          # ["AI", "AVGO", ...]
    oaa backtest --strategy earnings_event_directional --symbols earnings-week
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from oaa.strategies.events.calendar import EarningsEvent, load_calendar
from oaa.strategies.events.params import DEFAULT_CALENDAR_PATH

#: What `--symbols` accepts as shorthand for "whatever reports this week".
ALIAS = "earnings-week"


def trading_week(asof: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """Monday to Friday of the week containing `asof`.

    Over a weekend this looks FORWARD to the week ahead, because that is when
    someone screening on a Saturday means by "this week".
    """
    day = asof or dt.date.today()
    monday = day - dt.timedelta(days=day.weekday())
    if day.weekday() >= 5:
        monday += dt.timedelta(days=7)
    return monday, monday + dt.timedelta(days=4)


def earnings_universe(
    path: str = DEFAULT_CALENDAR_PATH,
    asof: dt.date | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    confirmed_only: bool = True,
) -> dict[str, dict[str, Any]]:
    """The week's reporters, keyed by ticker.

    Unconfirmed rows are excluded by default and that default matters: a
    backtest that includes a name whose date was a calendar's guess is
    measuring a print that may never have happened on the day it assumed.
    """
    first, last = (start, end) if start and end else trading_week(asof)
    calendar = load_calendar(path)
    out: dict[str, dict[str, Any]] = {}
    for symbol, event in sorted(calendar.items()):
        if not (first <= event.report_date <= last):
            continue
        if confirmed_only and not event.confirmed:
            continue
        out[symbol] = _row(event)
    return out


def symbols(
    path: str = DEFAULT_CALENDAR_PATH,
    asof: dt.date | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    confirmed_only: bool = True,
) -> list[str]:
    """Just the tickers, sorted - the shape a universe argument wants."""
    return sorted(
        earnings_universe(path, asof, start, end, confirmed_only=confirmed_only)
    )


def resolve(
    value: str | None, path: str = DEFAULT_CALENDAR_PATH, asof: dt.date | None = None
) -> list[str] | None:
    """Turn a `--symbols` argument into a list, expanding the alias.

    Returns None when `value` is empty, so a caller can fall back to its own
    default without having to special-case the alias.
    """
    if not value:
        return None
    if value.strip().lower() in {ALIAS, "earnings", "earnings_week"}:
        return symbols(path, asof)
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def _row(event: EarningsEvent) -> dict[str, Any]:
    return {
        "symbol": event.symbol,
        "report_date": event.report_date.isoformat(),
        "timing": event.timing,
        "session": "after close" if event.timing == "amc" else "before open",
        "arms_on": event.entry_date.isoformat(),
        "exits_on": event.exit_date.isoformat(),
        "history": list(event.history),
        "mean_abs_move_pct": event.mean_abs_history,
        "source": event.source,
    }
