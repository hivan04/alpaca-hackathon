"""The capital firewall.

One account, three books, one lock discipline.

    carry           RESIDENT. Short-premium defined-risk structures held for
                    3-10 sessions. Its margin requirement is reserved FIRST and
                    is never lent to anyone else.
    intraday        TRANSIENT tenant. Long options / debit verticals, flat by
                    the 15:15 cutoff, every session, without exception.
    opportunistic   TRANSIENT tenant on the same cutoff. Dormant by default.

The purpose changed when the carry book became resident. It is no longer a
nightly handoff of the whole account; it is a boundary that stops the transient
books from consuming margin the resident book needs at the close.

Three properties are enforced rather than assumed:

  1. **Liquidation is confirmed, not requested.** A 200 from `close_all_positions`
     means accepted, not filled. We poll until the book is provably flat.
  2. **Working orders count as "not flat".** A resting order that fills at 15:59
     is unexpected exposure into the close.
  3. **The two books never hold conflicting claims on the same capital.** The
     transient budget is computed from headroom *after* the carry book's
     requirement is reserved, and it is measured on a fresh account poll.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot
from oaa.firewall.clock import Phase, SessionClock, SessionTimes
from oaa.firewall.ledger import PositionLedger

log = get_logger("firewall")


class Book(str, Enum):
    CARRY = "carry"
    INTRADAY = "intraday"
    OPPORTUNISTIC = "opportunistic"
    #: Spot crypto, traded by its own process between the Friday equity close
    #: and the Sunday flatten. It never leases capital here - its window cannot
    #: overlap a session, so there is no Reg T to share - but it is listed so
    #: that `config.strategies` stays inside the set of books the firewall
    #: knows, and so that `Book.parse` does not silently relabel it.
    #:
    #: It is deliberately NOT resident. If a crypto position is somehow still
    #: alive when an equity session opens, the 15:15 cutoff treating it as a
    #: transient tenant and closing it is exactly the behaviour wanted; a
    #: resident label would leave it sitting there.
    WEEKEND = "weekend"
    #: The earnings events book. Listed for the same reason WEEKEND is: the
    #: runtime switchboard and `config.strategies` key off this set, and
    #: `Book.parse` silently relabelling it INTRADAY would be worse than any
    #: honest error. It is a scheduled cycle inside `oaa run` (events_arm
    #: 15:50, events_flatten 09:45) but it is NOT a firewall tenant - its
    #: cycles build their own RiskEngine with firewall=None, because it arms
    #: after the 15:15 cutoff and holds one night, which no phase permits.
    #:
    #: Deliberately NOT resident, exactly as WEEKEND is not. Should an events
    #: leg somehow still be open at a later 15:15 cutoff - the 09:45 flatten
    #: having failed - the cutoff treating it as a transient tenant and closing
    #: it is the conservative error. A resident label would leave it sitting
    #: there, unowned, into the close.
    EVENTS = "events"

    @property
    def is_transient(self) -> bool:
        return self is not Book.CARRY

    @property
    def is_resident(self) -> bool:
        return self is Book.CARRY

    @classmethod
    def parse(cls, value: str | None) -> Book:
        try:
            return cls(str(value or "intraday").strip().lower())
        except ValueError:
            return cls.INTRADAY


TRANSIENT_BOOKS = (Book.INTRADAY, Book.OPPORTUNISTIC)


@dataclass
class LiquidationReport:
    """What a cutoff actually achieved. Journalled verbatim."""

    triggered_at: dt.datetime
    scope: str = "transient"
    orders_cancelled: int = 0
    positions_before: int = 0
    positions_after: int = 0
    resident_untouched: int = 0
    attempts: int = 0
    confirmed_flat: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.confirmed_flat and not self.errors

    def summary(self) -> str:
        state = "FLAT" if self.confirmed_flat else "NOT FLAT"
        return (
            f"{self.scope} cutoff {state}: cancelled {self.orders_cancelled} orders, "
            f"{self.positions_before} -> {self.positions_after} position(s) "
            f"in {self.attempts} attempt(s), {self.resident_untouched} resident leg(s) "
            "left untouched"
            + (f", errors: {'; '.join(self.errors)}" if self.errors else "")
        )


@dataclass
class FirewallVerdict:
    """The answer to 'may this book trade, and with how much?'"""

    passed: bool
    book: Book
    phase: Phase
    checked_at: dt.datetime
    regt_buying_power: float = 0.0
    carry_requirement: float = 0.0
    allocated_budget: float = 0.0
    open_positions: int = 0
    transient_positions: int = 0
    resident_positions: int = 0
    open_orders: int = 0
    emergency_liquidated: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "book": self.book.value,
            "phase": self.phase.value,
            "checked_at": self.checked_at.isoformat(),
            "regt_buying_power": self.regt_buying_power,
            "carry_requirement": self.carry_requirement,
            "allocated_budget": self.allocated_budget,
            "open_positions": self.open_positions,
            "transient_positions": self.transient_positions,
            "resident_positions": self.resident_positions,
            "open_orders": self.open_orders,
            "emergency_liquidated": self.emergency_liquidated,
            "checks": self.checks,
            "reasons": self.reasons,
        }

    def summary(self) -> str:
        head = "PASS" if self.passed else "BLOCK"
        return (
            f"[{head}] {self.book.value} @ {self.phase.value} | "
            f"RegT ${self.regt_buying_power:,.0f} - carry ${self.carry_requirement:,.0f} "
            f"-> budget ${self.allocated_budget:,.0f}"
            + (f" | {'; '.join(self.reasons)}" if self.reasons else "")
        )


@dataclass
class LockState:
    """Who holds what. The carry reservation and the transient lease coexist by
    construction: the lease is computed from what the reservation leaves."""

    carry_reserved: float = 0.0
    carry_reserved_at: dt.datetime | None = None
    transient_owner: Book | None = None
    transient_budget: float = 0.0
    transient_acquired_at: dt.datetime | None = None
    session_date: dt.date | None = None
    transient_disabled_until: dt.date | None = None
    last_cutoff: LiquidationReport | None = None
    last_verdict: FirewallVerdict | None = None
    last_carry_verification: FirewallVerdict | None = None
    flattened_for_submission: bool = False


class TemporalFirewall:
    """Temporal windows plus a capital boundary between resident and transient."""

    def __init__(
        self,
        cfg: Any,
        journal: Any = None,
        clock: SessionClock | None = None,
        ledger: PositionLedger | None = None,
    ) -> None:
        self.cfg = cfg
        self.settings = getattr(cfg, "firewall", None)
        self.journal = journal
        times = (
            SessionTimes.from_config(self.settings.times)
            if self.settings is not None
            else SessionTimes()
        )
        self.clock = clock or SessionClock(
            times=times,
            timezone=getattr(getattr(cfg, "schedule", None), "timezone", "America/New_York"),
        )
        ledger_path = getattr(self.settings, "ledger_path", None) if self.settings else None
        self.ledger = ledger if ledger is not None else PositionLedger.load(ledger_path)
        self.state = LockState()

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self.settings is None or self.settings.enabled)

    def phase(self, now: dt.datetime | None = None) -> Phase:
        return self.clock.phase(now)

    def holder(self) -> Book | None:
        """Which transient book currently holds the lease, if any."""
        return self.state.transient_owner

    def budget_for(self, book: Book) -> float:
        """Headroom for this book.

        Every transient book draws on the same pool, matching `may_open`: the
        lease is the pool's, not one book's. This is a shared budget rather
        than a split one, so two transient books scanning in the same cycle can
        each size against the same headroom - the same exposure one book
        already has making several trades in a cycle, and bounded downstream by
        the risk engine's portfolio limits rather than here.
        """
        if book.is_resident:
            return self.state.carry_reserved
        owner = self.state.transient_owner
        if owner is None or owner.is_resident:
            return 0.0
        return self.state.transient_budget

    def _setting(self, name: str, default: Any) -> Any:
        value = getattr(self.settings, name, None) if self.settings else None
        return default if value is None else value

    # ------------------------------------------------------------------ #
    # Layer 1 - temporal
    # ------------------------------------------------------------------ #
    def may_open(self, book: Book, now: dt.datetime | None = None) -> tuple[bool, str]:
        """Is this book inside its own window, and does it hold its claim?"""
        if not self.enabled:
            return True, "firewall disabled"

        moment = self.clock.to_et(now) if now else self.clock.now()
        phase = self.phase(moment)

        if self.state.flattened_for_submission:
            return False, "the book has been flattened for submission - no new entries"

        if book.is_resident:
            if not phase.carry_may_open:
                return False, f"carry book may not open during phase '{phase.value}'"
            if self.state.carry_reserved <= 0:
                return False, "carry book has no reserved capital - run the carry allocation"
            return True, "ok"

        # -- transient books ------------------------------------------------ #
        if self.state.transient_disabled_until == moment.date():
            return False, f"{book.value} book is locked out for the session"
        if not phase.intraday_may_open:
            return False, f"{book.value} book may not open during phase '{phase.value}'"

        # The transient lease is held for the transient POOL, not for one book.
        # It exists to stop the day books claiming capital the resident carry
        # book has reserved - it was never meant to arbitrate between two
        # transient books, which are flattened together at the same 15:15
        # cutoff and charged against the same headroom figure.
        #
        # Until 29 Aug this compared the lease holder against the asking book by
        # identity. `_transient_scan` always acquires as INTRADAY, so
        # `event_premium` - the only opportunistic strategy - failed here on
        # every single cycle with "the transient lease is held by the intraday
        # book". It read as a dormant strategy correctly standing down. It was
        # a strategy that could not open a position under any market condition.
        owner = self.state.transient_owner
        if owner is None:
            return False, f"{book.value} book has not acquired the transient lease"
        if owner.is_resident:
            return False, "the transient lease is held by the resident book"
        if self.state.transient_budget <= 0:
            return False, "no transient headroom left once the carry book is reserved"
        return True, "ok"

    # ------------------------------------------------------------------ #
    # capital allocation
    # ------------------------------------------------------------------ #
    def carry_requirement(self, snapshot: AccountSnapshot) -> float:
        """Margin the resident book is currently consuming.

        Measured from live marks on the legs the ledger attributes to the carry
        book, not from a cached number computed at entry.
        """
        resident, _ = self.ledger.split(snapshot.positions)
        return round(sum(abs(p.market_value) for p in resident), 2)

    def allocate_carry(
        self, broker: Any, now: dt.datetime | None = None
    ) -> FirewallVerdict:
        """Reserve the resident book's capital before anything else can bid for it."""
        moment = self.clock.to_et(now) if now else self.clock.now()
        phase = self.phase(moment)
        verdict = FirewallVerdict(
            passed=False, book=Book.CARRY, phase=phase, checked_at=moment
        )

        if not self.enabled:
            verdict.passed = True
            verdict.allocated_budget = float("inf")
            verdict.checks["firewall_enabled"] = False
            verdict.reasons.append("firewall disabled in config")
            self.state.carry_reserved = verdict.allocated_budget
            return verdict

        if not phase.carry_may_open:
            verdict.checks["window"] = False
            verdict.reasons.append(
                f"carry entries run {self.clock.times.carry_entry_start:%H:%M}-"
                f"{self.clock.times.carry_entry_end:%H:%M} ET; phase is '{phase.value}'"
            )
            self._record(verdict)
            return verdict
        verdict.checks["window"] = True

        try:
            snapshot: AccountSnapshot = broker.account()
        except Exception as exc:  # noqa: BLE001
            verdict.checks["account_reachable"] = False
            verdict.reasons.append(f"could not poll the account: {exc}")
            self._record(verdict)
            return verdict
        verdict.checks["account_reachable"] = True

        self.ledger.reconcile([p.symbol for p in snapshot.positions])
        resident, transient = self.ledger.split(snapshot.positions)
        verdict.open_positions = len(snapshot.positions)
        verdict.resident_positions = len(resident)
        verdict.transient_positions = len(transient)
        verdict.open_orders = snapshot.open_orders

        used = round(sum(abs(p.market_value) for p in resident), 2)
        verdict.carry_requirement = used
        verdict.regt_buying_power = round(
            snapshot.regt_buying_power or snapshot.buying_power or 0.0, 2
        )

        ceiling = snapshot.equity * float(self._setting("carry_max_equity_pct", 0.50))
        headroom = max(0.0, ceiling - used)
        verdict.allocated_budget = round(headroom, 2)
        self.state.carry_reserved = round(max(used, ceiling), 2)
        self.state.carry_reserved_at = moment
        verdict.checks["capacity"] = headroom > 0
        verdict.passed = headroom > 0
        if not verdict.passed:
            verdict.reasons.append(
                f"carry book is at its ${ceiling:,.0f} ceiling (${used:,.0f} used)"
            )
        log.info(verdict.summary())
        self._record(verdict)
        return verdict

    def acquire_transient(
        self, broker: Any, book: Book = Book.INTRADAY, now: dt.datetime | None = None
    ) -> FirewallVerdict:
        """Lease the transient books whatever the resident book is not using.

        This is the capital half of the firewall. The lease is derived from a
        FRESH account poll with the carry requirement subtracted first, so the
        two books cannot both believe they own the same dollar.
        """
        moment = self.clock.to_et(now) if now else self.clock.now()
        phase = self.phase(moment)
        verdict = FirewallVerdict(passed=False, book=book, phase=phase, checked_at=moment)

        if not self.enabled:
            verdict.passed = True
            self.state.transient_owner = book
            self.state.transient_budget = float("inf")
            return verdict

        if self.state.transient_disabled_until == moment.date():
            verdict.checks["not_locked_out"] = False
            verdict.reasons.append("transient books are locked out for this session")
            self._record(verdict)
            return verdict
        verdict.checks["not_locked_out"] = True

        if not phase.intraday_may_open:
            verdict.checks["window"] = False
            verdict.reasons.append(
                f"{book.value} entries are closed during phase '{phase.value}'"
            )
            self._record(verdict)
            return verdict
        verdict.checks["window"] = True

        try:
            snapshot = broker.account()
        except Exception as exc:  # noqa: BLE001
            verdict.checks["account_reachable"] = False
            verdict.reasons.append(f"could not poll the account: {exc}")
            self._record(verdict)
            return verdict
        verdict.checks["account_reachable"] = True

        self.ledger.reconcile([p.symbol for p in snapshot.positions])
        resident, transient = self.ledger.split(snapshot.positions)
        verdict.open_positions = len(snapshot.positions)
        verdict.resident_positions = len(resident)
        verdict.transient_positions = len(transient)
        verdict.open_orders = snapshot.open_orders

        carry_used = round(sum(abs(p.market_value) for p in resident), 2)
        carry_reserve = max(carry_used, self.state.carry_reserved)
        verdict.carry_requirement = round(carry_reserve, 2)

        regt = snapshot.regt_buying_power
        if regt is None or regt <= 0:
            regt = snapshot.buying_power
            verdict.checks["regt_available"] = False
            verdict.reasons.append("regt_buying_power unavailable; fell back to buying_power")
        else:
            verdict.checks["regt_available"] = True
        verdict.regt_buying_power = round(regt or 0.0, 2)

        # THE line that makes the two books disjoint: the resident book's
        # requirement comes off the top before anything is leased out.
        utilisation = float(self._setting("transient_utilisation", 0.50))
        headroom = max(0.0, (regt or 0.0) - carry_reserve) * utilisation
        equity_cap = snapshot.equity * float(self._setting("transient_max_equity_pct", 0.15))
        budget = round(min(headroom, equity_cap), 2)
        verdict.allocated_budget = budget

        minimum = float(self._setting("min_trade_value", 500.0))
        if budget < minimum:
            verdict.checks["min_size"] = False
            verdict.reasons.append(
                f"transient headroom ${budget:,.0f} is below the minimum viable "
                f"trade size ${minimum:,.0f} once the carry book is reserved"
            )
            self._record(verdict)
            return verdict
        verdict.checks["min_size"] = True

        self.state.transient_owner = book
        self.state.transient_budget = budget
        self.state.transient_acquired_at = moment
        self.state.session_date = moment.date()
        verdict.passed = True
        log.info(verdict.summary())
        self._journal("firewall_lock", action="acquire", book=book.value, budget=budget)
        self._record(verdict)
        return verdict

    # ------------------------------------------------------------------ #
    # 15:15 - the transient hard cutoff
    # ------------------------------------------------------------------ #
    def run_intraday_cutoff(
        self,
        broker: Any,
        now: dt.datetime | None = None,
        confirm_attempts: int | None = None,
        confirm_delay: float | None = None,
        scope: str = "transient",
    ) -> LiquidationReport:
        """Cancel working orders, liquidate the TRANSIENT books, confirm flat.

        Confirmation is the part most implementations skip. `close_all_positions`
        returning 200 means the orders were accepted, not that they filled - and
        an unfilled liquidation at 15:15 is exactly the state this exists to
        prevent. Resident carry legs are deliberately left alone.
        """
        moment = self.clock.to_et(now) if now else self.clock.now()
        attempts_cap = int(
            confirm_attempts
            if confirm_attempts is not None
            else self._setting("liquidation_confirm_attempts", 4)
        )
        delay = float(
            confirm_delay
            if confirm_delay is not None
            else self._setting("liquidation_confirm_delay_seconds", 5.0)
        )
        everything = scope == "all"

        report = LiquidationReport(triggered_at=moment, scope=scope)
        log.warning("%s HARD CUTOFF at %s", scope.upper(), self.clock.describe(moment))

        try:
            snapshot = broker.account()
            self.ledger.reconcile([p.symbol for p in snapshot.positions])
            resident, transient = self.ledger.split(snapshot.positions)
            targets = snapshot.positions if everything else transient
            report.positions_before = len(targets)
            report.resident_untouched = 0 if everything else len(resident)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"account poll failed: {exc}")

        # 1. Kill working orders first, so nothing fills into the liquidation.
        try:
            report.orders_cancelled = broker.cancel_all()
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"cancel_all failed: {exc}")
            log.error("cancel_all failed during cutoff: %s", exc)

        # 2. Liquidate, then poll until flat or out of attempts.
        for attempt in range(1, attempts_cap + 1):
            report.attempts = attempt
            try:
                snapshot = broker.account()
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"account poll failed: {exc}")
                break

            resident, transient = self.ledger.split(snapshot.positions)
            remaining = snapshot.positions if everything else transient
            report.resident_untouched = 0 if everything else len(resident)

            if not remaining and snapshot.open_orders == 0:
                report.confirmed_flat = True
                report.positions_after = 0
                break

            log.warning(
                "cutoff attempt %d/%d: %d %s position(s) and %d working order(s) open",
                attempt, attempts_cap, len(remaining), scope, snapshot.open_orders,
            )
            for position in remaining:
                try:
                    broker.close_position(position.symbol)
                    self.ledger.forget(position.symbol)
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"close {position.symbol}: {exc}")
            if attempt < attempts_cap:
                time.sleep(delay)
        else:
            try:
                snapshot = broker.account()
                _, transient = self.ledger.split(snapshot.positions)
                report.positions_after = len(
                    snapshot.positions if everything else transient
                )
            except Exception:  # noqa: BLE001
                report.positions_after = -1

        if report.confirmed_flat:
            self.state.transient_disabled_until = moment.date()
            self.release_transient()
            log.info("transient books flat and locked for %s", moment.date())
        else:
            log.critical(
                "TRANSIENT BOOKS DID NOT GO FLAT: %d position(s) remain. "
                "Carry verification will fail.", report.positions_after,
            )

        self.state.last_cutoff = report
        self._journal("firewall_cutoff", **{
            "triggered_at": report.triggered_at.isoformat(),
            "scope": report.scope,
            "orders_cancelled": report.orders_cancelled,
            "positions_before": report.positions_before,
            "positions_after": report.positions_after,
            "resident_untouched": report.resident_untouched,
            "attempts": report.attempts,
            "confirmed_flat": report.confirmed_flat,
            "errors": report.errors,
        })
        return report

    # ------------------------------------------------------------------ #
    # 15:45 - carry book verification
    # ------------------------------------------------------------------ #
    def run_carry_verification(
        self, broker: Any, now: dt.datetime | None = None
    ) -> FirewallVerdict:
        """Confirm no residual transient exposure and that carry margin is covered.

        This is where the day is signed off. It does not open anything; it
        proves the resident book can survive the close on its own margin, and
        it decides whether the transient books are allowed to trade tomorrow.
        """
        moment = self.clock.to_et(now) if now else self.clock.now()
        phase = self.phase(moment)
        verdict = FirewallVerdict(
            passed=False, book=Book.CARRY, phase=phase, checked_at=moment
        )

        if not self.enabled:
            verdict.passed = True
            verdict.checks["firewall_enabled"] = False
            verdict.reasons.append("firewall disabled in config")
            return verdict

        if phase not in (Phase.CARRY_VERIFY, Phase.INTRADAY_CUTOFF, Phase.CLOSED):
            verdict.checks["window"] = False
            verdict.reasons.append(
                f"carry verification runs at {self.clock.times.carry_verification:%H:%M} ET, "
                f"current phase is '{phase.value}'"
            )
            self._record(verdict, carry=True)
            return verdict
        verdict.checks["window"] = True

        try:
            snapshot: AccountSnapshot = broker.account()
        except Exception as exc:  # noqa: BLE001
            verdict.checks["account_reachable"] = False
            verdict.reasons.append(f"could not poll the account: {exc}")
            self._disable_transient_next_session(moment)
            self._record(verdict, carry=True)
            return verdict
        verdict.checks["account_reachable"] = True

        self.ledger.reconcile([p.symbol for p in snapshot.positions])
        resident, transient = self.ledger.split(snapshot.positions)
        verdict.open_positions = len(snapshot.positions)
        verdict.resident_positions = len(resident)
        verdict.transient_positions = len(transient)
        verdict.open_orders = snapshot.open_orders

        # -- residual transient exposure ---------------------------------- #
        if transient or snapshot.open_orders > 0:
            log.critical(
                "CRITICAL RISK: %d transient position(s) and %d working order(s) "
                "still open at %s", len(transient), snapshot.open_orders,
                moment.strftime("%H:%M ET"),
            )
            verdict.checks["transient_flat"] = False
            verdict.reasons.append(
                f"{len(transient)} transient position(s) and {snapshot.open_orders} "
                "working order(s) open when only the carry book should remain"
            )
            if bool(self._setting("emergency_liquidate", True)):
                report = self.run_intraday_cutoff(broker, now=moment)
                verdict.emergency_liquidated = True
                verdict.reasons.append(report.summary())
            # The next-session penalty is for residual transient EXPOSURE, not
            # for a working order. `snapshot.open_orders` is account-wide, so a
            # resting CARRY entry at 15:45 - or any stale order the emergency
            # cutoff above has just cancelled - would disable the intraday book
            # for the whole of the following session. With a competition window
            # of a handful of sessions that is a full day of trading lost to an
            # order that belonged to a different book and may no longer exist.
            # Positions that survived the 15:15 cutoff are a control failure and
            # still earn the ratchet; an open order is a reason to cancel it,
            # which `run_intraday_cutoff` above has already done.
            if transient:
                self._disable_transient_next_session(moment)
                verdict.reasons.append(
                    "transient books disabled for the following session"
                )
            else:
                verdict.reasons.append(
                    f"{snapshot.open_orders} working order(s) at the sign-off with no "
                    "residual transient position - cancelled, no next-session penalty"
                )
            self._record(verdict, carry=True)
            return verdict
        verdict.checks["transient_flat"] = True

        # -- fresh Reg T, carry margin covered with headroom ---------------- #
        regt = snapshot.regt_buying_power
        if regt is None or regt <= 0:
            regt = snapshot.buying_power
            verdict.checks["regt_available"] = False
            verdict.reasons.append("regt_buying_power unavailable; fell back to buying_power")
        else:
            verdict.checks["regt_available"] = True
        verdict.regt_buying_power = round(regt or 0.0, 2)

        requirement = round(sum(abs(p.market_value) for p in resident), 2)
        verdict.carry_requirement = requirement
        cushion = float(self._setting("carry_margin_cushion", 1.25))
        # The cushion is a MULTIPLIER, not a margin above 1. `(cushion - 1.0)`
        # turned a 1.25x requirement into 0.25x, so the 15:45 sign-off passed a
        # book with one fifth of the buying power it was supposed to demand.
        covered = (regt or 0.0) >= requirement * cushion
        headroom_ok = (
            snapshot.leverage_headroom is None or snapshot.leverage_headroom <= 1.0
        )
        verdict.checks["carry_margin_covered"] = bool(covered and headroom_ok)

        if not verdict.checks["carry_margin_covered"]:
            verdict.reasons.append(
                f"carry book requires ${requirement:,.0f} with only "
                f"${regt or 0:,.0f} Reg T buying power behind it "
                f"(leverage headroom {snapshot.leverage_headroom})"
            )
            self._disable_transient_next_session(moment)
            self._record(verdict, carry=True)
            return verdict

        self.state.carry_reserved = requirement
        verdict.allocated_budget = requirement
        verdict.passed = True
        log.info("carry verification passed: %s", verdict.summary())
        self._record(verdict, carry=True)
        return verdict

    # ------------------------------------------------------------------ #
    # submission flatten
    # ------------------------------------------------------------------ #
    def run_submission_flatten(
        self, broker: Any, now: dt.datetime | None = None
    ) -> LiquidationReport:
        """Close the entire book - resident included - with the same discipline.

        Realised P&L on a flat account is unambiguous evidence. An open book at
        judging asks a judge to trust a mid-price mark on a wide quote.
        """
        report = self.run_intraday_cutoff(broker, now=now, scope="all")
        self.state.flattened_for_submission = True
        self.state.carry_reserved = 0.0
        self.release_transient()
        self._journal("firewall_submission_flatten", confirmed_flat=report.confirmed_flat)
        return report

    # ------------------------------------------------------------------ #
    # lock mechanics
    # ------------------------------------------------------------------ #
    def release_transient(self) -> None:
        if self.state.transient_owner is not None:
            log.info("transient lease released by %s", self.state.transient_owner.value)
            self._journal(
                "firewall_lock", action="release", book=self.state.transient_owner.value
            )
        self.state.transient_owner = None
        self.state.transient_budget = 0.0
        self.state.transient_acquired_at = None

    def release(self, book: Book) -> None:
        """Backwards-compatible release."""
        if book.is_resident:
            self.state.carry_reserved = 0.0
            self.state.carry_reserved_at = None
        elif self.state.transient_owner is book:
            self.release_transient()

    def _disable_transient_next_session(self, moment: dt.datetime) -> None:
        """A book that needed rescuing does not get fresh leverage tomorrow."""
        self.state.transient_disabled_until = moment.date() + dt.timedelta(days=1)
        log.warning(
            "transient books disabled for %s", self.state.transient_disabled_until
        )

    def reset_day(self, session_date: dt.date | None = None) -> None:
        """Clear the per-day lease. The carry reservation survives by design."""
        self.release_transient()
        if (
            self.state.transient_disabled_until is not None
            and session_date is not None
            and self.state.transient_disabled_until < session_date
        ):
            self.state.transient_disabled_until = None
        self.state.session_date = session_date
        log.debug("firewall rolled to %s", session_date)

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "now_et": self.clock.describe(),
            "phase": self.phase().value,
            "carry_reserved": self.state.carry_reserved,
            "transient_owner": (
                self.state.transient_owner.value if self.state.transient_owner else None
            ),
            "transient_budget": self.state.transient_budget,
            "transient_disabled_until": (
                self.state.transient_disabled_until.isoformat()
                if self.state.transient_disabled_until else None
            ),
            "flattened_for_submission": self.state.flattened_for_submission,
            "ledger": self.ledger.stats(),
            "last_cutoff": self.state.last_cutoff.summary() if self.state.last_cutoff else None,
            "last_verdict": self.state.last_verdict.summary() if self.state.last_verdict else None,
            "last_carry_verification": (
                self.state.last_carry_verification.summary()
                if self.state.last_carry_verification else None
            ),
        }

    def _record(self, verdict: FirewallVerdict, carry: bool = False) -> None:
        self.state.last_verdict = verdict
        if carry:
            self.state.last_carry_verification = verdict
        self._journal("firewall_verify", **verdict.as_dict())

    def _journal(self, kind: str, **fields: Any) -> None:
        if self.journal is not None:
            try:
                self.journal.event(kind, **fields)
            except Exception as exc:  # noqa: BLE001
                log.debug("journal write failed for %s: %s", kind, exc)
