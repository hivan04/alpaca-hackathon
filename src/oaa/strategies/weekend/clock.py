"""The weekend window.

The whole point of this book is to own the hours the options books cannot
trade. That makes the clock a safety device, not a convenience: if the window
is wrong the crypto book and the equity books compete for the same buying
power on a Monday morning, which is precisely the failure the capital firewall
exists to prevent.

Three instants, all UTC, all derived from the US equity session:

    OPEN         Friday 20:05 UTC   five minutes after the 16:00 ET close, so
                                    the 15:15/15:45 firewall sign-off has
                                    already released the transient lease.
    LAST_ENTRY   Sunday  12:00 UTC  eight clear hours for a position to revert
                                    before the flatten. An entry with no room
                                    to work is a coin flip on the close.
    FLAT         Sunday  20:00 UTC  hard liquidation, no exceptions - 17.5
                                    hours before Monday's 09:30 ET open, and a
                                    full night before any pre-market gap.

Everything between FLAT and the next OPEN is CLOSED: the book holds no crypto
while an equity session is live, so its capital never has to be reconciled
against Reg T.

DST: the times are pinned to UTC and re-derived from US/Eastern, so the window
tracks the equity close through the March and November shifts rather than
silently drifting an hour.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

try:  # py3.9+ stdlib; the repo targets 3.10
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

UTC = dt.timezone.utc
_ET = ZoneInfo("America/New_York") if ZoneInfo else UTC

FRIDAY, SATURDAY, SUNDAY = 4, 5, 6


class WindowPhase(str, Enum):
    """Where the clock is. The engine dispatches on exactly this."""

    CLOSED = "closed"          # an equity session is live or about to be
    OPEN = "open"              # entries and exits both allowed
    MANAGE_ONLY = "manage"     # past last entry: exits only
    FLATTEN = "flatten"        # past the hard cutoff: liquidate and stay out


@dataclass(frozen=True)
class WeekendWindow:
    """Session boundaries, expressed as offsets from the US equity close."""

    #: Minutes after Friday's 16:00 ET close before the book may open.
    open_delay_minutes: int = 5
    #: Hard flatten, hours before Monday's 09:30 ET open. 17.5 puts it at
    #: 16:00 ET / 20:00 UTC on Sunday - the equity close, one day on.
    flatten_lead_hours: float = 17.5
    #: No new entries within this many hours of the flatten.
    last_entry_lead_hours: float = 8.0
    #: Emergency brake: set false in YAML and the book never opens.
    enabled: bool = True

    # -- boundary construction -------------------------------------------- #
    def _friday_close_utc(self, now: dt.datetime) -> dt.datetime:
        """The 16:00 ET Friday close bounding the weekend `now` sits in."""
        et = now.astimezone(_ET)
        # Walk back to the most recent Friday (today counts if it IS Friday).
        days_back = (et.weekday() - FRIDAY) % 7
        friday = (et - dt.timedelta(days=days_back)).replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        if friday > et and days_back == 0:
            friday -= dt.timedelta(days=7)
        return friday.astimezone(UTC)

    def opens_at(self, now: dt.datetime) -> dt.datetime:
        return self._friday_close_utc(now) + dt.timedelta(minutes=self.open_delay_minutes)

    def monday_open_utc(self, now: dt.datetime) -> dt.datetime:
        friday_close = self._friday_close_utc(now).astimezone(_ET)
        monday = (friday_close + dt.timedelta(days=3)).replace(
            hour=9, minute=30, second=0, microsecond=0
        )
        return monday.astimezone(UTC)

    def flattens_at(self, now: dt.datetime) -> dt.datetime:
        return self.monday_open_utc(now) - dt.timedelta(hours=self.flatten_lead_hours)

    def last_entry_at(self, now: dt.datetime) -> dt.datetime:
        return self.flattens_at(now) - dt.timedelta(hours=self.last_entry_lead_hours)

    # -- the question the engine actually asks ----------------------------- #
    def phase(self, now: dt.datetime) -> WindowPhase:
        now = _aware(now)
        if not self.enabled:
            return WindowPhase.CLOSED
        opens, last_entry, flat = (
            self.opens_at(now),
            self.last_entry_at(now),
            self.flattens_at(now),
        )
        if now < opens:
            return WindowPhase.CLOSED
        if now >= flat:
            # Between the Sunday flatten and Friday's close there is nothing to
            # do; FLATTEN is returned only in the hours right after the cutoff
            # so a restart there still liquidates rather than idling.
            return WindowPhase.FLATTEN if now < flat + dt.timedelta(hours=6) else WindowPhase.CLOSED
        return WindowPhase.OPEN if now < last_entry else WindowPhase.MANAGE_ONLY

    def may_enter(self, now: dt.datetime) -> bool:
        return self.phase(now) is WindowPhase.OPEN

    def hours_to_flatten(self, now: dt.datetime) -> float:
        return (self.flattens_at(_aware(now)) - _aware(now)).total_seconds() / 3600.0

    def describe(self, now: dt.datetime) -> str:
        now = _aware(now)
        return (
            f"phase={self.phase(now).value} "
            f"opened={self.opens_at(now):%a %d %b %H:%M}Z "
            f"last_entry={self.last_entry_at(now):%a %H:%M}Z "
            f"flat={self.flattens_at(now):%a %H:%M}Z "
            f"({self.hours_to_flatten(now):+.1f}h)"
        )


def _aware(ts: dt.datetime) -> dt.datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
