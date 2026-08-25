"""Calendar spread into an earnings IV crush.

Thesis
------
Ahead of a scheduled binary event, the expiry that contains the event prices in
far more implied vol than the expiries around it. After the print, that front
implied vol collapses while the back month barely moves. Selling the front and
buying the back harvests that term-structure inversion.

This is disabled by default. It is a genuinely good trade, but it is an event
bet, and event bets add variance to a P&L score measured over one week. Enable
it in config/default.yaml if the week is quiet and the condor is not filling.

Note: earnings dates are not in Alpaca's API. Populate
`MarketContext.earnings_date` from a partner data adapter (see docs/PARTNERS.md)
or a static calendar before enabling this.
"""

from __future__ import annotations

import datetime as dt

from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import Right, TradeIdea
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry

log = get_logger("strategies.earnings")


@strategy_registry.register("earnings_calendar")
class EarningsCalendar(Strategy):
    description = "Sells the event expiry and buys the back month into earnings."

    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        market = ctx.market
        earnings = market.earnings_date or _from_enrichment(ctx)
        if earnings is None:
            log.debug("%s: no earnings date available - skipping", market.symbol)
            return []

        days_out = (earnings - market.asof.date()).days
        window = self.p("entry.days_before_earnings", [1, 3])
        if not window[0] <= days_out <= window[1]:
            return []

        iv_rank = market.iv_rank
        min_iv_rank = self.p("entry.min_iv_rank", 0.50)
        if iv_rank is not None and iv_rank < min_iv_rank:
            return []

        front = tuple(self.p("structure.front_dte", [1, 7]))
        back = tuple(self.p("structure.back_dte", [21, 45]))
        try:
            idea = self.builder(ctx).calendar_atm(
                front_dte=(front[0], front[1]),
                back_dte=(back[0], back[1]),
                right=Right.CALL,
                quantity=self.p("structure.fixed_quantity", 1),
                thesis=(
                    f"{market.symbol} reports in {days_out}d. The front expiry carries "
                    "event premium the back month does not; selling the inversion. "
                    "Risk is capped at the net debit."
                ),
            )
        except (StrategyError, DataError) as exc:
            log.debug("%s: calendar build failed - %s", market.symbol, exc)
            return []

        inversion = _inversion(idea)
        floor = self.p("entry.min_term_structure_inversion", 0.08)
        if inversion is not None and inversion < floor:
            log.debug("%s: term inversion %.3f below %.3f", market.symbol, inversion, floor)
            return []

        idea.confidence = 0.55
        idea.tags = ["earnings", "vol_term_structure", "defined_risk"]
        idea.meta["earnings_date"] = earnings.isoformat()
        return [idea]


def _inversion(idea: TradeIdea) -> float | None:
    front_iv = idea.meta.get("front_iv")
    back_iv = idea.meta.get("back_iv")
    if front_iv is None or back_iv is None:
        return None
    return round(float(front_iv) - float(back_iv), 4)


def _from_enrichment(ctx: StrategyContext) -> dt.date | None:
    """Partner adapters may drop an earnings date into `enrichment`."""
    raw = ctx.market.enrichment.get("earnings_date")
    if isinstance(raw, dt.date):
        return raw
    if isinstance(raw, str):
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            return None
    return None
