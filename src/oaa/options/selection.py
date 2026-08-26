"""Surface-aware option selection.

Buying the ATM call is what a script does. Choosing the strike, the expiry and
the *structure* conditioned on the expected move and the current surface is a
real decision, and it is the most defensible answer to "why does this need an
agent at all".

The decision table:

    expected move small,  IV low        ATM, 0-1 DTE          max gamma, cheap
    expected move small,  IV elevated   ATM, longer dated     less premium burn if it stalls
    expected move large,  IV low        slightly OTM          better convexity per dollar
    expected move large,  IV elevated   DEBIT VERTICAL        caps the cost of expensive IV
    IV extremely elevated NO TRADE      paying for a move already priced in

The last row is the one worth a slide. A strategy that declines to trade when
the option is expensive relative to the move it expects is a vol-aware
directional strategy; one that always buys the ATM call is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oaa.core.logging import get_logger

log = get_logger("options.selection")


@dataclass
class Selection:
    """What to build, and the reasoning that chose it."""

    mode: str                       # "single" | "vertical" | "none"
    dte_range: tuple[int, int] = (0, 2)
    long_delta: float = 0.50
    short_delta: float = 0.30
    reason: str = ""
    expected_move_pct: float = 0.0
    iv: float | None = None
    iv_rank: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def tradable(self) -> bool:
        return self.mode != "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "dte_range": list(self.dte_range),
            "long_delta": self.long_delta,
            "short_delta": self.short_delta,
            "expected_move_pct": round(self.expected_move_pct, 5),
            "iv": self.iv,
            "iv_rank": self.iv_rank,
            "reason": self.reason,
            **self.metrics,
        }


def expected_move_pct(
    spot: float,
    atr_value: float | None,
    horizon_fraction: float = 1.0,
) -> float:
    """Expected move over the remainder of the session, as a fraction of spot.

    ATR is a daily range; a trade opened at 13:00 has roughly a third of the
    session left, so the raw ATR would flatter the expected move badly. The
    horizon fraction scales it by remaining time, square-root style.
    """
    if not spot or spot <= 0 or not atr_value:
        return 0.0
    fraction = max(0.05, min(1.0, horizon_fraction))
    return round((atr_value / spot) * (fraction ** 0.5), 6)


def select(
    *,
    spot: float,
    atr_value: float | None,
    horizon_fraction: float,
    iv: float | None,
    iv_rank: float | None,
    large_move_pct: float = 0.006,
    iv_rank_no_trade_above: float = 0.85,
    prefer_vertical_above_iv_rank: float = 0.60,
    dte_max: int = 2,
) -> Selection:
    """Pick the structure. Returns `mode="none"` when the option is too rich."""
    move = expected_move_pct(spot, atr_value, horizon_fraction)
    metrics = {"horizon_fraction": round(horizon_fraction, 4), "atr": atr_value}

    if iv_rank is not None and iv_rank >= iv_rank_no_trade_above:
        return Selection(
            mode="none",
            reason=(
                f"IV rank {iv_rank:.0%} is at or above the {iv_rank_no_trade_above:.0%} "
                "no-trade ceiling - the move this signal expects is already priced in, "
                "and paying for it is a negative-expectancy way to be right"
            ),
            expected_move_pct=move, iv=iv, iv_rank=iv_rank, metrics=metrics,
        )

    large_move = move >= large_move_pct
    elevated = iv_rank is not None and iv_rank >= prefer_vertical_above_iv_rank

    if large_move and elevated:
        return Selection(
            mode="vertical",
            dte_range=(0, dte_max),
            long_delta=0.45,
            short_delta=0.22,
            reason=(
                f"expected move {move:.2%} is large and IV rank {iv_rank:.0%} is elevated: "
                "a debit vertical caps the cost of the expensive wing instead of paying "
                "for it outright"
            ),
            expected_move_pct=move, iv=iv, iv_rank=iv_rank, metrics=metrics,
        )
    if large_move:
        return Selection(
            mode="single",
            dte_range=(0, dte_max),
            long_delta=0.38,
            reason=(
                f"expected move {move:.2%} is large and premium is cheap: slightly OTM "
                "buys more convexity per dollar than the ATM strike"
            ),
            expected_move_pct=move, iv=iv, iv_rank=iv_rank, metrics=metrics,
        )
    if elevated:
        return Selection(
            mode="single",
            dte_range=(min(1, dte_max), max(dte_max, 2)),
            long_delta=0.50,
            reason=(
                f"expected move {move:.2%} is modest and IV rank {iv_rank:.0%} is elevated: "
                "ATM but longer dated, so a stalled move burns less premium"
            ),
            expected_move_pct=move, iv=iv, iv_rank=iv_rank, metrics=metrics,
        )
    return Selection(
        mode="single",
        dte_range=(0, min(1, dte_max)),
        long_delta=0.50,
        reason=(
            f"expected move {move:.2%} is modest and premium is cheap: ATM 0-1 DTE for "
            "maximum gamma at the lowest cost"
        ),
        expected_move_pct=move, iv=iv, iv_rank=iv_rank, metrics=metrics,
    )
