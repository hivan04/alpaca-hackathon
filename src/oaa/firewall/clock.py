"""Session clock and phase machine, in US/Eastern.

Every time in this system is derived from here. Servers run in UTC, London, or
wherever the container lands; the market does not care. Anything that compares
a wall clock to a market event goes through `SessionClock`.

The phase machine encodes the book structure:

    carry book          RESIDENT. Opens inside its own entry window and is then
                        held for 3-10 sessions. It is NOT flattened nightly -
                        theta accrues on calendar days, including weekends, and
                        a nightly round trip would pay the spread for nothing.
    intraday book       TRANSIENT tenant. Opens 09:45-14:45, hard-liquidated at
                        15:15, and never carried into the close.
    opportunistic book  TRANSIENT tenant on the same cutoff, dormant by default.

Because the carry book is resident, "flat" is no longer a whole-account
property. The 15:15 cutoff proves the *transient* books are flat; the 15:45
verification proves the carry book's margin is still covered with headroom.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

try:  # stdlib on 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

ET = "America/New_York"


class Phase(str, Enum):
    """Where we are in the trading day, from the firewall's point of view."""

    CLOSED = "closed"                      # outside RTH; the carry book is held
    OPEN_SETTLE = "open_settle"            # 09:30-09:45, quotes are wide, nothing opens
    INTRADAY = "intraday"                  # intraday book may open
    ACTIVE = "active"                      # intraday AND carry may open
    CARRY_ONLY = "carry_only"              # intraday wind-down, carry may still open
    WIND_DOWN = "wind_down"                # manage only, no new entries anywhere
    INTRADAY_CUTOFF = "intraday_cutoff"    # 15:15 hard liquidation of transient books
    CARRY_VERIFY = "carry_verify"          # 15:45 carry margin verification

    @property
    def intraday_may_open(self) -> bool:
        return self in (Phase.INTRADAY, Phase.ACTIVE)

    @property
    def carry_may_open(self) -> bool:
        return self in (Phase.ACTIVE, Phase.CARRY_ONLY)

    @property
    def transient_must_be_flat(self) -> bool:
        return self in (Phase.INTRADAY_CUTOFF, Phase.CARRY_VERIFY, Phase.CLOSED)


def _hhmm(value: str) -> dt.time:
    hour, minute = (int(part) for part in value.split(":")[:2])
    return dt.time(hour, minute)


@dataclass(frozen=True)
class SessionTimes:
    """Every boundary in the day. All ET, all configurable."""

    market_open: dt.time = dt.time(9, 30)
    intraday_start: dt.time = dt.time(9, 45)
    carry_entry_start: dt.time = dt.time(10, 0)
    intraday_last_entry: dt.time = dt.time(14, 45)
    carry_entry_end: dt.time = dt.time(15, 0)
    intraday_cutoff: dt.time = dt.time(15, 15)
    carry_verification: dt.time = dt.time(15, 45)
    market_close: dt.time = dt.time(16, 0)

    @classmethod
    def from_config(cls, cfg: object) -> SessionTimes:
        get = lambda name, default: _hhmm(getattr(cfg, name, None) or default)  # noqa: E731
        return cls(
            market_open=get("market_open", "09:30"),
            intraday_start=get("intraday_start", "09:45"),
            carry_entry_start=get("carry_entry_start", "10:00"),
            intraday_last_entry=get("intraday_last_entry", "14:45"),
            carry_entry_end=get("carry_entry_end", "15:00"),
            intraday_cutoff=get("intraday_cutoff", "15:15"),
            carry_verification=get("carry_verification", "15:45"),
            market_close=get("market_close", "16:00"),
        )

    def ordered(self) -> list[tuple[dt.time, str]]:
        return [
            (self.market_open, "market_open"),
            (self.intraday_start, "intraday_start"),
            (self.carry_entry_start, "carry_entry_start"),
            (self.intraday_last_entry, "intraday_last_entry"),
            (self.carry_entry_end, "carry_entry_end"),
            (self.intraday_cutoff, "intraday_cutoff"),
            (self.carry_verification, "carry_verification"),
            (self.market_close, "market_close"),
        ]

    def validate(self) -> None:
        """Boundaries must be strictly increasing, or the firewall has a hole."""
        times = [t for t, _ in self.ordered()]
        for earlier, later in zip(times, times[1:], strict=False):
            if earlier >= later:
                raise ValueError(
                    f"firewall session times are out of order: {earlier} >= {later}. "
                    "Every boundary must be strictly increasing."
                )
        gap = minutes_between(self.intraday_cutoff, self.carry_verification)
        if gap < 15:
            raise ValueError(
                f"only {gap} minutes between the intraday cutoff and carry "
                "verification. Liquidations need time to settle - keep at least 15."
            )


def minutes_between(a: dt.time, b: dt.time) -> int:
    return (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)


_minutes_between = minutes_between  # backwards-compatible alias


class SessionClock:
    """ET wall clock plus the phase machine.

    `frozen_now` exists so the backtest and the tests can drive the machine
    deterministically instead of sleeping through a real day.
    """

    def __init__(
        self,
        times: SessionTimes | None = None,
        timezone: str = ET,
        frozen_now: dt.datetime | None = None,
    ) -> None:
        self.times = times or SessionTimes()
        self.times.validate()
        self.timezone = timezone
        self.frozen_now = frozen_now

    # -- time ------------------------------------------------------------- #
    def now(self) -> dt.datetime:
        if self.frozen_now is not None:
            return self.to_et(self.frozen_now)
        if ZoneInfo is None:  # pragma: no cover
            return dt.datetime.now()
        return dt.datetime.now(ZoneInfo(self.timezone))

    def to_et(self, moment: dt.datetime) -> dt.datetime:
        if ZoneInfo is None:  # pragma: no cover
            return moment
        zone = ZoneInfo(self.timezone)
        if moment.tzinfo is None:
            return moment.replace(tzinfo=zone)
        return moment.astimezone(zone)

    def freeze(self, moment: dt.datetime | None) -> None:
        self.frozen_now = moment

    # -- calendar ---------------------------------------------------------- #
    def is_weekday(self, moment: dt.datetime | None = None) -> bool:
        return (moment or self.now()).weekday() < 5

    # -- phase -------------------------------------------------------------- #
    def phase(self, moment: dt.datetime | None = None) -> Phase:
        now = self.to_et(moment) if moment else self.now()
        clock = now.time()
        t = self.times

        # Weekends and out-of-hours: the carry book is simply held. There is no
        # nightly handoff any more, so there is nothing to do here.
        if not self.is_weekday(now):
            return Phase.CLOSED
        if clock < t.market_open:
            return Phase.CLOSED
        if clock < t.intraday_start:
            return Phase.OPEN_SETTLE
        if clock < t.carry_entry_start:
            return Phase.INTRADAY
        if clock < t.intraday_last_entry:
            return Phase.ACTIVE
        if clock < t.carry_entry_end:
            return Phase.CARRY_ONLY
        if clock < t.intraday_cutoff:
            return Phase.WIND_DOWN
        if clock < t.carry_verification:
            return Phase.INTRADAY_CUTOFF
        if clock < t.market_close:
            return Phase.CARRY_VERIFY
        return Phase.CLOSED

    # -- helpers ------------------------------------------------------------ #
    def at_or_past(self, boundary: str, moment: dt.datetime | None = None) -> bool:
        now = self.to_et(moment) if moment else self.now()
        target = getattr(self.times, boundary)
        return now.time() >= target

    def minutes_until(self, boundary: str, moment: dt.datetime | None = None) -> int:
        now = self.to_et(moment) if moment else self.now()
        target = getattr(self.times, boundary)
        return minutes_between(now.time(), target)

    def in_window(
        self, start: str, end: str, moment: dt.datetime | None = None
    ) -> bool:
        """Half-open [start, end) test against two named boundaries or HH:MM."""
        now = self.to_et(moment) if moment else self.now()
        lo = getattr(self.times, start, None) or _hhmm(start)
        hi = getattr(self.times, end, None) or _hhmm(end)
        return lo <= now.time() < hi

    def session_date(self, moment: dt.datetime | None = None) -> dt.date:
        now = self.to_et(moment) if moment else self.now()
        return now.date()

    def describe(self, moment: dt.datetime | None = None) -> str:
        now = self.to_et(moment) if moment else self.now()
        return f"{now:%Y-%m-%d %H:%M:%S %Z} phase={self.phase(now).value}"
