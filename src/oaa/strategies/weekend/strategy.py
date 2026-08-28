"""The weekend strategy, in the repo's Strategy shape.

It is registered so that `oaa strategies` lists it and the gate log speaks the
same language for all four books - but it is NOT enabled in config/default.yaml
and the options runner will never load it, because no entry in `strategies:`
names it. Its real entry point is `oaa weekend run`, which drives
`weekend.engine` directly.

Two interlocks make that safety property structural rather than a convention:

  * `generate` refuses any symbol that is not a slash-quoted crypto pair, so it
    cannot fire on SPY even if someone wires it into the equity universe.
  * `generate` refuses outside the weekend window, so it cannot hold a position
    into a session where the capital firewall would have to reason about it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from oaa.core.logging import get_logger
from oaa.core.types import AssetKind, Intent, Leg, Side, StructureType, TradeIdea
from oaa.signals.gates import GateResult
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry
from oaa.strategies.weekend.params import DEFAULT_PARAMS_PATH, WeekendParams, load_params
from oaa.strategies.weekend.signals import (
    WeekendSignal,
    evaluate,
    stop_price,
    target_price,
)

log = get_logger("strategies.weekend")
UTC = dt.timezone.utc


def build_idea(
    signal: WeekendSignal,
    params: WeekendParams,
    equity: float,
    entry_price: float | None = None,
) -> TradeIdea | None:
    """Turn an actionable signal into a defined-risk, fully sized TradeIdea.

    `max_loss` is (entry - stop) x qty. That is what lets a spot crypto
    position pass a risk engine configured with `allow_undefined_risk: false`:
    the loss is bounded by an order we commit to place, not by a strike.
    """
    from oaa.strategies.weekend.backtest import size_position

    if not signal.actionable:
        return None
    entry = float(entry_price or signal.price)
    stop = stop_price(entry, signal.atr, params)
    target = target_price(entry, signal.z or 0.0, signal.sigma or 0.0, params)
    qty = size_position(entry, stop, equity, params.sizing)
    if qty <= 0:
        return None

    leg = Leg(
        symbol=signal.symbol,
        side=Side.BUY,
        kind=AssetKind.CRYPTO,
        qty=qty,
        intent=Intent.BUY_TO_OPEN,
        limit_price=entry,
    )
    max_loss = round((entry - stop) * qty, 2)
    max_profit = round((target - entry) * qty, 2)
    cost = params.costs.round_trip_cost(entry * qty)

    return TradeIdea(
        symbol=signal.symbol,
        strategy="weekend_crypto_reversion",
        structure=StructureType.SINGLE_LONG,
        legs=[leg],
        quantity=1,
        book=params.book,
        net_price=round(entry * qty, 2),
        max_loss=max_loss,
        max_profit=max_profit,
        thesis=(
            f"{signal.symbol} is {abs(signal.z or 0):.1f} sigma below its 24h mean "
            f"with ADX {signal.adx:.0f} (ranging). Long to the mean: target "
            f"{target:,.0f}, stop {stop:,.0f}, {signal.expected_move_bp:.0f}bp gross "
            f"against a {params.costs.round_trip_bp:.0f}bp round trip."
        ),
        confidence=min(0.9, 0.4 + 0.1 * abs(signal.z or 0)),
        tags=["weekend", "crypto", "mean-reversion", "long-only"],
        meta={
            "stop": stop,
            "target": target,
            "z": signal.z,
            "sigma": signal.sigma,
            "adx": signal.adx,
            "atr": signal.atr,
            "expected_move_bp": signal.expected_move_bp,
            "modelled_round_trip_cost": round(cost, 2),
            "gates": [
                {"gate": c.gate, "passed": c.passed, "reason": c.reason}
                for c in signal.checks
            ],
        },
    )


@strategy_registry.register("weekend_crypto_reversion")
class WeekendCryptoReversion(Strategy):
    description = (
        "BTC/USD z-score mean reversion, ADX-gated, live only between the Friday "
        "equity close and the Sunday flatten."
    )
    book = "weekend"

    def __init__(self, ref: Any, config: Any) -> None:
        super().__init__(ref, config)
        self.params = load_params(self.p("params_path", DEFAULT_PARAMS_PATH))

    # ------------------------------------------------------------------ #
    def universe(self) -> list[str]:
        return list(self.params.symbols)

    def chain_dte_window(self) -> tuple[int, int] | None:
        return None  # no chain: this book trades spot

    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        market = ctx.market
        if market is None:
            return []

        # Interlock 1: crypto pairs only.
        if "/" not in market.symbol:
            log.debug("weekend book refused non-crypto symbol %s", market.symbol)
            return []

        # Interlock 2: the window, checked against the bar clock, not wall time,
        # so a replay is evaluated in the window it actually occurred in.
        now = market.asof if market.asof.tzinfo else market.asof.replace(tzinfo=UTC)
        if not self.params.window.may_enter(now):
            log.debug("weekend book closed at %s", now.isoformat())
            return []

        bars = market.intraday_bars or market.bars
        signal = evaluate(market.symbol, bars, self.params)
        if not signal.actionable:
            log.info("weekend skip %s", signal.summary())
            return []

        idea = build_idea(signal, self.params, equity=ctx.account.equity or 0.0)
        return [idea] if idea else []

    def should_exit(self, ctx: StrategyContext, idea: TradeIdea, pnl_pct: float) -> str | None:
        """Exits are price levels, not percentages: the stop and the target were
        computed from the band at entry and are carried on the idea."""
        market = ctx.market
        if market is None:
            return None
        stop, target = idea.meta.get("stop"), idea.meta.get("target")
        if stop and market.spot <= float(stop):
            return f"stop {float(stop):,.0f} hit"
        if target and market.spot >= float(target):
            return f"target {float(target):,.0f} reached - reversion complete"
        now = market.asof if market.asof.tzinfo else market.asof.replace(tzinfo=UTC)
        if not self.params.window.may_enter(now):
            hours = self.params.window.hours_to_flatten(now)
            if hours <= 0:
                return "weekend window closed - hard flatten"
        return None

    def gate_report(self, symbol: str, bars: list[dict[str, Any]]) -> list[GateResult]:
        return evaluate(symbol, bars, self.params).checks
