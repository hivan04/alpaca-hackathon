"""The events runner: arm before the print, flatten after it.

This is the book's real entry point (`oaa events arm` / `oaa events flatten`).
It runs in its own process, on its own schedule, and never leases capital from
the temporal firewall - the same arrangement the weekend book used, and for the
same reason: a book whose entire life is one overnight hold does not fit the
intraday/carry tenancy model, and bending that model to fit it is how an
options book breaks at 14:45 on a Tuesday.

What it still shares with everything else, deliberately:

    RiskEngine        every ticket is signed, or it is not sent
    ExecutionRouter   one audited order path
    Journal           the same decision log the judges read

The order of operations is the whole design:

    screen the week   -> LLM proposes, the calendar confirms
    price each event  -> implied vs realised, spread gate
    rank              -> top N by divergence
    read the evidence -> Alpaca news + StockTwits, per name
    call direction    -> Featherless, with an abstention as a valid answer
    size on confidence-> bounded three ways
    risk, then route  -> unchanged from the rest of the system
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot, Decision, DecisionAction, TradeIdea
from oaa.strategies.base import StrategyContext
from oaa.strategies.events import calendar as cal
from oaa.strategies.events.direction import DirectionCall, abstention_rate, predict
from oaa.strategies.events.llm_roles import role_llm
from oaa.strategies.events.params import EventsParams
from oaa.strategies.events.sentiment import alpaca_news_fetcher, gather
from oaa.strategies.events.sizing import nightly_budget
from oaa.strategies.events.strategy import EarningsEventDirectional
from oaa.strategies.events.watch import EventWatcher, WatchReport

log = get_logger("strategies.events.engine")


@dataclass
class ArmReport:
    """What one arming cycle did, in a shape the CLI and the tests both read."""

    asof: dt.date
    screened: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    considered: list[str] = field(default_factory=list)
    calls: list[DirectionCall] = field(default_factory=list)
    opened: list[TradeIdea] = field(default_factory=list)
    declined: dict[str, str] = field(default_factory=dict)
    budget: float = 0.0
    budget_used: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def abstention_rate(self) -> float:
        return abstention_rate(self.calls)

    def summary(self) -> str:
        return (
            f"{self.asof}: {len(self.considered)} event(s) considered, "
            f"{len(self.opened)} opened, {len(self.declined)} declined, "
            f"abstention rate {self.abstention_rate:.0%}, "
            f"${self.budget_used:,.0f} of ${self.budget:,.0f} risked"
        )


class EventsEngine:
    """Screen, decide, and route. One object, injected with everything it uses."""

    def __init__(
        self,
        *,
        settings: Any,
        broker: Any,
        data: Any,
        llm: Any,
        params: EventsParams,
        strategy: EarningsEventDirectional,
        risk: Any,
        router: Any,
        journal: Any = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.data = data
        self.llm = llm
        self.params = params
        self.strategy = strategy
        self.risk = risk
        self.router = router
        self.journal = journal
        self.news_fn = alpaca_news_fetcher(data)

        # Two questions, two models. The triage runs dozens of times a day on
        # a narrow question; the direction call runs once per name and sizes
        # the position. Each may name its own model and its own key; naming
        # neither returns this same client, so nothing changes for a config
        # that sets nothing. See llm_roles.py.
        base = getattr(getattr(settings, "config", None), "agents", None)
        base_llm = getattr(base, "llm", None) if base else None
        self.watch_llm = llm
        self.direction_llm = llm
        if base_llm is not None:
            self.watch_llm = role_llm(
                base_llm, role="watch",
                model=params.watch.model,
                api_key_env=params.watch.api_key_env,
                temperature=params.watch.temperature,
                max_tokens=params.watch.max_tokens,
                seed=params.watch.seed,
                fallback=llm,
            )
            self.direction_llm = role_llm(
                base_llm, role="direction",
                model=params.direction.model,
                api_key_env=params.direction.api_key_env,
                temperature=params.direction.temperature,
                max_tokens=params.direction.max_tokens,
                seed=params.direction.seed,
                fallback=llm,
            )
        #: Reads each name for days before it reports rather than once at the
        #: close of arm day. See `watch.py` for why a snapshot is the wrong
        #: shape for this job.
        self.watcher = EventWatcher(
            llm=self.watch_llm,
            params=params.watch,
            sentiment=params.sentiment,
            calendar=strategy.calendar,
            news_fn=self.news_fn,
            store_dir=settings.path(params.watch.store_dir) if settings else None,
        )

    # ------------------------------------------------------------------ #
    def week_window(self, asof: dt.date) -> tuple[dt.date, dt.date]:
        """Monday to Friday of the week containing `asof`, or of the week ahead
        when the screen is run over a weekend."""
        monday = asof - dt.timedelta(days=asof.weekday())
        if asof.weekday() >= 5:
            monday += dt.timedelta(days=7)
        return monday, monday + dt.timedelta(days=4)

    def screen(self, asof: dt.date) -> cal.ScreenResult:
        start, end = self.week_window(asof)
        return cal.screen_week(
            self.llm, self.strategy.calendar, start, end, self.params.universe_hint
        )

    # ------------------------------------------------------------------ #
    def arm(self, asof: dt.date | None = None, dry_run: bool | None = None) -> ArmReport:
        """Open positions into tonight's prints."""
        asof = asof or dt.date.today()
        report = ArmReport(asof=asof)

        screened = self.screen(asof)
        report.screened = screened.symbols()
        report.unverified = screened.unverified

        due = [e for e in screened.events if e.entry_date == asof]
        report.considered = [e.symbol for e in due]
        if not due:
            log.info("no confirmed print arms today (%s)", asof)
            return report

        account: AccountSnapshot = self.broker.account()
        report.budget = nightly_budget(float(account.equity or 0), self.params.sizing)
        remaining = report.budget

        # Highest divergence first, so the strongest events get the budget
        # before it is spent on the marginal ones.
        for event in sorted(due, key=lambda e: e.symbol):
            symbol = event.symbol
            try:
                market = self.data.context(symbol)
            except Exception as exc:  # noqa: BLE001 - one dead symbol, not the night
                report.errors.append(f"{symbol}: market context failed - {exc}")
                log.warning("%s: could not build a market context - %s", symbol, exc)
                continue

            # The fresh pack, then the week behind it. The last hours before
            # a print are the most informative hours, so today's items still
            # dominate; the dossier is what stops them being the ONLY items.
            pack = gather(symbol, self.params.sentiment, self.news_fn)
            pack = self.watcher.attach(pack, asof)
            call = predict(self.direction_llm, pack, self.params.direction)
            report.calls.append(call)
            if not call.actionable:
                report.declined[symbol] = call.skip_reason
                self._journal_skip(symbol, call, pack.counts())
                continue

            ctx = StrategyContext(
                account=account,
                config=self.settings.config,
                market=market,
                params={"direction_call": call, "budget_remaining": remaining},
            )
            ideas = self.strategy.generate(ctx)
            if not ideas:
                report.declined[symbol] = "no structure passed the screen or the sizing"
                continue

            idea = ideas[0]
            # One symbol's context is all this book has - it arms names one at
            # a time. The Greek gate reports the resulting coverage rather than
            # presenting a partial read as a whole-book one.
            verdict = self.risk.evaluate(
                idea, account, contexts={market.symbol: market} if market else None
            )
            if not verdict.approved:
                report.declined[symbol] = "; ".join(verdict.reasons) or "risk declined"
                self._journal(DecisionAction.SKIP, idea, verdict=verdict,
                              rationale=f"risk declined: {report.declined[symbol]}")
                continue

            if dry_run:
                report.opened.append(idea)
                remaining -= float(idea.meta.get("risk_dollars") or 0)
                self._journal(DecisionAction.SKIP, idea, verdict=verdict,
                              rationale="dry run - not routed")
                continue

            result = self.router.execute(idea, verdict)
            if result.ok:
                report.opened.append(idea)
                spent = float(idea.meta.get("risk_dollars") or 0)
                remaining -= spent
                report.budget_used += spent
                self.risk.record_open(idea)
                self._journal(DecisionAction.OPEN, idea, verdict=verdict, fill=result.fill)
            else:
                report.declined[symbol] = result.error or "execution failed"
                self._journal(DecisionAction.SKIP, idea, verdict=verdict,
                              rationale="execution failed", error=result.error)

        if report.calls and report.abstention_rate == 0.0:
            log.warning(
                "every direction call was actionable. A model that never abstains "
                "is not filtering - check the prompt and the confidence floor "
                "before trusting tonight's sizing."
            )
        log.info(report.summary())
        return report

    # ------------------------------------------------------------------ #
    def watch(self, asof: dt.date | None = None) -> WatchReport:
        """Read every name whose print is within the window, and stop at the
        ones whose print has passed.

        Called several times a day by `oaa run`. It opens nothing, closes
        nothing and touches no capital: its entire output is a dated note on a
        name the book may later arm, plus the retirement of the names it no
        longer needs to read.
        """
        return self.watcher.poll(asof or dt.date.today())

    # ------------------------------------------------------------------ #
    def flatten(self, asof: dt.date | None = None) -> list[str]:
        """Close everything this book opened for prints that have now reported.

        Positions are matched by underlying against the calendar rather than by
        an internal ledger: if a leg was opened by this book and is still on the
        account the morning after its print, it closes, whatever else happened
        overnight.
        """
        asof = asof or dt.date.today()
        due = {
            e.symbol for e in self.strategy.calendar.values()
            if e.confirmed and e.exit_date <= asof
        }
        closed: list[str] = []
        for position in self.broker.account().positions:
            underlying = (position.underlying or position.symbol).upper()
            if underlying not in due:
                continue
            try:
                self.router.close(position.symbol)
                closed.append(position.symbol)
                log.info("closed %s (%s reported)", position.symbol, underlying)
            except Exception as exc:  # noqa: BLE001
                log.error("could not close %s - %s", position.symbol, exc)
        if not closed:
            log.info("nothing to flatten for %s", asof)
        return closed

    # ------------------------------------------------------------------ #
    def _journal(
        self,
        action: DecisionAction,
        idea: TradeIdea,
        verdict: Any = None,
        fill: Any = None,
        rationale: str = "",
        error: str | None = None,
    ) -> None:
        if self.journal is None:
            return
        self.journal.record(Decision(
            cycle="events_arm",
            action=action,
            symbol=idea.symbol,
            strategy=idea.strategy,
            idea=idea,
            verdict=verdict,
            fill=fill,
            rationale=rationale or idea.thesis,
            agent_notes={k: v for k, v in idea.meta.items() if k.startswith("llm_")},
            error=error,
        ))

    def _journal_skip(self, symbol: str, call: DirectionCall, counts: dict[str, int]) -> None:
        """A declined name is evidence too - it is what the judges read to see
        the filter working rather than the book simply not firing."""
        if self.journal is None:
            return
        self.journal.record(Decision(
            cycle="events_arm",
            action=DecisionAction.SKIP,
            symbol=symbol,
            strategy="earnings_event_directional",
            rationale=call.skip_reason,
            agent_notes={**call.as_meta(), **counts},
        ))
