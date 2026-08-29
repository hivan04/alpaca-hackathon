"""Which names report this week, and when.

The LLM proposes; the calendar file disposes.

Featherless serves open-weight models, and a model's weights were frozen months
before any given Tuesday - it cannot know that Broadcom moved its print to the
2nd. Asked to "list next week's earnings" it will answer fluently and
sometimes wrongly, and a wrong date here is not a bad trade, it is a position
opened against no event at all.

So the screener runs in two halves. The model proposes candidates from the
liquid options universe, which is a judgement call it is genuinely good at
(which names have weeklies, which prints the market cares about). Every
proposal is then checked against `config/events/earnings_calendar.json`, whose
rows carry a confirmed date, a session and a source. Anything unverified is
logged as a proposal and never armed.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaa.core.errors import ConfigError
from oaa.core.logging import get_logger

log = get_logger("strategies.events.calendar")

AMC, BMO = "amc", "bmo"


@dataclass(frozen=True)
class EarningsEvent:
    """One confirmed print."""

    symbol: str
    report_date: dt.date
    #: "amc" - after the close on report_date; "bmo" - before the open.
    timing: str
    confirmed: bool
    source: str = ""
    #: Signed 1-day close-to-close reactions to previous prints, most recent
    #: first. Feeds the realised half of the volatility screen.
    history: tuple[float, ...] = ()

    @property
    def entry_date(self) -> dt.date:
        """The session whose close we arm into.

        After-close print: arm on the report date itself. Before-open print:
        arm on the previous session, because by the time it prints the market
        has not opened.
        """
        return self.report_date if self.timing == AMC else _prev_business_day(self.report_date)

    @property
    def exit_date(self) -> dt.date:
        """The session we close into. Always the first session after the print."""
        return _next_business_day(self.report_date) if self.timing == AMC else self.report_date

    @property
    def mean_abs_history(self) -> float | None:
        if not self.history:
            return None
        return round(sum(abs(m) for m in self.history) / len(self.history), 4)

    def summary(self) -> str:
        timing = "after close" if self.timing == AMC else "before open"
        return f"{self.symbol} {self.report_date:%d %b} ({timing})"


@dataclass
class ScreenResult:
    """What the screener found, and what it refused."""

    events: list[EarningsEvent] = field(default_factory=list)
    #: Proposed by the model, absent from the calendar file. Never armed - but
    #: worth surfacing, because a name that keeps appearing here is a gap in
    #: the calendar rather than a model error.
    unverified: list[str] = field(default_factory=list)
    #: In the calendar for this window but not proposed by the model.
    missed_by_model: list[str] = field(default_factory=list)
    llm_used: bool = False

    def symbols(self) -> list[str]:
        return [e.symbol for e in self.events]

    def summary(self) -> str:
        return (
            f"{len(self.events)} confirmed event(s); "
            f"{len(self.unverified)} unverified proposal(s); "
            f"model {'ran' if self.llm_used else 'unavailable - calendar only'}"
        )


# --------------------------------------------------------------------------- #
# the calendar file
# --------------------------------------------------------------------------- #
def load_calendar(path: str | Path) -> dict[str, EarningsEvent]:
    source = Path(path)
    if not source.exists():
        raise ConfigError(
            f"earnings calendar not found at {source}. This file is the only "
            "thing standing between an LLM's recollection and a live order - "
            "the book will not arm without it."
        )
    payload = json.loads(source.read_text())
    rows = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ConfigError(f"{source}: expected an 'events' list")

    calendar: dict[str, EarningsEvent] = {}
    for row in rows:
        try:
            symbol = str(row["symbol"]).upper()
            timing = str(row.get("timing", AMC)).lower()
            if timing not in {AMC, BMO}:
                raise ConfigError(f"{symbol}: timing must be 'amc' or 'bmo', got '{timing}'")
            calendar[symbol] = EarningsEvent(
                symbol=symbol,
                report_date=dt.date.fromisoformat(str(row["report_date"])),
                timing=timing,
                confirmed=bool(row.get("confirmed", False)),
                source=str(row.get("source", "")),
                history=tuple(float(m) for m in (row.get("history") or ())),
            )
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"{source}: bad calendar row {row!r} - {exc}") from exc
    return calendar


def events_between(
    calendar: dict[str, EarningsEvent], start: dt.date, end: dt.date, confirmed_only: bool = True
) -> list[EarningsEvent]:
    found = [e for e in calendar.values() if start <= e.report_date <= end]
    if confirmed_only:
        rejected = [e.symbol for e in found if not e.confirmed]
        if rejected:
            log.info("calendar: %s in window but unconfirmed - not armed", ", ".join(rejected))
        found = [e for e in found if e.confirmed]
    return sorted(found, key=lambda e: (e.report_date, e.symbol))


# --------------------------------------------------------------------------- #
# the LLM half
# --------------------------------------------------------------------------- #
SCREENER_SYSTEM = (
    "You are a screening assistant for an options desk. You are given a date "
    "window and asked which US-listed companies with liquid weekly options are "
    "scheduled to report earnings inside it. You are NOT the source of truth "
    "for dates - every name you return is checked against a confirmed calendar "
    "before anything is traded, and a name you invent is discarded, not acted "
    "on. Prefer names with weekly option expiries, a market capitalisation "
    "above roughly two billion dollars, and share prices above ten dollars. "
    "Return fewer names rather than padding the list."
)


def propose_candidates(
    llm: Any, start: dt.date, end: dt.date, hint: list[str] | None = None, limit: int = 40
) -> list[str]:
    """Ask Featherless which names report in the window. Never trusted alone."""
    if llm is None or getattr(llm, "provider", "null") == "null":
        return []
    hint_line = (
        f"\nNames already on the desk's watchlist: {', '.join(hint)}." if hint else ""
    )
    user = (
        f"Date window: {start:%A %d %B %Y} to {end:%A %d %B %Y} inclusive.\n"
        f"List the US-listed companies you believe report earnings in that "
        f"window and whose options a desk could trade.{hint_line}\n\n"
        'Respond as {"tickers": ["AAA", "BBB"], "reasoning": "one sentence"}. '
        f"At most {limit} tickers."
    )
    payload = llm.json_complete(SCREENER_SYSTEM, user, default={})
    tickers = payload.get("tickers")
    if not isinstance(tickers, list):
        log.warning("screener returned no ticker list - falling back to the calendar alone")
        return []
    clean: list[str] = []
    for item in tickers[:limit]:
        symbol = str(item).strip().upper()
        # Guard the obvious injection/format failure: the model returning a
        # sentence, a company name, or a option symbol instead of a ticker.
        if symbol.isalpha() and 1 <= len(symbol) <= 5:
            clean.append(symbol)
    log.info("screener proposed %d candidate(s): %s", len(clean), ", ".join(clean))
    return clean


def screen_week(
    llm: Any,
    calendar: dict[str, EarningsEvent],
    start: dt.date,
    end: dt.date,
    hint: list[str] | None = None,
) -> ScreenResult:
    """The full screen: propose, verify, report both halves.

    The calendar is authoritative in BOTH directions. A confirmed event the
    model failed to mention is still traded - the model's job is to widen the
    net, never to narrow it below what is known.
    """
    proposed = propose_candidates(llm, start, end, hint)
    confirmed = events_between(calendar, start, end)
    known = {e.symbol for e in confirmed}

    result = ScreenResult(
        events=confirmed,
        unverified=sorted(set(proposed) - known),
        missed_by_model=sorted(known - set(proposed)) if proposed else [],
        llm_used=bool(proposed),
    )
    if result.unverified:
        log.info(
            "screener proposals with no confirmed calendar row (not armed): %s",
            ", ".join(result.unverified),
        )
    log.info("earnings screen %s-%s: %s", start, end, result.summary())
    return result


def _prev_business_day(day: dt.date) -> dt.date:
    step = day - dt.timedelta(days=1)
    while step.weekday() >= 5:
        step -= dt.timedelta(days=1)
    return step


def _next_business_day(day: dt.date) -> dt.date:
    step = day + dt.timedelta(days=1)
    while step.weekday() >= 5:
        step += dt.timedelta(days=1)
    return step
