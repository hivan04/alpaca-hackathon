"""Limit pricing for multi-leg structures.

Alpaca's convention: a positive limit price is a net debit you pay, a negative
one is a net credit you receive. `limit_price_for` keeps that sign correct while
moving the price toward the far touch as we chase.
"""

from __future__ import annotations

from oaa.core.types import Side, TradeIdea


def structure_bid_ask(idea: TradeIdea) -> tuple[float | None, float | None]:
    """Worst-case and best-case net price for the whole structure.

    Buying a leg costs the ask; selling one earns the bid. The pessimistic
    net (what you pay if everything goes against you) is the "ask" side.
    """
    pessimistic = optimistic = 0.0
    for leg in idea.legs:
        quote = leg.quote
        if quote is None or quote.bid is None or quote.ask is None:
            return None, None
        if leg.side is Side.BUY:
            pessimistic += quote.ask * leg.ratio
            optimistic += quote.bid * leg.ratio
        else:
            pessimistic -= quote.bid * leg.ratio
            optimistic -= quote.ask * leg.ratio
    return round(optimistic, 4), round(pessimistic, 4)


def mid_price(idea: TradeIdea) -> float:
    """Net mid of the structure, signed. Falls back to the idea's own price."""
    total = 0.0
    for leg in idea.legs:
        quote = leg.quote
        if quote is None or quote.mid is None:
            return idea.net_price
        total += (quote.mid if leg.side is Side.BUY else -quote.mid) * leg.ratio
    return round(total, 4)


def limit_price_for(idea: TradeIdea, aggression: float = 0.5, step: int = 0) -> float:
    """Price between mid (aggression=0) and the far touch (aggression=1).

    `step` walks further toward the touch on each chase attempt.
    """
    best, worst = structure_bid_ask(idea)
    mid = mid_price(idea)
    if best is None or worst is None:
        return round(mid, 2)

    ratio = min(1.0, max(0.0, aggression + 0.25 * step))
    # `worst` is the price at which we are certain to be marketable.
    price = mid + (worst - mid) * ratio
    return round(price, 2)


def slippage_vs_mid(idea: TradeIdea, executed: float) -> float | None:
    """Fraction of the structure's own mid given up on the fill."""
    mid = mid_price(idea)
    if abs(mid) < 1e-9:
        return None
    return round(abs(executed - mid) / abs(mid), 4)
