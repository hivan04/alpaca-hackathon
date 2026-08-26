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


def _expiry_slice(
    today: dt.date,
    days: int,
    spot: float = 500.0,
    vol: float = 0.20,
    half_spread: float | None = None,
):
    expiry = today + dt.timedelta(days=days)
    t_years = days / 365
    quotes: list[OptionQuote] = []
    for strike in range(400, 605, 5):
        for right in (Right.CALL, Right.PUT):
            price, delta = _bs(spot, float(strike), t_years, vol, right is Right.CALL)
            if price < 0.15:
                continue
            half = (
                half_spread
                if half_spread is not None
                else max(0.02, round(price * 0.01, 2))
            )
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
def chain(today: dt.date) -> list[OptionQuote]:
    """A synthetic but arbitrage-free SPY chain: spot 500, 20% vol, strikes
    400-600 on the 5-point grid, a realistic 5c-wide market.

    TWO expiries, deliberately: the carry book trades 7-14 DTE (its decay has to
    fit inside the judged window) and the momentum book trades 14-45, so a
    single-expiry fixture would silently make one of them untestable.
    """
    return _expiry_slice(today, 10) + _expiry_slice(today, 21)


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


# --------------------------------------------------------------------------- #
# Intraday fixtures
# --------------------------------------------------------------------------- #
def five_minute_bars(
    session_days: int = 4,
    last_session_bars: int = 18,
    base: float = 500.0,
    bucket_volume: float = 100_000.0,
    breakout: bool = True,
    start: dt.date = dt.date(2026, 8, 24),
) -> list[dict]:
    """Several full 5-minute sessions plus a partial current one.

    The prior sessions exist so the time-of-day volume baseline has something
    to compare against: 09:45 volume is not comparable to 12:30 volume, and a
    flat daily average would make lunchtime look permanently dead.
    """
    rows: list[dict] = []
    day = start

    def push(stamp: dt.datetime, close: float, volume: float) -> None:
        rows.append({
            "timestamp": stamp,
            "open": close * 0.9995,
            "high": close * 1.0008,
            "low": close * 0.9992,
            "close": close,
            "volume": volume,
        })

    for d in range(session_days):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        stamp = dt.datetime.combine(day, dt.time(9, 30))
        for i in range(78):
            drift = 0.15 if i % 2 else -0.15
            # Real volume varies within a bucket across days; a fixture with
            # zero dispersion would make the z-score undefined rather than high.
            jitter = 1.0 + 0.08 * ((d * 7 + i) % 5 - 2)
            push(stamp, base + drift, bucket_volume * jitter)
            stamp += dt.timedelta(minutes=5)
        day += dt.timedelta(days=1)

    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    stamp = dt.datetime.combine(day, dt.time(9, 30))
    # Chop below the developing VWAP, then a genuine expansion through it.
    path = [
        499.9, 499.6, 499.9, 499.5, 499.8, 499.4, 499.7, 499.3,
        499.6, 499.2, 499.5, 499.1, 499.4, 499.0, 499.3,
        500.4, 501.4, 502.3,          # the expansion through session VWAP
    ][:last_session_bars]
    if not breakout:
        path = [499.9, 499.6] * (last_session_bars // 2)
    for i, close in enumerate(path):
        volume = bucket_volume
        if breakout and i >= len(path) - 5:
            volume = bucket_volume * 2.2
        push(stamp, close, volume)
        stamp += dt.timedelta(minutes=5)
    return rows


@pytest.fixture
def intraday_chain() -> list[OptionQuote]:
    """A 0-2 DTE chain around the intraday fixture's session date.

    The intraday book buys 0-2 DTE for maximum gamma per dollar, so it cannot
    be tested against the carry book's 7-21 DTE chain.
    """
    asof = dt.date(2026, 8, 28)
    # A penny-wide market, which is what SPY 0-2 DTE actually quotes at. The
    # carry book's fixture is deliberately wider; using it here would test the
    # wrong instrument and make the spread gate look impossible rather than
    # merely strict.
    return _expiry_slice(asof, 1, vol=0.18, half_spread=0.005) + _expiry_slice(
        asof, 2, vol=0.18, half_spread=0.005
    )


@pytest.fixture
def intraday_bars() -> list[dict]:
    return five_minute_bars()


@pytest.fixture
def choppy_intraday_bars() -> list[dict]:
    return five_minute_bars(breakout=False)


@pytest.fixture
def attention():
    """A movers snapshot with confirming breadth and a volume ranking."""
    from oaa.discovery.score import AttentionSnapshot, SymbolAttention

    return AttentionSnapshot(
        asof=dt.datetime(2026, 8, 28, 14, 40, tzinfo=dt.timezone.utc),
        symbols={
            "SPY": SymbolAttention(
                symbol="SPY", score=0.9,
                raw={"most_actives": {"volume": 90_000_000}},
                percent_change=0.9, direction="up", news_velocity=3.0,
            ),
            "QQQ": SymbolAttention(
                symbol="QQQ", score=0.6,
                raw={"most_actives": {"volume": 40_000_000}},
                percent_change=0.7, direction="up",
            ),
        },
        breadth={"gainers": 17, "losers": 3},
    )
