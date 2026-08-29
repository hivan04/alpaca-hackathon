"""The weekend signal stack.

Order matters, and it is deliberately cheapest-and-most-restrictive first, the
same discipline the intraday book uses. Every gate returns a `GateResult`, so a
weekend with no trades still produces the artefact that matters: a log of what
was seen and which number refused it.

    1. data        enough bars to fit a 24h mean and a Wilder ADX
    2. regime      ADX < 25 and not rising - mean reversion needs a range
    3. band        sigma inside [min, max] - too tight to pay costs, or a
                   distribution we did not fit
    4. shock       the last bar did not fall off a cliff (no knife-catching)
    5. displaced   z <= -entry_z, the actual signal, and fourth in line
    6. edge        expected reversion >= min_edge_multiple x round-trip cost

Gate 6 is the one that matters. Gates 1-5 say "this looks like the setup";
gate 6 says "and it is big enough to be worth 50 basis points of friction".
Most rejections land there, which is the honest answer for a 24/7 market with
percentage fees.

Long only, and that is a venue constraint, not a view: Alpaca does not permit
short crypto, so the upper band is an exit and never an entry. The strategy is
therefore asymmetric by construction, and the backtest reports the trades it
could not take on the rich side so the asymmetry is visible rather than hidden.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import fmean, pstdev
from typing import Any

from oaa.data.indicators import adx as wilder_adx
from oaa.data.indicators import atr as wilder_atr
from oaa.signals.gates import GateResult
from oaa.strategies.weekend.params import WeekendParams

Bar = dict[str, Any]


@dataclass
class WeekendSignal:
    """One symbol, one bar, fully explained."""

    symbol: str
    price: float
    z: float | None = None
    sigma: float | None = None
    mean: float | None = None
    adx: float | None = None
    adx_slope: float | None = None
    atr: float | None = None
    expected_move_bp: float | None = None
    edge_multiple: float | None = None
    checks: list[GateResult] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def blocked_by(self) -> str | None:
        for check in self.checks:
            if not check.passed:
                return check.gate
        return None

    @property
    def reason(self) -> str:
        for check in self.checks:
            if not check.passed:
                return f"{check.gate}: {check.reason}"
        return (
            f"z={self.z:.2f} sigma={self.sigma:.2%} adx={self.adx:.1f} "
            f"edge={self.edge_multiple:.1f}x cost"
        )

    def summary(self) -> str:
        marks = " ".join(("+" if c.passed else "-") + c.gate for c in self.checks)
        return f"{self.symbol} {self.price:,.0f} [{marks}] {self.reason}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "z": self.z,
            "sigma": self.sigma,
            "adx": self.adx,
            "adx_slope": self.adx_slope,
            "atr": self.atr,
            "expected_move_bp": self.expected_move_bp,
            "edge_multiple": self.edge_multiple,
            "actionable": self.actionable,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "gates": [
                {"gate": c.gate, "passed": c.passed, "reason": c.reason, **c.metrics}
                for c in self.checks
            ],
        }


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def log_closes(bars: Sequence[Bar]) -> list[float]:
    return [math.log(float(b["close"])) for b in bars if float(b.get("close") or 0) > 0]


def zscore(values: Sequence[float], lookback: int) -> tuple[float | None, float | None, float | None]:
    """(z, mean, sigma) of the last value against the trailing window.

    Computed on LOG price, so sigma is a proportional band and the same
    threshold means the same thing at $30k and at $120k. A z-score on raw price
    silently tightens as the asset appreciates.
    """
    if len(values) < lookback + 1:
        return None, None, None
    window = values[-lookback:]
    mu = fmean(window)
    sd = pstdev(window)
    if sd <= 0:
        return None, mu, sd
    return (values[-1] - mu) / sd, mu, sd


def adx_slope(bars: Sequence[Bar], period: int, lag: int = 8) -> float | None:
    """Change in ADX over `lag` bars. A trend being born reads as low-but-
    rising ADX, which passes a naive ceiling test right before it hurts."""
    if len(bars) < period * 2 + lag + 2:
        return None
    now = wilder_adx(bars, period)
    then = wilder_adx(bars[:-lag], period)
    if now is None or then is None:
        return None
    return round(now - then, 4)


def expected_reversion_bp(z: float, sigma: float, exit_z: float) -> float:
    """Gross move, in basis points, from here back to the exit band.

    sigma is a log-price standard deviation, so (z - exit_z) * sigma is a log
    return and expm1 converts it to the arithmetic move actually banked.
    """
    return math.expm1(abs(z - exit_z) * sigma) * 1e4


# --------------------------------------------------------------------------- #
# the stack
# --------------------------------------------------------------------------- #
def required_bars(params: WeekendParams) -> int:
    """How much history the stack actually reads.

    Everything here is a rolling window: the z-score reads `lookback_bars`, the
    Wilder ADX converges in a few multiples of its period, and the slope looks
    back eight bars. Nothing needs the whole series - and feeding it the whole
    series is not merely wasteful, it makes the replay diverge from the live
    book, which only ever fetches a few days of bars.
    """
    sp = params.signal
    return max(sp.min_bars, sp.lookback_bars + 1, sp.adx_period * 4 + 20)


def window_of(bars: Sequence[Bar], params: WeekendParams, upto: int) -> Sequence[Bar]:
    """The bounded slice ending at `upto` (inclusive) that the stack should see."""
    need = required_bars(params) + 40
    return bars[max(0, upto + 1 - need) : upto + 1]


def evaluate(symbol: str, bars: Sequence[Bar], params: WeekendParams) -> WeekendSignal:
    """Run every gate. Never raises: a bad read is a rejection, not a crash."""
    sp = params.signal
    price = float(bars[-1]["close"]) if bars else 0.0
    sig = WeekendSignal(symbol=symbol, price=price)

    # 1. data --------------------------------------------------------------- #
    need = max(sp.min_bars, sp.lookback_bars + 1, sp.adx_period * 2 + 10)
    if len(bars) < need:
        sig.checks.append(
            GateResult.veto("data", f"{len(bars)} bars, need {need}", bars=len(bars))
        )
        return sig
    sig.checks.append(GateResult.ok("data", bars=len(bars)))

    logs = log_closes(bars)
    z, mu, sd = zscore(logs, sp.lookback_bars)
    sig.z, sig.mean, sig.sigma = z, mu, sd
    sig.adx = wilder_adx(bars, sp.adx_period)
    sig.adx_slope = adx_slope(bars, sp.adx_period)
    sig.atr = wilder_atr(bars, sp.adx_period)

    # 2. regime ------------------------------------------------------------- #
    if sig.adx is None:
        sig.checks.append(GateResult.veto("regime", "ADX unavailable"))
        return sig
    if sig.adx >= sp.adx_max:
        sig.checks.append(
            GateResult.veto(
                "regime",
                f"ADX {sig.adx:.1f} >= {sp.adx_max:.0f}: trending, and a trend "
                f"does not revert on schedule",
                adx=sig.adx,
            )
        )
        return sig
    if sig.adx_slope is not None and sig.adx_slope > sp.adx_slope_max:
        sig.checks.append(
            GateResult.veto(
                "regime",
                f"ADX rising {sig.adx_slope:+.1f} over 8 bars: a trend forming "
                f"reads as chop until it does not",
                adx=sig.adx,
                adx_slope=sig.adx_slope,
            )
        )
        return sig
    sig.checks.append(GateResult.ok("regime", adx=sig.adx, adx_slope=sig.adx_slope or 0.0))

    # 3. band --------------------------------------------------------------- #
    if z is None or sd is None or sd <= 0:
        sig.checks.append(GateResult.veto("band", "degenerate band (zero variance)"))
        return sig
    if sd < sp.min_sigma:
        sig.checks.append(
            GateResult.veto(
                "band",
                f"sigma {sd:.2%} below the {sp.min_sigma:.2%} floor: the band is "
                f"narrower than the round trip costs",
                sigma=sd,
            )
        )
        return sig
    if sd > sp.max_sigma:
        sig.checks.append(
            GateResult.veto(
                "band",
                f"sigma {sd:.2%} above the {sp.max_sigma:.2%} ceiling: this is "
                f"not the distribution the mean was fitted on",
                sigma=sd,
            )
        )
        return sig
    sig.checks.append(GateResult.ok("band", sigma=sd))

    # 4. shock -------------------------------------------------------------- #
    last_return = _bar_return(bars[-1])
    if last_return is not None and last_return <= -abs(sp.shock_bar_return):
        sig.checks.append(
            GateResult.veto(
                "shock",
                f"last bar {last_return:.2%}: mid-liquidation, wait a bar",
                last_bar_return=last_return,
            )
        )
        return sig
    sig.checks.append(GateResult.ok("shock", last_bar_return=last_return or 0.0))

    # 5. displaced ---------------------------------------------------------- #
    if z > -abs(sp.entry_z):
        sig.checks.append(
            GateResult.veto(
                "displaced",
                f"z={z:+.2f}, entry needs <= {-abs(sp.entry_z):.2f}"
                + (" (rich side is an exit, not a short: no crypto shorting)" if z > 0 else ""),
                z=z,
            )
        )
        return sig
    sig.checks.append(GateResult.ok("displaced", z=z))

    # 6. edge --------------------------------------------------------------- #
    move_bp = expected_reversion_bp(z, sd, sp.exit_z)
    cost_bp = params.costs.round_trip_bp
    multiple = move_bp / cost_bp if cost_bp > 0 else 0.0
    sig.expected_move_bp, sig.edge_multiple = round(move_bp, 1), round(multiple, 2)
    if multiple < sp.min_edge_multiple:
        sig.checks.append(
            GateResult.veto(
                "edge",
                f"reversion to the mean is worth {move_bp:.0f}bp against a "
                f"{cost_bp:.0f}bp round trip ({multiple:.1f}x, need "
                f"{sp.min_edge_multiple:.1f}x)",
                expected_move_bp=move_bp,
                cost_bp=cost_bp,
                edge_multiple=multiple,
            )
        )
        return sig
    sig.checks.append(
        GateResult.ok("edge", expected_move_bp=move_bp, edge_multiple=multiple)
    )
    return sig


def _bar_return(bar: Bar) -> float | None:
    try:
        open_, close = float(bar["open"]), float(bar["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return (close - open_) / open_ if open_ > 0 else None


def stop_price(entry: float, atr_value: float | None, params: WeekendParams) -> float:
    """Hard stop. This is what makes the position defined-risk, and the risk
    engine's `allow_undefined_risk: false` rule apply to a spot instrument at
    all: max loss is (entry - stop) x qty, known before the order is sent."""
    x = params.exits
    distance = (atr_value or 0.0) * x.atr_stop_multiple
    pct = distance / entry if entry > 0 else x.min_stop_pct
    pct = min(max(pct, x.min_stop_pct), x.max_stop_pct)
    return round(entry * (1 - pct), 2)


def target_price(entry: float, z: float, sigma: float, params: WeekendParams) -> float:
    """Where the reversion is declared complete: the exit band, in price."""
    move = abs(z - params.signal.exit_z) * sigma
    return round(entry * math.exp(move), 2)
