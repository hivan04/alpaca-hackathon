"""The technical layer: three indicators, three different jobs.

The point of this module is that the events book should not be a pure bet on
what a language model read overnight. Price has an opinion too, and the two
disagreeing is information. So the LLM proposes a direction and this decides
whether the tape is set up to express it - and, if so, how large.

Each indicator does exactly one job and is not allowed to drift into another:

  * **Bollinger bands - the setup.** Width, not position. A squeeze means the
    bands have compressed into the bottom of their own recent range: realised
    movement has coiled while a dated catalyst approaches. That is the setup
    this book wants. It is deliberately NOT a direction read - bands say
    nothing about which way the spring unwinds, and the moment a width
    measurement is used to pick a side it has stopped measuring volatility.

  * **RSI - the veto.** One-sided, and only at extremes. A short into a market
    already at RSI 18 is a short into whatever bounce is coming; a long at RSI
    85 is the same trade in reverse. RSI at 60 blocks nothing and should not:
    it is a yellow light on exhaustion, never a green one on entry.

  * **ATR - the risk manager.** Never an entry gate. It cannot know whether to
    trade, only how much and how far away the stop belongs. Two uses: a stop
    at `atr_stop_multiple` x ATR on the UNDERLYING, wide enough that ordinary
    post-print noise does not close the position; and a size multiplier, so a
    name whose daily range has already doubled is not taken in the same size as
    a quiet one.

## The honest limit on the ATR stop

This book holds one position across one night. There is no cycle running
between the 15:45 arm and the 09:45 exit, so a stop level cannot be *watched*
overnight - the gap through it is exactly the risk the position was opened to
take. What the stop actually does is govern the morning: if the underlying has
gone through the level, the position closes at 09:45 without waiting for a
target. It is a discipline on the exit, not protection against the gap. The
protection against the gap is the structure: a vertical debit spread cannot
lose more than the debit, whatever the print does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oaa.core.logging import get_logger
from oaa.data.indicators import (
    atr,
    bollinger,
    bollinger_width,
    bollinger_width_series,
    closes,
    rsi,
    width_percentile,
)
from oaa.strategies.events.params import TechnicalParams

log = get_logger("strategies.events.technicals")


@dataclass
class TechnicalRead:
    """What the tape says about expressing this direction, on this name, now."""

    symbol: str
    squeeze: bool = False
    width: float | None = None
    width_percentile: float | None = None
    rsi: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    band_position: float | None = None
    #: Stop level on the UNDERLYING, `atr_stop_multiple` x ATR against the trade.
    stop_underlying: float | None = None
    #: Multiplies the confidence-derived position size. 1.0 = no adjustment.
    size_multiple: float = 1.0
    veto: str = ""
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []

    @property
    def ok(self) -> bool:
        return not self.veto

    def summary(self) -> str:
        if self.veto:
            return f"{self.symbol}: technicals veto - {self.veto}"
        bits = []
        if self.width_percentile is not None:
            bits.append(f"band width at the {self.width_percentile:.0%} percentile")
        if self.rsi is not None:
            bits.append(f"RSI {self.rsi:.0f}")
        if self.atr_pct is not None:
            bits.append(f"ATR {self.atr_pct:.2%} of spot")
        return f"{self.symbol}: {'squeeze' if self.squeeze else 'no squeeze'}, " + ", ".join(bits)

    def as_meta(self) -> dict[str, Any]:
        return {
            "ta_squeeze": self.squeeze,
            "ta_band_width": self.width,
            "ta_width_percentile": self.width_percentile,
            "ta_rsi": self.rsi,
            "ta_atr": self.atr,
            "ta_atr_pct": self.atr_pct,
            "ta_stop_underlying": self.stop_underlying,
            "ta_size_multiple": self.size_multiple,
        }


def evaluate(
    symbol: str,
    bars: list[dict[str, Any]],
    spot: float,
    bullish: bool,
    params: TechnicalParams,
) -> TechnicalRead:
    """Read the tape for one name. Never raises - missing data is a veto."""
    read = TechnicalRead(symbol=symbol)
    if not params.enabled:
        read.notes.append("technical layer disabled in config")
        return read

    rows = list(bars or [])[-params.bars_lookback :]
    needed = max(params.bollinger_period + 10, params.rsi_period + 1, params.atr_period + 1)
    if len(rows) < needed:
        # A veto, not a pass. The whole point of this layer is that the LLM is
        # not the only vote; degrading to "no data, trade anyway" would quietly
        # restore exactly the behaviour it was added to prevent.
        read.veto = f"only {len(rows)} daily bars, need {needed} for the indicator stack"
        return read

    values = closes(rows)

    # -- Bollinger: the setup ------------------------------------------- #
    read.width = bollinger_width(values, params.bollinger_period, params.bollinger_std)
    widths = bollinger_width_series(values, params.bollinger_period, params.bollinger_std)
    read.width_percentile = width_percentile(widths, params.width_lookback)
    if read.width_percentile is not None:
        read.squeeze = read.width_percentile <= params.squeeze_max_percentile
    middle, upper, lower = bollinger(values, params.bollinger_period, params.bollinger_std)
    if middle is not None and upper is not None and upper > lower:
        read.band_position = round((spot - lower) / (upper - lower), 4)

    if params.require_squeeze and not read.squeeze:
        pct = f"{read.width_percentile:.0%}" if read.width_percentile is not None else "unknown"
        read.veto = (
            f"no squeeze - band width sits at the {pct} percentile of its own "
            f"range, above the {params.squeeze_max_percentile:.0%} ceiling. "
            "Volatility is not coiled, so there is no spring to unwind."
        )
        return read

    # -- RSI: the veto, one-sided and only at extremes ------------------- #
    read.rsi = rsi(values, params.rsi_period)
    if read.rsi is not None:
        if not bullish and read.rsi <= params.rsi_oversold:
            read.veto = (
                f"RSI {read.rsi:.0f} is already below {params.rsi_oversold} - "
                "shorting into exhaustion, where the likeliest next move is the "
                "bounce rather than the continuation"
            )
            return read
        if bullish and read.rsi >= params.rsi_overbought:
            read.veto = (
                f"RSI {read.rsi:.0f} is already above {params.rsi_overbought} - "
                "buying into exhaustion"
            )
            return read

    # -- ATR: size and stop, never entry --------------------------------- #
    read.atr = atr(rows, params.atr_period)
    if read.atr and spot > 0:
        read.atr_pct = round(read.atr / spot, 6)
        distance = params.atr_stop_multiple * read.atr
        read.stop_underlying = round(spot - distance if bullish else spot + distance, 4)
        read.size_multiple = _atr_size_multiple(read.atr_pct, params)
        read.notes.append(
            f"stop {params.atr_stop_multiple:g}x ATR away at "
            f"{read.stop_underlying:.2f}; size x{read.size_multiple:.2f}"
        )
    else:
        # No ATR means no stop and no size adjustment - and this book is not
        # willing to size a position it cannot place a stop behind.
        read.veto = "no ATR available - cannot place a stop or size the position"
        return read

    log.info(read.summary())
    return read


def _atr_size_multiple(atr_pct: float, params: TechnicalParams) -> float:
    """Quieter than reference -> full size; noisier -> proportionally smaller.

    Capped at 1.0 deliberately: a calm tape is not a reason to take MORE than
    the confidence justified, only a reason not to take less.
    """
    if atr_pct <= 0:
        return params.atr_min_size_multiple
    ratio = params.atr_reference_pct / atr_pct
    return round(min(1.0, max(params.atr_min_size_multiple, ratio)), 4)


def stop_breached(stop_underlying: float | None, spot: float, bullish: bool) -> bool:
    """Has the underlying gone through the ATR stop?

    Checked at the morning exit, not overnight - see the module docstring.
    """
    if stop_underlying is None:
        return False
    return spot <= stop_underlying if bullish else spot >= stop_underlying
