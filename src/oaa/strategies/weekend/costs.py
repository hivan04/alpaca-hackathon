"""What a weekend round trip actually costs.

Paper fills at the mid and charges nothing. Crypto does neither, and unlike
options the fee is a *percentage of notional*, so it scales with position size
rather than with contract count. On Alpaca's retail crypto tier the taker fee
is 25 bp a side; a market-in / market-out round trip is therefore 50 bp before
a single tick of slippage, and the weekend order book is thinner than the
weekday one.

Why this file exists rather than a constant
-------------------------------------------
Because it is the gate. A 2-sigma reversion on a 40 bp band is a 70 bp gross
move; pay 50 bp to capture it and the strategy is a rounding error with
variance. `expected_edge_bp` is what `signals.evaluate` calls to decide whether
a signal is worth taking, and it is the single most restrictive gate in the
stack. If this book trades rarely, this is why - and that is the correct
behaviour, not a bug to tune away.
"""

from __future__ import annotations

from dataclasses import dataclass

BP = 1e-4


@dataclass(frozen=True)
class CryptoCostModel:
    """All figures in basis points of notional, per side unless stated."""

    #: Alpaca retail crypto taker fee, per side.
    taker_fee_bp: float = 25.0
    #: Maker fee, per side. Only earned when a limit order rests and is hit.
    maker_fee_bp: float = 15.0
    #: Half-spread paid when crossing, per side. Weekend books are wider than
    #: weekday ones; measure with `oaa weekend spread` before trusting this.
    half_spread_bp: float = 4.0
    #: Extra adverse fill beyond the touch, per side.
    slippage_bp: float = 3.0
    #: Fraction of fills expected to earn the maker rate. Entries rest on the
    #: bid (we are buying a dislocation, there is no hurry); exits at the
    #: profit target rest too, but stops and the Sunday flatten cross.
    maker_ratio: float = 0.5

    # ------------------------------------------------------------------ #
    @property
    def fee_bp_per_side(self) -> float:
        return self.maker_ratio * self.maker_fee_bp + (1 - self.maker_ratio) * self.taker_fee_bp

    @property
    def cost_bp_per_side(self) -> float:
        return self.fee_bp_per_side + self.half_spread_bp + self.slippage_bp

    @property
    def round_trip_bp(self) -> float:
        """The number the signal has to beat."""
        return 2 * self.cost_bp_per_side

    def round_trip_cost(self, notional: float) -> float:
        return notional * self.round_trip_bp * BP

    def apply(self, notional: float, crossing: bool) -> float:
        """Dollar cost of one side of a trade."""
        fee = self.taker_fee_bp if crossing else self.maker_fee_bp
        extra = (self.half_spread_bp + self.slippage_bp) if crossing else 0.0
        return notional * (fee + extra) * BP

    def net_of_costs(self, entry: float, exit_price: float, qty: float, crossing_exit: bool) -> float:
        """Realised P&L after both sides. Entry is assumed to rest, exit may
        cross - which is why a stop-out costs more than a target."""
        gross = (exit_price - entry) * qty
        return round(
            gross
            - self.apply(entry * qty, crossing=False)
            - self.apply(exit_price * qty, crossing=crossing_exit),
            2,
        )
