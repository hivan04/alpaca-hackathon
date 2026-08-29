"""The implied-volatility model.

`vol_carry` is an IV-rank and IV-RV-spread strategy. Both inputs come from the
option chain, and Alpaca serves no historical chain - so in replay they have to
be modelled, and how they are modelled decides whether the backtest means
anything at all.

The naive version is `IV = k x RV`. It is worthless: the IV-RV spread is then a
constant, so the premium gate either always passes or never does, and the
backtest measures nothing but the constant.

What this does instead reproduces the two properties that actually generate the
signal:

  IV is sticky.        Implied vol is anchored to a slow EWMA of realised vol,
                       not to the trailing 20 days. So when realised vol
                       collapses, IV lags down and the IV-RV spread WIDENS -
                       which is exactly the state the carry book wants to sell.
                       When vol spikes, RV jumps past the anchor and the spread
                       goes NEGATIVE, standing the strategy down into the move.
                       That single asymmetry is most of the strategy's edge and
                       most of its risk, and a constant multiple erases it.

  IV is systematic.    A single name's implied vol moves with the market's. The
                       anchor is scaled by the market's own vol level relative
                       to its trailing median, so a market-wide vol event lifts
                       every name's IV at once, and the macro gate's
                       "shared or idiosyncratic" question has something real to
                       chew on.

IV rank is then the percentile of the modelled IV within its own trailing year,
which is the same definition a live feed would use.

Limitations, stated plainly because the deck has to state them:
  * there is no volatility risk premium *surprise* here - no earnings crush, no
    event repricing that realised vol never explains
  * the term structure is a fixed mild upward slope (see `chain.py`), so a
    genuinely inverted curve never appears
  * the model cannot be validated against historical option prices, because
    obtaining those is the problem it exists to work around

Consequence: a backtest run through this is evidence that the LOGIC fires when
intended and stays quiet otherwise. It is not evidence of edge. The judged
number is live paper P&L.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from oaa.data.indicators import iv_rank as iv_rank_of, vol_estimator


@dataclass
class IVModel:
    """Turns a daily bar history into an ATM implied-vol and IV-rank series."""

    #: variance risk premium: implied sits above the realised-vol anchor
    vrp_multiple: float = 1.13
    #: halflife in trading days of the EWMA that IV is anchored to
    anchor_halflife: float = 45.0
    #: realised-vol lookback the strategy's RV reading uses
    rv_lookback: int = 20
    #: how strongly a name's IV follows the market's vol level
    market_beta: float = 0.45
    #: window for the IV-rank percentile
    rank_lookback: int = 252
    floor: float = 0.05
    cap: float = 1.75
    #: "garman_klass" (default) or "close_to_close" - see DataConfig
    estimator: str = "garman_klass"

    # ------------------------------------------------------------------ #
    def rv_series(self, bars: list[dict[str, Any]]) -> list[float | None]:
        """Trailing realised vol at every bar, using only prior data."""
        out: list[float | None] = []
        for i in range(len(bars)):
            window = bars[max(0, i - self.rv_lookback * 2) : i + 1]
            measure = vol_estimator(self.estimator)
            out.append(measure(window, self.rv_lookback) if len(window) > 2 else None)
        return out

    def anchor_series(self, rv: list[float | None]) -> list[float | None]:
        """EWMA of realised vol - the sticky level implied vol hangs off."""
        alpha = 1.0 - math.exp(math.log(0.5) / max(1.0, self.anchor_halflife))
        out: list[float | None] = []
        state: float | None = None
        for value in rv:
            if value is None:
                out.append(state)
                continue
            state = value if state is None else state + alpha * (value - state)
            out.append(state)
        return out

    # ------------------------------------------------------------------ #
    def market_factor(self, market_bars: list[dict[str, Any]] | None) -> list[float]:
        """Market vol level relative to its own trailing median, per bar."""
        if not market_bars:
            return []
        rv = self.rv_series(market_bars)
        out: list[float] = []
        for i, value in enumerate(rv):
            history = [v for v in rv[max(0, i - self.rank_lookback) : i + 1] if v]
            if not value or not history:
                out.append(1.0)
                continue
            median = sorted(history)[len(history) // 2]
            out.append(value / median if median > 0 else 1.0)
        return out

    # ------------------------------------------------------------------ #
    def build(
        self,
        bars: list[dict[str, Any]],
        market_factor: list[float] | None = None,
    ) -> dict[str, list[float | None]]:
        """Return aligned rv / iv / iv_rank series, one entry per bar.

        Index i uses bars[0..i] only. No lookahead anywhere in here - that is
        the whole reason it is computed as a series rather than per call.
        """
        rv = self.rv_series(bars)
        anchor = self.anchor_series(rv)
        factor = market_factor or []

        iv: list[float | None] = []
        for i, anchored in enumerate(anchor):
            if anchored is None:
                iv.append(None)
                continue
            mkt = factor[i] if i < len(factor) else 1.0
            value = self.vrp_multiple * anchored * (max(0.2, mkt) ** self.market_beta)
            iv.append(round(max(self.floor, min(self.cap, value)), 4))

        rank: list[float | None] = []
        for i, value in enumerate(iv):
            if value is None:
                rank.append(None)
                continue
            history = [v for v in iv[max(0, i - self.rank_lookback) : i + 1] if v is not None]
            # Shared with the live providers - see oaa.data.indicators.iv_rank.
            # Replay and live must not compute different numbers under one name.
            rank.append(iv_rank_of(value, history))

        return {"rv": rv, "iv": iv, "iv_rank": rank, "anchor": anchor}

    def describe(self) -> dict[str, Any]:
        return {
            "vrp_multiple": self.vrp_multiple,
            "anchor_halflife_days": self.anchor_halflife,
            "rv_lookback": self.rv_lookback,
            "market_beta": self.market_beta,
            "rank_lookback": self.rank_lookback,
            "estimator": self.estimator,
        }
