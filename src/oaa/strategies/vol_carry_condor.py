"""Short volatility carry via defined-risk iron condors.

Thesis
------
Index options persistently price implied variance above the variance that
subsequently gets realised. That gap is the variance risk premium, and it is
one of the few genuinely persistent edges available to a retail options account.

Selling it naked is how people blow up. We express it as an iron condor: both
tails are bought back, so the maximum loss is (wing width - credit) and is known
before the order is sent.

Entry conditions
----------------
  * IV rank above a floor - only sell premium when premium is actually rich
  * IV / realised vol above a floor - the carry has to be there today, not
    just historically
  * trend strength below a ceiling - a condor is a bet on range, and a strong
    trend is exactly the regime that runs through a short strike
"""

from __future__ import annotations

from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import TradeIdea
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry

log = get_logger("strategies.condor")


@strategy_registry.register("vol_carry_condor")
class VolCarryCondor(Strategy):
    description = "Sells OTM iron condors when implied vol is rich vs realised."

    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        market = ctx.market

        # --- regime gate ---------------------------------------------------- #
        iv_rank = market.iv_rank
        min_iv_rank = self.p("entry.min_iv_rank", 0.35)
        if iv_rank is not None and iv_rank < min_iv_rank:
            log.debug("%s: IV rank %.2f below %.2f", market.symbol, iv_rank, min_iv_rank)
            return []

        iv_rv = market.iv_rv_ratio
        min_ratio = self.p("entry.min_iv_rv_ratio", 1.10)
        if iv_rv is not None and iv_rv < min_ratio:
            log.debug("%s: IV/RV %.2f below %.2f - no carry today",
                      market.symbol, iv_rv, min_ratio)
            return []

        trend = abs(market.trend_strength or 0.0)
        max_trend = self.p("entry.max_trend_strength", 0.6)
        if trend > max_trend:
            log.debug("%s: trend %.2f too strong for a condor", market.symbol, trend)
            return []

        # --- build ------------------------------------------------------------ #
        dte = tuple(self.p("structure.target_dte", [14, 35]))
        try:
            idea = self.builder(ctx).iron_condor_by_delta(
                dte_range=(dte[0], dte[1]),
                short_put_delta=self.p("structure.short_put_delta", -0.16),
                short_call_delta=self.p("structure.short_call_delta", 0.16),
                wing_points=self.p("structure.wing_width_points", 5),
                quantity=self.p("structure.fixed_quantity", 1),
                thesis=self._thesis(market, iv_rank, iv_rv, trend),
            )
        except (StrategyError, DataError) as exc:
            log.debug("%s: could not build condor - %s", market.symbol, exc)
            return []

        # --- quality gate ------------------------------------------------------ #
        credit_to_width = idea.meta.get("credit_to_width")
        floor = self.p("structure.min_credit_to_width", 0.18)
        if credit_to_width is not None and credit_to_width < floor:
            log.debug("%s: credit/width %.3f below %.3f - not worth the tail risk",
                      market.symbol, credit_to_width, floor)
            return []

        idea.confidence = self._confidence(iv_rank, iv_rv, trend)
        idea.tags = ["short_vol", "defined_risk", "neutral"]
        idea.probability_of_profit = self._pop(idea)
        return [idea]

    # -- annotation ---------------------------------------------------------- #
    @staticmethod
    def _thesis(market, iv_rank, iv_rv, trend) -> str:  # type: ignore[no-untyped-def]
        parts = [f"{market.symbol} at {market.spot:.2f}"]
        if iv_rank is not None:
            parts.append(f"IV rank {iv_rank:.0%}")
        if iv_rv is not None:
            parts.append(f"IV/RV {iv_rv:.2f}")
        parts.append(f"trend {trend:.2f} (range-bound)")
        return (
            "Selling the variance risk premium: "
            + ", ".join(parts)
            + ". Both tails bought back, so loss is capped at the wing width "
            "less the credit received."
        )

    @staticmethod
    def _confidence(iv_rank, iv_rv, trend) -> float:  # type: ignore[no-untyped-def]
        score = 0.5
        if iv_rank is not None:
            score += (iv_rank - 0.35) * 0.6
        if iv_rv is not None:
            score += min(0.2, (iv_rv - 1.0) * 0.4)
        score -= trend * 0.25
        return round(max(0.0, min(1.0, score)), 3)

    @staticmethod
    def _pop(idea: TradeIdea) -> float | None:
        """Rough probability of profit from the short deltas.

        A 16-delta short strike is breached about 16% of the time, so a condor
        with two of them expires inside its wings roughly 1 - 0.16 - 0.16 of
        the time. Crude, but honest, and it beats claiming precision we do not
        have on the free data feed.
        """
        deltas = [
            abs(leg.quote.greeks.delta)
            for leg in idea.legs
            if leg.quote and leg.quote.greeks.delta is not None and leg.side.value == "sell"
        ]
        if len(deltas) < 2:
            return None
        return round(max(0.0, 1.0 - sum(deltas)), 3)
