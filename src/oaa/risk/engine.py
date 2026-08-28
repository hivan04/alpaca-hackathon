"""The deterministic risk engine.

Plain Python. No model, no LLM, no discretion. Every idea passes through here
before it can become an order, and execution refuses any ticket that is not
stamped by an approval from this class.

The ordering is deliberate: the cheapest, most categorical checks first, so a
structurally-forbidden trade never costs a sizing calculation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oaa.config.schema import Config
from oaa.core import clock
from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot, RiskVerdict, Side, TradeIdea
from oaa.options.occ import underlying_of
from oaa.risk.sizing import size_by_risk

log = get_logger("risk")


@dataclass
class DayState:
    """Per-session counters. Reset at the start of each trading day."""

    date: dt.date = field(default_factory=dt.date.today)
    opened_today: int = 0
    realised_pl: float = 0.0
    start_equity: float = 0.0
    peak_equity: float = 0.0
    halted: bool = False
    halt_reason: str | None = None

    def roll(
        self, today: dt.date, equity: float, previous_close: float | None = None
    ) -> None:
        """`previous_close` is the broker's own last_equity - the baseline the
        daily loss limit is supposed to measure from.

        Without it a restart mid-session re-baselined to whatever equity had
        already fallen to, silently re-arming the full loss budget: a -2.6%
        morning followed by a crash and restart could lose another 3% before
        the 3% halt fired. It also resets `halted`, so a halt did not survive
        the restart that a halt makes likely.
        """
        if today != self.date:
            self.date = today
            self.opened_today = 0
            self.realised_pl = 0.0
            self.start_equity = previous_close or equity
            self.halted = False
            self.halt_reason = None
        if self.start_equity <= 0:
            self.start_equity = previous_close or equity
        self.peak_equity = max(self.peak_equity, equity)


class RiskEngine:
    def __init__(self, cfg: Config, firewall: Any = None) -> None:
        self.cfg = cfg
        self.limits = cfg.risk
        self.state = DayState()
        #: (symbol, strategy) -> when that pair last opened something.
        #: Backs the re-entry cooldown; see `reentry_cooldown_minutes`.
        self.last_entry: dict[tuple[str, str], dt.datetime] = {}
        #: The temporal firewall. When present it is checked FIRST, before any
        #: other rule: a trade from the wrong book at the wrong minute is not a
        #: sizing question, it is a categorical refusal.
        self.firewall = firewall

    # -- session control ---------------------------------------------------- #
    def observe(self, account: AccountSnapshot, now: dt.datetime | None = None) -> None:
        now = now or clock.utcnow()
        self.state.roll(now.date(), account.equity, account.last_equity)

        if self.state.start_equity > 0:
            day_return = (account.equity - self.state.start_equity) / self.state.start_equity
            if day_return <= -abs(self.limits.daily_loss_limit_pct):
                self.halt(f"daily loss limit hit ({day_return:.2%})")

        if self.state.peak_equity > 0:
            drawdown = (account.equity - self.state.peak_equity) / self.state.peak_equity
            if drawdown <= -abs(self.limits.max_drawdown_halt_pct):
                self.halt(f"max drawdown breached ({drawdown:.2%})")

    def halt(self, reason: str) -> None:
        if not self.state.halted:
            log.error("TRADING HALTED: %s", reason)
        self.state.halted = True
        self.state.halt_reason = reason

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = None

    def record_open(
        self,
        idea: TradeIdea | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        """Count the entry, and stamp it for the re-entry cooldown.

        `idea` is optional so older call sites keep working, but a caller that
        omits it opts that (symbol, strategy) pair out of the cooldown - the
        engine has nothing to key on.
        """
        self.state.opened_today += 1
        if idea is not None:
            self.last_entry[(idea.symbol, idea.strategy)] = now or clock.utcnow()

    # -- the gate ----------------------------------------------------------- #
    def evaluate(
        self,
        idea: TradeIdea,
        account: AccountSnapshot,
        now: dt.datetime | None = None,
        market_open: bool = True,
    ) -> RiskVerdict:
        checks: dict[str, bool] = {}
        now = now or clock.utcnow()
        self.observe(account, now)

        def fail(rule: str, reason: str) -> RiskVerdict:
            checks[rule] = False
            log.info("REJECT %s [%s] %s", idea.symbol, rule, reason)
            verdict = RiskVerdict.reject(reason, checks)
            verdict.reasons.append(f"rule={rule}")
            return verdict

        # 0. The temporal firewall ------------------------------------------ #
        # This runs before everything else. A book that does not hold the
        # capital lock cannot open a position, regardless of how good the idea
        # is or how much equity is sitting there.
        if self.firewall is not None:
            from oaa.firewall.lock import Book

            book = Book(idea.book) if idea.book in {b.value for b in Book} else Book.INTRADAY
            allowed, why = self.firewall.may_open(book, now)
            if not allowed:
                return fail("firewall", why)
        checks["firewall"] = True

        # 1. Session state ------------------------------------------------- #
        if self.state.halted:
            return fail("halted", self.state.halt_reason or "trading halted")
        checks["halted"] = True

        if not market_open:
            return fail("market_closed", "market is closed")
        checks["market_closed"] = True

        # 2. Structural constraints ---------------------------------------- #
        if not self.limits.allow_undefined_risk and not idea.structure.is_defined_risk:
            return fail(
                "undefined_risk",
                f"{idea.structure.value} has uncapped downside and "
                "risk.allow_undefined_risk is false",
            )
        checks["undefined_risk"] = True

        if idea.max_loss is None or idea.max_loss <= 0:
            return fail("unknown_risk", "structure has no computable maximum loss")
        checks["unknown_risk"] = True

        if not 1 <= len(idea.legs) <= 4:
            return fail("leg_count", f"Alpaca accepts 1-4 legs, got {len(idea.legs)}")
        checks["leg_count"] = True

        if len({leg.symbol for leg in idea.legs}) != len(idea.legs):
            return fail("duplicate_legs", "duplicate leg symbols in one order")
        checks["duplicate_legs"] = True

        # 3. Portfolio shape ------------------------------------------------ #
        # Counts STRUCTURES, not legs. `option_positions()` returns one entry per
        # OCC contract, so a raw count made `max_positions: 25` bind at roughly
        # six iron condors - and the config comment that justified raising it
        # from 12 to 25 was reasoning in structures. The ledger knows which legs
        # belong to the same idea; without it, fall back to the leg count.
        open_positions = account.option_positions()
        if self.firewall is not None:
            entries = self.firewall.ledger.entries
            grouped = {
                entries[p.symbol.upper()].idea_id
                for p in open_positions
                if p.symbol.upper() in entries and entries[p.symbol.upper()].idea_id
            }
            loose = [p for p in open_positions if p.symbol.upper() not in entries]
            structure_count = len(grouped) + len(loose)
        else:
            structure_count = len(open_positions)
        if structure_count >= self.limits.max_positions:
            return fail(
                "max_positions",
                f"already at {structure_count} open structures, cap is "
                f"{self.limits.max_positions}",
            )
        checks["max_positions"] = True

        if self.state.opened_today >= self.limits.max_new_positions_per_day:
            return fail(
                "max_new_per_day",
                f"opened {self.state.opened_today} today, cap is "
                f"{self.limits.max_new_positions_per_day}",
            )
        checks["max_new_per_day"] = True

        # A structure whose every leg is ALREADY held on the same side is not a
        # new position, it is the same one doubled. Both this broker and Alpaca
        # net identical option symbols, so the position count and the leg count
        # below are unchanged by it and cannot catch it - which is how a book
        # polled every 15 minutes opened eight identical condors in one session
        # while every portfolio limit read green.
        held = {p.symbol: p.qty for p in account.option_positions()}
        if idea.legs and all(
            (leg.symbol in held)
            and ((held[leg.symbol] > 0) == (leg.side is Side.BUY))
            for leg in idea.legs
        ):
            return fail(
                "duplicate_structure",
                f"already holding this exact {idea.structure.value} on "
                f"{idea.symbol} - re-entering would double the position, not "
                "open a new one",
            )
        checks["duplicate_structure"] = True

        # A book that holds for days needs a cooldown measured in days; a book
        # that holds for minutes needs one measured in minutes. One global
        # number cannot serve both, and the 60-minute default let the SAME NVDA
        # condor open twice 75 minutes apart on a 10:00-15:15 scan grid - $784
        # of a $4,852 drawdown, from one duplicate.
        cooldown = int(
            idea.meta.get("reentry_cooldown_minutes")
            or getattr(self.limits, "reentry_cooldown_minutes", 0)
            or 0
        )
        if cooldown > 0:
            previous = self.last_entry.get((idea.symbol, idea.strategy))
            if previous is not None:
                elapsed = (now - previous).total_seconds() / 60.0
                # `<=`, not `<`. On a 15-minute scan grid an entry landing
                # exactly `cooldown` minutes after the last one is the same
                # trade re-taken, not a new opportunity - and a strict `<`
                # let 14:00 and 15:00 through as a pair on a 60-minute
                # cooldown, which is how NVDA and SPY ended up doubled.
                if elapsed <= cooldown:
                    return fail(
                        "reentry_cooldown",
                        f"{idea.strategy} opened {idea.symbol} "
                        f"{elapsed:.0f} minutes ago; the cooldown is "
                        f"{cooldown}. Polling more often is not the same as "
                        "finding more opportunities.",
                    )
        checks["reentry_cooldown"] = True

        # Scoped per BOOK. Without the book dimension a single resident carry
        # condor on SPY blocked EVERY intraday SPY trade for the three-to-ten
        # sessions it was held - and SPY and QQQ sit in both universes, so the
        # primary book would have been shut out of its two best underlyings by
        # the secondary one. The config comment already claimed "per book"; the
        # check did not implement it.
        same_underlying = [
            p for p in account.option_positions()
            if (p.underlying or underlying_of(p.symbol)) == idea.symbol
        ]
        if self.firewall is not None and idea.book:
            same_underlying = [
                p for p in same_underlying
                if self.firewall.ledger.book_of(p.symbol) == idea.book
            ]
        if len(same_underlying) >= self.limits.max_positions_per_underlying * len(idea.legs):
            return fail(
                "concentration",
                f"{idea.symbol} already has {len(same_underlying)} option legs open",
            )
        checks["concentration"] = True

        # 4. Sizing ---------------------------------------------------------- #
        quantity = size_by_risk(idea, account.equity, self.limits.max_risk_per_trade_pct)
        if quantity < 1:
            return fail(
                "sizing",
                f"max loss ${idea.max_loss:,.0f} exceeds "
                f"{self.limits.max_risk_per_trade_pct:.1%} of ${account.equity:,.0f} equity",
            )
        quantity = min(quantity, idea.quantity) if idea.quantity > 1 else quantity
        checks["sizing"] = True

        # 5. Aggregate exposure ---------------------------------------------- #
        trade_risk = idea.max_loss * quantity
        open_risk = sum(abs(p.market_value) for p in account.option_positions())
        if account.equity > 0:
            projected = (open_risk + trade_risk) / account.equity
            if projected > self.limits.max_portfolio_risk_pct:
                return fail(
                    "portfolio_risk",
                    f"projected portfolio risk {projected:.1%} exceeds "
                    f"{self.limits.max_portfolio_risk_pct:.1%}",
                )
        checks["portfolio_risk"] = True

        # 6. Cash ------------------------------------------------------------- #
        # A debit costs its premium; a CREDIT structure costs collateral, which
        # for a defined-risk spread is its max loss. Charging max(0, net_price)
        # made every short vertical, condor and butterfly - the entire carry
        # book - require $0 of buying power, so the gate could not bind on the
        # structures it most needed to.
        if idea.net_price >= 0:
            cash_needed = idea.net_price * 100 * quantity
        else:
            cash_needed = (idea.max_loss or 0.0) * quantity
        buffer = account.equity * self.limits.min_cash_buffer_pct
        available = (account.options_buying_power or account.buying_power)
        if available - cash_needed < buffer:
            return fail(
                "cash_buffer",
                f"needs ${cash_needed:,.0f}, buying power ${available:,.0f}, "
                f"buffer ${buffer:,.0f}",
            )
        checks["cash_buffer"] = True

        # 7. Time of day ------------------------------------------------------ #
        # The overnight book deliberately trades in the last five minutes, which
        # the generic no-trade window forbids. Its window is the firewall's, and
        # that has already been checked at step 0.
        if idea.book != "overnight" and not self._within_trading_window(now):
            return fail(
                "time_window",
                "inside the open/close no-trade window - spreads are widest there",
            )
        checks["time_window"] = True

        return self._finalise(idea, account, quantity, checks, now)

    def _finalise(
        self,
        idea: TradeIdea,
        account: AccountSnapshot,
        quantity: int,
        checks: dict[str, bool],
        now: dt.datetime,
    ) -> RiskVerdict:
        """Approve, log the size and the risk taken, and stamp the verdict."""
        trade_risk = (idea.max_loss or 0.0) * quantity
        log.info(
            "APPROVE [%s] %s x%d  risk=$%.0f (%.2f%% of equity)  %s",
            idea.book, idea.describe(), quantity, trade_risk,
            100 * trade_risk / account.equity if account.equity else 0, idea.strategy,
        )
        return RiskVerdict.approve(quantity, checks)

    # -- helpers ------------------------------------------------------------- #
    def _within_trading_window(self, now: dt.datetime) -> bool:
        """US equity session is 09:30-16:00 Eastern."""
        try:
            from zoneinfo import ZoneInfo

            local = now.astimezone(ZoneInfo(self.cfg.schedule.timezone))
        except Exception:  # noqa: BLE001 - never let a tz lookup block trading
            return True

        minutes = local.hour * 60 + local.minute
        open_min, close_min = 9 * 60 + 30, 16 * 60
        if minutes < open_min + self.limits.no_trade_open_minutes:
            return False
        if minutes > close_min - self.limits.no_trade_close_minutes:
            return False
        return True

    def status(self) -> dict[str, object]:
        return {
            "firewall": self.firewall.status() if self.firewall else None,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "opened_today": self.state.opened_today,
            "date": self.state.date.isoformat(),
            "start_equity": self.state.start_equity,
            "peak_equity": self.state.peak_equity,
        }
