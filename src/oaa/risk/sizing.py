"""Position sizing. Risk-first, not capital-first.

We size from the structure's known maximum loss, so every position risks the
same fraction of equity regardless of how wide the spread is.
"""

from __future__ import annotations

import math

from oaa.core.types import TradeIdea


def size_by_risk(
    idea: TradeIdea,
    equity: float,
    max_risk_pct: float,
    max_quantity: int = 20,
) -> int:
    """Contracts (structures) to trade so that max loss <= max_risk_pct * equity."""
    if idea.max_loss is None or idea.max_loss <= 0 or equity <= 0:
        return 0
    budget = equity * max_risk_pct
    qty = int(math.floor(budget / idea.max_loss))
    return max(0, min(qty, max_quantity))


def size_by_notional(
    idea: TradeIdea,
    equity: float,
    max_notional_pct: float,
    spot: float,
    max_quantity: int = 20,
) -> int:
    """Secondary cap so a cheap, wide structure cannot own the book."""
    if spot <= 0 or equity <= 0:
        return max_quantity
    notional_per = spot * 100
    budget = equity * max_notional_pct
    return max(0, min(int(math.floor(budget / notional_per)), max_quantity))


def kelly_fraction(win_rate: float, reward_risk: float, cap: float = 0.25) -> float:
    """Fractional Kelly, hard-capped.

    Full Kelly is far too aggressive for a one-week judged P&L window; this is
    used only to nudge sizing within the risk cap, never to exceed it.
    """
    if reward_risk <= 0:
        return 0.0
    edge = win_rate - (1 - win_rate) / reward_risk
    return round(max(0.0, min(cap, edge)), 4)
