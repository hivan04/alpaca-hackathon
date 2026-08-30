"""The ATM implied-vol TERM STRUCTURE: what the surface says about today vs later.

Why this exists
---------------
Every entry signal in the intraday book reads the same series - price. VWAP,
Bollinger width, RSI, persistence and the higher-timeframe drift are five
questions asked of one source, so when the source is uninformative they all go
quiet together. Measured over a 15-minute-cycle replay, 64% of intraday
rejections were "no VWAP cross" and a further 21% were the volume z-score:
loosening any one of them only promotes the next to being the wall.

The option chain is a SECOND source, and the book currently reads one scalar
out of it (ATM IV, and its rank). The term structure is the next scalar, and
it is close to free: the chain is already in the context, and
`ChainView.atm_iv` already prices an ATM contract per expiry.

The quantity
------------
    slope     = front_iv - back_iv                (vol points)
    slope_pct = (front_iv - back_iv) / back_iv    (relative, scale-free)

    slope > 0  BACKWARDATION - the market is paying more for near-dated vol
               than for later vol. It expects THIS session to move.
    slope < 0  CONTANGO - the normal state. The nearer expiry is cheaper
               because there is less time for anything to happen in it.

`slope_pct` is the one to gate on: a 2-vol-point slope means something very
different on a 12-vol name than on a 45-vol one, and this universe spans both.

Measured, not assumed
---------------------
This is the part that matters, and it is why the function refuses more often
than it answers.

In replay, a contract with no traded print that session falls back to the
modelled surface in `backtest/chain.py`, whose term structure is a CONSTANT
from config:

    ChainModel._atm_for_term:  atm_iv + term_slope * (sqrt(years) - anchor)
    backtest.chain.term_slope: 0.02

So a slope computed across two modelled anchors is not a measurement of
anything. It is `term_slope` read back out, with the same sign and nearly the
same magnitude on every symbol, on every session, forever. It would look like
a signal that fires reliably, and a backtest would happily attribute P&L to it.

Both anchors must therefore be RECOVERED from real prints (live: quoted by the
feed) or the result is `measured=False` and carries no vote. This follows the
precedent in `claude/confirmation-scoring.md`: unmeasurable is not the same as
failed, and neither is it the same as true.

The separation guard is the same argument in another form. Two expiries three
days apart on a 45-day ladder are the same maturity for this purpose, and the
"slope" between them is quote noise divided by a small number.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence

from oaa.core.types import MarketContext, OptionQuote, Right, TermStructure

#: Nearest expiry we will accept as the front anchor. A FLOOR, not a target: 0
#: DTE is excluded outright rather than merely deprioritised.
#:
#: This was a target first, and the first real run said why that is wrong. On a
#: session where the only expiries listed were 0 DTE and 32 DTE, the front
#: anchor landed on the 0 and the measured slopes came back +257.9% on TLT and
#: +621.4% on XLF. A 621% term slope is not the market forecasting a violent
#: session; it is Black-Scholes inverted on a contract with hours of life, where
#: the recovered vol is dominated by the pin and by half the bid-ask spread.
#: Those readings failed the band and cost nothing, so they were harmless - but
#: harmless nonsense in a rejection log is still nonsense a judge will read.
DEFAULT_FRONT_DTE = 1

#: The back anchor. 30 is the conventional vol-surface reference point and is
#: what "30-day implied" means everywhere else, so the number is comparable to
#: something outside this repo.
DEFAULT_BACK_DTE = 30

#: Minimum gap between the anchors. Below this the two expiries are the same
#: maturity and the slope is noise over a small denominator.
DEFAULT_MIN_SEPARATION_DAYS = 7

#: Beyond this the reading is rejected as unmeasurable rather than reported as
#: an extreme slope. Real index term structure inverts hard in a shock - VIX
#: futures went into backwardation of tens of percent in March 2020 - but not by
#: multiples. A number this large means an anchor is broken, and the honest
#: answer to a broken instrument is "no reading", not "an extreme reading".
#: Failing the band and being unmeasurable are the same outcome for the vote and
#: opposite outcomes for anyone reading the log to find out why.
DEFAULT_MAX_ABS_SLOPE_PCT = 1.0


def _is_modelled(quote: OptionQuote) -> bool:
    """True when this quote's IV came off a model rather than off the tape.

    Live quotes carry no `iv_source` (the feed's IV is the feed's IV), so
    absence means measured. Only an explicit "modelled..." is untrusted - see
    `backtest/realchain.py`, which sets one of three strings.
    """
    return str(quote.iv_source or "").startswith("modelled")


def _atm_iv_for_expiry(
    quotes: Sequence[OptionQuote],
    spot: float,
    require_measured: bool,
) -> tuple[float | None, bool]:
    """Average the call and put IV nearest the money. Returns (iv, measured).

    Both rights when both are there: a single-right ATM IV inherits whatever
    skew the surface has at that strike, and the two rights straddle it.
    """
    if spot <= 0:
        return None, False
    best: dict[Right, OptionQuote] = {}
    for quote in quotes:
        if quote.implied_volatility is None or quote.implied_volatility <= 0:
            continue
        if require_measured and _is_modelled(quote):
            continue
        incumbent = best.get(quote.right)
        if incumbent is None or abs(quote.strike - spot) < abs(incumbent.strike - spot):
            best[quote.right] = quote
    if not best:
        return None, False
    ivs = [float(q.implied_volatility or 0.0) for q in best.values()]
    measured = all(not _is_modelled(q) for q in best.values())
    return round(sum(ivs) / len(ivs), 6), measured


def term_structure(
    chain: Iterable[OptionQuote],
    spot: float,
    asof: dt.date,
    front_dte: int = DEFAULT_FRONT_DTE,
    back_dte: int = DEFAULT_BACK_DTE,
    min_separation_days: int = DEFAULT_MIN_SEPARATION_DAYS,
    max_abs_slope_pct: float = DEFAULT_MAX_ABS_SLOPE_PCT,
    require_measured: bool = True,
) -> TermStructure | None:
    """The ATM IV slope between a front and a back expiry, or None.

    None means the chain could not answer - no two expiries far enough apart,
    or no ATM contract with a usable IV at one of them. It does NOT mean flat.
    Callers must treat the two differently; a strategy that reads None as 0.0
    has invented a measurement.

    `require_measured=False` is for diagnostics and for the modelled-chain
    replay path, where the answer is a restatement of `backtest.chain.term_slope`
    and is labelled as such. Never gate on it.
    """
    quotes = [q for q in chain if q.implied_volatility]
    if not quotes or spot <= 0:
        return None

    by_expiry: dict[dt.date, list[OptionQuote]] = {}
    for quote in quotes:
        by_expiry.setdefault(quote.expiry, []).append(quote)

    dated = sorted(
        ((expiry, (expiry - asof).days) for expiry in by_expiry),
        key=lambda pair: pair[1],
    )
    dated = [(expiry, days) for expiry, days in dated if days >= 0]
    if len(dated) < 2:
        return None

    # `front_dte` is a FLOOR. Picking "closest to 1" from a ladder whose only
    # near expiry is 0 DTE selects the 0, and the vol recovered from a contract
    # with hours of life is pin and spread rather than a view on the session.
    eligible = [pair for pair in dated if pair[1] >= front_dte]
    if not eligible:
        return None
    front_expiry, front_days = min(eligible, key=lambda pair: abs(pair[1] - front_dte))
    # The back anchor is chosen from expiries that clear the separation floor,
    # not from the whole ladder. Picking "closest to 30" first and checking the
    # gap afterwards throws away a usable 21-day anchor whenever a 25-day one
    # happens to sit nearer the target.
    candidates = [
        pair for pair in dated
        if pair[1] - front_days >= min_separation_days
    ]
    if not candidates:
        return None
    back_expiry, back_days = min(candidates, key=lambda pair: abs(pair[1] - back_dte))

    front_iv, front_measured = _atm_iv_for_expiry(
        by_expiry[front_expiry], spot, require_measured
    )
    back_iv, back_measured = _atm_iv_for_expiry(
        by_expiry[back_expiry], spot, require_measured
    )
    if front_iv is None or back_iv is None or back_iv <= 0:
        return None

    slope_pct = (front_iv - back_iv) / back_iv
    measured = bool(front_measured and back_measured)
    if max_abs_slope_pct > 0 and abs(slope_pct) > max_abs_slope_pct:
        # An anchor is broken. Say so rather than reporting the number: a slope
        # this large fails any sane band, so the VOTE is unaffected either way,
        # but "outside the band - backwardation" in a rejection log claims a
        # measurement was made and read.
        measured = False

    return TermStructure(
        front_expiry=front_expiry,
        back_expiry=back_expiry,
        front_dte=int(front_days),
        back_dte=int(back_days),
        front_iv=round(front_iv, 4),
        back_iv=round(back_iv, 4),
        slope=round(front_iv - back_iv, 4),
        slope_pct=round(slope_pct, 4),
        measured=measured,
        source=(
            "recovered from real prints at both anchors" if measured
            else f"slope {slope_pct:+.0%} is beyond the {max_abs_slope_pct:.0%} "
                 f"plausibility ceiling - an anchor is broken"
            if abs(slope_pct) > max_abs_slope_pct > 0
            else "at least one anchor is modelled - carries no vote"
        ),
    )


def term_structure_from_config(
    chain: Iterable[OptionQuote],
    spot: float,
    asof: dt.date,
    cfg: object,
    require_measured: bool = True,
) -> TermStructure | None:
    """`term_structure` with the anchors read from config. ONE definition.

    Every producer of a MarketContext - the two live providers and the replay
    source - goes through here, so the live path and the replay path cannot
    drift into computing different numbers under one name. That is not a
    hypothetical: `claude/iv-rank-divergence.md` is exactly that failure, and
    the gate sitting on top of IV rank could not tell it had happened.
    """
    data = getattr(cfg, "data", None)
    return term_structure(
        chain,
        spot,
        asof,
        front_dte=int(getattr(data, "term_front_dte", DEFAULT_FRONT_DTE)),
        back_dte=int(getattr(data, "term_back_dte", DEFAULT_BACK_DTE)),
        min_separation_days=int(
            getattr(data, "term_min_separation_days", DEFAULT_MIN_SEPARATION_DAYS)
        ),
        max_abs_slope_pct=float(
            getattr(data, "term_max_abs_slope_pct", DEFAULT_MAX_ABS_SLOPE_PCT)
        ),
        require_measured=require_measured,
    )


def term_structure_for(
    market: MarketContext,
    cfg: object,
    require_measured: bool = True,
) -> TermStructure | None:
    """`term_structure_from_config` against an already-built MarketContext.

    For callers that receive a context rather than build one - diagnostics,
    the dashboard, and any strategy that wants a second opinion at different
    anchors than the ones the provider used.
    """
    return term_structure_from_config(
        market.chain, market.spot, market.asof.date(), cfg,
        require_measured=require_measured,
    )
