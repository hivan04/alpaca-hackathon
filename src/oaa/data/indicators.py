"""Indicators. Pure functions over bar lists - no I/O, trivially testable."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

Bar = dict[str, Any]


def closes(bars: Sequence[Bar]) -> list[float]:
    return [float(b["close"]) for b in bars if b.get("close") is not None]


def ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    k = 2 / (period + 1)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = v * k + out * (1 - k)
    return round(out, 6)


def sma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return round(sum(values[-period:]) / period, 6)


def realised_vol(bars: Sequence[Bar], lookback: int = 20, annualise: bool = True) -> float | None:
    """Close-to-close realised volatility, annualised by default."""
    px = closes(bars)
    if len(px) < lookback + 1:
        return None
    window = px[-(lookback + 1):]
    rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    vol = math.sqrt(var)
    return round(vol * math.sqrt(252) if annualise else vol, 6)


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        high, low = float(bars[i]["high"]), float(bars[i]["low"])
        prev_close = float(bars[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return round(sum(trs[-period:]) / period, 6)


def adx(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Wilder's ADX. Used to tell a trend from a chop - condors want chop,
    debit spreads want trend."""
    if len(bars) < period * 2 + 1:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, len(bars)):
        high, low = float(bars[i]["high"]), float(bars[i]["low"])
        prev_high, prev_low = float(bars[i - 1]["high"]), float(bars[i - 1]["low"])
        prev_close = float(bars[i - 1]["close"])
        up, down = high - prev_high, prev_low - low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    def smooth(seq: list[float]) -> list[float]:
        out = [sum(seq[:period])]
        for v in seq[period:]:
            out.append(out[-1] - out[-1] / period + v)
        return out

    st, sp, sm = smooth(trs), smooth(plus_dm), smooth(minus_dm)
    dx: list[float] = []
    for tr_v, p_v, m_v in zip(st, sp, sm, strict=False):
        if tr_v <= 0:
            continue
        pdi, mdi = 100 * p_v / tr_v, 100 * m_v / tr_v
        denom = pdi + mdi
        if denom > 0:
            dx.append(100 * abs(pdi - mdi) / denom)
    if len(dx) < period:
        return None
    return round(sum(dx[-period:]) / period, 4)


def trend_strength(bars: Sequence[Bar], fast: int = 8, slow: int = 21) -> float | None:
    """Signed, normalised trend read in [-1, 1].

    Sign = direction, magnitude = conviction. Combines MA separation with
    the fraction of recent closes on the right side of the slow MA.
    """
    px = closes(bars)
    fast_ma, slow_ma = ema(px, fast), ema(px, slow)
    if fast_ma is None or slow_ma is None or slow_ma == 0:
        return None
    separation = (fast_ma - slow_ma) / slow_ma
    recent = px[-slow:]
    above = sum(1 for p in recent if p > slow_ma) / len(recent)
    directional = (above - 0.5) * 2
    raw = 0.6 * math.tanh(separation * 40) + 0.4 * directional
    return round(max(-1.0, min(1.0, raw)), 4)


def iv_rank(current_iv: float | None, history: Sequence[float]) -> float | None:
    """Where today's IV sits in its own recent range, 0..1."""
    if current_iv is None or len(history) < 5:
        return None
    lo, hi = min(history), max(history)
    if hi - lo < 1e-9:
        return 0.5
    return round(max(0.0, min(1.0, (current_iv - lo) / (hi - lo))), 4)


def volume_ratio(bars: Sequence[Bar], lookback: int = 20) -> float | None:
    vols = [float(b.get("volume") or 0) for b in bars]
    if len(vols) < lookback + 1:
        return None
    avg = sum(vols[-(lookback + 1):-1]) / lookback
    return round(vols[-1] / avg, 4) if avg > 0 else None


def max_drawdown(equity: Sequence[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return round(worst, 5)


def sharpe(returns: Sequence[float], periods_per_year: int = 252) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return round(mean / sd * math.sqrt(periods_per_year), 4)
