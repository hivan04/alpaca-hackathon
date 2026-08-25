"""The capital lock.

One account, two books, one lock. A book cannot open a position unless it
holds the lock, and it cannot hold the lock unless the other book has been
*proven* flat — not assumed flat, polled and confirmed.

This is deliberately paranoid. The failure it prevents is not "a strategy
loses money", it is "the broker force-liquidates the account at 16:00 because
4x intraday leverage was still on the books when the 2x Reg T limit applied".
That failure is unrecoverable inside a one-week judged window.
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

log = get_logger("firewall")


class Book(str, Enum):
    INTRADAY = "intraday"
    OVERNIGHT = "overnight"

    @property
    def other(self) -> Book:
        return Book.OVERNIGHT if self is Book.INTRADAY else Book.INTRADAY


@dataclass
class LiquidationReport:
    """What the hard cutoff actually achieved. Journalled verbatim."""

    triggered_at: dt.datetime
    orders_cancelled: int = 0
    positions_before: int = 0
    positions_after: int = 0
    attempts: int = 0
    confirmed_flat: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.confirmed_flat and not self.errors

    def summary(self) -> str:
        state = "FLAT" if self.confirmed_flat else "NOT FLAT"
        return (
            f"cutoff {state}: cancelled {self.orders_cancelled} orders, "
            f"{self.positions_before} -> {self.positions_after} positions "
            f"in {self.attempts} attempt(s)"
            + (f", errors: {'; '.join(self.errors)}" if self.errors else "")
        )


@dataclass
class FirewallVerdict:
    """The answer to 'may the overnight book trade, and with how much?'"""

    passed: bool
    book: Book
    phase: Phase
    checked_at: dt.datetime
    regt_buying_power: float = 0.0
    allocated_budget: float = 0.0
    open_positions: int = 0
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
            "allocated_budget": self.allocated_budget,
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "emergency_liquidated": self.emergency_liquidated,
            "checks": self.checks,
            "reasons": self.reasons,
        }

    def summary(self) -> str:
        head = "PASS" if self.passed else "BLOCK"
        return (
            f"[{head}] {self.book.value} @ {self.phase.value} | "
            f"RegT ${self.regt_buying_power:,.0f} -> budget ${self.allocated_budget:,.0f}"
            + (f" | {'; '.join(self.reasons)}" if self.reasons else "")
        )


@dataclass
class LockState:
    owner: Book | None = None
    acquired_at: dt.datetime | None = None
    session_date: dt.date | None = None
    budget: float = 0.0
    intraday_disabled_until: dt.date | None = None
    last_cutoff: LiquidationReport | None = None
    last_verdict: FirewallVerdict | None = None


class TemporalFirewall:
    """Sequential lock-and-verify between the intraday and overnight books."""

    def __init__(
        self,
        cfg: Any,
        journal: Any = None,
        clock: SessionClock | None = None,
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
        return self.state.owner

    def budget_for(self, book: Book) -> float:
        return self.state.budget if self.state.owner is book else 0.0

    # ------------------------------------------------------------------ #
    # Layer 1 - temporal
    # ------------------------------------------------------------------ #
    def may_open(self, book: Book, now: dt.datetime | None = None) -> tuple[bool, str]:
        """Is this book inside its own window, and does it hold the lock?"""
        if not self.enabled:
            return True, "firewall disabled"

        moment = self.clock.to_et(now) if now else self.clock.now()
        phase = self.phase(moment)

        if book is Book.INTRADAY:
            if self.state.intraday_disabled_until == moment.date():
                return False, "intraday book locked for the day by the 15:15 cutoff"
            if not phase.intraday_may_open:
                return False, f"intraday book may not open during phase '{phase.value}'"
        else:
            if not phase.overnight_may_open:
                return False, f"overnight book may not open during phase '{phase.value}'"

        owner = self.state.owner
        if owner is not None and owner is not book:
            return False, f"capital lock is held by the {owner.value} book"
        if book is Book.OVERNIGHT and owner is not Book.OVERNIGHT:
            return False, "overnight book has not passed 15:54 verification"
        return True, "ok"

    # ------------------------------------------------------------------ #
    # 15:15 - the intraday hard cutoff
    # ------------------------------------------------------------------ #
    def run_intraday_cutoff(
        self,
        broker: Any,
        now: dt.datetime | None = None,
        confirm_attempts: int | None = None,
        confirm_delay: float | None = None,
    ) -> LiquidationReport:
        """Cancel everything, liquidate the day book, then CONFIRM it is flat.

        Confirmation is the part most implementations skip. `close_all_positions`
        returning 200 means the orders were accepted, not that they filled — and
        an unfilled liquidation at 15:15 is exactly the state this whole system
        exists to prevent.
        """
        moment = self.clock.to_et(now) if now else self.clock.now()
        attempts_cap = confirm_attempts if confirm_attempts is not None else (
            self.settings.liquidation_confirm_attempts if self.settings else 4
        )
        delay = confirm_delay if confirm_delay is not None else (
            self.settings.liquidation_confirm_delay_seconds if self.settings else 5.0
        )

        report = LiquidationReport(triggered_at=moment)
        log.warning("INTRADAY HARD CUTOFF at %s", self.clock.describe(moment))

        try:
            snapshot = broker.account()
            report.positions_before = len(snapshot.positions)
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
                remaining = broker.account().positions
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"account poll failed: {exc}")
                break

            if not remaining:
                report.confirmed_flat = True
                report.positions_after = 0
                break

            log.warning(
                "cutoff attempt %d/%d: %d position(s) still open",
                attempt, attempts_cap, len(remaining),
            )
            for position in remaining:
                try:
                    broker.close_position(position.symbol)
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"close {position.symbol}: {exc}")
            if attempt < attempts_cap:
                time.sleep(delay)
        else:
            try:
                report.positions_after = len(broker.account().positions)
            except Exception:  # noqa: BLE001
                report.positions_after = -1

        if report.confirmed_flat:
            # Lock the intraday book out for the rest of the day and hand the
            # capital back, so the overnight book can claim it at 15:54.
            self.state.intraday_disabled_until = moment.date()
            if self.state.owner is Book.INTRADAY:
                self.release(Book.INTRADAY)
            log.info("intraday book flat and locked for %s", moment.date())
        else:
            log.critical(
                "INTRADAY BOOK DID NOT GO FLAT: %d position(s) remain. "
                "Overnight entry will be blocked.", report.positions_after,
            )

        self.state.last_cutoff = report
        self._journal("firewall_cutoff", **{
            "triggered_at": report.triggered_at.isoformat(),
            "orders_cancelled": report.orders_cancelled,
            "positions_before": report.positions_before,
            "positions_after": report.positions_after,
            "attempts": report.attempts,
            "confirmed_flat": report.confirmed_flat,
            "errors": report.errors,
        })
        return report

    # ------------------------------------------------------------------ #
    # 15:54 - overnight pre-trade verification
    # ------------------------------------------------------------------ #
    def run_overnight_verification(
        self,
        broker: Any,
        now: dt.datetime | None = None,
        target_trade_value: float | None = None,
    ) -> FirewallVerdict:
        """Prove flat, read fresh Reg T buying power, size against it, take the lock.

        Order matters: positions are checked *before* buying power is read, so
        the number we size against is measured in a state we have verified.
        """
        moment = self.clock.to_et(now) if now else self.clock.now()
        phase = self.phase(moment)
        verdict = FirewallVerdict(passed=False, book=Book.OVERNIGHT, phase=phase, checked_at=moment)

        if not self.enabled:
            verdict.passed = True
            verdict.checks["firewall_enabled"] = False
            verdict.reasons.append("firewall disabled in config")
            return verdict

        # -- window ------------------------------------------------------- #
        if phase not in (Phase.OVERNIGHT_VERIFY, Phase.OVERNIGHT_ENTRY):
            verdict.checks["window"] = False
            verdict.reasons.append(
                f"verification runs at {self.clock.times.overnight_verify:%H:%M} ET, "
                f"current phase is '{phase.value}'"
            )
            self._record(verdict)
            return verdict
        verdict.checks["window"] = True

        # -- fresh account poll ------------------------------------------- #
        try:
            snapshot: AccountSnapshot = broker.account()
        except Exception as exc:  # noqa: BLE001
            verdict.checks["account_reachable"] = False
            verdict.reasons.append(f"could not poll the account: {exc}")
            self._record(verdict)
            return verdict
        verdict.checks["account_reachable"] = True
        verdict.open_positions = len(snapshot.positions)
        verdict.open_orders = snapshot.open_orders

        # -- rogue intraday positions ------------------------------------- #
        if verdict.open_positions > 0 or verdict.open_orders > 0:
            log.critical(
                "CRITICAL RISK: %d position(s) and %d working order(s) still open at %s",
                verdict.open_positions, verdict.open_orders, moment.strftime("%H:%M ET"),
            )
            verdict.checks["flat_before_entry"] = False
            verdict.reasons.append(
                f"{verdict.open_positions} position(s) and {verdict.open_orders} "
                "order(s) open when the book should be cash"
            )
            if self.settings is None or self.settings.emergency_liquidate:
                report = self.run_intraday_cutoff(broker, now=moment)
                verdict.emergency_liquidated = True
                verdict.reasons.append(report.summary())
            # Abort regardless. A book that needed rescuing at 15:54 does not
            # get to put on a fresh overnight position ninety seconds later.
            verdict.reasons.append("overnight entry aborted for this session")
            self._record(verdict)
            return verdict
        verdict.checks["flat_before_entry"] = True

        # -- Reg T buying power ------------------------------------------- #
        regt = snapshot.regt_buying_power
        if regt is None or regt <= 0:
            # Fall back to the generic figure, but say so loudly - the whole
            # point of this check is to size against the *overnight* limit.
            regt = snapshot.buying_power
            verdict.reasons.append(
                "regt_buying_power unavailable; fell back to buying_power"
            )
            verdict.checks["regt_available"] = False
        else:
            verdict.checks["regt_available"] = True
        verdict.regt_buying_power = round(regt, 2)

        if regt <= 0:
            verdict.checks["buying_power"] = False
            verdict.reasons.append("no overnight buying power available")
            self._record(verdict)
            return verdict
        verdict.checks["buying_power"] = True

        # -- sizing --------------------------------------------------------- #
        utilisation = self.settings.overnight_regt_utilisation if self.settings else 0.95
        ceiling = regt * utilisation
        equity_cap = snapshot.equity * (
            self.settings.overnight_max_equity_pct if self.settings else 0.50
        )
        budget = min(ceiling, equity_cap)
        if target_trade_value is not None and target_trade_value > budget:
            verdict.reasons.append(
                f"target ${target_trade_value:,.0f} exceeds the verified budget; "
                f"downscaled to ${budget:,.0f}"
            )
        elif target_trade_value is not None:
            budget = target_trade_value
        verdict.allocated_budget = round(max(0.0, budget), 2)

        if verdict.allocated_budget < (self.settings.min_trade_value if self.settings else 500.0):
            verdict.checks["min_size"] = False
            verdict.reasons.append(
                f"verified budget ${verdict.allocated_budget:,.0f} is below the "
                "minimum viable trade size"
            )
            self._record(verdict)
            return verdict
        verdict.checks["min_size"] = True

        # -- take the lock --------------------------------------------------- #
        self._acquire(Book.OVERNIGHT, moment, verdict.allocated_budget)
        verdict.passed = True
        log.info(verdict.summary())
        self._record(verdict)
        return verdict

    # ------------------------------------------------------------------ #
    # 09:35 - overnight exit
    # ------------------------------------------------------------------ #
    def run_overnight_exit(self, broker: Any, now: dt.datetime | None = None) -> LiquidationReport:
        """Liquidate the overnight book and hand the capital to the day book."""
        moment = self.clock.to_et(now) if now else self.clock.now()
        log.info("OVERNIGHT EXIT at %s", self.clock.describe(moment))
        report = self.run_intraday_cutoff(broker, now=moment)
        # The cutoff helper locks the *intraday* book by design; undo that here,
        # because a clean 09:35 exit is precisely what frees the day to trade.
        self.state.intraday_disabled_until = None
        if report.confirmed_flat:
            self.release(Book.OVERNIGHT)
        self._journal("firewall_overnight_exit", confirmed_flat=report.confirmed_flat)
        return report

    # ------------------------------------------------------------------ #
    # lock mechanics
    # ------------------------------------------------------------------ #
    def _acquire(self, book: Book, moment: dt.datetime, budget: float) -> None:
        self.state.owner = book
        self.state.acquired_at = moment
        self.state.session_date = moment.date()
        self.state.budget = budget
        log.info("capital lock acquired by %s book (budget $%s)", book.value, f"{budget:,.0f}")
        self._journal("firewall_lock", action="acquire", book=book.value, budget=budget)

    def acquire_intraday(self, broker: Any, now: dt.datetime | None = None) -> FirewallVerdict:
        """The day book's equivalent of the 15:54 gate, run at 10:00."""
        moment = self.clock.to_et(now) if now else self.clock.now()
        phase = self.phase(moment)
        verdict = FirewallVerdict(passed=False, book=Book.INTRADAY, phase=phase, checked_at=moment)

        if not self.enabled:
            verdict.passed = True
            return verdict

        allowed, reason = self.may_open(Book.INTRADAY, moment)
        verdict.checks["window"] = allowed
        if not allowed:
            verdict.reasons.append(reason)
            self._record(verdict)
            return verdict

        try:
            snapshot = broker.account()
        except Exception as exc:  # noqa: BLE001
            verdict.reasons.append(f"could not poll the account: {exc}")
            self._record(verdict)
            return verdict

        verdict.open_positions = len(snapshot.positions)
        if verdict.open_positions > 0:
            # An overnight position still on at 10:00 means the 09:35 exit failed.
            verdict.checks["flat_before_entry"] = False
            verdict.reasons.append(
                f"{verdict.open_positions} position(s) survived the 09:35 overnight exit"
            )
            self._record(verdict)
            return verdict
        verdict.checks["flat_before_entry"] = True

        dtbp = snapshot.daytrading_buying_power or snapshot.buying_power
        utilisation = self.settings.intraday_dtbp_utilisation if self.settings else 0.50
        verdict.regt_buying_power = round(snapshot.regt_buying_power or 0.0, 2)
        verdict.allocated_budget = round(dtbp * utilisation, 2)
        self._acquire(Book.INTRADAY, moment, verdict.allocated_budget)
        verdict.passed = True
        self._record(verdict)
        return verdict

    def release(self, book: Book) -> None:
        if self.state.owner is book:
            self.state.owner = None
            self.state.budget = 0.0
            self.state.acquired_at = None
            log.info("capital lock released by %s book", book.value)
            self._journal("firewall_lock", action="release", book=book.value)

    def reset_day(self, session_date: dt.date | None = None) -> None:
        """Clear the per-day locks. Called by the runner on a date rollover."""
        self.state.intraday_disabled_until = None
        self.state.owner = None
        self.state.budget = 0.0
        self.state.session_date = session_date
        log.debug("firewall reset for %s", session_date)

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "now_et": self.clock.describe(),
            "phase": self.phase().value,
            "lock_owner": self.state.owner.value if self.state.owner else None,
            "budget": self.state.budget,
            "intraday_locked_for": (
                self.state.intraday_disabled_until.isoformat()
                if self.state.intraday_disabled_until else None
            ),
            "last_cutoff": self.state.last_cutoff.summary() if self.state.last_cutoff else None,
            "last_verdict": self.state.last_verdict.summary() if self.state.last_verdict else None,
        }

    def _record(self, verdict: FirewallVerdict) -> None:
        self.state.last_verdict = verdict
        self._journal("firewall_verify", **verdict.as_dict())

    def _journal(self, kind: str, **fields: Any) -> None:
        if self.journal is not None:
            try:
                self.journal.event(kind, **fields)
            except Exception as exc:  # noqa: BLE001
                log.debug("journal write failed for %s: %s", kind, exc)
