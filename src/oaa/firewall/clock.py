"""Session clock and phase machine, in US/Eastern.

Every time in this system is derived from here. Servers run in UTC, London,
or wherever the container lands; the market does not care. Anything that
compares a wall clock to a market event goes through `SessionClock`.
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

    CLOSED = "closed"                    # outside RTH, no overnight book held
    OVERNIGHT_HOLD = "overnight_hold"    # position carried from yesterday
    OVERNIGHT_EXIT = "overnight_exit"    # 09:35 - liquidate the overnight book
    SETTLE = "settle"                    # post-exit, pre-intraday: flat by design
    INTRADAY = "intraday"                # intraday book may open
    INTRADAY_WIND_DOWN = "wind_down"     # manage only, no new intraday entries
    INTRADAY_CUTOFF = "intraday_cutoff"  # 15:15 - hard liquidation of the day book
    OVERNIGHT_SIGNAL = "overnight_signal"  # 15:45 - models compute, nothing routed
    OVERNIGHT_VERIFY = "overnight_verify"  # 15:54 - prove flat, size against Reg T
    OVERNIGHT_ENTRY = "overnight_entry"    # 15:55 - dispatch the pairs trade

    @property
    def intraday_may_open(self) -> bool:
        return self is Phase.INTRADAY

    @property
    def overnight_may_open(self) -> bool:
        return self is Phase.OVERNIGHT_ENTRY


def _hhmm(value: str) -> dt.time:
    hour, minute = (int(part) for part in value.split(":")[:2])
    return dt.time(hour, minute)


@dataclass(frozen=True)
class SessionTimes:
    """Every boundary in the day. All ET, all configurable."""

    market_open: dt.time = dt.time(9, 30)
    overnight_exit: dt.time = dt.time(9, 35)
    intraday_start: dt.time = dt.time(10, 0)
    intraday_last_entry: dt.time = dt.time(15, 0)
    intraday_cutoff: dt.time = dt.time(15, 15)
    overnight_signal: dt.time = dt.time(15, 45)
    overnight_verify: dt.time = dt.time(15, 54)
    overnight_entry: dt.time = dt.time(15, 55)
    market_close: dt.time = dt.time(16, 0)

    @classmethod
    def from_config(cls, cfg: object) -> SessionTimes:
        get = lambda name, default: _hhmm(getattr(cfg, name, None) or default)  # noqa: E731
        return cls(
            market_open=get("market_open", "09:30"),
            overnight_exit=get("overnight_exit", "09:35"),
            intraday_start=get("intraday_start", "10:00"),
            intraday_last_entry=get("intraday_last_entry", "15:00"),
            intraday_cutoff=get("intraday_cutoff", "15:15"),
            overnight_signal=get("overnight_signal", "15:45"),
            overnight_verify=get("overnight_verify", "15:54"),
            overnight_entry=get("overnight_entry", "15:55"),
            market_close=get("market_close", "16:00"),
        )

    def ordered(self) -> list[tuple[dt.time, str]]:
        return [
            (self.market_open, "market_open"),
            (self.overnight_exit, "overnight_exit"),
            (self.intraday_start, "intraday_start"),
            (self.intraday_last_entry, "intraday_last_entry"),
            (self.intraday_cutoff, "intraday_cutoff"),
            (self.overnight_signal, "overnight_signal"),
            (self.overnight_verify, "overnight_verify"),
            (self.overnight_entry, "overnight_entry"),
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
        gap = _minutes_between(self.intraday_cutoff, self.overnight_verify)
        if gap < 15:
            raise ValueError(
                f"only {gap} minutes between the intraday cutoff and overnight "
                "verification. Liquidations need time to settle - keep at least 15."
            )


def _minutes_between(a: dt.time, b: dt.time) -> int:
    return (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)


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

        if not self.is_weekday(now):
            return Phase.OVERNIGHT_HOLD

        if clock < t.market_open:
            # Before the bell: yesterday's overnight book is still on.
            return Phase.OVERNIGHT_HOLD
        if clock < t.overnight_exit:
            return Phase.OVERNIGHT_HOLD
        if clock < t.intraday_start:
            # The overnight exit fires at the boundary; the rest is settling.
            return Phase.OVERNIGHT_EXIT if clock == t.overnight_exit else Phase.SETTLE
        if clock < t.intraday_last_entry:
            return Phase.INTRADAY
        if clock < t.intraday_cutoff:
            return Phase.INTRADAY_WIND_DOWN
        if clock < t.overnight_signal:
            return Phase.INTRADAY_CUTOFF
        if clock < t.overnight_verify:
            return Phase.OVERNIGHT_SIGNAL
        if clock < t.overnight_entry:
            return Phase.OVERNIGHT_VERIFY
        if clock < t.market_close:
            return Phase.OVERNIGHT_ENTRY
        return Phase.OVERNIGHT_HOLD

    # -- helpers ------------------------------------------------------------ #
    def at_or_past(self, boundary: str, moment: dt.datetime | None = None) -> bool:
        now = self.to_et(moment) if moment else self.now()
        target = getattr(self.times, boundary)
        return now.time() >= target

    def minutes_until(self, boundary: str, moment: dt.datetime | None = None) -> int:
        now = self.to_et(moment) if moment else self.now()
        target = getattr(self.times, boundary)
        return _minutes_between(now.time(), target)

    def session_date(self, moment: dt.datetime | None = None) -> dt.date:
        """The trading date a moment belongs to.

        After the close, the overnight book belongs to the session that just
        ended — so a 20:00 snapshot is still 'today', not 'tomorrow'.
        """
        now = self.to_et(moment) if moment else self.now()
        return now.date()

    def describe(self, moment: dt.datetime | None = None) -> str:
        now = self.to_et(moment) if moment else self.now()
        return f"{now:%Y-%m-%d %H:%M:%S %Z} phase={self.phase(now).value}"
