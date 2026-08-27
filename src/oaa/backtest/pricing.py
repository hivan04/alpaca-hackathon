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


def implied_vol_from_price(
    price: float,
    spot: float,
    strike: float,
    years: float,
    is_call: bool,
    rate: float = 0.04,
    low: float = 0.01,
    high: float = 5.0,
    tolerance: float = 1e-5,
    max_iterations: int = 60,
) -> float | None:
    """Invert Black-Scholes: what vol does this traded price imply?

    This is the function that lets the replay stop guessing. Alpaca serves real
    historical option BARS but no historical greeks or implied vol, so IV has
    to be recovered rather than modelled - and IV recovered from a real traded
    price is market data with one arithmetic step applied, while IV inferred
    from realised volatility is an assumption dressed as a measurement.

    Bisection rather than Newton: vega collapses on deep out-of-the-money and
    near-expiry contracts, which is exactly where a Newton step diverges, and
    those contracts are the wings this strategy sells. Sixty bisections on a
    monotonic function is fast enough and cannot blow up.

    Returns None when the price is not invertible - at or below intrinsic
    (no time value to explain), or outside the bracket. The caller must treat
    that as missing data, never as zero vol.
    """
    if price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None

    intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    if price <= intrinsic + 1e-9:
        return None
    # No-arbitrage ceiling: a call cannot be worth more than the stock.
    if price >= (spot if is_call else strike):
        return None

    lo, hi = low, high
    if bs_price(spot, strike, years, hi, is_call, rate) < price:
        return None                      # even 500% vol does not reach this print

    # Converge on VOL, not on price. Deep in- or out-of-the-money contracts have
    # almost no vega, so a whole range of vols prices within any sane price
    # tolerance - stopping on |value - price| there returns whichever vol the
    # bisection happened to land on, which looks like a measurement and is not.
    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        if hi - lo < tolerance:
            break
        if bs_price(spot, strike, years, mid, is_call, rate) < price:
            lo = mid
        else:
            hi = mid
    vol = (lo + hi) / 2

    # Refuse to report a vol the price cannot actually pin down. One vol tick
    # must move the price by at least a cent, or this contract carries no
    # volatility information and the honest answer is "unknown".
    sensitivity = abs(
        bs_price(spot, strike, years, vol * 1.05, is_call, rate)
        - bs_price(spot, strike, years, vol * 0.95, is_call, rate)
    )
    if sensitivity < 0.01:
        return None
    return round(vol, 6)


def bs_greeks(
    spot: float, strike: float, years: float, vol: float, is_call: bool, rate: float = 0.04
) -> dict[str, float]:
    """Analytic greeks at a given vol. Per share; theta is per calendar day."""
    if spot <= 0 or strike <= 0 or years <= 0 or vol <= 0:
        return {"delta": bs_delta(spot, strike, years, vol, is_call, rate),
                "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * sqrt_t)
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return {
        "delta": round(bs_delta(spot, strike, years, vol, is_call, rate), 4),
        "gamma": round(pdf / (spot * vol * sqrt_t), 6),
        "vega": round(spot * pdf * sqrt_t / 100.0, 4),
        "theta": round(-(spot * pdf * vol) / (2 * sqrt_t) / 365.0, 4),
    }
