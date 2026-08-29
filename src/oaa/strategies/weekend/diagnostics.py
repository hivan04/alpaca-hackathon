"""Why the weekend book did not trade.

Zero trades is an answer, but "the regime gate rejected 90 bars" is not yet a
finding - it does not say whether the gate was right. This module measures the
distributions the gates are drawn against, so the question becomes arithmetic:

    is a 2-sigma weekend reversion in BTC bigger than the round trip costs?

On the first three weekends measured (Aug 2026) the answer was no. Weekend BTC
has a 24-hour log-price sigma around 0.12-0.17%, so a move from -2 sigma back
to the mean is worth 22-29bp gross against a 54bp round trip. The band floor
was not mis-set; it was correctly reporting that the trade does not pay at that
horizon. In the one volatile weekend in the sample (sigma 0.72%, gross 126bp)
the ADX gate stood the book down instead - which is the genuine tension in the
strategy and the thing to state plainly rather than tune away:

    the dispersion that makes a 2-sigma move worth capturing is produced by
    the same flow that makes the tape trend.

`horizon_study` is what settles it: it re-measures sigma and the achievable
gross move across bar sizes and lookbacks, so the choice of timeframe is made
against the fee, not against intuition.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics as st
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from oaa.data.indicators import adx as wilder_adx
from oaa.data.indicators import resample
from oaa.strategies.weekend.clock import WindowPhase
from oaa.strategies.weekend.data import bar_time
from oaa.strategies.weekend.params import WeekendParams
from oaa.strategies.weekend.signals import (
    adx_slope,
    evaluate,
    log_closes,
    window_of,
    zscore,
)

Bar = dict[str, Any]


def _with_datetimes(bars: Sequence[Bar]) -> list[Bar]:
    """`resample` keys off a real datetime; cached bars carry ISO strings."""
    out = []
    for bar in bars:
        row = dict(bar)
        row["timestamp"] = bar_time(bar)
        out.append(row)
    return out


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))] if ordered else float("nan")


# --------------------------------------------------------------------------- #
@dataclass
class Observation:
    ts: dt.datetime
    z: float
    sigma: float
    adx: float
    adx_slope: float
    gross_move_bp: float


@dataclass
class Diagnosis:
    symbol: str
    timeframe: str
    lookback: int
    cost_bp: float
    observations: list[Observation] = field(default_factory=list)
    funnel: Counter = field(default_factory=Counter)

    @property
    def n(self) -> int:
        return len(self.observations)

    def _f(self, attr: str) -> list[float]:
        return [getattr(o, attr) for o in self.observations]

    def sigma_summary(self) -> dict[str, float]:
        sigmas = self._f("sigma")
        return {
            "min": min(sigmas),
            "p25": _percentile(sigmas, 0.25),
            "median": st.median(sigmas),
            "p75": _percentile(sigmas, 0.75),
            "max": max(sigmas),
        }

    def gross_move_at(self, z: float, exit_z: float) -> float:
        """The gross basis points a z-sigma reversion is worth at MEDIAN sigma.
        This single number decides whether the strategy can exist."""
        return math.expm1(abs(z - exit_z) * st.median(self._f("sigma"))) * 1e4

    def verdict(self, params: WeekendParams) -> str:
        gross = self.gross_move_at(-params.signal.entry_z, params.signal.exit_z)
        multiple = gross / self.cost_bp if self.cost_bp else 0.0
        if multiple >= params.signal.min_edge_multiple:
            return (
                f"VIABLE at this horizon: a {params.signal.entry_z:.1f}-sigma reversion "
                f"is worth {gross:.0f}bp against a {self.cost_bp:.0f}bp round trip "
                f"({multiple:.1f}x)."
            )
        if multiple >= 1.0:
            return (
                f"MARGINAL: {gross:.0f}bp gross against {self.cost_bp:.0f}bp costs "
                f"({multiple:.1f}x). It clears the fee but not the "
                f"{params.signal.min_edge_multiple:.1f}x safety margin."
            )
        return (
            f"NOT VIABLE at this horizon: a {params.signal.entry_z:.1f}-sigma reversion "
            f"is worth {gross:.0f}bp and the round trip costs {self.cost_bp:.0f}bp. "
            f"The trade loses money before it is right."
        )

    def counts(self, params: WeekendParams) -> dict[str, int]:
        sp = params.signal
        displaced = [o for o in self.observations if o.z <= -sp.entry_z]
        paying = [
            o for o in displaced
            if o.gross_move_bp >= sp.min_edge_multiple * self.cost_bp
        ]
        ranging = [o for o in paying if o.adx < sp.adx_max]
        return {
            "bars_in_window": self.n,
            f"z <= -{sp.entry_z:g}": len(displaced),
            f"...worth >= {sp.min_edge_multiple:g}x costs": len(paying),
            f"...and ADX < {sp.adx_max:g}": len(ranging),
        }


def diagnose(
    bars: Sequence[Bar],
    params: WeekendParams,
    symbol: str | None = None,
    timeframe_minutes: int | None = None,
    lookback: int | None = None,
) -> Diagnosis:
    """Measure every in-window bar, whether or not it would have traded."""
    sp = params.signal
    lookback = lookback or sp.lookback_bars
    series = _with_datetimes(bars)
    if timeframe_minutes and timeframe_minutes != 15:
        series = resample(series, minutes=timeframe_minutes)

    out = Diagnosis(
        symbol=symbol or params.symbols[0],
        timeframe=f"{timeframe_minutes or 15}Min",
        lookback=lookback,
        cost_bp=params.costs.round_trip_bp,
    )
    for i in range(lookback + 1, len(series)):
        ts = series[i]["timestamp"]
        if params.window.phase(ts) is not WindowPhase.OPEN:
            continue
        seen = window_of(series, params, i)
        z, _mean, sigma = zscore(log_closes(seen), lookback)
        if z is None or sigma is None:
            continue
        adx_value = wilder_adx(seen, sp.adx_period) or 0.0
        out.observations.append(
            Observation(
                ts=ts,
                z=z,
                sigma=sigma,
                adx=adx_value,
                adx_slope=adx_slope(seen, sp.adx_period) or 0.0,
                gross_move_bp=math.expm1(abs(z - sp.exit_z) * sigma) * 1e4 if z < 0 else 0.0,
            )
        )
        # The real gate stack, for the funnel - same code the live book runs.
        signal = evaluate(out.symbol, seen, params)
        out.funnel[signal.blocked_by or "actionable"] += 1
    return out


# --------------------------------------------------------------------------- #
def horizon_study(
    bars: Sequence[Bar],
    params: WeekendParams,
    grid: Sequence[tuple[int, int]] = ((15, 96), (60, 24), (60, 48), (60, 72), (240, 21)),
) -> list[dict[str, Any]]:
    """Sigma and achievable gross move across bar sizes and lookbacks.

    The point is not to find the setting with the most trades. It is to find
    whether ANY horizon puts BTC's weekend dispersion above the fee - and if
    none does, to say so.
    """
    rows: list[dict[str, Any]] = []
    for minutes, lookback in grid:
        try:
            result = diagnose(bars, params, timeframe_minutes=minutes, lookback=lookback)
        except (ValueError, ZeroDivisionError):
            continue
        if result.n < 5:
            continue
        counts = result.counts(params)
        gross = result.gross_move_at(-params.signal.entry_z, params.signal.exit_z)
        rows.append(
            {
                "timeframe": f"{minutes}Min",
                "lookback": lookback,
                "span_hours": round(minutes * lookback / 60, 1),
                "bars": result.n,
                "sigma_median_pct": round(st.median(result._f("sigma")) * 100, 3),
                "gross_at_entry_bp": round(gross, 0),
                "cost_bp": result.cost_bp,
                "edge_multiple": round(gross / result.cost_bp, 2) if result.cost_bp else 0.0,
                "signals": counts[f"z <= -{params.signal.entry_z:g}"],
                "signals_that_pay": counts[f"...worth >= {params.signal.min_edge_multiple:g}x costs"],
                "and_ranging": counts[f"...and ADX < {params.signal.adx_max:g}"],
            }
        )
    return rows
