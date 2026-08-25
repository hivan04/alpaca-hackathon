"""The design matrix for the overnight gap model.

Target
------
The pair's overnight return per $1 of long-leg notional:

    w  = beta * close_x / close_y          (dollar hedge weight)
    r  = (open_y / close_y - 1) - w * (open_x / close_x - 1)

That is the P&L of a dollar-neutral long-y / short-x pair held from the close
to the next open, which is exactly what the strategy earns or loses.

Features are all knowable at 15:45 ET. Nothing here peeks at the open.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from typing import Any

import numpy as np

FEATURES: tuple[str, ...] = (
    "zscore",
    "zscore_abs",
    "zscore_change",
    "beta",
    "beta_change",
    "spread_norm",
    "vol_y",
    "vol_x",
    "vol_spread",
    "corr_20",
    "intraday_ret_y",
    "intraday_ret_x",
    "intraday_ret_spread",
    "prev_gap",
    "gap_mean_10",
    "gap_std_10",
    "range_y",
    "range_x",
    "volume_ratio_y",
    "volume_ratio_x",
    "dow",
    "is_month_end",
)


def feature_names() -> list[str]:
    return list(FEATURES)


def _safe(value: float | None, default: float = 0.0) -> float:
    if value is None:
        return default
    v = float(value)
    return default if (math.isnan(v) or math.isinf(v)) else v


def _returns(closes: Sequence[float]) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    if arr.size < 2:
        return np.zeros(0)
    return np.diff(arr) / arr[:-1]


def _vol(closes: Sequence[float], window: int = 20) -> float:
    rets = _returns(closes)
    if rets.size < 2:
        return 0.0
    window_rets = rets[-window:]
    return float(np.std(window_rets, ddof=1) * math.sqrt(252)) if window_rets.size > 1 else 0.0


def overnight_gap_return(
    close_y: float,
    open_y: float,
    close_x: float,
    open_x: float,
    beta: float,
) -> float:
    """The realised target. Also used by the backtest to score a night."""
    if close_y <= 0 or close_x <= 0:
        return 0.0
    weight = beta * close_x / close_y
    return (open_y / close_y - 1.0) - weight * (open_x / close_x - 1.0)


def build_features(
    *,
    zscore: float,
    prev_zscore: float | None,
    beta: float,
    prev_beta: float | None,
    spread: float,
    spread_std: float,
    bars_y: Sequence[dict[str, Any]],
    bars_x: Sequence[dict[str, Any]],
    recent_gaps: Sequence[float] = (),
    asof: dt.date | None = None,
) -> dict[str, float]:
    """One row of the design matrix.

    `bars_y` / `bars_x` are daily OHLCV dicts, oldest first, with today's bar
    last. At 15:45 today's bar is still forming, which is fine — every feature
    that uses it uses only information already printed.
    """
    day = asof or dt.date.today()
    closes_y = [float(b["close"]) for b in bars_y]
    closes_x = [float(b["close"]) for b in bars_x]

    today_y = bars_y[-1] if bars_y else {}
    today_x = bars_x[-1] if bars_x else {}

    def intraday(bar: dict[str, Any]) -> float:
        open_px, close_px = _safe(bar.get("open")), _safe(bar.get("close"))
        return (close_px / open_px - 1.0) if open_px > 0 else 0.0

    def day_range(bar: dict[str, Any]) -> float:
        high, low, close_px = _safe(bar.get("high")), _safe(bar.get("low")), _safe(bar.get("close"))
        return ((high - low) / close_px) if close_px > 0 else 0.0

    def vol_ratio(bars: Sequence[dict[str, Any]], window: int = 20) -> float:
        volumes = [_safe(b.get("volume")) for b in bars]
        if len(volumes) < window + 1:
            return 1.0
        baseline = float(np.mean(volumes[-(window + 1) : -1]))
        return float(volumes[-1] / baseline) if baseline > 0 else 1.0

    # Spread series, for its own volatility.
    span = min(len(closes_y), len(closes_x), 60)
    if span >= 3:
        y_arr = np.asarray(closes_y[-span:], dtype=float)
        x_arr = np.asarray(closes_x[-span:], dtype=float)
        spread_series = y_arr - beta * x_arr
        base = np.abs(y_arr[:-1])
        spread_rets = np.diff(spread_series) / np.where(base > 0, base, 1.0)
        vol_spread = float(np.std(spread_rets, ddof=1) * math.sqrt(252)) if spread_rets.size > 1 else 0.0
        corr = float(np.corrcoef(y_arr, x_arr)[0, 1]) if span >= 5 else 0.0
    else:
        vol_spread, corr = 0.0, 0.0

    gaps = np.asarray(list(recent_gaps)[-10:], dtype=float)

    row = {
        "zscore": _safe(zscore),
        "zscore_abs": abs(_safe(zscore)),
        "zscore_change": _safe(zscore) - _safe(prev_zscore, _safe(zscore)),
        "beta": _safe(beta, 1.0),
        "beta_change": _safe(beta, 1.0) - _safe(prev_beta, _safe(beta, 1.0)),
        "spread_norm": _safe(spread) / spread_std if spread_std > 1e-12 else 0.0,
        "vol_y": _vol(closes_y),
        "vol_x": _vol(closes_x),
        "vol_spread": vol_spread,
        "corr_20": _safe(corr),
        "intraday_ret_y": intraday(today_y),
        "intraday_ret_x": intraday(today_x),
        "intraday_ret_spread": intraday(today_y) - intraday(today_x),
        "prev_gap": float(gaps[-1]) if gaps.size else 0.0,
        "gap_mean_10": float(np.mean(gaps)) if gaps.size else 0.0,
        "gap_std_10": float(np.std(gaps, ddof=1)) if gaps.size > 1 else 0.0,
        "range_y": day_range(today_y),
        "range_x": day_range(today_x),
        "volume_ratio_y": vol_ratio(bars_y),
        "volume_ratio_x": vol_ratio(bars_x),
        "dow": float(day.weekday()),
        "is_month_end": 1.0 if _is_month_end(day) else 0.0,
    }
    return {name: _safe(row.get(name)) for name in FEATURES}


def _is_month_end(day: dt.date) -> bool:
    """True on the last three business days of the month.

    Month-end rebalancing flows are a well-documented driver of overnight
    dislocation, so the model gets to see it rather than being surprised.
    """
    next_month = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    last_day = next_month - dt.timedelta(days=1)
    business_days = 0
    cursor = day
    while cursor <= last_day:
        if cursor.weekday() < 5:
            business_days += 1
        cursor += dt.timedelta(days=1)
    return business_days <= 3


def to_matrix(rows: Sequence[dict[str, float]]) -> np.ndarray:
    return np.asarray([[row.get(name, 0.0) for name in FEATURES] for row in rows], dtype=float)
