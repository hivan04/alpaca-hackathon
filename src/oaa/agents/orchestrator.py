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
    15:50  events_arm          the events book: read the sentiment, call the
                               direction, open into tonight's confirmed prints
    16:10  report

and, first thing the next morning:

    09:45  events_flatten      close what last night's prints have now reported

The events book is the one book here that does NOT hold a firewall lease. Its
life is a single overnight hold that begins after the 15:15 transient cutoff,
which is a shape the intraday/carry tenancy model has no phase for. Bending the
firewall to admit it would weaken the interlock that keeps the day books out of
the carry book's margin, so instead `_events_engine` builds it a RiskEngine with
`firewall=None` - identical to what `oaa events arm` has always done - and every
other guarantee (signed risk stamp, one audited order path, the same journal)
is unchanged. See `docs/` and the events book README for the full argument.

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
from zoneinfo import ZoneInfo

from oaa.agents.critic import Critic
from oaa.agents.llm import get_llm
from oaa.agents.memory import Memory
from oaa.brokers.base import Broker
from oaa.config.loader import Settings
from oaa.core import clock
from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger, tape
from oaa.core.switchboard import Switchboard
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

        # Which books are switched on for THIS account, right now. Per profile,
        # re-read at the top of every cycle, and falling back to the config's
        # own `enabled` flag wherever the file says nothing.
        self.switchboard = Switchboard.open(getattr(self.cfg.telemetry, "run_dir", None))
        self._configured = {ref.name: ref.enabled for ref in self.cfg.strategies}
        self.strategies: list[Strategy] = []
        self.carry: list[Strategy] = []
        self.intraday: list[Strategy] = []
        self.opportunistic: list[Strategy] = []
        self._load_strategies()

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
    # ------------------------------------------------------------------ #
    # the runtime switchboard
    # ------------------------------------------------------------------ #
    def _switched_on(self, name: str) -> bool:
        return self.switchboard.enabled(name, self._configured.get(name, False))

    def _load_strategies(self) -> None:
        """Build every book this account may trade, then keep the ones that are
        switched on. Books the config disables are still BUILT when the
        switchboard turns them on, which is what lets a toggle take effect
        without restarting the agent."""
        switched_on = {n for n, on in self.switchboard.state().items() if on}
        built = load_strategies(self.cfg, include=switched_on)
        #: Every book that COULD trade here, switched on or not. Exit rules are
        #: looked up from this list so a position whose book was switched off
        #: mid-hold is still managed by its own strategy rather than falling
        #: through to the global rules.
        self._built = built
        self.strategies = [s for s in built if self._switched_on(s.name)]
        self.carry = [s for s in self.strategies if s.capital_book == "carry"]
        self.intraday = [s for s in self.strategies if s.capital_book == "intraday"]
        self.opportunistic = [
            s for s in self.strategies if s.capital_book == "opportunistic"
        ]

    def _refresh_strategies(self) -> None:
        """Cheap: a stat() per cycle, a rebuild only when the file changed."""
        if self.switchboard.reload_if_changed():
            before = {s.name for s in self.strategies}
            self._load_strategies()
            after = {s.name for s in self.strategies}
            if before != after:
                log.info(
                    "switchboard changed: on=%s off=%s",
                    sorted(after - before) or "-", sorted(before - after) or "-",
                )
                self.journal.event(
                    "switchboard", enabled=sorted(after), turned_on=sorted(after - before),
                    turned_off=sorted(before - after), source=str(self.switchboard.path),
                )

    def run_cycle(self, action: str, name: str = "manual") -> CycleResult:
        # A book switched off stops OPENING at the next scan. Positions it
        # already holds are still managed and closed below - an off switch that
        # abandoned open risk would be a worse bug than the one it prevents.
        self._refresh_strategies()
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
            "events_arm": self.events_arm,
            "events_flatten": self.events_flatten,
            "events_watch": self.events_watch,
            "daily_report": self.daily_report,
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

        # The global cutoff is null as of 29 Aug - it gated every book, and the
        # event strategy arms on dated prints that fall after it. Kept as a
        # null-safe backstop for any future dated deadline. The carry book's own
        # cutoff lives in its strategy params and now vetoes per candidate
        # (visible in the rejection log) rather than short-circuiting the cycle.
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
                contexts=contexts,
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
        # One lease covers the whole transient pool - intraday and
        # opportunistic scan together in this cycle and go flat together at
        # 15:15, so the lease is acquired once under INTRADAY and `may_open`
        # admits any transient book while it is held.
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
                contexts=contexts,
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
    # THE EVENTS BOOK
    # ================================================================== #
    # Two cycles, and a different use of the model from every other book here.
    #
    # Elsewhere the LLM is a CRITIC: a deterministic gate stack finds the
    # candidate and the model scores what the rules already proposed. In this
    # book the model is a JUDGE. The calendar opens the gate - the entry
    # condition is a date, so the book cannot fail to fire the way a threshold
    # crossing can - and what the model then supplies is the one thing no
    # indicator on this repo's tape can: a directional read of the sentiment
    # circulating around a name in the hours before it reports. It reads an
    # evidence pack (Alpaca headlines + the StockTwits public stream, sanitised
    # and budget-capped), returns a direction, a confidence and the evidence it
    # actually cited, and an abstention is a valid and expected answer. The
    # confidence then sets the size. Nothing downstream changes: the idea still
    # meets RiskEngine, still routes through ExecutionRouter, still lands in the
    # journal under `cycle == "events_arm"` for the judges to read.
    #
    # The engine is built lazily and cached: constructing it costs a params
    # file read and an LLM handle, and 22 of the day's 24 cycles have no use
    # for either.
    _events_engine_cache: Any = None

    def _events_ref(self) -> Any:
        for ref in self.cfg.strategies:
            if ref.book == "events":
                return ref
        return None

    def _events_engine(self) -> Any:
        """Build (once) the same engine `oaa events arm` builds.

        The RiskEngine here takes `firewall=None` deliberately. This book arms
        at 15:50, after the 15:15 transient cutoff, and holds overnight: no
        firewall phase permits that, and no phase should have to. The trade-off
        is stated rather than hidden - an events leg is unattributed to the
        ledger and would therefore be treated as transient if it were somehow
        still open at a later 15:15 cutoff. The morning flatten is what makes
        that unreachable in the normal case, and a sweep is the conservative
        error if it ever is reached.
        """
        if self._events_engine_cache is not None:
            return self._events_engine_cache

        from oaa.strategies.events import EventsEngine, load_params
        from oaa.strategies.events.strategy import EarningsEventDirectional

        ref = self._events_ref()
        if ref is None:
            raise StrategyError(
                "no strategy in config declares `book: events` - the events "
                "cycles have nothing to run. Add earnings_event_directional "
                "to config/default.yaml's strategies list."
            )
        params_path = self.settings.path(
            ref.params_file or "config/strategies/earnings_event.yaml"
        )
        self._events_engine_cache = EventsEngine(
            settings=self.settings,
            broker=self.broker,
            data=self.data,
            llm=self.llm,
            params=load_params(params_path),
            strategy=EarningsEventDirectional(ref, self.cfg),
            risk=RiskEngine(self.cfg, firewall=None),
            router=self.executor,
            journal=self.journal,
        )
        log.info("events engine ready (firewall bypassed by design), params=%s", params_path)
        return self._events_engine_cache

    def _events_switched_on(self, result: CycleResult) -> bool:
        """The Control tab's toggle, honoured here as it is for every book."""
        ref = self._events_ref()
        if ref is None:
            result.errors.append("no strategy declares `book: events`")
            return False
        if not self._switched_on(ref.name):
            result.notes.append(f"{ref.name} is switched off - standing down")
            return False
        return True

    def events_watch(self, cycle: str = "events_watch") -> CycleResult:
        """Read the names whose prints are coming. Hourly, 04:00-16:00 ET.

        This is the half of the book that used not to exist. The direction
        model saw a name once, at 15:50 on arm day, and judged the print from
        whatever was on the wire in that minute - so an estimate revision that
        landed on the Tuesday was information the book never had. The watch
        reads each name on the day the news arrives, judges the new items once,
        and retires the name the day it reports.

        It opens nothing and touches no capital, so it runs whether or not the
        book is switched on: standing the ARM down is a decision about risk,
        and losing the run-up as a side effect of it would be an accident.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        if self._events_ref() is None:
            result.errors.append("no strategy declares `book: events`")
            return result
        try:
            report = self._events_engine().watch(clock.today())
        except Exception as exc:  # noqa: BLE001 - a dead feed is not a dead loop
            log.exception("events watch failed: %s", exc)
            result.errors.append(f"events watch failed: {exc}")
            return result

        result.symbols_scanned = len(report.watching)
        result.notes.append(report.summary())
        result.errors.extend(report.errors)
        if report.noted:
            result.notes.append("noted: " + ", ".join(sorted(report.noted)))
        if report.retired:
            result.notes.append(
                "stopped watching (reported): " + ", ".join(sorted(report.retired))
            )
        # The tape line: one per watch cycle, whatever happened. A cycle that
        # read fourteen names and found nothing is a real and useful state -
        # reporting only the interesting ones would make a silent feed look
        # identical to a quiet market, which is the failure this book keeps
        # having to design against.
        leans = ", ".join(sorted(report.noted)) or "nothing material"
        tape().info(
            "WATCH %d name(s) read, %d new item(s), %d note(s): %s",
            len(report.watching), sum(report.new_items.values()),
            len(report.noted), leans,
        )
        self.journal.event(
            "events_watch",
            watching=report.watching,
            new_items=report.new_items,
            noted=report.noted,
            quiet=report.quiet,
            retired=report.retired,
        )
        return result

    def _arm_is_too_late(self, result: CycleResult) -> bool:
        """Refuse an arm that the clock has already left behind.

        `schedule.no_entry_after` has been in the params since the book was
        written and, until 30 Aug, was read by NOTHING - the same shape of
        defect as the `model` and `seed` fields that a comment promised and no
        code consumed.

        It matters because of how the runner recovers. `Runner._due` fires
        every cycle whose time has passed and has not fired today, which is
        correct and deliberate: a crash at 15:10 must not skip the 15:15
        cutoff. But a laptop that sleeps through the afternoon and wakes at
        17:00 hits the same path, and an unguarded `events_arm` would then try
        to open a debit spread into a closed market - on the judged account,
        whose full history the judges read.

        Standing down is the conservative error. A night not traded costs the
        book one opportunity; an order sent hours after the close is on the
        record permanently and cannot be explained away. The refusal is
        journalled with the clock that caused it, so it reads as a decision
        rather than as a night the agent mysteriously did nothing.
        """
        from zoneinfo import ZoneInfo

        try:
            params = self._events_engine().params
            deadline = params.no_entry_after_at()
            zone = ZoneInfo(self.cfg.schedule.timezone)
        except Exception as exc:  # noqa: BLE001 - never let the guard be the outage
            log.warning("could not evaluate the arm deadline (%s) - arming anyway", exc)
            return False

        local = clock.now(zone)
        if local.time() <= deadline:
            return False

        note = (
            f"{local:%H:%M} {self.cfg.schedule.timezone} is past the "
            f"{deadline:%H:%M} no-entry deadline - standing down rather than "
            "arming into a closed or closing market"
        )
        log.warning("events arm refused: %s", note)
        result.notes.append(note)
        self.journal.event(
            "events_arm",
            action="skip",
            reason="past no_entry_after",
            clock=local.isoformat(),
            no_entry_after=params.schedule.no_entry_after,
        )
        return True

    def events_arm(self, cycle: str = "events_arm") -> CycleResult:
        """15:50 ET. Open into tonight's confirmed prints.

        Everything expensive is inside `EventsEngine.arm`: the week screen, the
        vol screen, the evidence pack, the direction call, the sizing. This
        method's job is to run it inside the loop's error and reporting
        contract, so a bad night degrades to a logged cycle rather than a dead
        process.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        if not self._events_switched_on(result):
            return result
        if self.risk.state.halted:
            result.notes.append(f"risk halted: {self.risk.state.halt_reason}")
            return result
        if self._arm_is_too_late(result):
            return result

        try:
            report = self._events_engine().arm(clock.today())
        except Exception as exc:  # noqa: BLE001 - one bad night, not the process
            log.exception("events arm failed: %s", exc)
            result.errors.append(f"events arm failed: {exc}")
            return result

        result.symbols_scanned = len(report.screened)
        result.ideas_generated = len(report.considered)
        result.ideas_approved = len(report.opened)
        result.orders_placed = len(report.opened)
        result.notes.append(report.summary())
        result.errors.extend(report.errors)
        if report.unverified:
            result.notes.append(
                "unconfirmed, not armed: " + ", ".join(sorted(report.unverified))
            )
        # A judge that never abstains is not judging. The engine logs this too;
        # it is repeated on the cycle so it reaches the dashboard and the EOD
        # report rather than only the process log.
        if report.calls and report.abstention_rate == 0.0:
            result.notes.append(
                "WARNING: abstention rate 0% - the direction model declined "
                "nothing tonight, which is what a broken filter looks like"
            )
        self.journal.event(
            "events_arm",
            considered=report.considered,
            opened=[idea.symbol for idea in report.opened],
            declined=report.declined,
            abstention_rate=report.abstention_rate,
            budget=report.budget,
            budget_used=report.budget_used,
            unverified=report.unverified,
        )
        self.journal.snapshot(self._account())
        return result

    def events_flatten(self, cycle: str = "events_flatten") -> CycleResult:
        """09:45 ET. Close what reported overnight, into the IV collapse.

        Unconditional by design: this runs whether or not the book is switched
        on, because a book switched off mid-hold must still have its open risk
        closed. The same reasoning as `_refresh_strategies`.
        """
        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        if self._events_ref() is None:
            result.errors.append("no strategy declares `book: events`")
            return result
        try:
            closed = self._events_engine().flatten(clock.today())
        except Exception as exc:  # noqa: BLE001
            log.exception("events flatten failed: %s", exc)
            result.errors.append(f"events flatten failed: {exc}")
            return result

        result.positions_closed = len(closed)
        result.notes.append(
            f"closed {len(closed)} events leg(s): {', '.join(closed)}" if closed
            else "no events positions were open"
        )
        self.journal.event("events_flatten", closed=closed)
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

            # Attribute the close to the strategy that OPENED it, off the
            # ledger, before the executor call - `ledger.forget` runs below and
            # the attribution is gone after it. Without this every close is
            # journalled with `strategy: null`, lands in a phantom "unknown"
            # book in the daily report, and no book can be shown to have made
            # or lost anything.
            entry = self.firewall.ledger.entries.get(position.symbol.upper())
            # `book_of` defaults an unknown symbol to "intraday", so it cannot
            # be the fallback here - it would label an unattributed leg with a
            # book it never belonged to. No entry means genuinely unknown.
            owner = (entry.strategy or entry.book or None) if entry else None
            decision = Decision(
                cycle=cycle, action=DecisionAction.CLOSE, symbol=symbol,
                strategy=owner, rationale=reason,
            )
            try:
                decision.fill = self.executor.close(position.symbol, abs(position.qty))
                # Only a confirmed close counts as one. Every broker backend
                # returns None on failure rather than raising, so counting the
                # attempt meant a leg that could not be closed was logged as
                # closed AND had its book attribution erased - after which the
                # 15:15 cutoff treats the unattributed leg as transient and
                # liquidates one leg of a multi-session condor. Dry run also
                # returns None, and must not mutate the ledger either.
                confirmed = decision.fill is not None
                if not confirmed:
                    if not self.cfg.execution.dry_run:
                        result.errors.append(
                            f"close {position.symbol}: broker returned no fill - "
                            "position left open and still attributed"
                        )
                        log.error("close NOT confirmed for %s: %s",
                                  position.symbol, reason)
                    self.journal.record(decision)
                    continue
                book = self.firewall.ledger.book_of(position.symbol)
                self.firewall.ledger.forget(position.symbol)
                result.positions_closed += 1
                log.info("closing %s: %s", position.symbol, reason)
                # The P&L is on the snapshot we already hold. Logging the close
                # without it - which is what this did until 30 Aug - asks an
                # operator to go and look up the one number they wanted.
                pnl = getattr(position, "unrealized_pl", None)
                pct = getattr(position, "unrealized_plpc", None)
                # ...and onto the decision, not just the tape. The mark at the
                # moment of a confirmed close IS the realised amount for this
                # leg; the daily report reads `realized_pl` off the payload.
                if pnl is not None:
                    decision.realized_pl = float(pnl)
                tape().info(
                    "CLOSE %s | %s | %s | %s",
                    position.symbol,
                    "P&L unavailable" if pnl is None else f"P&L ${pnl:+,.2f}",
                    "" if pct is None else f"{pct * 100:+.1f}%",
                    reason,
                )
                if self.memory:
                    self.memory.record(
                        symbol=symbol, strategy=book,
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
            (s for s in self._built if entry and s.name == entry.strategy), None
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

    def daily_report(self, cycle: str = "daily_report") -> CycleResult:
        """After the close: the session, read back out of the journal.

        Runs last, opens nothing, and is deliberately not allowed to fail the
        day: a report that raised would put a `cycle_error` in tomorrow's
        report about yesterday's report. It also takes a final account
        snapshot first, so the closing equity in the file is the closing
        equity and not the 15:45 one.
        """
        from oaa.telemetry.daily import generate_daily_report

        result = CycleResult(cycle=cycle, started=dt.datetime.now(dt.timezone.utc))
        tz = self.cfg.schedule.timezone
        try:
            self.journal.snapshot(self._account())
        except Exception as exc:  # noqa: BLE001 - a stale close price is
            # better than no report at all.
            result.errors.append(f"closing snapshot failed: {exc}")
            log.warning("daily report: closing snapshot failed (%s)", exc)

        day = dt.datetime.now(ZoneInfo(tz)).date()
        try:
            session, paths = generate_daily_report(
                journal=self.journal,
                day=day,
                out_dir=self.settings.path(self.report_dir()),
                profile=self.cfg.profile,
                timezone=tz,
                llm=self.llm,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"daily report failed: {exc}")
            log.exception("daily report failed: %s", exc)
            return result

        result.notes.append(session.headline())
        result.notes.append(str(paths["markdown"]))
        self.journal.event(
            "daily_report",
            date=session.date,
            day_pl=session.day_pl,
            fills=len(session.fills),
            declined=len(session.potential),
            gate_rejections=session.gate_rejections,
            path=str(paths["markdown"]),
        )
        # On the tape rather than at INFO: `telemetry.console: focused` filters
        # INFO, and "the day's report exists, here it is" is precisely an
        # operator line - the same class as OPEN and CLOSE.
        tape().info("REPORT %s -> %s", session.headline(), paths["markdown"])
        return result

    def report_dir(self) -> str:
        """`<telemetry.report_dir>/<profile>` - one folder per account."""
        base = getattr(self.cfg.telemetry, "report_dir", "reports")
        return f"{base}/{self.cfg.profile}"

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
        contexts: dict[str, MarketContext] | None = None,
    ) -> None:
        """`contexts` is this cycle's full snapshot set, not just this symbol's.

        The aggregate-Greek gate needs it: the greeks of an already-open
        position are recovered by matching its OCC symbol against the chains,
        and a position can be on a symbol other than the one being traded -
        which is precisely the case the gate exists to catch.
        """
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
                    f"critic scored {idea.score:.2f}, below the "
                    f"{self.cfg.agents.critic.min_score_to_trade:.2f} bar: " + decision.rationale
                )
                self.journal.record(decision)
                return

            # 2. deterministic risk (firewall checked first, inside) ---------- #
            verdict = self.risk.evaluate(
                idea, account, market_open=market_open, contexts=contexts,
            )
            decision.verdict = verdict
            if verdict.metrics:
                idea.meta["risk_metrics"] = dict(verdict.metrics)
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
        """Attribute every leg to its book, so 15:15 liquidates the right ones.

        Also the single place both execution paths (single-leg and combo) meet
        after a confirmed open, which is why the tape line lives here rather
        than in either branch above.
        """
        idea.meta.setdefault("opened_at", clock.utcnow().isoformat())
        self.risk.record_open(idea)
        self._open_ideas[idea.id] = idea
        self.firewall.ledger.register(idea, book=strategy.capital_book)
        risk = "risk unknown" if idea.max_loss is None else f"max loss ${idea.max_loss:,.0f}"
        tape().info(
            "OPEN  %s %s x%d @ %.2f | %s | %s | %s",
            idea.symbol, idea.structure.value if hasattr(idea.structure, "value")
            else idea.structure, idea.quantity, idea.net_price, risk,
            strategy.name, (idea.thesis or "no thesis recorded")[:90],
        )

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
