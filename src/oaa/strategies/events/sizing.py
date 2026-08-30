"""Confidence -> contracts, with the bounds that make it survivable.

The instruction is "higher confidence, bigger bet". Taken literally that is a
recipe for one 0.9-confidence call to be the whole week's P&L, so the mapping
is linear between two hard bounds and then clipped three more times:

  1. `max_risk_per_trade_pct` of equity, at full confidence.
  2. `nightly_risk_budget_pct` of equity across every position opened that
     night - three names at once share one budget rather than each getting a
     full allocation.
  3. `max_contracts`, an absolute ceiling that does not scale with equity, so a
     good week cannot quietly turn into a bigger bet than the book was designed
     to hold.

Risk is measured as the debit paid, which for a vertical is the true maximum
loss. No assumption about stops is involved: the structure cannot lose more.
"""

from __future__ import annotations

from dataclasses import dataclass

from oaa.core.logging import get_logger
from oaa.strategies.events.params import SizingParams

log = get_logger("strategies.events.sizing")


@dataclass
class SizeDecision:
    contracts: int
    risk_dollars: float
    multiple: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.contracts > 0


def confidence_multiple(confidence: float, floor: float, params: SizingParams) -> float:
    """Map [floor, 1.0] confidence onto [min_size_multiple, 1.0] linearly.

    At the confidence floor you take the smallest position the book allows,
    not zero - a call that clears the gate is worth a position, and the gate is
    where marginal calls are supposed to die.
    """
    if confidence <= floor:
        return params.min_size_multiple
    span = max(1e-6, 1.0 - floor)
    fraction = (confidence - floor) / span
    return round(
        params.min_size_multiple + fraction * (1.0 - params.min_size_multiple), 4
    )


def size(
    *,
    confidence: float,
    confidence_floor: float,
    max_loss_per_contract: float,
    equity: float,
    params: SizingParams,
    budget_remaining: float | None = None,
    extra_multiple: float = 1.0,
) -> SizeDecision:
    """How many contracts this call is worth.

    `extra_multiple` is the ATR adjustment from the technical layer: a name
    whose daily range has already doubled is not taken in the same size as a
    quiet one, however confident the direction call. It only ever scales DOWN -
    a calm tape is not a reason to take more than the confidence justified.
    """
    if max_loss_per_contract <= 0:
        return SizeDecision(0, 0.0, 0.0, "structure has no computable max loss")
    if equity <= 0:
        return SizeDecision(0, 0.0, 0.0, "no equity")

    multiple = round(
        confidence_multiple(confidence, confidence_floor, params)
        * min(1.0, max(0.0, extra_multiple)),
        4,
    )
    allowance = equity * params.max_risk_per_trade_pct * multiple
    if budget_remaining is not None:
        allowance = min(allowance, budget_remaining)
    if allowance <= 0:
        return SizeDecision(0, 0.0, multiple, "nightly risk budget is exhausted")

    contracts = int(allowance // max_loss_per_contract)
    if contracts < params.min_contracts:
        return SizeDecision(
            0, 0.0, multiple,
            f"one contract risks ${max_loss_per_contract:,.2f}, above the "
            f"${allowance:,.2f} this call is allowed",
        )
    contracts = min(contracts, params.max_contracts)
    risk = round(contracts * max_loss_per_contract, 2)
    log.debug(
        "size %d contract(s): confidence %.2f -> %.2fx, risking $%.2f of $%.2f allowed",
        contracts, confidence, multiple, risk, allowance,
    )
    return SizeDecision(contracts, risk, multiple)


def nightly_budget(equity: float, params: SizingParams) -> float:
    return round(max(0.0, equity) * params.nightly_risk_budget_pct, 2)
