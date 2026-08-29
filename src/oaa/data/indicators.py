"""Indicators. Pure functions over bar lists - no I/O, trivially testable."""

from __future__ import annotations

import datetime as dt_module
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


def garman_klass_vol(
    bars: Sequence[Bar], lookback: int = 20, annualise: bool = True
) -> float | None:
    """Realised volatility from the daily range, not the closing print.

    Why this exists: the free Alpaca feed is IEX, which is roughly 2% of the
    consolidated tape, and its daily "close" is the last IEX trade rather than
    the official closing auction. That injects noise straight into a
    close-to-close estimate. Measured on our own universe, close-to-close
    volatility runs 1.2-1.45x the Garman-Klass estimate from the same bars -
    on MSFT, 30.4% against 21.0%, about nine volatility points of pure
    microstructure noise.

    That is not a cosmetic difference. `vol_carry` gates on IV - RV >= 3%, so an
    RV inflated by noise makes rich premium look fairly priced and vetoes
    trades that were never actually marginal. MSFT was rejected on a -19.7%
    IV-RV spread; most of that was the estimator, not the market.

    Garman-Klass uses the open, high, low and close of each bar, so a single
    bad closing print moves it far less. It also has ~7x the efficiency of a
    close-to-close estimator at the same sample size, which matters on a
    20-day lookback.

        sigma^2 = 0.5 * ln(H/L)^2 - (2 ln2 - 1) * ln(C/O)^2

    Still an estimator over a thin feed - the honest fix is the SIP feed - but
    it is strictly better than close-to-close on the data we have.
    """
    if len(bars) < lookback:
        return None
    window = list(bars)[-lookback:]
    terms: list[float] = []
    for bar in window:
        high, low = float(bar.get("high", 0)), float(bar.get("low", 0))
        close, open_ = float(bar.get("close", 0)), float(bar.get("open", 0))
        if min(high, low, close, open_) <= 0 or low > high:
            continue
        hl = math.log(high / low)
        co = math.log(close / open_)
        terms.append(0.5 * hl * hl - (2 * math.log(2) - 1) * co * co)
    if len(terms) < 2:
        return None
    variance = sum(terms) / len(terms)
    if variance <= 0:
        # Garman-Klass can go negative on a bar that closed outside its own
        # range - a broken print. Fall back rather than return a nan.
        return realised_vol(bars, lookback, annualise)
    vol = math.sqrt(variance)
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


#: Observations required before an IV rank means anything. Below this the
#: answer is None - an unmeasurable rank is a missing input, not 0.5, and the
#: strategy decides for itself what a missing input costs.
IV_RANK_MIN_OBSERVATIONS = 20


def iv_rank(
    current_iv: float | None,
    history: Sequence[float],
    min_observations: int = IV_RANK_MIN_OBSERVATIONS,
) -> float | None:
    """Percentile of today's IV within its own trailing history, 0..1.

    ONE definition, shared by the live providers and by the replay's `IVModel`.
    They used to differ: replay ranked one observation per SESSION against a
    trailing year as a percentile, while live min-max scaled an in-memory list
    of intraday polls. Same name on the gate, two different numbers, and
    `premium_gate.iv_rank_min` sat on top of it deciding whether the carry book
    traded at all.

    Percentile rather than min-max on purpose: under min-max a single vol spike
    anywhere in the window pins every later reading near zero, so the book
    stands down for a year because of one bad afternoon.
    """
    if current_iv is None:
        return None
    values = [float(v) for v in history if v is not None]
    if len(values) < min_observations:
        return None
    below = sum(1 for v in values if v <= float(current_iv))
    return round(max(0.0, min(1.0, below / len(values))), 4)


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


# --------------------------------------------------------------------------- #
# Intraday indicators
# --------------------------------------------------------------------------- #
# The intraday book combines three indicators by giving each a DIFFERENT
# question to answer, not by having all three vote on direction. RSI and
# Bollinger *position* are mean-reversion measures and would contradict a VWAP
# momentum trigger on every single signal - price breaking above VWAP is
# simultaneously "momentum, buy" and "overbought, fade". A naive consensus gate
# across all three either never fires or fires incoherently.
#
#   VWAP              trigger   which direction, and is now the moment?
#   Bollinger WIDTH   filter    is this a real move or is it chop?
#   RSI               veto      has it already run too far?
#
# Only VWAP has an opinion on direction, so the other two cannot conflict with
# it. Band *width* is used rather than band position precisely because width is
# a volatility-regime measurement and is direction-agnostic.
# --------------------------------------------------------------------------- #
def vwap(bars: Sequence[Bar], session_only: bool = True) -> float | None:
    """Volume-weighted average price over the bars supplied.

    `session_only` keeps only bars from the last calendar date present, which is
    what "session VWAP" means - a VWAP that bleeds across days anchors to
    yesterday's value area and is not the level anyone is trading against.
    """
    rows = list(bars)
    if not rows:
        return None
    if session_only:
        last_day = _bar_date(rows[-1])
        if last_day is not None:
            rows = [b for b in rows if _bar_date(b) == last_day]
    numerator = 0.0
    denominator = 0.0
    for bar in rows:
        volume = float(bar.get("volume") or 0.0)
        if volume <= 0:
            continue
        high = float(bar.get("high", bar.get("close", 0.0)) or 0.0)
        low = float(bar.get("low", bar.get("close", 0.0)) or 0.0)
        close = float(bar.get("close", 0.0) or 0.0)
        typical = (high + low + close) / 3 if high and low else close
        numerator += typical * volume
        denominator += volume
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def vwap_series(bars: Sequence[Bar]) -> list[float]:
    """Running session VWAP, one value per bar. Needed to detect a *cross*."""
    out: list[float] = []
    num = 0.0
    den = 0.0
    current_day = None
    for bar in bars:
        day = _bar_date(bar)
        if day != current_day:
            current_day, num, den = day, 0.0, 0.0
        volume = float(bar.get("volume") or 0.0)
        high = float(bar.get("high", bar.get("close", 0.0)) or 0.0)
        low = float(bar.get("low", bar.get("close", 0.0)) or 0.0)
        close = float(bar.get("close", 0.0) or 0.0)
        typical = (high + low + close) / 3 if high and low else close
        num += typical * max(volume, 0.0)
        den += max(volume, 0.0)
        out.append(round(num / den, 6) if den > 0 else close)
    return out


def bollinger(values: Sequence[float], period: int = 20, num_std: float = 2.0):
    """(middle, upper, lower). Returns (None, None, None) when short of data."""
    if len(values) < period or period <= 1:
        return None, None, None
    window = list(values[-period:])
    middle = sum(window) / period
    var = sum((v - middle) ** 2 for v in window) / (period - 1)
    sd = math.sqrt(var)
    return round(middle, 6), round(middle + num_std * sd, 6), round(middle - num_std * sd, 6)


def bollinger_width(
    values: Sequence[float], period: int = 20, num_std: float = 2.0
) -> float | None:
    """(upper - lower) / middle. A volatility-regime read, not a direction read."""
    middle, upper, lower = bollinger(values, period, num_std)
    if middle is None or not middle:
        return None
    return round((upper - lower) / middle, 6)


def bollinger_width_series(
    values: Sequence[float], period: int = 20, num_std: float = 2.0
) -> list[float]:
    out: list[float] = []
    for end in range(period, len(values) + 1):
        width = bollinger_width(values[:end], period, num_std)
        if width is not None:
            out.append(width)
    return out


def width_is_rising(widths: Sequence[float], lookback: int = 6) -> bool:
    """Expanding bands mean volatility is genuinely picking up. Contracting
    bands during a VWAP cross mean chop, and the trade is rejected."""
    if len(widths) < lookback + 1:
        return False
    return widths[-1] > widths[-(lookback + 1)]


def width_percentile(widths: Sequence[float], lookback: int = 100) -> float | None:
    """Where the current band width sits in its own recent distribution, 0..1.

    A breakout from compressed volatility (a prior 'squeeze') is the
    best-established version of this setup, so the strategy can optionally
    require the width to have been below its Nth percentile first.
    """
    window = list(widths[-lookback:])
    if len(window) < 10:
        return None
    current = window[-1]
    below = sum(1 for w in window if w < current)
    return round(below / len(window), 4)


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI.

    Used ONE-SIDED and only at extremes: a veto on exhaustion, never an entry
    signal. RSI at 65 blocks nothing.
    """
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 4)


def resample(bars: Sequence[Bar], minutes: int = 60) -> list[Bar]:
    """Aggregate bars into fixed intraday buckets, e.g. 1-minute into 1-hour.

    Buckets are anchored to the clock hour rather than to the first bar, so the
    result is the same whether the window starts at 09:30 or 10:07 - two runs
    over the same tape must not produce different bars. The final bucket is
    included even though it is still forming: it is the one carrying the
    current price, and dropping it would mean acting on an hour-old close.
    """
    grouped: dict[tuple[Any, int], list[Bar]] = {}
    order: list[tuple[Any, int]] = []
    for bar in bars:
        stamp = bar.get("timestamp")
        if not isinstance(stamp, dt_module.datetime):
            continue
        slot = (stamp.hour * 60 + stamp.minute) // minutes
        key = (stamp.date(), slot)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(bar)

    out: list[Bar] = []
    for key in order:
        rows = grouped[key]
        highs = [float(b["high"]) for b in rows if b.get("high") is not None]
        lows = [float(b["low"]) for b in rows if b.get("low") is not None]
        out.append({
            "timestamp": rows[0]["timestamp"],
            "open": float(rows[0]["open"]),
            "high": max(highs) if highs else float(rows[0]["open"]),
            "low": min(lows) if lows else float(rows[0]["open"]),
            "close": float(rows[-1]["close"]),
            "volume": sum(float(b.get("volume") or 0.0) for b in rows),
        })
    return out


def time_bucket(bar: Bar, minutes: int = 30) -> str:
    """Label a bar by its half-hour slot, e.g. '09:30'.

    Volume confirmation has to be bucketed by time of day: 09:45 volume is not
    comparable to 12:30 volume, and a flat daily average makes the lunchtime
    tape look permanently dead and the open look permanently explosive.
    """
    stamp = bar.get("timestamp")
    if not isinstance(stamp, dt_module.datetime):
        return "unknown"
    slot = (stamp.minute // minutes) * minutes
    return f"{stamp.hour:02d}:{slot:02d}"


def volume_zscore_by_bucket(
    bars: Sequence[Bar], bucket_minutes: int = 30, min_samples: int = 3
) -> float | None:
    """z-score of the latest bar's volume against the same time-of-day bucket."""
    rows = list(bars)
    if not rows:
        return None
    target = time_bucket(rows[-1], bucket_minutes)
    if target == "unknown":
        return volume_ratio(rows)
    latest_day = _bar_date(rows[-1])
    history = [
        float(b.get("volume") or 0.0)
        for b in rows[:-1]
        if time_bucket(b, bucket_minutes) == target and _bar_date(b) != latest_day
    ]
    if len(history) < min_samples:
        return None
    mean = sum(history) / len(history)
    var = sum((v - mean) ** 2 for v in history) / max(1, len(history) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    return round((float(rows[-1].get("volume") or 0.0) - mean) / sd, 4)


def persistence(values: Sequence[float], reference: Sequence[float], bars: int = 2) -> int:
    """How many of the last `bars` closes sat on the same side of `reference`.

    Rejects the single-bar spike that immediately reverts - the dominant false
    positive in any VWAP-cross system.
    """
    if len(values) < bars or len(reference) < bars:
        return 0
    tail_v, tail_r = values[-bars:], reference[-bars:]
    above = sum(1 for v, r in zip(tail_v, tail_r, strict=False) if v > r)
    below = sum(1 for v, r in zip(tail_v, tail_r, strict=False) if v < r)
    return above if above >= below else -below


def crossed(values: Sequence[float], reference: Sequence[float], lookback: int = 1) -> int:
    """+1 if the series crossed above the reference, -1 below, 0 otherwise.

    `lookback` is how many recent bars are searched for the cross. On a 5-minute
    bar a cycle can easily land one or two bars after the event; insisting the
    cross happened on the very last bar would discard most real signals for a
    reason that is an artefact of the polling interval, not of the market.
    Keep it small - the signal decays in minutes.
    """
    n = min(len(values), len(reference))
    if n < 2:
        return 0
    for offset in range(1, min(lookback, n - 1) + 1):
        prev = values[-offset - 1] - reference[-offset - 1]
        now = values[-offset] - reference[-offset]
        if prev <= 0 < now:
            return 1
        if prev >= 0 > now:
            return -1
    return 0


def _bar_date(bar: Bar):
    stamp = bar.get("timestamp")
    return stamp.date() if isinstance(stamp, dt_module.datetime) else None


def vol_estimator(name: str = "garman_klass"):
    """Resolve the configured realised-volatility estimator.

    One place, so the backtest and the live agent cannot silently disagree
    about what "realised vol" means - which would make every IV-RV comparison
    between them meaningless.
    """
    return garman_klass_vol if name == "garman_klass" else realised_vol
