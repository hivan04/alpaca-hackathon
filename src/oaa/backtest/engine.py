"""Replay engine.

An honest note on scope: Alpaca's free tier does not give a full historical
option chain with greeks, so this is a *replay* harness rather than a
tick-accurate options backtester. It drives the real strategy, risk and
execution code over a sequence of MarketContexts from whatever source you
plug in, with a conservative fill model.

That makes it useful for exactly what a one-week event needs it for:
  * proving a strategy fires when you expect it to, and stays quiet otherwise
  * catching sizing and risk-limit bugs before they cost live paper P&L
  * producing an equity curve for the deck

It is not evidence of edge, and the deck should not claim it is. The judged
number is live paper P&L.
"""

from __future__ import annotations

import abc
import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field

from oaa.brokers.sim import SimBroker
from oaa.config.loader import Settings
from oaa.core.logging import get_logger
from oaa.core.types import MarketContext, TradeIdea
from oaa.data.indicators import max_drawdown, sharpe
from oaa.execution.pricer import limit_price_for, structure_bid_ask
from oaa.risk.engine import RiskEngine
from oaa.strategies.base import Strategy, StrategyContext, load_strategies

log = get_logger("backtest")


class ContextSource(abc.ABC):
    """Yields (timestamp, {symbol: MarketContext}) in chronological order."""

    @abc.abstractmethod
    def __iter__(self) -> Iterator[tuple[dt.datetime, dict[str, MarketContext]]]: ...


@dataclass
class BacktestResult:
    equity_curve: list[tuple[dt.datetime, float]] = field(default_factory=list)
    trades: list[dict[str, object]] = field(default_factory=list)
    ideas_generated: int = 0
    ideas_approved: int = 0
    start_equity: float = 0.0
    end_equity: float = 0.0

    @property
    def total_return(self) -> float:
        if self.start_equity <= 0:
            return 0.0
        return round((self.end_equity - self.start_equity) / self.start_equity, 5)

    def metrics(self) -> dict[str, object]:
        equity = [value for _, value in self.equity_curve]
        returns = [
            (equity[i] - equity[i - 1]) / equity[i - 1]
            for i in range(1, len(equity))
            if equity[i - 1] > 0
        ]
        return {
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(self.end_equity, 2),
            "total_return": self.total_return,
            "max_drawdown": max_drawdown(equity) if equity else 0.0,
            "sharpe": sharpe(returns) if returns else None,
            "bars": len(self.equity_curve),
            "trades": len(self.trades),
            "ideas_generated": self.ideas_generated,
            "ideas_approved": self.ideas_approved,
            "approval_rate": (
                round(self.ideas_approved / self.ideas_generated, 3)
                if self.ideas_generated else 0.0
            ),
        }


class BacktestEngine:
    def __init__(self, settings: Settings, strategies: list[Strategy] | None = None) -> None:
        self.settings = settings
        self.cfg = settings.config
        self.broker = SimBroker(self.cfg, starting_cash=self.cfg.backtest.initial_cash)
        self.risk = RiskEngine(self.cfg)
        self.strategies = strategies or load_strategies(self.cfg)

    def run(self, source: ContextSource) -> BacktestResult:
        result = BacktestResult(start_equity=self.cfg.backtest.initial_cash)

        for timestamp, contexts in source:
            self.broker.now = timestamp
            account = self.broker.account()
            self.risk.observe(account, timestamp)

            for symbol, market in contexts.items():
                for strategy in self.strategies:
                    if symbol not in strategy.universe():
                        continue
                    ctx = StrategyContext(
                        market=market, account=account,
                        config=self.cfg, params=strategy.params,
                    )
                    try:
                        ideas = strategy.generate(ctx)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("%s/%s: %s", symbol, strategy.name, exc)
                        continue

                    for idea in ideas:
                        result.ideas_generated += 1
                        verdict = self.risk.evaluate(
                            idea, account, now=timestamp, market_open=True
                        )
                        if not verdict.approved:
                            continue
                        result.ideas_approved += 1
                        self._fill(idea, verdict.adjusted_quantity or idea.quantity, timestamp, result)
                        self.risk.record_open()
                        account = self.broker.account()

            equity = self.broker.account().equity
            result.equity_curve.append((timestamp, equity))

        result.end_equity = result.equity_curve[-1][1] if result.equity_curve else result.start_equity
        return result

    def _fill(
        self,
        idea: TradeIdea,
        quantity: int,
        timestamp: dt.datetime,
        result: BacktestResult,
    ) -> None:
        """Conservative fill: cross a configurable fraction of the spread.

        Filling at mid flatters every options backtest ever written. The
        default crosses half the spread on entry, which is closer to what a
        marketable limit actually gets on a multi-leg order.
        """
        best, worst = structure_bid_ask(idea)
        fraction = self.cfg.backtest.slippage_spread_fraction
        if best is None or worst is None:
            price = limit_price_for(idea, fraction)
        else:
            mid = (best + worst) / 2
            price = round(mid + (worst - mid) * fraction, 4)

        commission = self.cfg.backtest.commission_per_contract * len(idea.legs) * quantity
        self.broker.cash -= price * 100 * quantity + commission
        for leg in idea.legs:
            signed = quantity * leg.ratio * (1 if leg.side.value == "buy" else -1)
            self.broker._apply(leg.symbol, signed, leg.quote.mid if leg.quote else abs(price))

        result.trades.append({
            "ts": timestamp.isoformat(),
            "symbol": idea.symbol,
            "strategy": idea.strategy,
            "structure": idea.structure.value,
            "quantity": quantity,
            "price": price,
            "max_loss": idea.max_loss,
            "max_profit": idea.max_profit,
            "thesis": idea.thesis,
        })
