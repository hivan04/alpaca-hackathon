"""Does a displaced weekend actually revert?

Everything else in this package assumes it does. This module is the test, and
it is deliberately model-free: no entry rule, no stop, no sizing. For every
in-window bar it records the z-score, the regime, and what the tape did NEXT at
several horizons. Then it groups.

The three questions it answers
------------------------------
1. **Is there reversion at all?** Mean forward return conditional on z <= -2,
   against the unconditional mean over the same bars. If the conditional is not
   better than the baseline, the z-score carries no information and no amount
   of threshold tuning will create any.
2. **Does it survive the fee?** The same mean, minus the modelled round trip.
   A conditional edge of 30bp is a fact and still not a strategy.
3. **Does ADX help?** The same table split by regime. The ADX gate is a
   hypothesis; if the ranging bucket is not better than the trending one, the
   gate is decoration and should be dropped rather than defended.

Independence, and why the t-stat here is not the naive one
----------------------------------------------------------
Consecutive 15-minute bars inside one dislocation are not independent
observations, and an 8-hour forward window sampled every 15 minutes overlaps
itself 32 times. Pooling them produces exactly the table that ended this
strategy's first draft: 11 observations from ONE weekend's rally, a 100% hit
rate and t = +14.6, all of it a single event counted eleven times.

So every cell reports three counts - bars, distinct weekends, and independent
episodes (a greedy non-overlapping subset, one observation per forward window)
- and the t-statistic is computed on the independent subset alone. The verdict
refuses to endorse anything drawn from fewer than eight weekends, however good
the number looks.

Truncation, deliberately
------------------------
A forward horizon that runs past the Sunday cutoff is truncated to the last
in-window bar, not discarded. Live, that position would be flattened at the
cutoff, so measuring the untruncated return would credit the strategy with a
move it could never have held for.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics as st
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from oaa.data.indicators import adx as wilder_adx
from oaa.strategies.weekend.clock import WindowPhase
from oaa.strategies.weekend.diagnostics import _with_datetimes
from oaa.strategies.weekend.params import WeekendParams
from oaa.strategies.weekend.signals import log_closes, window_of, zscore

Bar = dict[str, Any]

#: Forward horizons in 15-minute bars: 1h, 2h, 4h, 8h.
HORIZONS = (4, 8, 16, 32)

#: z buckets, low to high. The first two are the ones the strategy trades.
BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("z <= -2.5", -99.0, -2.5),
    ("-2.5 < z <= -2", -2.5, -2.0),
    ("-2 < z <= -1.5", -2.0, -1.5),
    ("-1.5 < z <= -1", -1.5, -1.0),
    ("-1 < z < 1", -1.0, 1.0),
    ("z >= 1", 1.0, 99.0),
)


@dataclass
class Sample:
    z: float
    sigma: float
    adx: float
    #: Bar index in the series - what makes overlap detectable.
    index: int = 0
    #: The Friday that opened this weekend. Two observations sharing one are
    #: one market event seen twice, not two pieces of evidence.
    weekend: str = ""
    forward_bp: dict[int, float] = field(default_factory=dict)


@dataclass
class Cell:
    label: str
    horizon_bars: int
    values_bp: list[float] = field(default_factory=list)
    #: (bar index, weekend key) per value, so overlap and clustering are visible.
    keys: list[tuple[int, str]] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.values_bp)

    @property
    def weekends(self) -> int:
        return len({w for _i, w in self.keys})

    def independent(self) -> list[float]:
        """Greedy non-overlapping subset: at most one observation per forward
        window. This is the sample size that actually carries information."""
        if not self.keys:
            return list(self.values_bp)
        ordered = sorted(zip(self.keys, self.values_bp, strict=True), key=lambda kv: kv[0][0])
        out: list[float] = []
        last = None
        for (index, _weekend), value in ordered:
            if last is None or index >= last + self.horizon_bars:
                out.append(value)
                last = index
        return out

    @property
    def episodes(self) -> int:
        return len(self.independent())

    @property
    def mean_bp(self) -> float:
        return round(st.mean(self.values_bp), 1) if self.values_bp else 0.0

    @property
    def median_bp(self) -> float:
        return round(st.median(self.values_bp), 1) if self.values_bp else 0.0

    @property
    def hit_rate(self) -> float:
        if not self.values_bp:
            return 0.0
        return round(sum(1 for v in self.values_bp if v > 0) / self.n, 3)

    def net_bp(self, cost_bp: float) -> float:
        return round(self.mean_bp - cost_bp, 1)

    def t_stat(self) -> float | None:
        """Computed on the INDEPENDENT subset only.

        The naive version - pooling every overlapping bar - is what turned one
        weekend into t = +14.6. Anything below three independent episodes gets
        no t-statistic at all rather than a flattering one.
        """
        values = self.independent()
        if len(values) < 3:
            return None
        sd = st.pstdev(values)
        if sd <= 0:
            return None
        return round(st.mean(values) / (sd / math.sqrt(len(values))), 2)


def collect(bars: Sequence[Bar], params: WeekendParams) -> list[Sample]:
    sp = params.signal
    series = _with_datetimes(bars)
    in_window = [
        i for i in range(len(series))
        if params.window.phase(series[i]["timestamp"])
        in {WindowPhase.OPEN, WindowPhase.MANAGE_ONLY}
    ]
    window_set = set(in_window)
    samples: list[Sample] = []

    for i in range(sp.lookback_bars + 1, len(series)):
        if params.window.phase(series[i]["timestamp"]) is not WindowPhase.OPEN:
            continue
        seen = window_of(series, params, i)
        z, _mean, sigma = zscore(log_closes(seen), sp.lookback_bars)
        if z is None or sigma is None:
            continue
        sample = Sample(
            z=z,
            sigma=sigma,
            adx=wilder_adx(seen, sp.adx_period) or 0.0,
            index=i,
            weekend=_weekend_key(series[i]["timestamp"]),
        )
        entry = float(series[i]["close"])
        for horizon in HORIZONS:
            # Truncate at the window edge: live, the cutoff would have closed it.
            j = i
            for step in range(1, horizon + 1):
                if i + step >= len(series) or (i + step) not in window_set:
                    break
                j = i + step
            if j == i:
                continue
            sample.forward_bp[horizon] = (float(series[j]["close"]) / entry - 1) * 1e4
        if sample.forward_bp:
            samples.append(sample)
    return samples


def tabulate(
    samples: Sequence[Sample],
    params: WeekendParams,
    adx_split: bool = False,
) -> list[dict[str, Any]]:
    """One row per (bucket, horizon), optionally split by regime."""
    cost = params.costs.round_trip_bp
    rows: list[dict[str, Any]] = []
    regimes = (
        (("ranging", lambda s: s.adx < params.signal.adx_max),
         ("trending", lambda s: s.adx >= params.signal.adx_max))
        if adx_split
        else (("all", lambda s: True),)
    )
    for label, low, high in BUCKETS:
        for regime, keep in regimes:
            members = [s for s in samples if low < s.z <= high and keep(s)]
            if not members:
                continue
            for horizon in HORIZONS:
                usable = [s for s in members if horizon in s.forward_bp]
                cell = Cell(
                    label=label,
                    horizon_bars=horizon,
                    values_bp=[s.forward_bp[horizon] for s in usable],
                    keys=[(s.index, s.weekend) for s in usable],
                )
                if not cell.n:
                    continue
                rows.append(
                    {
                        "bucket": label,
                        "regime": regime,
                        "horizon_h": horizon / 4,
                        "n": cell.n,
                        "weekends": cell.weekends,
                        "episodes": cell.episodes,
                        "mean_bp": cell.mean_bp,
                        "median_bp": cell.median_bp,
                        "hit_rate": cell.hit_rate,
                        "net_of_costs_bp": cell.net_bp(cost),
                        "t": cell.t_stat(),
                    }
                )
    return rows


def baseline(samples: Sequence[Sample]) -> dict[float, float]:
    """Unconditional mean forward return. The number the conditional has to
    beat - being long BTC over a weekend is not an edge, it is a beta."""
    out: dict[float, float] = {}
    for horizon in HORIZONS:
        values = [s.forward_bp[horizon] for s in samples if horizon in s.forward_bp]
        if values:
            out[horizon / 4] = round(st.mean(values), 1)
    return out


#: Below these, no result is reported as evidence - whatever it says.
MIN_WEEKENDS = 8
MIN_EPISODES = 20


def _weekend_key(ts: Any) -> str:
    friday = ts - dt.timedelta(days=(ts.weekday() - 4) % 7)
    return f"{friday:%Y-%m-%d}"


def verdict(rows: Sequence[dict[str, Any]], params: WeekendParams) -> str:
    """One sentence, and it is allowed to be a negative one.

    The order of the checks matters: sample adequacy is tested BEFORE the
    number, so a spectacular result from one weekend is described as one
    weekend rather than as a spectacular result.
    """
    traded = [r for r in rows if r["bucket"].startswith(("z <= -2.5", "-2.5 < z <= -2"))]
    if not traded:
        return "No observations in the traded buckets - the sample is too short to judge."
    best = max(traded, key=lambda r: r["net_of_costs_bp"])
    weekends = best.get("weekends", 0)
    episodes = best.get("episodes", 0)

    if weekends < MIN_WEEKENDS or episodes < MIN_EPISODES:
        return (
            f"NOT ENOUGH DATA to judge. The best traded bucket ({best['bucket']} at "
            f"{best['horizon_h']:g}h) nets {best['net_of_costs_bp']:+.0f}bp - but from "
            f"{weekends} weekend(s) and {episodes} independent episode(s) "
            f"({best['n']} overlapping bars). Overlapping bars inside one dislocation "
            f"are one event counted many times; this needs at least {MIN_WEEKENDS} "
            f"weekends and {MIN_EPISODES} episodes before it means anything."
        )
    if best["net_of_costs_bp"] <= 0:
        return (
            f"No horizon pays. The best traded bucket ({best['bucket']} at "
            f"{best['horizon_h']:g}h, {episodes} episodes across {weekends} weekends) "
            f"returns {best['mean_bp']:.0f}bp gross and {best['net_of_costs_bp']:.0f}bp "
            f"after the {params.costs.round_trip_bp:.0f}bp round trip. The reversion is "
            f"real or not, but it is smaller than the fee either way."
        )
    if (best["t"] or 0) < 2:
        return (
            f"{best['bucket']} at {best['horizon_h']:g}h nets "
            f"{best['net_of_costs_bp']:.0f}bp across {weekends} weekends, but t="
            f"{best['t']} on {episodes} independent episodes. Directionally "
            f"encouraging, statistically not there - do not size on it."
        )
    return (
        f"{best['bucket']} at {best['horizon_h']:g}h nets {best['net_of_costs_bp']:.0f}bp "
        f"after costs: {episodes} independent episodes across {weekends} weekends, "
        f"t={best['t']}, hit rate {best['hit_rate']:.0%}. That is the horizon the book "
        f"should trade."
    )
