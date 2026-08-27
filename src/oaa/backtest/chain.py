"""Modelled option chain for the replay harness.

Alpaca's free tier does not serve a historical option chain. There is no
endpoint that will tell you what SPY's 14-delta put cost on 4 June, so a
historical backtest has exactly three options:

  1. pretend options are free or fill at mid          - dishonest
  2. do not backtest the options book at all          - useless
  3. model the chain, state the model, and bias every
     assumption against the strategy                  - what this does

Everything in this module is a MODEL, not data. The underlying prices are real
Alpaca bars; the option prices sitting on top of them are Black-Scholes with a
volatility surface, a spread model and a liquidity model. The dashboard says so
in the methodology panel and the deck must say so too.

The four modelled pieces, and the direction each one is biased:

  surface     ATM implied vol comes from the IV model (see `ivmodel.py`); the
              skew is a standard equity put skew, so the puts a short-premium
              book sells are priced RICHER than flat vol - that flatters the
              seller. It is the realistic direction, and the spread and
              liquidity models below more than pay it back.
  spread      half-spread is a floor plus a fraction of mid, widened away from
              the money. Entry and exit both CROSS it. This is the dominant
              modelled cost and it is deliberately not generous.
  liquidity   open interest and volume decay with |moneyness| and with time to
              expiry, so the config's `min_open_interest` / `min_volume` filters
              actually bind. A chain where everything is liquid is a chain that
              lets the strategy trade contracts that do not exist.
  expiries    Fridays only, weeklies restricted to the tiers that really have
              them. A model that offers a perfect DTE every single day hands
              the strategy an expiry it could not have traded.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from oaa.backtest.pricing import bs_delta, bs_price
from oaa.core.types import Greeks, OptionQuote, Right
from oaa.options.occ import build_occ

_DAYS = 365.0


# --------------------------------------------------------------------------- #
# liquidity tiers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LiquidityTier:
    """How tight and how deep this kind of underlying quotes."""

    name: str = "single_name"
    #: absolute floor on the half-spread, in dollars per share
    half_spread_min: float = 0.03
    #: half-spread as a fraction of mid, before the moneyness widener
    spread_frac: float = 0.030
    #: multiplies the half-spread once per unit of |standardised moneyness|
    otm_widen: float = 0.45
    #: ATM open interest on a front-month expiry
    base_oi: int = 4_000
    #: daily volume as a fraction of open interest
    turnover: float = 0.18
    #: does this underlying list weekly expiries?
    weeklies: bool = False
    #: listed strike increment; None falls back to the price-based ladder
    strike_step: float | None = None


DEFAULT_TIERS: dict[str, LiquidityTier] = {
    "index_etf": LiquidityTier(
        name="index_etf", half_spread_min=0.01, spread_frac=0.010, otm_widen=0.30,
        base_oi=40_000, turnover=0.55, weeklies=True, strike_step=1.0,
    ),
    "mega_cap": LiquidityTier(
        name="mega_cap", half_spread_min=0.02, spread_frac=0.018, otm_widen=0.38,
        base_oi=12_000, turnover=0.32, weeklies=True, strike_step=2.5,
    ),
    "single_name": LiquidityTier(
        name="single_name", half_spread_min=0.03, spread_frac=0.030, otm_widen=0.45,
        base_oi=4_000, turnover=0.18, weeklies=False,
    ),
    "illiquid": LiquidityTier(
        name="illiquid", half_spread_min=0.05, spread_frac=0.060, otm_widen=0.60,
        base_oi=900, turnover=0.10, weeklies=False,
    ),
}

DEFAULT_TIER_MAP: dict[str, str] = {
    **dict.fromkeys(("SPY", "QQQ", "IWM", "DIA", "XSP", "EEM", "XLF", "XLE", "XLK"), "index_etf"),
    **dict.fromkeys((
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AMD",
        "NFLX", "AVGO", "JPM", "V", "MA", "UNH", "XOM", "COST", "WMT",
    ), "mega_cap"),
}


def tier_for(symbol: str, tier_map: dict[str, str], tiers: dict[str, LiquidityTier],
             default: str = "single_name") -> LiquidityTier:
    key = tier_map.get(symbol.upper(), default)
    return tiers.get(key, tiers.get(default, LiquidityTier()))


# --------------------------------------------------------------------------- #
# strikes and expiries
# --------------------------------------------------------------------------- #
def strike_increment(spot: float) -> float:
    """What the listed strike ladder actually looks like at this price."""
    if spot < 25:
        return 0.5
    if spot < 50:
        return 1.0
    if spot < 200:
        return 2.5 if spot >= 100 else 1.0
    if spot < 500:
        return 5.0
    return 10.0


def strike_ladder(
    spot: float,
    window_pct: float,
    step: float | None = None,
    max_per_side: int = 30,
) -> list[float]:
    """The listed ladder, clipped to a window and to a strike count.

    The count cap is a performance guard, not a market fact: SPY really does
    list a hundred strikes each side, but a 14-delta structure never looks at
    the ninetieth, and building them costs the replay real time.
    """
    step = step or strike_increment(spot)
    low, high = spot * (1 - window_pct), spot * (1 + window_pct)
    low = max(low, spot - max_per_side * step)
    high = min(high, spot + max_per_side * step)
    first = math.floor(low / step) * step
    out: list[float] = []
    k = first
    while k <= high + 1e-9:
        if k > 0:
            out.append(round(k, 2))
        k += step
    return out


def listed_expiries(
    asof: dt.date, min_dte: int, max_dte: int, weeklies: bool
) -> list[dt.date]:
    """Fridays inside the DTE window. Without weeklies, monthlies only.

    A holiday-shortened week moves expiry to Thursday; the harness ignores that
    - it shifts a hold by one day and changes nothing about a thesis.
    """
    out: list[dt.date] = []
    for offset in range(max(0, min_dte), max_dte + 1):
        day = asof + dt.timedelta(days=offset)
        if day.weekday() != 4:
            continue
        is_monthly = 15 <= day.day <= 21
        if weeklies or is_monthly:
            out.append(day)
    return out


# --------------------------------------------------------------------------- #
# the surface
# --------------------------------------------------------------------------- #
@dataclass
class ChainModel:
    """Turns (spot, ATM implied vol, date) into a quotable chain."""

    #: put-skew slope per unit of standardised moneyness. Negative = puts richer.
    skew: float = -0.11
    #: curvature of the smile
    smile: float = 0.06
    #: term-structure slope: ATM vol per sqrt(year) of extra maturity
    term_slope: float = 0.02
    rate: float = 0.04
    strike_window_pct: float = 0.14
    max_strikes_per_side: int = 30
    min_dte: int = 3
    max_dte: int = 45
    tiers: dict[str, LiquidityTier] = field(default_factory=lambda: dict(DEFAULT_TIERS))
    tier_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TIER_MAP))
    default_tier: str = "single_name"
    #: contracts under this mid are unquotable in practice
    min_quotable_mid: float = 0.03

    # -- surface -------------------------------------------------------- #
    def iv_at(self, atm_iv: float, spot: float, strike: float, years: float) -> float:
        """ATM vol plus a standard equity skew, in standardised-moneyness units."""
        atm = self._atm_for_term(atm_iv, years)
        denom = max(1e-6, atm * math.sqrt(max(years, 1e-6)))
        m = math.log(max(strike, 1e-9) / max(spot, 1e-9)) / denom
        m = max(-4.0, min(4.0, m))
        vol = atm * (1.0 + self.skew * m + self.smile * m * m)
        return max(0.25 * atm, min(2.5 * atm, vol))

    def _atm_for_term(self, atm_iv: float, years: float) -> float:
        """Mild upward term structure - the 30d is usually above the 7d."""
        anchor = math.sqrt(30.0 / _DAYS)
        return max(0.02, atm_iv + self.term_slope * (math.sqrt(max(years, 1e-6)) - anchor))

    # -- spreads -------------------------------------------------------- #
    @staticmethod
    def _tick(price: float) -> float:
        """Penny-pilot convention: 1c under $3.00, 5c above."""
        return 0.01 if price < 3.0 else 0.05

    def half_spread(self, mid: float, moneyness: float, tier: LiquidityTier) -> float:
        raw = max(tier.half_spread_min, tier.spread_frac * mid)
        raw *= 1.0 + tier.otm_widen * abs(moneyness)
        tick = self._tick(mid)
        return max(tick / 2, round(raw / tick) * tick)

    # -- liquidity ------------------------------------------------------ #
    def liquidity(
        self, moneyness: float, dte: int, tier: LiquidityTier
    ) -> tuple[int, int]:
        """Open interest and volume decay away from the money and out in time."""
        decay = math.exp(-(moneyness * moneyness) / 2.0)
        # front-month carries the open interest; 45 DTE is thinner than 7
        term = 1.0 / (1.0 + max(0, dte) / 30.0)
        oi = int(tier.base_oi * decay * (0.45 + 0.55 * term))
        volume = int(oi * tier.turnover * (0.5 + 0.5 * decay))
        return max(0, oi), max(0, volume)

    # -- the chain ------------------------------------------------------ #
    def build(
        self,
        symbol: str,
        spot: float,
        asof: dt.datetime,
        atm_iv: float,
        min_dte: int | None = None,
        max_dte: int | None = None,
    ) -> list[OptionQuote]:
        tier = tier_for(symbol, self.tier_map, self.tiers, self.default_tier)
        day = asof.date()
        expiries = listed_expiries(
            day,
            self.min_dte if min_dte is None else min_dte,
            self.max_dte if max_dte is None else max_dte,
            tier.weeklies,
        )
        strikes = strike_ladder(
            spot, self.strike_window_pct, tier.strike_step, self.max_strikes_per_side
        )
        quotes: list[OptionQuote] = []

        for expiry in expiries:
            dte = (expiry - day).days
            years = max(dte, 0.5) / _DAYS
            atm = self._atm_for_term(atm_iv, years)
            denom = max(1e-6, atm * math.sqrt(years))
            for strike in strikes:
                m = math.log(max(strike, 1e-9) / max(spot, 1e-9)) / denom
                vol = self.iv_at(atm_iv, spot, strike, years)
                oi, volume = self.liquidity(m, dte, tier)
                for right in (Right.CALL, Right.PUT):
                    is_call = right is Right.CALL
                    mid = bs_price(spot, strike, years, vol, is_call, self.rate)
                    if mid < self.min_quotable_mid:
                        continue
                    half = self.half_spread(mid, m, tier)
                    bid = round(max(0.01, mid - half), 2)
                    ask = round(mid + half, 2)
                    quotes.append(
                        OptionQuote(
                            symbol=build_occ(symbol, expiry, right, strike),
                            underlying=symbol,
                            expiry=expiry,
                            strike=strike,
                            right=right,
                            bid=bid,
                            ask=ask,
                            last=round(mid, 2),
                            implied_volatility=round(vol, 4),
                            greeks=self._greeks(spot, strike, years, vol, is_call),
                            open_interest=oi,
                            volume=volume,
                            asof=asof,
                        )
                    )
        return quotes

    # -- greeks ---------------------------------------------------------- #
    def _greeks(
        self, spot: float, strike: float, years: float, vol: float, is_call: bool
    ) -> Greeks:
        delta = bs_delta(spot, strike, years, vol, is_call, self.rate)
        sqrt_t = math.sqrt(max(years, 1e-9))
        d1 = (
            math.log(max(spot, 1e-9) / max(strike, 1e-9))
            + (self.rate + 0.5 * vol * vol) * years
        ) / max(1e-9, vol * sqrt_t)
        pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        gamma = pdf / max(1e-9, spot * vol * sqrt_t)
        vega = spot * pdf * sqrt_t / 100.0
        theta = -(spot * pdf * vol) / (2 * sqrt_t) / _DAYS
        return Greeks(
            delta=round(delta, 4),
            gamma=round(gamma, 6),
            theta=round(theta, 4),
            vega=round(vega, 4),
        )

    # -- repricing an existing contract ---------------------------------- #
    def reprice(
        self, quote_symbol: str, spot: float, asof: dt.date, atm_iv: float,
        strike: float, expiry: dt.date, is_call: bool, tier_symbol: str,
    ) -> dict[str, float]:
        """Mark one already-open contract at a later date. Used every bar."""
        dte = (expiry - asof).days
        years = max(dte, 0) / _DAYS
        if years <= 0:
            intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
            return {"mid": round(intrinsic, 4), "bid": round(intrinsic, 4),
                    "ask": round(intrinsic, 4), "iv": atm_iv, "dte": 0}
        vol = self.iv_at(atm_iv, spot, strike, years)
        mid = bs_price(spot, strike, years, vol, is_call, self.rate)
        atm = self._atm_for_term(atm_iv, years)
        m = math.log(max(strike, 1e-9) / max(spot, 1e-9)) / max(1e-6, atm * math.sqrt(years))
        tier = tier_for(tier_symbol, self.tier_map, self.tiers, self.default_tier)
        half = self.half_spread(max(mid, 0.01), m, tier)
        return {
            "mid": round(mid, 4),
            "bid": round(max(0.0, mid - half), 4),
            "ask": round(mid + half, 4),
            "iv": round(vol, 4),
            "dte": dte,
        }

    # -- provenance ------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "skew": self.skew,
            "smile": self.smile,
            "term_slope": self.term_slope,
            "rate": self.rate,
            "strike_window_pct": self.strike_window_pct,
            "max_strikes_per_side": self.max_strikes_per_side,
            "tiers": {k: vars(v) for k, v in self.tiers.items()},
        }
