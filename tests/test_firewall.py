"""The capital firewall: phases, the resident/transient boundary, liquidation.

The property that matters is at the bottom: the resident and transient books
never hold conflicting claims on the same capital.
"""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.config.schema import Config
from oaa.core.types import AccountSnapshot, PositionSnapshot
from oaa.firewall.clock import Phase, SessionClock, SessionTimes
from oaa.firewall.ledger import PositionLedger
from oaa.firewall.lock import Book, TemporalFirewall


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("clock_time", "expected"),
    [
        ("08:00", Phase.CLOSED),            # pre-bell; the carry book is simply held
        ("09:31", Phase.OPEN_SETTLE),       # wide and unstable, nothing opens
        ("09:50", Phase.INTRADAY),          # intraday only
        ("11:00", Phase.ACTIVE),            # both books may open
        ("14:50", Phase.CARRY_ONLY),        # intraday wind-down
        ("15:05", Phase.WIND_DOWN),         # manage only
        ("15:20", Phase.INTRADAY_CUTOFF),
        ("15:50", Phase.CARRY_VERIFY),
        ("16:30", Phase.CLOSED),
    ],
)
def test_phase_machine(frozen_clock, clock_time, expected):
    clock = SessionClock(frozen_now=frozen_clock(clock_time))
    assert clock.phase() is expected


def test_weekend_is_closed_not_an_error(frozen_clock):
    saturday = frozen_clock("11:00", dt.date(2026, 9, 5))
    assert SessionClock(frozen_now=saturday).phase() is Phase.CLOSED


def test_boundaries_must_be_strictly_increasing():
    with pytest.raises(ValueError, match="out of order"):
        SessionTimes(intraday_start=dt.time(9, 0)).validate()


def test_cutoff_and_verification_need_room_to_settle():
    with pytest.raises(ValueError, match="at least 15"):
        SessionTimes(carry_verification=dt.time(15, 20)).validate()


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeBroker:
    """Minimal broker double. `stubborn` refuses to fill the liquidation, which
    is the exact failure the confirm-poll exists to catch."""

    def __init__(self, positions=None, stubborn=False, regt=200_000.0, equity=100_000.0):
        self._positions = list(positions or [])
        self.stubborn = stubborn
        self.regt = regt
        self.equity = equity
        self.cancelled = 0
        self.closed: list[str] = []
        self.open_orders = 0

    def account(self):
        return AccountSnapshot(
            equity=self.equity, last_equity=self.equity, cash=self.equity,
            buying_power=self.regt, regt_buying_power=self.regt,
            daytrading_buying_power=self.regt * 2,
            positions=list(self._positions), open_orders=self.open_orders,
        )

    def cancel_all(self):
        self.cancelled += 1
        self.open_orders = 0
        return 1

    def close_position(self, symbol, qty=None):
        self.closed.append(symbol)
        if not self.stubborn:
            self._positions = [p for p in self._positions if p.symbol != symbol]
        return True


def position(symbol: str, value: float = 5_000.0) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol, qty=-1, avg_entry_price=1.0, market_value=value,
        underlying=symbol[:3],
    )


def build(tmp_path, frozen_clock, at="15:20", **positions_by_book):
    cfg = Config()
    cfg.firewall.ledger_path = str(tmp_path / "ledger.json")
    ledger = PositionLedger(path=tmp_path / "ledger.json")
    from oaa.firewall.ledger import LedgerEntry

    live = []
    for book, symbols in positions_by_book.items():
        for symbol in symbols:
            live.append(position(symbol))
            ledger.entries[symbol] = LedgerEntry(symbol=symbol, book=book)
    broker = FakeBroker(positions=live)
    firewall = TemporalFirewall(cfg, ledger=ledger)
    firewall.clock.freeze(frozen_clock(at))
    return firewall, broker


# --------------------------------------------------------------------------- #
# the cutoff
# --------------------------------------------------------------------------- #
def test_cutoff_liquidates_transient_and_leaves_the_resident_book_alone(
    tmp_path, frozen_clock
):
    firewall, broker = build(
        tmp_path, frozen_clock, carry=["CARRY1", "CARRY2"], intraday=["DAY1"]
    )
    report = firewall.run_intraday_cutoff(broker, confirm_delay=0)

    assert report.confirmed_flat
    assert broker.closed == ["DAY1"]
    assert report.resident_untouched == 2
    assert {p.symbol for p in broker.account().positions} == {"CARRY1", "CARRY2"}


def test_an_unfilled_liquidation_is_not_reported_as_flat(tmp_path, frozen_clock):
    """A 200 from close_all_positions means accepted, not filled."""
    firewall, broker = build(tmp_path, frozen_clock, intraday=["DAY1"])
    broker.stubborn = True
    report = firewall.run_intraday_cutoff(broker, confirm_attempts=2, confirm_delay=0)

    assert not report.confirmed_flat
    assert report.attempts == 2
    assert firewall.state.transient_disabled_until is None  # never locked as "done"


def test_working_orders_count_as_not_flat(tmp_path, frozen_clock):
    firewall, broker = build(tmp_path, frozen_clock)
    broker.open_orders = 1
    broker.cancel_all = lambda: 0          # a cancel that silently does nothing
    report = firewall.run_intraday_cutoff(broker, confirm_attempts=2, confirm_delay=0)
    assert not report.confirmed_flat


def test_unattributed_legs_are_treated_as_transient(tmp_path, frozen_clock):
    """Closing something we did not deliberately choose to hold is the
    recoverable error; carrying it into the close is not."""
    firewall, broker = build(tmp_path, frozen_clock)
    broker._positions.append(position("MYSTERY"))
    report = firewall.run_intraday_cutoff(broker, confirm_delay=0)
    assert "MYSTERY" in broker.closed
    assert report.confirmed_flat


# --------------------------------------------------------------------------- #
# carry verification
# --------------------------------------------------------------------------- #
def test_carry_verification_passes_on_a_clean_resident_book(tmp_path, frozen_clock):
    firewall, broker = build(tmp_path, frozen_clock, at="15:50", carry=["CARRY1"])
    verdict = firewall.run_carry_verification(broker)
    assert verdict.passed
    assert verdict.checks["transient_flat"]
    assert verdict.resident_positions == 1


def test_residual_transient_exposure_disables_the_next_session(tmp_path, frozen_clock):
    firewall, broker = build(
        tmp_path, frozen_clock, at="15:50", carry=["CARRY1"], intraday=["DAY1"]
    )
    verdict = firewall.run_carry_verification(broker)
    assert not verdict.passed
    assert verdict.emergency_liquidated
    assert firewall.state.transient_disabled_until is not None


def test_verification_outside_its_window_refuses(tmp_path, frozen_clock):
    firewall, broker = build(tmp_path, frozen_clock, at="11:00")
    verdict = firewall.run_carry_verification(broker)
    assert not verdict.passed
    assert verdict.checks["window"] is False


# --------------------------------------------------------------------------- #
# temporal permissions
# --------------------------------------------------------------------------- #
def test_books_may_only_open_in_their_own_windows(tmp_path, frozen_clock):
    firewall, broker = build(tmp_path, frozen_clock, at="11:00")
    # Nothing is leased or reserved yet.
    assert firewall.may_open(Book.INTRADAY)[0] is False
    assert firewall.may_open(Book.CARRY)[0] is False

    firewall.acquire_transient(broker, Book.INTRADAY)
    firewall.allocate_carry(broker)
    assert firewall.may_open(Book.INTRADAY)[0] is True
    assert firewall.may_open(Book.CARRY)[0] is True

    firewall.clock.freeze(frozen_clock("15:20"))
    assert firewall.may_open(Book.INTRADAY)[0] is False
    assert firewall.may_open(Book.CARRY)[0] is False


def test_the_opportunistic_book_can_open_on_the_shared_transient_lease(
    tmp_path, frozen_clock
):
    """`event_premium` is the only opportunistic strategy, and until 29 Aug it
    could not open a position under ANY market condition.

    `_transient_scan` acquires the lease as INTRADAY, and `may_open` compared
    the holder against the asking book by identity - so the opportunistic book
    failed with "the transient lease is held by the intraday book" on every
    cycle. It read as a dormant strategy standing down; it was a strategy that
    was structurally blocked. One lease covers the transient pool.
    """
    firewall, broker = build(tmp_path, frozen_clock, at="11:00")
    firewall.acquire_transient(broker, Book.INTRADAY)

    allowed, why = firewall.may_open(Book.OPPORTUNISTIC)
    assert allowed, why
    assert firewall.budget_for(Book.OPPORTUNISTIC) > 0
    assert firewall.budget_for(Book.OPPORTUNISTIC) == firewall.budget_for(Book.INTRADAY)


def test_the_shared_lease_does_not_let_the_resident_book_in(tmp_path, frozen_clock):
    """Sharing the lease between the transient books must not weaken the wall
    it exists to hold: carry still needs its own reservation."""
    firewall, broker = build(tmp_path, frozen_clock, at="11:00")
    firewall.acquire_transient(broker, Book.INTRADAY)

    assert firewall.may_open(Book.CARRY)[0] is False
    assert firewall.budget_for(Book.CARRY) == firewall.state.carry_reserved


def test_no_transient_book_opens_before_the_lease_is_acquired(tmp_path, frozen_clock):
    firewall, _ = build(tmp_path, frozen_clock, at="11:00")
    for book in (Book.INTRADAY, Book.OPPORTUNISTIC):
        allowed, why = firewall.may_open(book)
        assert allowed is False
        assert "lease" in why
        assert firewall.budget_for(book) == 0.0


def test_submission_flatten_closes_the_resident_book_too(tmp_path, frozen_clock):
    firewall, broker = build(
        tmp_path, frozen_clock, at="11:00", carry=["CARRY1"], intraday=["DAY1"]
    )
    report = firewall.run_submission_flatten(broker)
    assert report.confirmed_flat
    assert set(broker.closed) == {"CARRY1", "DAY1"}
    assert firewall.may_open(Book.CARRY)[0] is False   # no entries after the flatten


# --------------------------------------------------------------------------- #
# THE property
# --------------------------------------------------------------------------- #
def test_the_two_books_never_hold_conflicting_claims_on_the_same_capital(
    tmp_path, frozen_clock
):
    """The transient lease is computed from what Reg T leaves AFTER the resident
    book's requirement is reserved. Whatever the carry book is using, the sum of
    the two claims can never exceed the buying power behind them."""
    for carry_value in (0.0, 20_000.0, 60_000.0, 140_000.0):
        firewall, broker = build(tmp_path, frozen_clock, at="11:00")
        if carry_value:
            broker._positions.append(position("CARRY1", carry_value))
            from oaa.firewall.ledger import LedgerEntry

            firewall.ledger.entries["CARRY1"] = LedgerEntry(symbol="CARRY1", book="carry")

        firewall.allocate_carry(broker)
        firewall.acquire_transient(broker, Book.INTRADAY)

        carry_claim = firewall.budget_for(Book.CARRY)
        transient_claim = firewall.budget_for(Book.INTRADAY)
        regt = broker.account().regt_buying_power

        assert carry_claim + transient_claim <= regt + 1e-6, (
            f"carry ${carry_claim:,.0f} + transient ${transient_claim:,.0f} "
            f"exceeds Reg T ${regt:,.0f}"
        )
        # And the transient lease shrinks as the resident book grows.
        assert transient_claim >= 0


def test_the_transient_lease_shrinks_as_the_carry_book_grows(tmp_path, frozen_clock):
    from oaa.firewall.ledger import LedgerEntry

    leases = []
    for carry_value in (0.0, 80_000.0, 160_000.0):
        firewall, broker = build(tmp_path, frozen_clock, at="11:00")
        if carry_value:
            broker._positions.append(position("CARRY1", carry_value))
            firewall.ledger.entries["CARRY1"] = LedgerEntry(symbol="CARRY1", book="carry")
        firewall.allocate_carry(broker)
        firewall.acquire_transient(broker, Book.INTRADAY)
        leases.append(firewall.budget_for(Book.INTRADAY))

    assert leases == sorted(leases, reverse=True)


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def test_the_ledger_survives_a_restart(tmp_path):
    from oaa.firewall.ledger import LedgerEntry

    path = tmp_path / "ledger.json"
    ledger = PositionLedger(path=path)
    ledger.entries["CARRY1"] = LedgerEntry(symbol="CARRY1", book="carry")
    ledger.save()

    reloaded = PositionLedger.load(path)
    assert reloaded.is_resident("CARRY1")
    assert reloaded.book_of("NEVER_SEEN") == "intraday"
