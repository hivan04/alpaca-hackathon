"""Replay engine.

An honest note on scope, because it decides how the output may be used.

Alpaca's free tier does not serve a historical option chain with greeks, so
this is a *replay* harness, not a tick-accurate options backtester. It drives
the real strategy, risk and execution code over a sequence of MarketContexts
built from REAL Alpaca bars and REAL Alpaca headlines, with a MODELLED option
chain sitting on top (`chain.py`, `ivmodel.py`) and a fill model that crosses
the spread in the direction that costs money.

That makes it useful for exactly what a one-week event needs it for:

  * proving a strategy fires when you expect it to, and stays quiet otherwise
  * catching sizing and risk-limit bugs before they cost live paper P&L
  * showing, trade by trade, which gate let each position through
  * producing an equity curve for the deck, with the assumptions attached

It is not evidence of edge, and the deck must not claim it is. The judged
number is live paper P&L.

What the engine does that a naive loop does not
-----------------------------------------------
  clock         every strategy and risk check runs with the wall clock frozen
                to the replayed timestamp, so "is there earnings inside the
                expiry window" is asked about June, not about today
  marking       every open leg is repriced from the model each session, so the
                equity curve is a mark-to-market path and not a step function
                that only moves when something closes
  exits         the strategy's own `should_exit` runs each session, and expiry
                settles at intrinsic - positions are not held to the end of the
                window and marked at a fantasy mid
  costs         regulatory and exchange fees are deducted in cash at the close
                of each round trip, margin interest accrues on the requirement
                for as long as the structure is held, and the spread is paid
                inside the fill on BOTH sides rather than reported beside it
  pipeline      the decision path is the LIVE one, in the live order:
                modelled cost -> critic -> risk engine -> partner veto. A
                replay that skips the critic over-reports trades, because every
                candidate the critic would have passed on gets taken.
  provenance    each trade keeps the gate metrics, the critic's verdict and
                reasoning, the risk checks, the market state and the headlines
                that were on the tape when it opened
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

from oaa.backtest.chain import ChainModel
from oaa.backtest.critic import MODE_HEURISTIC, ReplayCritic
from oaa.brokers.sim import SimBroker
from oaa.config.loader import Settings
from oaa.core import clock
from oaa.core.logging import get_logger
from oaa.core.types import Leg, MarketContext, Side, TradeIdea
from oaa.data.indicators import max_drawdown, sharpe
from oaa.options.occ import parse_occ
from oaa.risk.engine import RiskEngine
from oaa.strategies.base import Strategy, StrategyContext, load_strategies
from oaa.telemetry.costs import CostModel

log = get_logger("backtest")

MULTIPLIER = 100
_TRADING_DAYS = 252


class ContextSource(abc.ABC):
    """Yields (timestamp, {symbol: MarketContext}) in chronological order."""

    @abc.abstractmethod
    def __iter__(self) -> Iterator[tuple[dt.datetime, dict[str, MarketContext]]]: ...


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass
class TradeRecord:
    """One round trip, with everything needed to justify it after the fact."""

    trade_id: str
    symbol: str
    strategy: str
    book: str
    structure: str
    quantity: int
    opened_at: str
    closed_at: str | None = None
    held_days: float = 0.0
    entry_price: float = 0.0          # net per structure, + = debit paid
    exit_price: float = 0.0           # net proceeds per structure
    gross_pnl: float = 0.0
    fees: float = 0.0                 # regulatory + exchange, cash
    margin_interest: float = 0.0
    spread_cost: float = 0.0          # modelled, already inside the fills
    net_pnl: float = 0.0
    return_on_risk: float = 0.0
    max_loss: float | None = None
    max_profit: float | None = None
    probability_of_profit: float | None = None
    confidence: float = 0.0
    exit_reason: str = "open"
    status: str = "open"
    #: fraction of this trade's marks that came from a real option bar rather
    #: than the model, across entry and exit
    real_mark_fraction: float | None = None
    legs: list[dict[str, Any]] = field(default_factory=list)
    # -- justification -------------------------------------------------- #
    thesis: str = ""
    gates: dict[str, Any] = field(default_factory=dict)
    #: the critic's verdict: score, verdict, reasoning, concerns, source
    critic: dict[str, Any] = field(default_factory=dict)
    risk_checks: dict[str, bool] = field(default_factory=dict)
    market_state: dict[str, Any] = field(default_factory=dict)
    headlines: list[str] = field(default_factory=list)
    news_sentiment: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RejectionRecord:
    ts: str
    symbol: str
    strategy: str
    stage: str            # "strategy_gate" | "risk"
    vetoed_by: str
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestResult:
    equity_curve: list[tuple[dt.datetime, float]] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    rejections: list[RejectionRecord] = field(default_factory=list)
    #: Structures re-priced onto a single surface because their legs
    #: disagreed about provenance. High = a thin real option tape.
    mixed_surface_marks: int = 0
    #: Marks that broke the structure's own arithmetic bound and were
    #: clamped. Should be zero; non-zero is a bug to chase.
    risk_bound_clamps: int = 0
    ideas_generated: int = 0
    ideas_approved: int = 0
    start_equity: float = 0.0
    end_equity: float = 0.0
    provenance: dict[str, Any] = field(default_factory=dict)

    # -- convenience ----------------------------------------------------- #
    @property
    def closed(self) -> list[TradeRecord]:
        return [t for t in self.trades if t.status == "closed"]

    @property
    def total_return(self) -> float:
        if self.start_equity <= 0:
            return 0.0
        return round((self.end_equity - self.start_equity) / self.start_equity, 5)

    def returns(self) -> list[float]:
        equity = [v for _, v in self.equity_curve]
        return [
            (equity[i] - equity[i - 1]) / equity[i - 1]
            for i in range(1, len(equity))
            if equity[i - 1] > 0
        ]

    def metrics(self) -> dict[str, Any]:
        """Every headline number the dashboard shows, computed in one place."""
        equity = [v for _, v in self.equity_curve]
        returns = self.returns()
        closed = self.closed
        pnls = [t.net_pnl for t in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        periods = _periods_per_year(self.equity_curve)
        volatility = _annualised_vol(returns, periods)
        downside = _annualised_vol([r for r in returns if r < 0], periods)

        gross = sum(t.gross_pnl for t in closed)
        fees = sum(t.fees for t in closed)
        interest = sum(t.margin_interest for t in closed)
        spread = sum(t.spread_cost for t in closed)

        return {
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(self.end_equity, 2),
            "net_pnl": round(self.end_equity - self.start_equity, 2),
            "total_return": self.total_return,
            "cagr": _cagr(self.start_equity, self.end_equity, self.equity_curve),
            "max_drawdown": max_drawdown(equity) if equity else 0.0,
            "sharpe": sharpe(returns, periods_per_year=periods) if returns else None,
            "sortino": (
                round((sum(returns) / len(returns)) * periods / downside, 3)
                if returns and downside else None
            ),
            "volatility_annual": volatility,
            "sessions": len(self.equity_curve),
            "trades": len(self.trades),
            "closed_trades": len(closed),
            "open_at_end": len(self.trades) - len(closed),
            "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "best_trade": round(max(pnls), 2) if pnls else 0.0,
            "worst_trade": round(min(pnls), 2) if pnls else 0.0,
            "profit_factor": (
                round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) else None
            ),
            "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
            "avg_hold_days": (
                round(sum(t.held_days for t in closed) / len(closed), 2) if closed else 0.0
            ),
            "gross_pnl": round(gross, 2),
            "fees_paid": round(fees, 2),
            "margin_interest": round(interest, 2),
            "spread_cost": round(spread, 2),
            "total_modelled_cost": round(fees + interest + spread, 2),
            "ideas_generated": self.ideas_generated,
            "ideas_approved": self.ideas_approved,
            "approval_rate": (
                round(self.ideas_approved / self.ideas_generated, 3)
                if self.ideas_generated else 0.0
            ),
            "rejections": len(self.rejections),
            "mixed_surface_marks": self.mixed_surface_marks,
            "risk_bound_clamps": self.risk_bound_clamps,
        }

    def rejection_funnel(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rejections:
            out[row.vetoed_by] = out.get(row.vetoed_by, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics(),
            "provenance": self.provenance,
            "equity_curve": [(ts.isoformat(), v) for ts, v in self.equity_curve],
            "trades": [t.as_dict() for t in self.trades],
            "rejections": [asdict(r) for r in self.rejections],
            "rejection_funnel": self.rejection_funnel(),
        }


# --------------------------------------------------------------------------- #
# open position bookkeeping
# --------------------------------------------------------------------------- #
@dataclass
class OpenStructure:
    record: TradeRecord
    idea: TradeIdea
    strategy: Strategy
    quantity: int
    entry_net: float
    opened_at: dt.datetime
    legs: list[dict[str, Any]]        # symbol, side, ratio, strike, expiry, is_call


class _RejectionSink:
    """Stands in for the firewall so a strategy's rejection log is captured.

    `Strategy._reject` writes to `ctx.firewall.journal`. In live trading that is
    the SQLite journal; here it is this list, which becomes the gate funnel on
    the dashboard - the artefact that shows the agent declining trades.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.journal = self
        #: the replay moment the engine is currently on. Strategies do not pass
        #: a timestamp with their rejections, so without this every row landed
        #: with an empty `ts` and the funnel could not be sliced by session or
        #: by weekday - which is exactly the cut you need when a book is quiet.
        self.moment: dt.datetime | None = None

    def event(self, kind: str, **fields: Any) -> None:
        if kind != "gate_rejection":
            return
        fields.setdefault("ts", self.moment.isoformat() if self.moment else "")
        self.rows.append(fields)


# --------------------------------------------------------------------------- #
class BacktestEngine:
    def __init__(
        self,
        settings: Settings,
        strategies: list[Strategy] | None = None,
        chain_model: ChainModel | None = None,
        critic: ReplayCritic | None = None,
        memory: Any = None,
        partners: Any = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.config
        self.broker = SimBroker(self.cfg, starting_cash=self.cfg.backtest.initial_cash)
        self.risk = RiskEngine(self.cfg)
        self.strategies = strategies or load_strategies(self.cfg)
        self.chain = chain_model or ChainModel()
        #: When set, open legs are marked from REAL option bars where a print
        #: exists, and only fall back to `self.chain` for contract-days that
        #: never traded. Set by the runner; see backtest/realchain.py.
        self.real_chain: Any = None
        self.costs = CostModel.from_config(self.cfg)
        #: The same critic class the live agent runs. Heuristic by default -
        #: see backtest/critic.py for why `llm` is not the default.
        self.critic = critic or ReplayCritic(self.cfg, mode=MODE_HEURISTIC)
        #: Outcomes of trades ALREADY CLOSED, fed to the critic exactly as the
        #: live agent feeds it. Built up as the replay runs, so it can only
        #: ever contain the past.
        self.memory = memory
        #: Partner adapters at the `risk` stage can veto, never approve.
        self.partners = partners
        #: The intraday book's catalyst gate. Deterministic - the same engine
        #: the live loop uses - reading the REAL Alpaca headlines the source
        #: attached to each context.
        self.catalyst: Any = None
        self.fraction = self.cfg.backtest.slippage_spread_fraction
        self.commission = self.cfg.backtest.commission_per_contract
        self._open: list[OpenStructure] = []
        self._seq = 0
        #: Times a structure had to be re-priced onto one surface because
        #: its legs disagreed about provenance. Reported per run: a high
        #: count means the real option tape is thin for this universe.
        self._mixed_surface_marks = 0
        #: Times the mark-to-market loss exceeded the structure's own
        #: arithmetic bound and had to be clamped. Should be ZERO.
        self._risk_bound_clamps = 0

    # ------------------------------------------------------------------ #
    def run(self, source: ContextSource, progress: Any = None) -> BacktestResult:
        result = BacktestResult(start_equity=self.cfg.backtest.initial_cash)
        result.provenance = self._provenance(source)
        sink = _RejectionSink()
        last_moment: dt.datetime | None = None
        last_contexts: dict[str, MarketContext] = {}

        try:
            for step, (moment, contexts) in enumerate(source):
                clock.freeze(moment)          # the replayed session IS "now"
                last_moment, last_contexts = moment, contexts
                self.broker.now = moment

                # 1. mark every open leg before anything reads the account
                self._mark(contexts, moment)

                # 2. manage what is already on, then look for new risk
                self._manage(contexts, moment, result)
                sink.moment = moment
                self._scan(contexts, moment, result, sink, source)

                equity = self.broker.account().equity
                result.equity_curve.append((moment, round(equity, 2)))
                if progress is not None:
                    progress(step, moment, equity)

            # 3. the window ends: close everything at the last known marks so
            #    the final number is realisable and not an unrealised mid.
            if last_moment is not None:
                self._flatten(last_contexts, last_moment, result)
                if result.equity_curve:
                    result.equity_curve[-1] = (
                        last_moment, round(self.broker.account().equity, 2)
                    )
        finally:
            clock.unfreeze()

        for row in sink.rows:
            result.rejections.append(
                RejectionRecord(
                    ts=str(row.get("ts", "")),
                    symbol=str(row.get("symbol", "")),
                    strategy=str(row.get("strategy", "")),
                    stage="strategy_gate",
                    vetoed_by=str(row.get("vetoed_by") or "unknown"),
                    reason=str(row.get("reason", "")),
                    metrics=dict(row.get("metrics") or {}),
                )
            )

        result.end_equity = (
            result.equity_curve[-1][1] if result.equity_curve else result.start_equity
        )
        result.provenance["critic"] = self.critic.describe()
        # Re-read the source AFTER the replay. `_provenance` snapshots it at the
        # start, when every counter is still zero - which reported "0% of marks
        # came from real bars" on runs whose every leg was in fact marked from a
        # real bar. Coverage is accumulated during the run, so it can only be
        # read at the end.
        if hasattr(source, "describe"):
            result.provenance["source"] = source.describe()
        result.mixed_surface_marks = self._mixed_surface_marks
        result.risk_bound_clamps = self._risk_bound_clamps
        return result

    # ------------------------------------------------------------------ #
    # marking
    # ------------------------------------------------------------------ #
    def _leg_marks(
        self, contexts: dict[str, MarketContext], moment: dt.datetime,
        legs: list[dict[str, Any]], symbol: str,
    ) -> dict[str, dict[str, float]]:
        """Reprice every leg of a structure from the model, at this session."""
        market = contexts.get(symbol)
        marks: dict[str, dict[str, float]] = {}
        if market is None:
            return marks
        atm_iv = market.implied_vol or 0.20
        pricer = self.real_chain or self.chain
        for leg in legs:
            marks[leg["symbol"]] = pricer.reprice(
                leg["symbol"], market.spot, moment, atm_iv,
                leg["strike"], leg["expiry"], leg["is_call"], symbol,
            )

        # Every leg of a structure must be priced on ONE surface.
        #
        # `reprice` decides per CONTRACT: a leg with a real bar is marked at its
        # traded close, a leg without one is marked from the model. In a condor
        # the short strikes trade and the far wings often do not, so on a
        # stressed session the short is marked at a real, elevated print while
        # its own wing is marked on a calm modelled vol. The value of a vertical
        # is then no longer bounded by its strike width, and the arithmetic that
        # makes the structure defined-risk stops holding: measured, a 5-wide put
        # spread marked 4.69 across a mixed surface against 2.96 on a single
        # one, and losses of 170% of `max_loss` appeared in the trade log.
        #
        # Where the mark is mixed, re-price EVERY leg from the model, anchored
        # on the vol the real prints actually imply - so the real information is
        # kept, but one surface prices the whole structure.
        provenance = [marks[leg["symbol"]].get("real", 0.0) for leg in legs]
        if len(legs) > 1 and 0.0 < sum(provenance) < len(provenance):
            observed = [
                float(marks[leg["symbol"]].get("iv") or 0.0)
                for leg in legs
                if marks[leg["symbol"]].get("real", 0.0) >= 1.0
                and marks[leg["symbol"]].get("iv")
            ]
            anchor = sum(observed) / len(observed) if observed else atm_iv
            self._mixed_surface_marks += 1
            for leg in legs:
                marks[leg["symbol"]] = pricer.reprice(
                    leg["symbol"], market.spot, moment, anchor,
                    leg["strike"], leg["expiry"], leg["is_call"], symbol,
                    force_model=True,
                )
        return marks

    def _bounded_gross(
        self, gross: float, position: OpenStructure, spread: float = 0.0
    ) -> float:
        """Hold the structure to the risk it was approved on.

        `RiskEngine` approved this trade on a `max_loss` computed from the
        strike widths. A defined-risk structure cannot lose more than that
        before costs - if the marks say otherwise, the marks are wrong, not the
        arithmetic. Clamping here means a pricing artefact cannot manufacture a
        loss the position could never actually take, and the counter says how
        often it happened. A non-zero count is a bug to chase, not a setting.

        Costs are charged OUTSIDE this bound on purpose: `max_loss` is a
        pre-cost concept, and a trade realising slightly worse than its defined
        risk after crossing the spread twice is honest, not an artefact.
        """
        # `max_loss` is a MID-price concept: it is (width - credit) computed from
        # quotes at mid. The fills cross the spread on the way in and out, and
        # that cost sits inside `gross`, so a trade may legitimately realise a
        # little worse than its defined risk. Widening the bound by the modelled
        # spread keeps the clamp for genuine pricing faults and stops it eating
        # real, honest execution cost.
        allowance = abs(spread) * position.quantity
        max_loss = (position.idea.max_loss or 0.0) * position.quantity + allowance
        max_profit = (position.idea.max_profit or 0.0) * position.quantity
        if max_loss > 0 and gross < -max_loss:
            self._risk_bound_clamps += 1
            log.warning(
                "%s: marked loss %.2f exceeds defined risk %.2f - clamped. The "
                "legs disagreed about provenance or a print was stale.",
                position.record.trade_id, gross, max_loss,
            )
            return -max_loss
        if max_profit > 0 and gross > max_profit:
            self._risk_bound_clamps += 1
            return max_profit
        return gross

    def _mark(self, contexts: dict[str, MarketContext], moment: dt.datetime) -> None:
        for position in self._open:
            marks = self._leg_marks(contexts, moment, position.legs, position.record.symbol)
            for leg in position.legs:
                mark = marks.get(leg["symbol"])
                if mark is not None:
                    self.broker.mark(leg["symbol"], mark["mid"])

    # ------------------------------------------------------------------ #
    # execution prices
    # ------------------------------------------------------------------ #
    def _execution_price(self, mark: dict[str, float], buying: bool) -> float:
        """Cross `slippage_spread_fraction` of the half-spread, always adversely.

        fraction 0.0 fills at mid (what paper trading does, and what flatters
        every options backtest ever written); 1.0 pays the full quoted side.
        The default is 0.5.
        """
        mid = mark["mid"]
        half = max(0.0, (mark["ask"] - mark["bid"]) / 2)
        step = half * self.fraction
        return round(max(0.0, mid + step if buying else mid - step), 4)

    def _net_price(
        self, legs: list[dict[str, Any]], marks: dict[str, dict[str, float]], closing: bool
    ) -> tuple[float, float]:
        """(net price per structure, modelled spread paid per structure).

        Sign convention matches `TradeIdea.net_price`: opening, positive is a
        debit paid and negative is a credit received. Closing, the number is
        the net PROCEEDS of unwinding one structure on the same convention, so
        `proceeds - entry` is the gross P&L per structure directly.

        Each leg is priced on the side it actually trades: a leg being bought
        pays up from mid, a leg being sold gets hit down from mid. Crossing is
        therefore paid twice over a round trip, which is what really happens.
        """
        net = 0.0
        spread = 0.0
        for leg in legs:
            mark = marks.get(leg["symbol"])
            if mark is None:
                continue
            originally_bought = leg["side"] == "buy"
            buying_now = (not originally_bought) if closing else originally_bought
            price = self._execution_price(mark, buying_now)
            net += (1 if originally_bought else -1) * leg["ratio"] * price
            half = max(0.0, (mark["ask"] - mark["bid"]) / 2)
            spread += half * self.fraction * leg["ratio"] * MULTIPLIER
        return round(net, 4), round(spread, 2)

    # ------------------------------------------------------------------ #
    # scanning
    # ------------------------------------------------------------------ #
    def _scan(
        self,
        contexts: dict[str, MarketContext],
        moment: dt.datetime,
        result: BacktestResult,
        sink: _RejectionSink,
        source: Any = None,
    ) -> None:
        account = self.broker.account()
        self.risk.observe(account, moment)

        for symbol, market in contexts.items():
            for strategy in self.strategies:
                if symbol not in strategy.universe():
                    continue
                ctx = StrategyContext(
                    market=market,
                    contexts=contexts,
                    account=account,
                    config=self.cfg,
                    params=strategy.params,
                    firewall=sink,
                    catalyst=self.catalyst,
                    attention=getattr(source, "attention", None),
                )
                try:
                    ideas = strategy.generate(ctx)
                except Exception as exc:  # noqa: BLE001
                    # A strategy that THROWS used to be indistinguishable from a
                    # strategy that declines: DEBUG-level, no rejection record,
                    # nothing in the funnel. A book could fail on every single
                    # candidate of a run and the report would show it as simply
                    # quiet - which is exactly how a month-long replay produced
                    # zero intraday trades AND zero intraday rejections while
                    # the same code traded 40 times over five days.
                    #
                    # An error is not a decision. It gets a record of its own so
                    # it shows up in the funnel and cannot be read as restraint.
                    log.warning(
                        "%s/%s raised during generate(): %s: %s",
                        symbol, strategy.name, type(exc).__name__, exc,
                        exc_info=log.isEnabledFor(logging.DEBUG),
                    )
                    result.rejections.append(
                        RejectionRecord(
                            ts=moment.isoformat(), symbol=symbol,
                            strategy=strategy.name, stage="error",
                            vetoed_by="strategy_error",
                            reason=f"{type(exc).__name__}: {exc}",
                            metrics={},
                        )
                    )
                    continue

                for idea in ideas:
                    result.ideas_generated += 1

                    # The live order, from agents/orchestrator.py. Changing it
                    # changes what the backtest is a backtest OF.
                    # 0. modelled cost, attached first so a rejection carries it
                    idea.meta["modelled_cost"] = self.costs.round_trip(idea).as_dict()

                    # 1. the critic scores and may pass
                    critique = self.critic.score(
                        idea, market, account,
                        opened_today=self.risk.state.opened_today,
                        memory=self.memory.as_prompt() if self.memory else "",
                    )
                    idea.score = float(critique.get("score", idea.confidence))
                    if not self.critic.accepts(critique):
                        result.rejections.append(
                            RejectionRecord(
                                ts=moment.isoformat(), symbol=symbol,
                                strategy=strategy.name, stage="critic",
                                vetoed_by="critic",
                                reason=(
                                    f"critic passed (score {idea.score:.2f} < "
                                    f"{self.cfg.agents.critic.min_score_to_trade:.2f}): "
                                    + str(critique.get("reasoning", ""))[:400]
                                ),
                                metrics={
                                    "score": idea.score,
                                    "source": critique.get("source"),
                                    "concerns": critique.get("concerns") or [],
                                },
                            )
                        )
                        continue

                    # 2. the deterministic risk engine - the only thing that
                    #    can approve. The critic cannot.
                    verdict = self.risk.evaluate(
                        idea, account, now=moment, market_open=True
                    )
                    if not verdict.approved:
                        rule = next(
                            (r.split("=", 1)[1] for r in verdict.reasons if r.startswith("rule=")),
                            "risk",
                        )
                        result.rejections.append(
                            RejectionRecord(
                                ts=moment.isoformat(), symbol=symbol,
                                strategy=strategy.name, stage="risk", vetoed_by=rule,
                                reason=verdict.reasons[0] if verdict.reasons else "",
                                metrics={"checks": verdict.checks},
                            )
                        )
                        continue

                    # 3. partner adapters at the risk stage may veto
                    if self.partners is not None:
                        allowed, why = self.partners.veto(idea)
                        if not allowed:
                            result.rejections.append(
                                RejectionRecord(
                                    ts=moment.isoformat(), symbol=symbol,
                                    strategy=strategy.name, stage="partner",
                                    vetoed_by="partner",
                                    reason=why or "partner veto",
                                )
                            )
                            continue

                    result.ideas_approved += 1
                    self._open_structure(
                        idea, strategy, market,
                        verdict.adjusted_quantity or idea.quantity,
                        moment, verdict.checks, critique, result,
                    )
                    self.risk.record_open(idea, now=moment)
                    account = self.broker.account()

    # ------------------------------------------------------------------ #
    def _open_structure(
        self,
        idea: TradeIdea,
        strategy: Strategy,
        market: MarketContext,
        quantity: int,
        moment: dt.datetime,
        checks: dict[str, bool],
        critique: dict[str, Any],
        result: BacktestResult,
    ) -> None:
        legs = [_leg_meta(leg) for leg in idea.legs]
        marks = {
            meta["symbol"]: _mark_from_quote(leg)
            for meta, leg in zip(legs, idea.legs, strict=True)
        }
        net, spread = self._net_price(legs, marks, closing=False)

        # Time-based exits read this. Stamped here and in the live
        # orchestrator so a hold rule means the same thing in both paths.
        idea.meta["opened_at"] = moment.isoformat()

        self._seq += 1
        trade_id = f"BT{self._seq:04d}"
        fees_open = self.commission * len(idea.legs) * quantity

        self.broker.cash -= net * MULTIPLIER * quantity + fees_open
        for leg, meta in zip(idea.legs, legs, strict=True):
            signed = quantity * leg.ratio * (1 if leg.side is Side.BUY else -1)
            self.broker._apply(leg.symbol, signed, marks[meta["symbol"]]["mid"])

        record = TradeRecord(
            trade_id=trade_id,
            symbol=idea.symbol,
            strategy=idea.strategy,
            book=idea.book,
            structure=idea.structure.value,
            quantity=quantity,
            opened_at=moment.isoformat(),
            entry_price=net,
            spread_cost=round(spread * quantity, 2),
            fees=round(fees_open, 2),
            max_loss=idea.max_loss,
            max_profit=idea.max_profit,
            probability_of_profit=idea.probability_of_profit,
            confidence=idea.confidence,
            thesis=idea.thesis,
            gates=dict(idea.meta.get("gates") or {}),
            critic=dict(critique or {}),
            risk_checks=dict(checks),
            legs=[
                {**meta, "entry_mark": marks[meta["symbol"]]["mid"],
                 "entry_iv": marks[meta["symbol"]].get("iv")}
                for meta in legs
            ],
            market_state=_market_state(market),
            headlines=list(market.enrichment.get("headlines") or []),
            news_sentiment=float(market.enrichment.get("news_sentiment") or 0.0),
        )
        result.trades.append(record)
        self._open.append(
            OpenStructure(
                record=record, idea=idea, strategy=strategy, quantity=quantity,
                entry_net=net, opened_at=moment, legs=legs,
            )
        )
        log.info(
            "OPEN  %s %s x%d net %.2f  (%s)",
            trade_id, idea.describe(), quantity, net, idea.strategy,
        )

    # ------------------------------------------------------------------ #
    # management
    # ------------------------------------------------------------------ #
    def _manage(
        self, contexts: dict[str, MarketContext], moment: dt.datetime, result: BacktestResult
    ) -> None:
        account = self.broker.account()
        for position in list(self._open):
            marks = self._leg_marks(contexts, moment, position.legs, position.record.symbol)
            if not marks:
                continue
            # `<` not `<=`. A 0 DTE option is NOT expired at 11:15 on its
            # expiry day - it expires at the close, and until then it is worth
            # intrinsic PLUS the remaining time value. Settling it at intrinsic
            # on the next scan discarded every cent of that premium, which for
            # an at-the-money long is the entire position: it turned the whole
            # intraday book into a -100%-or-jackpot lottery (measured 11-22 Aug:
            # twelve trades settled this way, -$4,602 of a -$4,272 result, one
            # of them +254%). The defect only became reachable once the chain
            # started listing the front expiry this book is built to buy.
            expired = any(leg["expiry"] < moment.date() for leg in position.legs)
            if expired:
                self._close(position, contexts, moment, "expiry - settled at intrinsic", result)
                continue

            proceeds, _ = self._net_price(position.legs, marks, closing=True)
            gross = self._bounded_gross(
                (proceeds - position.entry_net) * MULTIPLIER * position.quantity,
                position,
            )
            denominator = (
                (position.idea.max_profit or 0.0) * position.quantity
                or (position.idea.max_loss or 1.0) * position.quantity
            )
            pnl_pct = gross / denominator if denominator else 0.0

            ctx = StrategyContext(
                market=contexts.get(position.record.symbol),
                contexts=contexts,
                account=account,
                config=self.cfg,
                params=position.strategy.params,
            )
            try:
                reason = position.strategy.should_exit(ctx, position.idea, pnl_pct)
            except Exception as exc:  # noqa: BLE001
                log.debug("should_exit failed for %s: %s", position.record.trade_id, exc)
                reason = None
            if reason:
                self._close(position, contexts, moment, reason, result)

    def _flatten(
        self, contexts: dict[str, MarketContext], moment: dt.datetime, result: BacktestResult
    ) -> None:
        for position in list(self._open):
            self._close(
                position, contexts, moment,
                "backtest window ended - closed at the last modelled mark", result,
            )

    def _close(
        self,
        position: OpenStructure,
        contexts: dict[str, MarketContext],
        moment: dt.datetime,
        reason: str,
        result: BacktestResult,
    ) -> None:
        marks = self._leg_marks(contexts, moment, position.legs, position.record.symbol)
        for leg in position.legs:
            marks.setdefault(leg["symbol"], _intrinsic_mark(leg, contexts, position.record.symbol))

        proceeds, spread = self._net_price(position.legs, marks, closing=True)
        quantity = position.quantity
        raw_gross = (proceeds - position.entry_net) * MULTIPLIER * quantity
        gross = self._bounded_gross(raw_gross, position, spread)
        if gross != raw_gross:
            # Clamp the FILL, not just the number printed. Adjusting `gross`
            # alone left the trade log claiming a better result than the account
            # actually took, so the equity curve and the sum of trade P&L
            # disagreed - the one thing a backtest must never do.
            proceeds = position.entry_net + gross / (MULTIPLIER * quantity)

        held_days = max(0.0, (moment - position.opened_at).total_seconds() / 86400)
        margin_balance = (position.idea.max_loss or 0.0) * quantity
        breakdown = self.costs.round_trip(
            position.idea, held_days=held_days, margin_balance=margin_balance,
        )
        # The spread is charged inside the fills on both sides, so only the fee
        # and interest lines move cash here. Double-charging it would be a
        # different kind of dishonesty from ignoring it, but still dishonesty.
        cash_fees = round(breakdown.regulatory + breakdown.exchange, 4)
        cash_fees += self.commission * len(position.legs) * quantity
        interest = breakdown.margin_interest

        self.broker.cash += proceeds * MULTIPLIER * quantity - cash_fees - interest
        for leg in position.legs:
            signed = -quantity * leg["ratio"] * (1 if leg["side"] == "buy" else -1)
            self.broker._apply(leg["symbol"], signed, marks[leg["symbol"]]["mid"])

        record = position.record
        record.closed_at = moment.isoformat()
        record.held_days = round(held_days, 2)
        record.exit_price = proceeds
        record.gross_pnl = round(gross, 2)
        record.fees = round(record.fees + cash_fees, 2)
        record.margin_interest = round(interest, 2)
        record.spread_cost = round(record.spread_cost + spread * quantity, 2)
        record.net_pnl = round(gross - cash_fees - interest, 2)
        risk_taken = (record.max_loss or 0.0) * quantity
        record.return_on_risk = round(record.net_pnl / risk_taken, 4) if risk_taken else 0.0
        record.exit_reason = reason
        record.status = "closed"
        real = [marks[leg["symbol"]].get("real") for leg in position.legs
                if leg["symbol"] in marks and "real" in marks[leg["symbol"]]]
        if real:
            record.real_mark_fraction = round(sum(real) / len(real), 3)
        for leg in record.legs:
            mark = marks.get(leg["symbol"])
            if mark:
                leg["exit_mark"] = mark["mid"]
                if "real" in mark:
                    leg["exit_mark_source"] = "bar" if mark["real"] else "modelled"

        self._open.remove(position)
        if self.memory is not None:
            # The critic sees this from the NEXT session onward, never before -
            # the clock is frozen, so the row is stamped in replayed time.
            try:
                self.memory.record(
                    symbol=record.symbol,
                    strategy=record.strategy,
                    structure=record.structure,
                    pnl=record.net_pnl,
                    pnl_pct=record.return_on_risk,
                    held_days=record.held_days,
                    thesis=record.thesis,
                    notes={"exit_reason": reason, "trade_id": record.trade_id},
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("could not record %s to replay memory: %s", record.trade_id, exc)
        log.info(
            "CLOSE %s %s  gross %.2f  fees %.2f  net %.2f  (%s)",
            record.trade_id, record.symbol, gross, cash_fees + interest,
            record.net_pnl, reason,
        )

    # ------------------------------------------------------------------ #
    def _provenance(self, source: ContextSource) -> dict[str, Any]:
        described = source.describe() if hasattr(source, "describe") else {}
        return {
            "profile": self.cfg.profile,
            "initial_cash": self.cfg.backtest.initial_cash,
            "slippage_spread_fraction": self.fraction,
            "commission_per_contract": self.commission,
            "cost_model_enabled": self.costs.enabled,
            "strategies": [s.name for s in self.strategies],
            "decision_pipeline": [
                "modelled_cost", "critic", "risk_engine",
                "partner_veto" if self.partners is not None else "partner_veto (no adapters)",
                "execute",
            ],
            "memory": self.memory is not None,
            "risk": {
                # Which PROFILE's limits these are. dev and judged carry
                # different caps, so a backtest run on the wrong one is
                # calibrating against a configuration that will never trade.
                "profile": self.cfg.profile,
                "max_positions": self.cfg.risk.max_positions,
                "max_risk_per_trade_pct": self.cfg.risk.max_risk_per_trade_pct,
                "max_portfolio_risk_pct": self.cfg.risk.max_portfolio_risk_pct,
                "max_new_positions_per_day": self.cfg.risk.max_new_positions_per_day,
                "daily_loss_limit_pct": self.cfg.risk.daily_loss_limit_pct,
                "allow_undefined_risk": self.cfg.risk.allow_undefined_risk,
            },
            "source": described,
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _leg_meta(leg: Leg) -> dict[str, Any]:
    occ = parse_occ(leg.symbol)
    return {
        "symbol": leg.symbol,
        "side": leg.side.value,
        "ratio": leg.ratio,
        "strike": occ.strike,
        "expiry": occ.expiry,
        "is_call": occ.right.value == "call",
    }


def _mark_from_quote(leg: Leg) -> dict[str, float]:
    quote = leg.quote
    if quote is None or quote.mid is None:
        price = leg.limit_price or 0.0
        return {"mid": price, "bid": price, "ask": price, "iv": 0.0}
    return {
        "mid": float(quote.mid),
        "bid": float(quote.bid if quote.bid is not None else quote.mid),
        "ask": float(quote.ask if quote.ask is not None else quote.mid),
        "iv": float(quote.implied_volatility or 0.0),
    }


def _intrinsic_mark(
    leg: dict[str, Any], contexts: dict[str, MarketContext], symbol: str
) -> dict[str, float]:
    market = contexts.get(symbol)
    spot = market.spot if market else leg["strike"]
    value = (
        max(0.0, spot - leg["strike"]) if leg["is_call"] else max(0.0, leg["strike"] - spot)
    )
    return {"mid": round(value, 4), "bid": round(value, 4), "ask": round(value, 4), "iv": 0.0}


def _market_state(market: MarketContext) -> dict[str, Any]:
    return {
        "spot": market.spot,
        "prev_close": market.prev_close,
        "implied_vol": market.implied_vol,
        "realised_vol": market.realised_vol,
        "iv_rank": market.iv_rank,
        "iv_rv_spread": (
            round(market.implied_vol - market.realised_vol, 4)
            if market.implied_vol is not None and market.realised_vol is not None else None
        ),
        "adx": market.adx,
        "trend_strength": market.trend_strength,
        "volume_ratio": market.volume_ratio,
        "news_count": market.enrichment.get("news_count", 0),
        "chain_contracts": len(market.chain),
    }


def _periods_per_year(curve: list[tuple[dt.datetime, float]]) -> int:
    if len(curve) < 3:
        return _TRADING_DAYS
    days = len({ts.date() for ts, _ in curve})
    per_day = max(1, len(curve) // max(1, days))
    return _TRADING_DAYS * per_day


def _annualised_vol(returns: list[float], periods: int) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round((variance ** 0.5) * (periods ** 0.5), 5)


def _cagr(
    start: float, end: float, curve: list[tuple[dt.datetime, float]]
) -> float | None:
    if start <= 0 or end <= 0 or len(curve) < 2:
        return None
    span = (curve[-1][0] - curve[0][0]).days
    if span < 5:
        return None
    return round((end / start) ** (365.0 / span) - 1, 5)
