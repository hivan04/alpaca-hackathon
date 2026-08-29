"""The volatility screen: is this print worth a position at all?

For each confirmed event the screen prices the ATM straddle in the expiry that
CONTAINS the print, and sets that implied move against what the stock has
actually done on its last four reports.

    ratio = implied move / mean absolute realised move

Above 1, the options are charging more than the recent prints have paid; below
1, less. The screen does not pick a side - it ranks by how far the market's
price sits from the stock's own history, in either direction, because both a
rich and a cheap event are tradeable and a fairly priced one is not.

What it is not
--------------
Four quarters is four observations. One outlier moves the mean by several
points - MDB's +37.96% carries a quarter of its own average. The ratio is a
ranking device for deciding where to spend the LLM's attention and the night's
capital, not an edge estimate. The gate that removes the most candidates is
`max_relative_spread`, and it should be: a vertical crosses four half-spreads
on a round trip, and on a thin weekly that is most of the debit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from oaa.core.logging import get_logger
from oaa.core.types import MarketContext, Right
from oaa.options.chain import ChainView
from oaa.strategies.events.calendar import EarningsEvent
from oaa.strategies.events.params import ScreenParams

log = get_logger("strategies.events.volscreen")


@dataclass
class VolRead:
    """One name's screen result."""

    symbol: str
    event: EarningsEvent
    spot: float
    expiry: dt.date | None = None
    implied_move_pct: float | None = None
    realised_mean_abs_pct: float | None = None
    ratio: float | None = None
    straddle_price: float | None = None
    relative_spread: float | None = None
    rejected: str = ""

    @property
    def ok(self) -> bool:
        return not self.rejected and self.implied_move_pct is not None

    @property
    def divergence(self) -> float:
        """How far the option's price sits from the stock's own history."""
        if self.ratio is None:
            return 0.0
        return abs(self.ratio - 1.0)

    def summary(self) -> str:
        if self.rejected:
            return f"{self.symbol}: rejected - {self.rejected}"
        realised = f"{self.realised_mean_abs_pct:.2f}%" if self.realised_mean_abs_pct else "n/a"
        ratio = f"{self.ratio:.2f}x" if self.ratio else "n/a"
        return (
            f"{self.symbol}: implied {self.implied_move_pct:.2f}% vs realised "
            f"{realised} ({ratio}), spread {self.relative_spread:.1%} of debit"
            if self.relative_spread is not None
            else f"{self.symbol}: implied {self.implied_move_pct:.2f}% vs realised {realised}"
        )


def implied_move(view: ChainView, spot: float, expiry: dt.date) -> tuple[float, float, float] | None:
    """ATM straddle as a fraction of spot, plus the straddle's price and width.

    Returns (implied_move_pct, straddle_mid, straddle_spread) or None when the
    expiry has no usable two-sided ATM quote. Using the straddle rather than an
    IV number is deliberate: it is the price actually payable, it needs no
    assumption about days-to-expiry or rate, and on a one-week option the two
    answers diverge sharply.
    """
    try:
        call = view.atm(expiry, Right.CALL)
        put = view.atm(expiry, Right.PUT)
    except Exception:  # noqa: BLE001 - a missing strike is a data condition
        return None
    mids, spreads = [], []
    for quote in (call, put):
        if quote.bid is None or quote.ask is None or quote.ask <= 0:
            return None
        mids.append((quote.bid + quote.ask) / 2)
        spreads.append(quote.ask - quote.bid)
    straddle = sum(mids)
    if straddle <= 0 or spot <= 0:
        return None
    return round(straddle / spot * 100, 3), round(straddle, 4), round(sum(spreads), 4)


def screen_one(
    event: EarningsEvent,
    market: MarketContext,
    view: ChainView,
    params: ScreenParams,
) -> VolRead:
    """Price one event. Every rejection carries a reason for the log."""
    read = VolRead(symbol=event.symbol, event=event, spot=market.spot)

    # The expiry must contain the print. An expiry that lands BEFORE the
    # reaction session prices no event at all - it is the single most
    # expensive way to be right about a direction and lose money anyway.
    candidates = [e for e in view.expiries() if e >= event.exit_date]
    if not candidates:
        read.rejected = f"no expiry on or after the reaction date {event.exit_date}"
        return read
    read.expiry = min(candidates)

    priced = implied_move(view, market.spot, read.expiry)
    if priced is None:
        read.rejected = f"no two-sided ATM quote in {read.expiry}"
        return read
    read.implied_move_pct, read.straddle_price, straddle_spread = priced

    if read.implied_move_pct < params.min_implied_move_pct:
        read.rejected = (
            f"implied move {read.implied_move_pct:.2f}% below the "
            f"{params.min_implied_move_pct:.2f}% floor - no event priced in"
        )
        return read
    if read.straddle_price / 2 < params.min_option_price:
        read.rejected = f"ATM options priced at {read.straddle_price / 2:.2f} - too thin to trade"
        return read

    # A vertical is half a straddle's legs, but crosses its spreads four times
    # on a round trip. Measuring the straddle's own width against its price is
    # the cheapest honest proxy available before strikes are chosen.
    read.relative_spread = round(straddle_spread * 2 / read.straddle_price, 4)
    if read.relative_spread > params.max_relative_spread:
        read.rejected = (
            f"round-trip spread {read.relative_spread:.1%} of premium exceeds the "
            f"{params.max_relative_spread:.0%} ceiling - the edge is paid to the market maker"
        )
        return read

    read.realised_mean_abs_pct = (
        event.mean_abs_history * 1.0 if event.mean_abs_history is not None else None
    )
    if read.realised_mean_abs_pct:
        read.ratio = round(read.implied_move_pct / read.realised_mean_abs_pct, 3)
    return read


def rank(reads: list[VolRead], params: ScreenParams) -> list[VolRead]:
    """Top N by divergence from the stock's own history.

    A name with no history ranks last rather than being dropped: it may still
    be a fine trade, it simply has nothing to be mispriced against, so it only
    gets a slot when better-evidenced names have not filled the book.
    """
    usable = [r for r in reads if r.ok]
    if params.rank_by == "implied_move":
        usable.sort(key=lambda r: r.implied_move_pct or 0.0, reverse=True)
    else:
        usable.sort(key=lambda r: (r.ratio is not None, r.divergence), reverse=True)
    kept = usable[: params.top_n]
    for read in usable[params.top_n :]:
        log.debug("%s ranked out of the top %d", read.symbol, params.top_n)
    return kept
