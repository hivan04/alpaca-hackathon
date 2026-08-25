"""The temporal firewall: the interlock that stops the two books colliding."""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.brokers.sim import SimBroker
from oaa.config.schema import Config
from oaa.core.types import PositionSnapshot
from oaa.firewall.clock import Phase, SessionClock, SessionTimes
from oaa.firewall.lock import Book, TemporalFirewall


# --------------------------------------------------------------------------- #
# the phase machine
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("time", "expected"),
    [
        ("08:00", Phase.OVERNIGHT_HOLD),      # pre-bell, yesterday's book is on
        ("09:31", Phase.OVERNIGHT_HOLD),      # bell rung, exit has not fired
        ("09:35", Phase.OVERNIGHT_EXIT),      # the exit boundary itself
        ("09:50", Phase.SETTLE),              # flat by design
        ("10:30", Phase.INTRADAY),
        ("15:05", Phase.INTRADAY_WIND_DOWN),  # manage only
        ("15:20", Phase.INTRADAY_CUTOFF),
        ("15:46", Phase.OVERNIGHT_SIGNAL),
        ("15:54", Phase.OVERNIGHT_VERIFY),
        ("15:56", Phase.OVERNIGHT_ENTRY),
        ("16:30", Phase.OVERNIGHT_HOLD),
    ],
)
def test_phase_boundaries(frozen_clock, time, expected):
    clock = SessionClock()
    assert clock.phase(frozen_clock(time)) is expected


def test_weekend_is_always_a_hold(frozen_clock):
    saturday = dt.date(2026, 9, 5)
    clock = SessionClock()
    assert clock.phase(frozen_clock("11:00", saturday)) is Phase.OVERNIGHT_HOLD


def test_session_times_must_be_increasing():
    with pytest.raises(ValueError, match="out of order"):
        SessionTimes(intraday_cutoff=dt.time(16, 0), overnight_signal=dt.time(15, 45)).validate()


def test_cutoff_needs_settling_room_before_verification():
    with pytest.raises(ValueError, match="15"):
        SessionTimes(
            intraday_cutoff=dt.time(15, 45), overnight_signal=dt.time(15, 50),
            overnight_verify=dt.time(15, 52), overnight_entry=dt.time(15, 55),
        ).validate()


# --------------------------------------------------------------------------- #
# layer 1: temporal
# --------------------------------------------------------------------------- #
def _firewall(cfg: Config | None = None) -> TemporalFirewall:
    return TemporalFirewall(cfg or Config())


def test_neither_book_may_open_during_the_cutoff(frozen_clock):
    fw = _firewall()
    now = frozen_clock("15:20")
    assert not fw.may_open(Book.INTRADAY, now)[0]
    assert not fw.may_open(Book.OVERNIGHT, now)[0]


def test_overnight_cannot_open_during_the_intraday_window(frozen_clock):
    allowed, why = _firewall().may_open(Book.OVERNIGHT, frozen_clock("11:00"))
    assert not allowed
    assert "intraday" in why or "may not open" in why


def test_intraday_cannot_open_in_the_entry_window(frozen_clock):
    allowed, _ = _firewall().may_open(Book.INTRADAY, frozen_clock("15:56"))
    assert not allowed


def test_overnight_needs_the_lock_even_inside_its_window(frozen_clock):
    fw = _firewall()
    allowed, why = fw.may_open(Book.OVERNIGHT, frozen_clock("15:56"))
    assert not allowed
    assert "verification" in why


def test_lock_is_exclusive(frozen_clock):
    fw = _firewall()
    fw._acquire(Book.INTRADAY, frozen_clock("11:00"), 50_000)
    allowed, why = fw.may_open(Book.OVERNIGHT, frozen_clock("15:56"))
    assert not allowed
    assert "intraday" in why


# --------------------------------------------------------------------------- #
# 15:15 cutoff
# --------------------------------------------------------------------------- #
def test_cutoff_liquidates_and_confirms_flat(cfg, frozen_clock):
    broker = SimBroker(cfg)
    broker._positions["SPY260918C00500000"] = PositionSnapshot(
        symbol="SPY260918C00500000", qty=2, avg_entry_price=3.0, market_value=600.0
    )
    fw = TemporalFirewall(cfg)
    report = fw.run_intraday_cutoff(broker, now=frozen_clock("15:15"), confirm_delay=0)

    assert report.confirmed_flat
    assert report.positions_before == 1
    assert report.positions_after == 0
    assert broker.account().positions == []


def test_cutoff_locks_the_intraday_book_for_the_rest_of_the_day(cfg, frozen_clock):
    fw = TemporalFirewall(cfg)
    fw.run_intraday_cutoff(SimBroker(cfg), now=frozen_clock("15:15"), confirm_delay=0)
    # Even back inside the intraday window (a manual re-run, say), it stays shut.
    allowed, why = fw.may_open(Book.INTRADAY, frozen_clock("11:00"))
    assert not allowed
    assert "locked for the day" in why


def test_cutoff_reports_failure_when_positions_survive(cfg, frozen_clock):
    class StubbornBroker(SimBroker):
        def close_position(self, symbol, qty=None):
            return None  # accepted, never fills - the realistic failure mode

    broker = StubbornBroker(cfg)
    broker._positions["SPY260918C00500000"] = PositionSnapshot(
        symbol="SPY260918C00500000", qty=1, avg_entry_price=3.0
    )
    report = TemporalFirewall(cfg).run_intraday_cutoff(
        broker, now=frozen_clock("15:15"), confirm_attempts=2, confirm_delay=0
    )
    assert not report.confirmed_flat
    assert report.attempts == 2


# --------------------------------------------------------------------------- #
# 15:54 verification
# --------------------------------------------------------------------------- #
def test_verification_passes_on_a_flat_account(cfg, frozen_clock):
    broker = SimBroker(cfg, starting_cash=100_000)
    verdict = TemporalFirewall(cfg).run_overnight_verification(
        broker, now=frozen_clock("15:54")
    )
    assert verdict.passed
    assert verdict.checks["flat_before_entry"]
    assert verdict.regt_buying_power > 0
    assert verdict.allocated_budget > 0


def test_verification_sizes_against_regt_not_daytrading_power(cfg, frozen_clock):
    broker = SimBroker(cfg, starting_cash=100_000)
    account = broker.account()
    verdict = TemporalFirewall(cfg).run_overnight_verification(
        broker, now=frozen_clock("15:54")
    )
    # Reg T is 2x, day-trading is 4x. The budget must derive from the smaller one.
    assert verdict.regt_buying_power == account.regt_buying_power
    assert verdict.allocated_budget <= account.regt_buying_power
    assert verdict.allocated_budget < (account.daytrading_buying_power or 0)


def test_verification_is_capped_by_the_equity_ceiling(cfg, frozen_clock):
    cfg.firewall.overnight_max_equity_pct = 0.10
    broker = SimBroker(cfg, starting_cash=100_000)
    verdict = TemporalFirewall(cfg).run_overnight_verification(
        broker, now=frozen_clock("15:54")
    )
    assert verdict.allocated_budget == pytest.approx(10_000, rel=0.01)


def test_verification_downscales_an_oversized_target(cfg, frozen_clock):
    broker = SimBroker(cfg, starting_cash=100_000)
    verdict = TemporalFirewall(cfg).run_overnight_verification(
        broker, now=frozen_clock("15:54"), target_trade_value=10_000_000
    )
    assert verdict.passed
    assert verdict.allocated_budget < 10_000_000
    assert any("downscaled" in r for r in verdict.reasons)


def test_verification_aborts_and_liquidates_when_positions_remain(cfg, frozen_clock):
    broker = SimBroker(cfg, starting_cash=100_000)
    broker._positions["SPY260918C00500000"] = PositionSnapshot(
        symbol="SPY260918C00500000", qty=1, avg_entry_price=3.0, market_value=300.0
    )
    fw = TemporalFirewall(cfg)
    verdict = fw.run_overnight_verification(broker, now=frozen_clock("15:54"))

    assert not verdict.passed
    assert verdict.emergency_liquidated
    assert broker.account().positions == []          # rescued
    assert fw.holder() is None                       # but the night is off
    assert any("aborted" in r for r in verdict.reasons)


def test_verification_refuses_outside_its_window(cfg, frozen_clock):
    verdict = TemporalFirewall(cfg).run_overnight_verification(
        SimBroker(cfg), now=frozen_clock("11:00")
    )
    assert not verdict.passed
    assert not verdict.checks["window"]


def test_verification_blocks_when_working_orders_remain(cfg, frozen_clock):
    class OrdersOpen(SimBroker):
        def account(self):
            snapshot = super().account()
            return snapshot.model_copy(update={"open_orders": 2})

    verdict = TemporalFirewall(cfg).run_overnight_verification(
        OrdersOpen(cfg), now=frozen_clock("15:54")
    )
    assert not verdict.passed


def test_passing_verification_grants_the_lock_and_a_budget(cfg, frozen_clock):
    fw = TemporalFirewall(cfg)
    broker = SimBroker(cfg, starting_cash=100_000)
    verdict = fw.run_overnight_verification(broker, now=frozen_clock("15:54"))

    assert fw.holder() is Book.OVERNIGHT
    assert fw.budget_for(Book.OVERNIGHT) == verdict.allocated_budget
    assert fw.budget_for(Book.INTRADAY) == 0.0
    assert fw.may_open(Book.OVERNIGHT, frozen_clock("15:56"))[0]


# --------------------------------------------------------------------------- #
# the full daily sequence
# --------------------------------------------------------------------------- #
def test_the_two_books_never_hold_capital_simultaneously(cfg, frozen_clock):
    """The property the whole design exists to guarantee."""
    fw = TemporalFirewall(cfg)
    broker = SimBroker(cfg, starting_cash=100_000)

    # 10:00 - the day book takes the lock
    fw.clock.freeze(frozen_clock("10:00"))
    assert fw.acquire_intraday(broker, now=frozen_clock("10:00")).passed
    assert fw.holder() is Book.INTRADAY
    assert not fw.may_open(Book.OVERNIGHT, frozen_clock("10:00"))[0]

    # 15:15 - cutoff hands the capital back
    fw.run_intraday_cutoff(broker, now=frozen_clock("15:15"), confirm_delay=0)
    assert fw.holder() is None

    # 15:54 - the night book takes it
    assert fw.run_overnight_verification(broker, now=frozen_clock("15:54")).passed
    assert fw.holder() is Book.OVERNIGHT
    assert not fw.may_open(Book.INTRADAY, frozen_clock("15:54"))[0]

    # 09:35 next day - released again
    fw.run_overnight_exit(broker, now=frozen_clock("09:35"))
    assert fw.holder() is None


def test_overnight_exit_unlocks_the_day_book(cfg, frozen_clock):
    fw = TemporalFirewall(cfg)
    broker = SimBroker(cfg)
    fw.run_intraday_cutoff(broker, now=frozen_clock("15:15"), confirm_delay=0)
    assert fw.state.intraday_disabled_until is not None

    fw.run_overnight_exit(broker, now=frozen_clock("09:35"))
    assert fw.state.intraday_disabled_until is None
    assert fw.may_open(Book.INTRADAY, frozen_clock("10:30"))[0]


def test_day_rollover_clears_the_lockout(cfg, frozen_clock):
    fw = TemporalFirewall(cfg)
    fw.run_intraday_cutoff(SimBroker(cfg), now=frozen_clock("15:15"), confirm_delay=0)
    fw.reset_day(dt.date(2026, 9, 3))
    assert fw.may_open(Book.INTRADAY, frozen_clock("11:00", dt.date(2026, 9, 3)))[0]


def test_disabled_firewall_is_permissive(frozen_clock):
    cfg = Config()
    cfg.firewall.enabled = False
    fw = TemporalFirewall(cfg)
    assert fw.may_open(Book.OVERNIGHT, frozen_clock("11:00"))[0]
    assert fw.may_open(Book.INTRADAY, frozen_clock("15:56"))[0]
