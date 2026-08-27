"""The autonomous loop.

One account, three books, one capital boundary. The orchestrator's job is to
run each book inside its own window and never let the transient books consume
margin the resident book needs at the close.

    09:15  discover            candidate pool + macro regime read (pre-market)
    09:45  intraday_scan       transient lease acquired, momentum book trades
    10:00  carry_scan          resident book scans for rich premium
    13:00  manage_positions    mechanical exits on both books
    15:15  intraday_cutoff     HARD cutoff: cancel, liquidate TRANSIENT, confirm
    15:45  carry_verify        prove no residual transient exposure, carry margin
                               covered with fresh Reg T headroom
    16:10  report

Within a trading cycle the pipeline is:

    universe -> market data -> [partner: data_enrichment]
             -> strategy gate stack (momentum / premium / catalyst / event / macro)
             -> [partner: signal]
             -> AI critic scores it and writes the reasoning
             -> deterministic risk engine (firewall first, then limits)
             -> [partner: risk veto]
             -> execution (atomic multi-leg, or a rollback-safe legged combo)
             -> ledger registration -> [partner: telemetry] -> journal

Nothing waits for a human.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from oaa.agents.critic import Critic
from oaa.agents.llm import get_llm
from oaa.agents.memory import Memory
from oaa.brokers.base import Broker
from oaa.config.loader import Settings
from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import (
    AccountSnapshot,
    Decision,
    DecisionAction,
    MarketContext,
    TradeIdea,
)
from oaa.data.base import MarketDataProvider
from oaa.discovery.engine import DiscoveryEngine
from oaa.discovery.macro import MacroView
from oaa.execution.combo import ComboExecutor, plan_from_idea
from oaa.execution.router import ExecutionRouter
from oaa.firewall.lock import Book, TemporalFirewall
from oaa.options.occ import underlying_of
from oaa.partners.base import PartnerHub
from oaa.risk.engine import RiskEngine
from oaa.signals.catalyst import CatalystEngine, MacroCalendar
from oaa.signals.gates import parse_utc
from oaa.strategies.base import Strategy, StrategyContext, load_strategies
from oaa.telemetry.costs import CostModel
from oaa.telemetry.journal import Journal

log = get_logger("orchestrator")


@dataclass
class CycleResult:
    cycle: str
    started: dt.datetime
    symbols_scanned: int = 0
    ideas_generated: int = 0
    ideas_approved: int = 0
    orders_placed: int = 0
    positions_closed: int = 0
    firewall_passed: bool | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"[{self.cycle}]"]
        if self.symbols_scanned:
            parts.append(f"scanned {self.symbols_scanned}")
        if self.ideas_generated:
            parts.append(f"ideas {self.ideas_generated}")
        if self.ideas_approved:
            parts.append(f"approved {self.ideas_approved}")
        if self.orders_placed:
            parts.append(f"orders {self.orders_placed}")
        if self.positions_closed:
            parts.append(f"closed {self.positions_closed}")
        if self.firewall_passed is not None:
            parts.append(f"firewall {'PASS' if self.firewall_passed else 'BLOCK'}")
        if self.notes:
            parts.append("| " + "; ".join(self.notes))
        if self.errors:
            parts.append(f"| errors {len(self.errors)}")
        return " ".join(parts)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        data: MarketDataProvider,
        journal: Journal | None = None,
        partners: PartnerHub | None = None,
        firewall: TemporalFirewall | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.config
        self.broker = broker
        self.data = data

        settings.ensure_run_dirs()
        t = self.cfg.telemetry
        self.journal = journal or Journal(
            settings.path(t.journal), settings.path(t.db), settings.path(t.equity_curve)
        )
        self.partners = partners or PartnerHub(self.cfg, self.journal)
        self.firewall = firewall or TemporalFirewall(self.cfg, journal=self.journal)
        self.risk = RiskEngine(self.cfg, firewall=self.firewall)
        self.executor = ExecutionRouter(self.cfg, broker)
        self.combo = ComboExecutor(self.cfg, broker, journal=self.journal)
        self.costs = CostModel.from_config(self.cfg)

        self.strategies: list[Strategy] = load_strategies(self.cfg)
        self.carry = [s for s in self.strategies if s.capital_book == "carry"]
        self.intraday = [s for s in self.strategies if s.capital_book == "intraday"]
        self.opportunistic = [
            s for s in self.strategies if s.capital_book == "opportunistic"
        ]

        # The catalyst engine and its committed macro calendar. Deterministic,
        # so the intraday loop never waits on a model call.
        calendar_path = settings.path(
            self._strategy_param("intraday_momentum", "catalyst_gate.macro_calendar")
            or "config/macro_events.yaml"
        )
        self.catalyst = CatalystEngine(
            weights=self._strategy_param("intraday_momentum", "catalyst_gate.factor_weights"),
            lookback_minutes=int(
                self._strategy_param("intraday_momentum", "catalyst_gate.lookback_minutes") or 30
            ),
            calendar=MacroCalendar.load(calendar_path),
        )

        llm = get_llm(self.cfg.agents.llm)
        self.llm = llm
        self.critic = Critic(self.cfg, llm)
        self.memory = (
            Memory(settings.path(self.cfg.agents.memory.path), self.cfg.agents.memory.lookback_days)
            if self.cfg.agents.memory.enabled
            else None
        )
        self._open_ideas: dict[str, TradeIdea] = {}

        self.discovery: DiscoveryEngine | None = None
        self.macro: MacroView = MacroView(rationale="no discovery cycle has run yet")
        self.attention: Any = None
        if self.cfg.discovery.enabled:
            try:
                self.discovery = DiscoveryEngine(settings, llm=llm, journal=self.journal)
            except Exception as exc:  # noqa: BLE001
                log.warning("discovery unavailable (%s) - continuing without it", exc)

        log.info(
            "orchestrator ready: %d carry + %d intraday + %d opportunistic strategies, "
            "%d partner adapters, broker=%s, LLM=%s, profile=%s, dry_run=%s",
            len(self.carry), len(self.intraday), len(self.opportunistic),
            self.partners.count(), broker.name, llm.provider, self.cfg.profile,
            self.cfg.execution.dry_run,
        )
        self.journal.event(
            "startup",
            profile=self.cfg.profile,
            broker=self.broker.name,
            data_provider=self.data.name,
            carry_strategies=[s.name for s in self.carry],
            intraday_strategies=[s.name for s in self.intraday],
            opportunistic_strategies=[s.name for s in self.opportunistic],
            partners=self.partners.stages(),
            llm=llm.provider,
            firewall=self.firewall.status(),
            dry_run=self.cfg.execution.dry_run,
        )

    # ------------------------------------------------------------------ #
    def _strategy_param(self, strategy: str, path: str) -> Any:
        for candidate in self.strategies:
            if candidate.name == strategy:
                return candidate.p(path)
        return None

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def run_cycle(self, action: str, name: str = "manual") -> CycleResult:
        handlers = {
            "scan_and_trade": self.scan_and_trade,
            "manage_positions": self.manage_positions,
            "report": self.report,
            "flatten": self.flatten,
            "discover": self.discover,
            "carry_scan": self.carry_scan,
            "intraday_scan": self.intraday_scan,
            "intraday_cutoff": self.intraday_cutoff,
            "carry_verify": self.carry_verify,
            "submission_flatten": self.submission_flatten,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ValueError(f"unknown cycle action '{action}'")
        result = handler(name)
        log.info(result.summary())
        return result

    # ================================================================== #
    # DISCOVERY
    # ================================================================== #
    def discover(self, cycle: str = "discover") -> CycleResult:
        """Pre-market. Refresh the candidate pool and take the regime read.

        Deliberately fault-tolerant: an attention feed being down is not a
        reason to skip a trading day.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        if self.discovery is None or not self.discovery.enabled:
            result.notes.append("discovery disabled")
            return result

        names = [s.name for s in self.strategies]
        try:
            outcome = self.discovery.run(strategies=names)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"discovery failed: {exc}")
            log.exception("discovery cycle failed: %s", exc)
            return result

        self.macro = outcome.macro
        self.attention = outcome.snapshot
        result.symbols_scanned = len(outcome.snapshot.symbols)
        result.notes.append(outcome.summary())
        result.notes.append(outcome.macro.summary())

        stood_down = [n for n in names if not outcome.macro.may_trade(n)]
        if stood_down:
            result.notes.append("stood down: " + ", ".join(stood_down))

        upcoming = self.catalyst.calendar.next_event(dt.datetime.now(dt.timezone.utc))
        if upcoming:
            result.notes.append(
                f"next scheduled print: {upcoming.name} at {upcoming.when:%Y-%m-%d %H:%M UTC}"
            )
        return result

    # ================================================================== #
    # CARRY BOOK - resident
    # ================================================================== #
    def carry_scan(self, cycle: str = "carry_scan") -> CycleResult:
        """Reserve the resident book's capital, then look for rich premium.

        Runs inside the carry entry window only. Its structures are then HELD -
        there is no nightly exit, because theta accrues on calendar days and a
        nightly round trip would pay the spread for nothing.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        if not self.carry:
            result.notes.append("no carry strategies enabled")
            return result

        account = self._account()
        self.risk.observe(account)
        if self.risk.state.halted:
            result.notes.append(self.risk.state.halt_reason or "halted")
            return result

        cutoff = parse_utc(self.cfg.management.entry_cutoff_utc)
        if cutoff and dt.datetime.now(dt.timezone.utc) >= cutoff:
            result.notes.append(
                f"past the {cutoff:%Y-%m-%d %H:%M UTC} entry cutoff - no new carry structures"
            )
            return result

        verdict = self.firewall.allocate_carry(self.broker)
        result.firewall_passed = verdict.passed
        result.notes.append(verdict.summary())
        if not verdict.passed:
            return result

        self._refresh_macro_if_stale()
        symbols = sorted({s for strat in self.carry for s in strat.universe()})
        contexts = self._gather_contexts(symbols)
        result.symbols_scanned = len(contexts)

        candidates = self._generate(
            self.carry, contexts, account, self.firewall.budget_for(Book.CARRY), result
        )
        candidates = self.partners.run("signal", candidates) or candidates
        result.ideas_generated = len(candidates)
        if not candidates:
            result.notes.append("no underlying passed all four carry gates")
            return result

        candidates.sort(key=lambda c: -(c[0].weight * c[1].confidence))
        market_open = self.broker.is_market_open()
        for strategy, idea, market in candidates:
            self._execute_idea(
                strategy=strategy, idea=idea, market=market, account=account,
                cycle=cycle, result=result, market_open=market_open,
            )
            account = self._account()

        self.journal.snapshot(account)
        return result

    def carry_verify(self, cycle: str = "carry_verify") -> CycleResult:
        """15:45 ET. The day's sign-off on the resident book."""
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        verdict = self.firewall.run_carry_verification(self.broker)
        result.firewall_passed = verdict.passed
        result.notes.append(verdict.summary())
        if verdict.emergency_liquidated:
            result.errors.append("emergency liquidation fired at the carry verification")
        if not verdict.passed:
            result.errors.append(
                "carry verification failed - transient books disabled for the next session"
            )
        self.journal.snapshot(self._account())
        return result

    # ================================================================== #
    # TRANSIENT BOOKS - intraday and opportunistic
    # ================================================================== #
    def intraday_scan(self, cycle: str = "intraday_scan") -> CycleResult:
        return self._transient_scan(cycle, self.intraday + self.opportunistic)

    def scan_and_trade(self, cycle: str = "scan") -> CycleResult:
        """Backwards-compatible alias for the transient scan."""
        return self._transient_scan(cycle, self.intraday + self.opportunistic)

    def _transient_scan(self, cycle: str, strategies: list[Strategy]) -> CycleResult:
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        if not strategies:
            result.notes.append("no transient strategies enabled")
            return result

        account = self._account()
        self.risk.observe(account)
        if self.risk.state.halted:
            log.warning("cycle skipped - %s", self.risk.state.halt_reason)
            result.notes.append(self.risk.state.halt_reason or "halted")
            return result

        # The transient books lease whatever the resident book is not using.
        if self.firewall.holder() is None:
            verdict = self.firewall.acquire_transient(self.broker, Book.INTRADAY)
            result.firewall_passed = verdict.passed
            if not verdict.passed:
                result.notes.append(verdict.summary())
                log.info("transient lease refused: %s", verdict.summary())
                return result
            result.notes.append(verdict.summary())

        budget = self.firewall.budget_for(Book.INTRADAY)
        market_open = self.broker.is_market_open()
        symbols = sorted({s for strat in strategies for s in strat.universe()})
        contexts = self._gather_contexts(symbols)
        result.symbols_scanned = len(contexts)

        candidates = self._generate(strategies, contexts, account, budget, result)
        candidates = self.partners.run("signal", candidates) or candidates
        result.ideas_generated = len(candidates)
        if not candidates:
            log.info("[%s] no candidates from %d symbols", cycle, len(contexts))
            return result

        candidates.sort(key=lambda c: -(c[0].weight * c[1].confidence))
        for strategy, idea, market in candidates:
            self._execute_idea(
                strategy=strategy, idea=idea, market=market, account=account,
                cycle=cycle, result=result, market_open=market_open,
            )
            account = self._account()

        self.journal.snapshot(account)
        return result

    def intraday_cutoff(self, cycle: str = "intraday_cutoff") -> CycleResult:
        """15:15 ET. Cancel everything, liquidate the TRANSIENT books, confirm flat.

        The resident carry structures are deliberately left alone: the ledger
        knows which legs belong to which book, so a multi-session iron condor is
        not collateral damage of the day book going home.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        report = self.firewall.run_intraday_cutoff(self.broker)
        result.positions_closed = max(0, report.positions_before - max(0, report.positions_after))
        result.firewall_passed = report.confirmed_flat
        result.notes.append(report.summary())
        if not report.confirmed_flat:
            result.errors.append(
                "transient books did not go flat - carry verification will fail"
            )
            self.risk.halt("15:15 cutoff failed to confirm a flat transient book")
        self.journal.snapshot(self._account())
        return result

    # ================================================================== #
    # MANAGEMENT
    # ================================================================== #
    def manage_positions(self, cycle: str = "manage") -> CycleResult:
        """Mechanical exits. No discretionary exits, no LLM in the exit path.

        Each position is routed back to the strategy that opened it, so the
        carry book gets its 30%-of-max-profit / DTE-floor / short-strike rules
        and the intraday book gets its target / stop / time-stop / VWAP-recross
        rules, rather than one global pair of thresholds pretending to fit both.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        account = self._account()
        positions = account.option_positions()
        result.symbols_scanned = len(positions)

        mgmt = self.cfg.management
        today = dt.date.today()
        contexts = self._gather_contexts(
            sorted({p.underlying or underlying_of(p.symbol) for p in positions})
        ) if positions else {}

        for position in positions:
            symbol = position.underlying or underlying_of(position.symbol)
            reason = self._exit_reason(position, contexts, account, cycle)
            if reason is None:
                pnl_pct = position.unrealized_plpc
                if pnl_pct >= mgmt.profit_target_pct:
                    reason = f"profit target {mgmt.profit_target_pct:.0%} hit ({pnl_pct:.1%})"
                elif pnl_pct <= -abs(mgmt.stop_loss_pct):
                    reason = f"stop loss {mgmt.stop_loss_pct:.0%} hit ({pnl_pct:.1%})"
                elif position.expiry is not None:
                    dte = (position.expiry - today).days
                    if dte <= mgmt.close_at_dte:
                        reason = f"{dte}d to expiry - closing to avoid assignment risk"
            if reason is None:
                continue

            decision = Decision(
                cycle=cycle, action=DecisionAction.CLOSE, symbol=symbol, rationale=reason,
            )
            try:
                decision.fill = self.executor.close(position.symbol, abs(position.qty))
                self.firewall.ledger.forget(position.symbol)
                result.positions_closed += 1
                log.info("closing %s: %s", position.symbol, reason)
                if self.memory:
                    self.memory.record(
                        symbol=symbol, strategy=self.firewall.ledger.book_of(position.symbol),
                        structure="leg", pnl=position.unrealized_pl,
                        pnl_pct=position.unrealized_plpc, held_days=0.0, thesis=reason,
                    )
            except Exception as exc:  # noqa: BLE001
                decision.error = str(exc)
                result.errors.append(f"close {position.symbol}: {exc}")
                log.exception("failed closing %s", position.symbol)
            self.journal.record(decision)

        self.journal.snapshot(account)
        return result

    def _exit_reason(
        self,
        position: Any,
        contexts: dict[str, MarketContext],
        account: AccountSnapshot,
        cycle: str,
    ) -> str | None:
        """Ask the owning strategy first; fall back to the global rules."""
        book = self.firewall.ledger.book_of(position.symbol)
        entry = self.firewall.ledger.entries.get(position.symbol.upper())
        idea = self._open_ideas.get(entry.idea_id) if entry else None
        owner = next(
            (s for s in self.strategies if entry and s.name == entry.strategy), None
        )
        if owner is None or idea is None:
            return None
        ctx = StrategyContext(
            account=account, config=self.cfg, contexts=contexts,
            params=owner.params, budget=self.firewall.budget_for(Book.parse(book)),
            firewall=self.firewall, macro=self.macro, catalyst=self.catalyst,
            attention=self.attention,
        )
        try:
            return owner.should_exit(ctx, idea, position.unrealized_plpc)
        except Exception as exc:  # noqa: BLE001
            log.debug("exit rule failed for %s: %s", position.symbol, exc)
            return None

    # ================================================================== #
    def flatten(self, cycle: str = "flatten") -> CycleResult:
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        result.positions_closed = self.executor.flatten_all()
        self.firewall.release_transient()
        self.journal.event("flatten", closed=result.positions_closed)
        self.journal.snapshot(self._account())
        return result

    def submission_flatten(self, cycle: str = "submission_flatten") -> CycleResult:
        """Close the ENTIRE book, resident included, with the same confirmed-flat
        discipline as the 15:15 cutoff.

        Realised P&L on a flat account is unambiguous evidence. Open positions
        ask a judge to trust a mid-price mark on an instrument with a wide quote.
        Liquidating early also leaves time to fix a failed close: a structure
        that will not fill at 14:50 UTC is a genuine problem; at 13:45 it is an
        inconvenience.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        before = self._account()
        report = self.firewall.run_submission_flatten(self.broker)
        result.positions_closed = max(
            0, len(before.positions) - max(0, report.positions_after)
        )
        result.firewall_passed = report.confirmed_flat
        result.notes.append(report.summary())
        if not report.confirmed_flat:
            result.errors.append(
                "submission flatten did not confirm - resolve manually before submitting"
            )
        self.journal.snapshot(self._account())
        return result

    def report(self, cycle: str = "report") -> CycleResult:
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        account = self._account()
        self.journal.snapshot(account)
        self.journal.event(
            "report", equity=account.equity, day_pl=account.day_pl,
            positions=len(account.positions), risk=self.risk.status(),
            firewall=self.firewall.status(),
        )
        log.info(
            "equity $%s | day P&L %+.2f (%.2f%%) | %d positions | RegT $%s",
            f"{account.equity:,.2f}", account.day_pl, account.day_pl_pct * 100,
            len(account.positions), f"{account.regt_buying_power or 0:,.0f}",
        )
        return result

    # ================================================================== #
    # shared pipeline
    # ================================================================== #
    def _refresh_macro_if_stale(self, max_age_hours: float = 4.0) -> None:
        if self.discovery is None or not self.discovery.enabled:
            return
        age = (dt.datetime.now(dt.timezone.utc) - self.macro.asof).total_seconds() / 3600
        if age < max_age_hours:
            return
        log.info("macro view is %.1fh old - refreshing before the carry decision", age)
        try:
            outcome = self.discovery.run(
                strategies=[s.name for s in self.strategies], apply_filters=False
            )
            self.macro = outcome.macro
            self.attention = outcome.snapshot
        except Exception as exc:  # noqa: BLE001
            log.warning("macro refresh failed (%s) - keeping the morning view", exc)

    def _generate(
        self,
        strategies: list[Strategy],
        contexts: dict[str, MarketContext],
        account: AccountSnapshot,
        budget: float,
        result: CycleResult,
    ) -> list[tuple[Strategy, TradeIdea, MarketContext | None]]:
        candidates: list[tuple[Strategy, TradeIdea, MarketContext | None]] = []

        for strategy in strategies:
            if strategy.mode == "portfolio":
                ctx = self._context(account, contexts, strategy, budget)
                try:
                    for idea in strategy.generate(ctx):
                        idea.book = strategy.capital_book
                        candidates.append((strategy, idea, None))
                except (StrategyError, DataError) as exc:
                    log.debug("%s: %s", strategy.name, exc)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{strategy.name}: {exc}")
                    log.exception("strategy %s failed", strategy.name)
                continue

            wanted = set(strategy.universe())
            for symbol, market in contexts.items():
                if symbol not in wanted:
                    continue
                ctx = self._context(account, contexts, strategy, budget, market)
                try:
                    for idea in strategy.generate(ctx):
                        idea.book = strategy.capital_book
                        candidates.append((strategy, idea, market))
                except (StrategyError, DataError) as exc:
                    log.debug("%s/%s: %s", symbol, strategy.name, exc)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{strategy.name}/{symbol}: {exc}")
                    log.exception("strategy %s failed on %s", strategy.name, symbol)
        return candidates

    def _context(
        self,
        account: AccountSnapshot,
        contexts: dict[str, MarketContext],
        strategy: Strategy,
        budget: float,
        market: MarketContext | None = None,
    ) -> StrategyContext:
        return StrategyContext(
            market=market, account=account, config=self.cfg, contexts=contexts,
            params=strategy.params, budget=budget, firewall=self.firewall,
            macro=self.macro, catalyst=self.catalyst, attention=self.attention,
        )

    def _execute_idea(
        self,
        strategy: Strategy,
        idea: TradeIdea,
        market: MarketContext | None,
        account: AccountSnapshot,
        cycle: str,
        result: CycleResult,
        market_open: bool = True,
    ) -> None:
        decision = Decision(cycle=cycle, symbol=idea.symbol, strategy=strategy.name, idea=idea)
        try:
            # 0. modelled cost, attached before anything else so the rejection
            #    log carries it too. Paper fills do not charge this; the deck
            #    reports gross, modelled cost and net side by side.
            breakdown = self.costs.round_trip(idea)
            idea.meta["modelled_cost"] = breakdown.as_dict()

            # 1. critic ------------------------------------------------------ #
            critique = self.critic.score(
                idea,
                market or _synthetic_market(idea),
                account,
                opened_today=self.risk.state.opened_today,
                memory=self.memory.as_prompt() if self.memory else "",
            )
            idea.score = float(critique.get("score", idea.confidence))
            decision.agent_notes = critique
            decision.rationale = str(critique.get("reasoning", ""))[:2000]

            if not self.critic.accepts(critique):
                decision.action = DecisionAction.SKIP
                decision.rationale = (
                    f"critic passed (score {idea.score:.2f} < "
                    f"{self.cfg.agents.critic.min_score_to_trade:.2f}): " + decision.rationale
                )
                self.journal.record(decision)
                return

            # 2. deterministic risk (firewall checked first, inside) ---------- #
            verdict = self.risk.evaluate(idea, account, market_open=market_open)
            decision.verdict = verdict
            if not verdict.approved:
                decision.action = DecisionAction.SKIP
                self.journal.record(decision)
                return

            # 3. partner veto ------------------------------------------------- #
            allowed, veto_reason = self.partners.veto(idea)
            if not allowed:
                verdict.approved = False
                verdict.reasons.append(veto_reason or "partner veto")
                decision.action = DecisionAction.SKIP
                self.journal.record(decision)
                return

            result.ideas_approved += 1

            # 4. execute -------------------------------------------------------- #
            legged = (
                idea.structure.is_multileg
                and self.cfg.execution.multileg_mode == "legged"
            )
            if legged:
                plan = plan_from_idea(idea)
                outcome = self.combo.execute(plan, risk_stamp=verdict.stamp)
                decision.action = DecisionAction.OPEN if outcome.ok else DecisionAction.SKIP
                decision.rationale += f" | {outcome.summary()}"
                if outcome.filled_steps:
                    decision.fill = outcome.filled_steps[0].fill
                if outcome.ok or outcome.dry_run:
                    result.orders_placed += len(outcome.filled_steps) or 1
                    self._record_open(idea, strategy)
                else:
                    decision.error = outcome.summary()
                    result.errors.append(outcome.summary())
                    if outcome.unwind_errors:
                        self.risk.halt("combo unwind failed - manual intervention required")
            else:
                execution = self.executor.execute(idea, verdict)
                decision.fill = execution.fill
                decision.action = DecisionAction.OPEN if execution.ok else DecisionAction.SKIP
                if execution.error:
                    decision.error = execution.error
                if execution.ok:
                    result.orders_placed += 1
                    self._record_open(idea, strategy)

        except Exception as exc:  # noqa: BLE001
            decision.error = str(exc)
            decision.action = DecisionAction.SKIP
            result.errors.append(f"{idea.symbol}: {exc}")
            log.exception("failed processing %s", idea.symbol)

        self.partners.run("telemetry", decision)
        self.journal.record(decision)

    def _record_open(self, idea: TradeIdea, strategy: Strategy) -> None:
        """Attribute every leg to its book, so 15:15 liquidates the right ones."""
        self.risk.record_open(idea)
        self._open_ideas[idea.id] = idea
        self.firewall.ledger.register(idea, book=strategy.capital_book)

    # ------------------------------------------------------------------ #
    def _account(self) -> AccountSnapshot:
        return self.broker.account()

    def _gather_contexts(self, symbols: list[str]) -> dict[str, MarketContext]:
        """Fetch market data in parallel, then let partners enrich it."""
        contexts: dict[str, MarketContext] = {}
        if not symbols:
            return contexts
        workers = min(4, max(1, len(symbols)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.data.context, symbol): symbol for symbol in symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    contexts[symbol] = future.result()
                except DataError as exc:
                    log.warning("no data for %s: %s", symbol, exc)
                except Exception as exc:  # noqa: BLE001
                    log.exception("context fetch failed for %s: %s", symbol, exc)

        for symbol, market in list(contexts.items()):
            enriched = self.partners.run("data_enrichment", market)
            if isinstance(enriched, MarketContext):
                contexts[symbol] = enriched
        return contexts

    def status(self) -> dict[str, Any]:
        account = self._account()
        return {
            "profile": self.cfg.profile,
            "broker": self.broker.name,
            "data_provider": self.data.name,
            "dry_run": self.cfg.execution.dry_run,
            "market_open": self.broker.is_market_open(),
            "equity": account.equity,
            "day_pl": account.day_pl,
            "regt_buying_power": account.regt_buying_power,
            "daytrading_buying_power": account.daytrading_buying_power,
            "positions": len(account.positions),
            "firewall": self.firewall.status(),
            "macro": self.macro.as_dict(),
            "discovery": (
                self.discovery.pool.stats() if self.discovery else {"enabled": False}
            ),
            "strategies": {
                "carry": [s.name for s in self.carry],
                "intraday": [s.name for s in self.intraday],
                "opportunistic": [s.name for s in self.opportunistic],
            },
            "partners": self.partners.stages(),
            "risk": self.risk.status(),
            "counts": self.journal.counts(),
        }

    def close(self) -> None:
        self.partners.teardown()
        self.broker.close()


def _synthetic_market(idea: TradeIdea) -> MarketContext:
    """A minimal context so the critic can score an idea with no live snapshot."""
    return MarketContext(
        symbol=idea.symbol,
        asof=dt.datetime.now(dt.timezone.utc),
        spot=float(idea.meta.get("spot", 0.0)) or 1.0,
        enrichment={"gates": idea.meta.get("gates", {})},
    )
