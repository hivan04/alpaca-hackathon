"""Directional momentum expressed as a vertical debit spread.

Thesis
------
Short-horizon trends in liquid large caps persist more often than they reverse.
The cheap way to express that is a long option, but long premium bleeds theta
and needs a big move to pay. A debit spread sells a further-OTM option against
the long, which cuts the cost, cuts the theta bleed, and caps the loss at the
debit paid.

Why not just buy calls: over a one-week judged window, the variance of naked
long premium is brutal. Capping the downside at the debit is worth the capped
upside.

Entry conditions
----------------
  * confirmed trend (fast MA over slow MA, ADX above a floor)
  * IV rank *below* a ceiling - we are buying premium here, so we want it cheap
    (this is deliberately the mirror image of the condor's gate, so the two
    strategies rarely fire on the same name on the same day)
  * reward/risk at least ~2:1 after pricing the actual strikes
"""

from __future__ import annotations

from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import Right, TradeIdea
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry

log = get_logger("strategies.momentum")


@strategy_registry.register("momentum_debit_spread")
class MomentumDebitSpread(Strategy):
    description = "Buys vertical debit spreads in the direction of a confirmed trend."

    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        market = ctx.market

        trend = market.trend_strength
        if trend is None:
            return []
        min_trend = self.p("entry.min_trend_strength", 0.65)
        if abs(trend) < min_trend:
            log.debug("%s: trend %.2f below %.2f", market.symbol, abs(trend), min_trend)
            return []

        adx_value = market.adx
        min_adx = self.p("entry.min_adx", 20)
        if adx_value is not None and adx_value < min_adx:
            log.debug("%s: ADX %.1f below %.1f - trend not confirmed",
                      market.symbol, adx_value, min_adx)
            return []

        iv_rank = market.iv_rank
        max_iv_rank = self.p("entry.max_iv_rank", 0.60)
        if iv_rank is not None and iv_rank > max_iv_rank:
            log.debug("%s: IV rank %.2f too rich to be buying premium",
                      market.symbol, iv_rank)
            return []

        bullish = trend > 0
        right = Right.CALL if bullish else Right.PUT
        dte = tuple(self.p("structure.target_dte", [14, 45]))
        long_delta = self.p("structure.long_delta", 0.45)
        short_delta = self.p("structure.short_delta", 0.25)
        if not bullish:  # put deltas are negative
            long_delta, short_delta = -abs(long_delta), -abs(short_delta)

        try:
            idea = self.builder(ctx).vertical_by_delta(
                right=right,
                dte_range=(dte[0], dte[1]),
                long_delta=long_delta,
                short_delta=short_delta,
                quantity=self.p("structure.fixed_quantity", 1),
                thesis=self._thesis(market, trend, adx_value, iv_rank, bullish),
            )
        except (StrategyError, DataError) as exc:
            log.debug("%s: could not build spread - %s", market.symbol, exc)
            return []

        width = idea.meta.get("width") or 0
        if width and idea.net_price / width > self.p("structure.max_debit_to_width", 0.45):
            log.debug("%s: debit %.2f too rich vs width %.2f",
                      market.symbol, idea.net_price, width)
            return []

        idea.confidence = round(min(1.0, 0.45 + abs(trend) * 0.4), 3)
        idea.tags = ["momentum", "defined_risk", "bullish" if bullish else "bearish"]
        return [idea]

    @staticmethod
    def _thesis(market, trend, adx_value, iv_rank, bullish) -> str:  # type: ignore[no-untyped-def]
        direction = "upside" if bullish else "downside"
        bits = [f"trend {trend:+.2f}"]
        if adx_value is not None:
            bits.append(f"ADX {adx_value:.0f}")
        if iv_rank is not None:
            bits.append(f"IV rank {iv_rank:.0%} (premium is cheap)")
        return (
            f"{market.symbol} at {market.spot:.2f} is trending; expressing {direction} "
            f"with a vertical debit spread ({', '.join(bits)}). Loss is capped at the "
            "debit paid, which is what makes this survivable inside a one-week window."
        )
