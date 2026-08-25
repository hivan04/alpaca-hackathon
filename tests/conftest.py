from __future__ import annotations

import datetime as dt

import pytest

from oaa.config.schema import Config
from oaa.core.types import AccountSnapshot, Greeks, OptionQuote, Right
from oaa.options.occ import build_occ


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def today() -> dt.date:
    return dt.date(2026, 9, 1)


def make_quote(
    root: str = "SPY",
    expiry: dt.date | None = None,
    strike: float = 500.0,
    right: Right = Right.CALL,
    bid: float = 1.00,
    ask: float = 1.10,
    delta: float | None = 0.30,
    iv: float | None = 0.20,
    oi: int = 5000,
) -> OptionQuote:
    expiry = expiry or dt.date(2026, 9, 18)
    return OptionQuote(
        symbol=build_occ(root, expiry, right, strike),
        underlying=root,
        expiry=expiry,
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        implied_volatility=iv,
        greeks=Greeks(delta=delta, gamma=0.01, theta=-0.05, vega=0.2),
        open_interest=oi,
        volume=500,
    )


def _bs(spot: float, strike: float, t_years: float, vol: float, is_call: bool):
    """Black-Scholes price and delta. The test chain has to be arbitrage-free
    enough that spread widths and credit/width ratios come out realistic -
    a hand-waved price grid makes strategy tests meaningless."""
    import math

    if t_years <= 0 or vol <= 0:
        intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
        return intrinsic, (1.0 if intrinsic > 0 else 0.0) * (1 if is_call else -1)

    def cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    if is_call:
        return spot * cdf(d1) - strike * cdf(d2), cdf(d1)
    return strike * cdf(-d2) - spot * cdf(-d1), cdf(d1) - 1.0


@pytest.fixture
def chain(today: dt.date) -> list[OptionQuote]:
    """A synthetic but arbitrage-free SPY chain: spot 500, 21 DTE, 20% vol,
    strikes 400-600 on the 5-point grid, a realistic 5c-wide market."""
    expiry = today + dt.timedelta(days=21)
    spot, vol = 500.0, 0.20
    t_years = 21 / 365
    quotes: list[OptionQuote] = []
    for strike in range(400, 605, 5):
        for right in (Right.CALL, Right.PUT):
            price, delta = _bs(spot, float(strike), t_years, vol, right is Right.CALL)
            if price < 0.15:
                continue
            half = max(0.02, round(price * 0.01, 2))
            quotes.append(
                make_quote(
                    expiry=expiry,
                    strike=float(strike),
                    right=right,
                    bid=round(price - half, 2),
                    ask=round(price + half, 2),
                    delta=round(delta, 4),
                    iv=vol,
                    oi=5000,
                )
            )
    return quotes


@pytest.fixture
def account() -> AccountSnapshot:
    return AccountSnapshot(
        account_id="TEST",
        equity=100_000.0,
        last_equity=100_000.0,
        cash=100_000.0,
        buying_power=100_000.0,
        options_buying_power=100_000.0,
        options_trading_level=3,
    )


@pytest.fixture
def bars() -> list[dict]:
    """90 daily bars trending gently up with realistic intrabar range."""
    out = []
    price = 100.0
    for i in range(90):
        price *= 1.0 + (0.004 if i % 3 else -0.002)
        out.append({
            "timestamp": dt.datetime(2026, 6, 1) + dt.timedelta(days=i),
            "open": price * 0.998,
            "high": price * 1.008,
            "low": price * 0.992,
            "close": price,
            "volume": 1_000_000 + i * 1000,
        })
    return out


# --------------------------------------------------------------------------- #
# Two-book / pairs fixtures
# --------------------------------------------------------------------------- #
def daily_bars(
    closes: list[float],
    start: dt.date = dt.date(2024, 1, 1),
    gap_sd: float = 0.008,
    seed: int = 17,
) -> list[dict]:
    """Daily OHLCV with realistic overnight gap dispersion.

    Gap size matters to the tests: the strategy places its option strikes at
    the empirical gap quantiles, so a fixture with unrealistically tiny gaps
    produces near-ATM hedges and tests the wrong thing.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    out: list[dict] = []
    day = start
    for i, close in enumerate(closes):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        open_px = close * (1 + float(rng.normal(0, gap_sd)))
        out.append({
            "timestamp": dt.datetime.combine(day, dt.time(0, 0)),
            "open": open_px,
            "high": max(open_px, close) * 1.005,
            "low": min(open_px, close) * 0.995,
            "close": close,
            "volume": 1_000_000 + i * 500,
        })
        day += dt.timedelta(days=1)
    return out


def cointegrated_series(n: int = 400, beta: float = 1.5, seed: int = 3):
    """A genuinely cointegrated pair: y = beta*x + intercept + AR(1) noise."""
    import numpy as np

    rng = np.random.default_rng(seed)
    x = 100 + np.cumsum(rng.normal(0, 0.5, n))
    noise = np.zeros(n)
    for i in range(1, n):
        noise[i] = 0.85 * noise[i - 1] + rng.normal(0, 0.4)
    y = beta * x + 20 + noise
    return list(y), list(x)


def option_chain_for(
    symbol: str,
    spot: float,
    expiry: dt.date,
    vol: float = 0.22,
    width: float = 0.25,
    step: float = 1.0,
    days: float = 1.0,
) -> list[OptionQuote]:
    """Arbitrage-free 1DTE chain around `spot`, for overlay tests."""
    import math

    t = max(days, 0.5) / 365.0

    def cdf(v: float) -> float:
        return 0.5 * (1.0 + math.erf(v / math.sqrt(2.0)))

    quotes: list[OptionQuote] = []
    low, high = spot * (1 - width), spot * (1 + width)
    strike = round(low / step) * step
    while strike <= high:
        if strike > 0:
            d1 = (math.log(spot / strike) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))
            d2 = d1 - vol * math.sqrt(t)
            call = spot * cdf(d1) - strike * cdf(d2)
            put = strike * cdf(-d2) - spot * cdf(-d1)
            for right, price, delta in (
                (Right.CALL, call, cdf(d1)),
                (Right.PUT, put, cdf(d1) - 1.0),
            ):
                price = max(price, 0.05)
                half = max(0.02, price * 0.02)
                quotes.append(OptionQuote(
                    symbol=build_occ(symbol, expiry, right, strike),
                    underlying=symbol, expiry=expiry, strike=float(strike), right=right,
                    bid=round(price - half, 2), ask=round(price + half, 2),
                    implied_volatility=vol,
                    greeks=Greeks(delta=round(delta, 4), gamma=0.02, theta=-0.08, vega=0.05),
                    open_interest=2000, volume=400,
                ))
        strike += step
    return quotes


@pytest.fixture
def pair_contexts():
    """Two MarketContexts forming a cointegrated pair, with short-dated chains."""
    from oaa.core.types import MarketContext

    y, x = cointegrated_series(n=400, beta=1.5)
    bars_y, bars_x = daily_bars(y), daily_bars(x)
    asof = dt.datetime(2026, 9, 1, 19, 45, tzinfo=dt.timezone.utc)
    expiry = dt.date(2026, 9, 2)

    def context(symbol: str, bars: list[dict]) -> MarketContext:
        spot = bars[-1]["close"]
        return MarketContext(
            symbol=symbol, asof=asof, spot=spot, prev_close=bars[-2]["close"],
            bars=bars,
            chain=option_chain_for(symbol, spot, expiry, step=max(0.5, round(spot / 100))),
            realised_vol=0.20, implied_vol=0.24, iv_rank=0.5,
            trend_strength=0.1, adx=15.0,
        )

    return {"AAA": context("AAA", bars_y), "BBB": context("BBB", bars_x)}


@pytest.fixture
def frozen_clock():
    """Build an ET datetime for a given HH:MM on a Wednesday."""
    from zoneinfo import ZoneInfo

    def at(hhmm: str, day: dt.date = dt.date(2026, 9, 2)) -> dt.datetime:
        hour, minute = (int(p) for p in hhmm.split(":"))
        return dt.datetime.combine(
            day, dt.time(hour, minute), tzinfo=ZoneInfo("America/New_York")
        )

    return at
