"""The autonomous loop.

Two books, one account, one capital lock. The orchestrator's job is to run each
book inside its own window and never let them touch.

    09:35  overnight_exit     liquidate the overnight book, release the lock
    10:00  scan_and_trade     intraday book acquires the lock, trades
    15:15  intraday_cutoff    HARD cutoff: cancel, liquidate, confirm flat
    15:45  overnight_signal   Kalman + ML compute. Nothing is routed.
    15:54  overnight_verify   prove flat, read fresh Reg T, acquire the lock
    15:55  overnight_entry    dispatch the pairs combo
    16:10  report

Within a trading cycle the pipeline is:

    universe -> market data -> [partner: data_enrichment]
             -> strategies (per-symbol or portfolio)
             -> [partner: signal]
             -> AI assistant / critic scores it and writes the reasoning
             -> deterministic risk engine (firewall first, then limits)
             -> [partner: risk veto]
             -> execution (single ticket, or a rollback-safe combo)
             -> [partner: telemetry] -> journal

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
from oaa.strategies.base import Strategy, StrategyContext, load_strategies
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

        self.strategies: list[Strategy] = load_strategies(self.cfg)
        self.intraday = [s for s in self.strategies if s.capital_book == "intraday"]
        self.overnight = [s for s in self.strategies if s.capital_book == "overnight"]

        llm = get_llm(self.cfg.agents.llm)
        self.llm = llm
        self.critic = Critic(self.cfg, llm)
        self.memory = (
            Memory(settings.path(self.cfg.agents.memory.path), self.cfg.agents.memory.lookback_days)
            if self.cfg.agents.memory.enabled
            else None
        )
        self._pending: list[tuple[Strategy, TradeIdea, MarketContext | None]] = []

        # Universe discovery and the macro lens. Optional and non-blocking: if
        # it fails, the system trades the configured universe with a neutral
        # regime rather than not trading.
        self.discovery: DiscoveryEngine | None = None
        self.macro: MacroView = MacroView(rationale="no discovery cycle has run yet")
        if self.cfg.discovery.enabled:
            try:
                self.discovery = DiscoveryEngine(
                    settings, llm=llm, journal=self.journal
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("discovery unavailable (%s) - continuing without it", exc)

        log.info(
            "orchestrator ready: %d intraday + %d overnight strategies, "
            "%d partner adapters, broker=%s, LLM=%s, profile=%s, dry_run=%s",
            len(self.intraday), len(self.overnight), self.partners.count(),
            broker.name, llm.provider, self.cfg.profile, self.cfg.execution.dry_run,
        )
        self.journal.event(
            "startup",
            profile=self.cfg.profile,
            broker=self.broker.name,
            data_provider=self.data.name,
            intraday_strategies=[s.name for s in self.intraday],
            overnight_strategies=[s.name for s in self.overnight],
            partners=self.partners.stages(),
            llm=llm.provider,
            firewall=self.firewall.status(),
            dry_run=self.cfg.execution.dry_run,
        )

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def run_cycle(self, action: str, name: str = "manual") -> CycleResult:
        handlers = {
            "scan_and_trade": self.scan_and_trade,
            "manage_positions": self.manage_positions,
            "report": self.report,
            "flatten": self.flatten,
            "intraday_cutoff": self.intraday_cutoff,
            "discover": self.discover,
            "overnight_signal": self.overnight_signal,
            "overnight_verify": self.overnight_verify,
            "overnight_entry": self.overnight_entry,
            "overnight_exit": self.overnight_exit,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ValueError(f"unknown cycle action '{action}'")
        result = handler(name)
        log.info(result.summary())
        return result

    # ================================================================== #
    # FIREWALL CYCLES
    # ================================================================== #
    def intraday_cutoff(self, cycle: str = "intraday_cutoff") -> CycleResult:
        """15:15 ET. Cancel everything, liquidate the day book, confirm flat."""
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        report = self.firewall.run_intraday_cutoff(self.broker)
        result.positions_closed = max(0, report.positions_before - max(0, report.positions_after))
        result.firewall_passed = report.confirmed_flat
        result.notes.append(report.summary())
        if not report.confirmed_flat:
            result.errors.append("intraday book did not go flat - overnight entry will abort")
            self.risk.halt("15:15 cutoff failed to confirm a flat book")
        self.journal.snapshot(self._account())
        return result

    def discover(self, cycle: str = "discover") -> CycleResult:
        """Pre-market. Refresh the candidate pool and take the regime read.

        Runs before anything trades, so the macro view is in place for the whole
        session. Deliberately fault-tolerant — an attention feed being down is
        not a reason to skip a trading day.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        if self.discovery is None or not self.discovery.enabled:
            result.notes.append("discovery disabled")
            return result

        names = [s.name for s in self.strategies]
        try:
            outcome = self.discovery.run(strategies=names, pairs=self._live_pairs())
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"discovery failed: {exc}")
            log.exception("discovery cycle failed: %s", exc)
            return result

        self.macro = outcome.macro
        result.symbols_scanned = len(outcome.snapshot.symbols)
        result.notes.append(outcome.summary())
        result.notes.append(outcome.macro.summary())

        stood_down = [n for n in names if not outcome.macro.may_trade(n)]
        if stood_down:
            result.notes.append("stood down: " + ", ".join(stood_down))
        return result

    def overnight_signal(self, cycle: str = "overnight_signal") -> CycleResult:
        """15:45 ET. Fit the models and compute tonight's forecasts.

        Nothing is routed here. Separating computation from verification means
        the slow work (fetching bars, refitting) is done before the nine-minute
        window in which the trade actually has to be placed.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        if not self.overnight:
            result.notes.append("no overnight strategies enabled")
            return result

        self._refresh_macro_if_stale()
        account = self._account()
        symbols = sorted({s for strat in self.overnight for s in strat.universe()})
        contexts = self._gather_contexts(symbols)
        result.symbols_scanned = len(contexts)

        candidates: list[tuple[Strategy, TradeIdea, MarketContext | None]] = []
        for strategy in self.overnight:
            ctx = StrategyContext(
                account=account, config=self.cfg, contexts=contexts,
                params=strategy.params, budget=0.0, firewall=self.firewall,
                macro=self.macro,
            )
            try:
                for idea in strategy.generate(ctx):
                    idea.book = "overnight"
                    candidates.append((strategy, idea, contexts.get(idea.meta.get("long_leg", ""))))
            except (StrategyError, DataError) as exc:
                log.info("%s: %s", strategy.name, exc)
                result.notes.append(f"{strategy.name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{strategy.name}: {exc}")
                log.exception("overnight signal failed for %s", strategy.name)

        self._pending = candidates
        result.ideas_generated = len(candidates)

        for _, idea, _ in candidates:
            self.journal.record(Decision(
                cycle=cycle, action=DecisionAction.HOLD, symbol=idea.symbol,
                strategy=idea.strategy, idea=idea,
                rationale="signal computed; awaiting 15:54 firewall verification",
                agent_notes={"forecast": idea.meta.get("forecast", {})},
            ))
        if not candidates:
            result.notes.append("no pair passed its entry gates tonight")
        return result

    def _refresh_macro_if_stale(self, max_age_hours: float = 4.0) -> None:
        """Re-read the regime before the overnight decision if the morning view
        has gone stale. The 15:45 read is the one that actually gets used."""
        if self.discovery is None or not self.discovery.enabled:
            return
        age = (dt.datetime.now(dt.timezone.utc) - self.macro.asof).total_seconds() / 3600
        if age < max_age_hours:
            return
        log.info("macro view is %.1fh old - refreshing before the overnight decision", age)
        try:
            outcome = self.discovery.run(
                strategies=[s.name for s in self.strategies],
                pairs=self._live_pairs(),
                apply_filters=False,      # candidates were settled pre-market
            )
            self.macro = outcome.macro
        except Exception as exc:  # noqa: BLE001
            log.warning("macro refresh failed (%s) - keeping the morning view", exc)

    def overnight_verify(self, cycle: str = "overnight_verify") -> CycleResult:
        """15:54 ET. The gate. Prove flat, read fresh Reg T, acquire the lock."""
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        target = sum(
            (idea.meta.get("gross_notional") or 0.0) for _, idea, _ in self._pending
        ) or None
        verdict = self.firewall.run_overnight_verification(self.broker, target_trade_value=target)
        result.firewall_passed = verdict.passed
        result.notes.append(verdict.summary())
        if verdict.emergency_liquidated:
            result.errors.append("emergency liquidation fired at 15:54")
        if not verdict.passed:
            self._pending = []
        return result

    def overnight_entry(self, cycle: str = "overnight_entry") -> CycleResult:
        """15:55 ET. Route the pairs combo, but only while holding the lock."""
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))

        allowed, why = self.firewall.may_open(Book.OVERNIGHT)
        result.firewall_passed = allowed
        if not allowed:
            result.notes.append(f"blocked: {why}")
            log.warning("overnight entry blocked: %s", why)
            return result

        if not self._pending:
            # The 15:45 cycle may not have run (restart, or first day). Recompute
            # rather than silently skipping the night.
            log.info("no pending signals - recomputing inside the entry window")
            self.overnight_signal("overnight_entry_recompute")

        budget = self.firewall.budget_for(Book.OVERNIGHT)
        account = self._account()
        result.ideas_generated = len(self._pending)

        for strategy, idea, market in self._pending:
            idea.meta["verified_budget"] = budget
            self._execute_idea(
                strategy=strategy, idea=idea, market=market,
                account=account, cycle=cycle, result=result, combo=True,
            )
            account = self._account()

        self._pending = []
        self.journal.snapshot(account)
        return result

    def overnight_exit(self, cycle: str = "overnight_exit") -> CycleResult:
        """09:35 ET. Liquidate the overnight book and free the capital."""
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        before = self._account()
        report = self.firewall.run_overnight_exit(self.broker)
        result.positions_closed = max(0, len(before.positions) - max(0, report.positions_after))
        result.firewall_passed = report.confirmed_flat
        result.notes.append(report.summary())

        if self.memory:
            for position in before.positions:
                self.memory.record(
                    symbol=position.underlying or position.symbol,
                    strategy="overnight_pairs",
                    structure="pairs_collar",
                    pnl=position.unrealized_pl,
                    pnl_pct=position.unrealized_plpc,
                    held_days=1.0,
                    thesis="overnight gap held to the 09:35 liquidation",
                )
        if not report.confirmed_flat:
            result.errors.append("overnight book did not fully liquidate at 09:35")
        self.journal.snapshot(self._account())
        return result

    # ================================================================== #
    # INTRADAY
    # ================================================================== #
    def scan_and_trade(self, cycle: str = "scan") -> CycleResult:
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        account = self._account()
        self.risk.observe(account)

        if self.risk.state.halted:
            log.warning("cycle skipped - %s", self.risk.state.halt_reason)
            result.notes.append(self.risk.state.halt_reason or "halted")
            return result

        # The day book must take the lock before it can open anything.
        if self.firewall.holder() is not Book.INTRADAY:
            verdict = self.firewall.acquire_intraday(self.broker)
            result.firewall_passed = verdict.passed
            if not verdict.passed:
                result.notes.append(verdict.summary())
                log.info("intraday book could not acquire the lock: %s", verdict.summary())
                return result

        budget = self.firewall.budget_for(Book.INTRADAY)
        market_open = self.broker.is_market_open()
        symbols = self._intraday_symbols()
        contexts = self._gather_contexts(symbols)
        result.symbols_scanned = len(contexts)

        candidates = self._generate(self.intraday, contexts, account, budget, result)
        candidates = self.partners.run("signal", candidates) or candidates
        result.ideas_generated = len(candidates)
        if not candidates:
            log.info("[%s] no candidates from %d symbols", cycle, len(contexts))
            return result

        candidates.sort(key=lambda c: -(c[0].weight * c[1].confidence))
        for strategy, idea, market in candidates:
            self._execute_idea(
                strategy=strategy, idea=idea, market=market, account=account,
                cycle=cycle, result=result, combo=idea.structure.is_pairs,
                market_open=market_open,
            )
            account = self._account()

        self.journal.snapshot(account)
        return result

    def manage_positions(self, cycle: str = "manage") -> CycleResult:
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        account = self._account()
        positions = account.option_positions()
        result.symbols_scanned = len(positions)

        mgmt = self.cfg.management
        today = dt.date.today()

        for position in positions:
            reason: str | None = None
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
                cycle=cycle, action=DecisionAction.CLOSE,
                symbol=position.underlying or underlying_of(position.symbol),
                rationale=reason,
            )
            try:
                decision.fill = self.executor.close(position.symbol, abs(position.qty))
                result.positions_closed += 1
                log.info("closing %s: %s", position.symbol, reason)
                if self.memory:
                    self.memory.record(
                        symbol=decision.symbol or position.symbol, strategy="managed",
                        structure="leg", pnl=position.unrealized_pl, pnl_pct=pnl_pct,
                        held_days=0.0, thesis=reason,
                    )
            except Exception as exc:  # noqa: BLE001
                decision.error = str(exc)
                result.errors.append(f"close {position.symbol}: {exc}")
                log.exception("failed closing %s", position.symbol)
            self.journal.record(decision)

        self.journal.snapshot(account)
        return result

    # ================================================================== #
    def flatten(self, cycle: str = "flatten") -> CycleResult:
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        result.positions_closed = self.executor.flatten_all()
        self.firewall.release(Book.INTRADAY)
        self.firewall.release(Book.OVERNIGHT)
        self.journal.event("flatten", closed=result.positions_closed)
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
                ctx = StrategyContext(
                    account=account, config=self.cfg, contexts=contexts,
                    params=strategy.params, budget=budget, firewall=self.firewall,
                    macro=self.macro,
                )
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
                ctx = StrategyContext(
                    market=market, account=account, config=self.cfg,
                    contexts=contexts, params=strategy.params,
                    budget=budget, firewall=self.firewall, macro=self.macro,
                )
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

    def _execute_idea(
        self,
        strategy: Strategy,
        idea: TradeIdea,
        market: MarketContext | None,
        account: AccountSnapshot,
        cycle: str,
        result: CycleResult,
        combo: bool = False,
        market_open: bool = True,
    ) -> None:
        decision = Decision(cycle=cycle, symbol=idea.symbol, strategy=strategy.name, idea=idea)
        try:
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
            if combo:
                plan = plan_from_idea(idea)
                outcome = self.combo.execute(plan, risk_stamp=verdict.stamp)
                decision.action = DecisionAction.OPEN if outcome.ok else DecisionAction.SKIP
                decision.rationale += f" | {outcome.summary()}"
                if outcome.filled_steps:
                    decision.fill = outcome.filled_steps[0].fill
                if outcome.ok or outcome.dry_run:
                    result.orders_placed += len(outcome.filled_steps) or 1
                    self.risk.record_open()
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
                    self.risk.record_open()

        except Exception as exc:  # noqa: BLE001
            decision.error = str(exc)
            decision.action = DecisionAction.SKIP
            result.errors.append(f"{idea.symbol}: {exc}")
            log.exception("failed processing %s", idea.symbol)

        self.partners.run("telemetry", decision)
        self.journal.record(decision)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _live_pairs(self) -> list[tuple[str, str]]:
        """The pairs the overnight book may trade tonight.

        Handed to the macro lens so it can compare each leg's attention against
        its partner's - which is the whole judgement it is being asked to make.
        """
        pairs: list[tuple[str, str]] = []
        for strategy in self.overnight:
            for spec in getattr(strategy, "pairs", lambda: [])():
                pairs.append((spec.left, spec.right))
        return pairs

    def _account(self) -> AccountSnapshot:
        return self.broker.account()

    def _intraday_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for strategy in self.intraday:
            symbols.update(strategy.universe())
        universe = set(self.cfg.universe.active())
        return sorted(symbols & universe) if universe else sorted(symbols)

    def _gather_contexts(self, symbols: list[str]) -> dict[str, MarketContext]:
        """Fetch market data in parallel, then let partners enrich it.

        Threads, not async: the data provider is synchronous and the rate
        limiter is shared, so this is bounded by the API budget, not by CPU.
        """
        contexts: dict[str, MarketContext] = {}
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
                "intraday": [s.name for s in self.intraday],
                "overnight": [s.name for s in self.overnight],
            },
            "partners": self.partners.stages(),
            "risk": self.risk.status(),
            "counts": self.journal.counts(),
        }

    def close(self) -> None:
        self.partners.teardown()
        self.broker.close()


def _synthetic_market(idea: TradeIdea) -> MarketContext:
    """A minimal context so the critic can score a combo with no single symbol."""
    return MarketContext(
        symbol=idea.symbol,
        asof=dt.datetime.now(dt.timezone.utc),
        spot=float(idea.meta.get("gross_notional", 0.0)) or 1.0,
        enrichment={"forecast": idea.meta.get("forecast", {})},
    )
