"""Black-Scholes, used only by the backtest.

Alpaca's free tier has no historical option chain with greeks, so a historical
backtest cannot look up what the 1DTE put actually cost on 14 March. The choice
is between pretending the overlay is free (which flatters the strategy and is
dishonest) and modelling its cost.

We model it, and we deliberately model it *expensively*:
  * implied vol is marked up over trailing realised vol, because short-dated
    options carry a variance risk premium and 1DTE options carry the most
  * the fill crosses half the spread on the way in
  * the exit at 09:35 assumes intrinsic value only, surrendering whatever time
    value remains

Every one of those choices makes the backtest worse than reality is likely to
be. That is the right direction for an error to point.
"""

from __future__ import annotations

import math


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    is_call: bool,
    rate: float = 0.04,
) -> float:
    """European option price. Zero dividends - a one-night hold does not care."""
    if spot <= 0 or strike <= 0:
        return 0.0
    intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    if years <= 0 or vol <= 0:
        return intrinsic

    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    discount = math.exp(-rate * years)
    if is_call:
        return max(intrinsic, spot * _cdf(d1) - strike * discount * _cdf(d2))
    return max(intrinsic, strike * discount * _cdf(-d2) - spot * _cdf(-d1))


def bs_delta(
    spot: float, strike: float, years: float, vol: float, is_call: bool, rate: float = 0.04
) -> float:
    if spot <= 0 or strike <= 0 or years <= 0 or vol <= 0:
        return 1.0 if (is_call and spot > strike) else (-1.0 if (not is_call and spot < strike) else 0.0)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * math.sqrt(years))
    return _cdf(d1) if is_call else _cdf(d1) - 1.0


def overnight_option_cost(
    spot: float,
    strike: float,
    realised_vol: float,
    is_call: bool,
    days: float = 1.0,
    vol_premium: float = 1.35,
    spread_fraction: float = 0.5,
    spread_pct: float = 0.12,
    min_price: float = 0.02,
) -> float:
    """What the protective leg costs per share, priced pessimistically.

    `vol_premium` marks realised vol up to a plausible implied. 1.35 is a
    conservative multiple for very short-dated options on liquid names; raise
    it if the backtest is meant to be more punishing still.
    """
    years = max(days, 0.5) / 365.0
    implied = max(0.05, realised_vol * vol_premium)
    fair = bs_price(spot, strike, years, implied, is_call)
    # Cross half the spread on entry.
    return max(min_price, fair * (1.0 + spread_pct * spread_fraction))


def intrinsic_at_open(spot: float, strike: float, is_call: bool) -> float:
    """Exit value assumed at 09:35: intrinsic only, time value surrendered."""
    return max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
