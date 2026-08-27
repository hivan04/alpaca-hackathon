"""Alpaca market data via alpaca-py.

Free-tier realities baked in:
  * options quotes come from the `indicative` feed unless you pay for OPRA
  * the last 15 minutes of history are withheld
  * 200 requests/minute, so chain requests are always narrowed by strike and DTE
"""

from __future__ import annotations

import datetime as dt
import time
from collections import deque
from typing import Any

from oaa.core.errors import DataError
from oaa.core.logging import get_logger
from oaa.core.types import Greeks, MarketContext, OptionQuote, Right
from oaa.data.base import MarketDataProvider, data_registry
from oaa.data.indicators import (
    adx,
    iv_rank,
    trend_strength,
    vol_estimator,
    volume_ratio,
)
from oaa.options.occ import parse_occ

log = get_logger("data.alpaca")


class RateLimiter:
    """Simple sliding-window limiter. Cheaper than eating 429s."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._hits: deque[float] = deque()

    def acquire(self) -> None:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > 60:
            self._hits.popleft()
        if len(self._hits) >= self.per_minute:
            sleep_for = 60 - (now - self._hits[0]) + 0.05
            log.debug("rate limit reached, sleeping %.1fs", sleep_for)
            time.sleep(max(0.0, sleep_for))
        self._hits.append(time.monotonic())


@data_registry.register("alpaca")
class AlpacaDataProvider(MarketDataProvider):
    name = "alpaca"

    def __init__(self, cfg: Any, credentials: Any = None) -> None:
        super().__init__(cfg, credentials)
        self._stock: Any = None
        self._option: Any = None
        self._limiter = RateLimiter(cfg.data.rate_limit.requests_per_minute)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._iv_history: dict[str, list[float]] = {}

    # -- clients ----------------------------------------------------------- #
    def _connect(self) -> None:
        if self._stock is not None:
            return
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.historical.stock import StockHistoricalDataClient
        except ImportError as exc:  # pragma: no cover
            raise DataError("alpaca-py is not installed - run `make install`") from exc

        creds = self.credentials
        if not creds or not creds.configured:
            raise DataError("Alpaca credentials missing - fill in .env")
        self._stock = StockHistoricalDataClient(creds.api_key, creds.secret_key)
        self._option = OptionHistoricalDataClient(creds.api_key, creds.secret_key)

    def _cached(self, key: str, produce: Any) -> Any:
        if not self.cfg.data.cache.enabled:
            return produce()
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.cfg.data.cache.ttl_seconds:
            return hit[1]
        value = produce()
        self._cache[key] = (now, value)
        return value

    # -- prices ------------------------------------------------------------ #
    def spot(self, symbol: str) -> float:
        def fetch() -> float:
            from alpaca.data.requests import StockLatestTradeRequest

            self._connect()
            self._limiter.acquire()
            result = self._stock.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.cfg.data.stock_feed)
            )
            trade = result.get(symbol)
            if trade is None:
                raise DataError(f"no last trade for {symbol}")
            return float(trade.price)

        return self._cached(f"spot:{symbol}", fetch)

    def bars(
        self, symbol: str, lookback_days: int = 90, timeframe: str = "1Day"
    ) -> list[dict[str, Any]]:
        def fetch() -> list[dict[str, Any]]:
            from alpaca.data.requests import StockBarsRequest

            self._connect()
            self._limiter.acquire()
            tf = _timeframe(timeframe)
            end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                minutes=self.cfg.data.delayed_minutes
            )
            start = end - dt.timedelta(days=int(lookback_days * 1.6) + 5)
            resp = self._stock.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=tf,
                    start=start,
                    end=end,
                    feed=self.cfg.data.stock_feed,
                )
            )
            rows = resp.data.get(symbol, []) if hasattr(resp, "data") else []
            return [
                {
                    "timestamp": b.timestamp,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume or 0),
                }
                for b in rows
            ]

        return self._cached(f"bars:{symbol}:{lookback_days}:{timeframe}", fetch)

    # -- options ----------------------------------------------------------- #
    def option_chain(
        self,
        symbol: str,
        min_dte: int | None = None,
        max_dte: int | None = None,
        strike_low: float | None = None,
        strike_high: float | None = None,
    ) -> list[OptionQuote]:
        opts = self.cfg.options
        min_dte = opts.min_days_to_expiry if min_dte is None else min_dte
        max_dte = opts.max_days_to_expiry if max_dte is None else max_dte

        if strike_low is None or strike_high is None:
            strike_low, strike_high = self.chain_strike_window(self.spot(symbol))

        key = f"chain:{symbol}:{min_dte}:{max_dte}:{strike_low}:{strike_high}"

        def fetch() -> list[OptionQuote]:
            from alpaca.data.enums import OptionsFeed
            from alpaca.data.requests import OptionChainRequest

            self._connect()
            self._limiter.acquire()
            today = dt.date.today()
            request = OptionChainRequest(
                underlying_symbol=symbol,
                feed=OptionsFeed(self.cfg.data.option_feed),
                expiration_date_gte=(today + dt.timedelta(days=min_dte)).isoformat(),
                expiration_date_lte=(today + dt.timedelta(days=max_dte)).isoformat(),
                strike_price_gte=float(strike_low),
                strike_price_lte=float(strike_high),
            )
            try:
                snapshots = self._option.get_option_chain(request)
            except Exception as exc:  # noqa: BLE001
                raise DataError(f"option chain fetch failed for {symbol}: {exc}") from exc
            return [
                q for q in (_to_quote(sym, snap) for sym, snap in snapshots.items())
                if q is not None
            ]

        return self._cached(key, fetch)

    def quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        from alpaca.data.requests import OptionSnapshotRequest

        self._connect()
        self._limiter.acquire()
        snaps = self._option.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=symbols)
        )
        out: dict[str, OptionQuote] = {}
        for sym, snap in snaps.items():
            quote = _to_quote(sym, snap)
            if quote is not None:
                out[sym] = quote
        return out

    # -- the object strategies actually read -------------------------------- #
    def context(self, symbol: str, lookback_days: int = 90) -> MarketContext:
        spot = self.spot(symbol)
        history = self.bars(symbol, lookback_days=lookback_days)
        chain = self.option_chain(symbol)

        atm_iv = _atm_iv(chain, spot)
        if atm_iv is not None:
            hist = self._iv_history.setdefault(symbol, [])
            hist.append(atm_iv)
            del hist[:-120]

        intraday: list[dict[str, Any]] = []
        if self.cfg.data.fetch_intraday:
            try:
                intraday = self.bars(
                    symbol,
                    lookback_days=self.cfg.data.intraday_lookback_days,
                    timeframe=self.cfg.data.intraday_timeframe,
                )
            except (DataError, Exception):  # noqa: BLE001
                intraday = []

        return MarketContext(
            symbol=symbol,
            asof=dt.datetime.now(dt.timezone.utc),
            spot=spot,
            prev_close=history[-2]["close"] if len(history) > 1 else None,
            bars=history,
            intraday_bars=intraday,
            chain=chain,
            realised_vol=vol_estimator(self.cfg.data.volatility_estimator)(history, 20),
            implied_vol=atm_iv,
            iv_rank=iv_rank(atm_iv, self._iv_history.get(symbol, [])),
            trend_strength=trend_strength(history),
            adx=adx(history),
            volume_ratio=volume_ratio(history),
        )


# --------------------------------------------------------------------------- #
def _timeframe(spec: str) -> Any:
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    table = {
        "1min": TimeFrame(1, TimeFrameUnit.Minute),
        "5min": TimeFrame(5, TimeFrameUnit.Minute),
        "15min": TimeFrame(15, TimeFrameUnit.Minute),
        "1hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1day": TimeFrame.Day,
    }
    return table.get(spec.lower().replace(" ", ""), TimeFrame.Day)


def _to_quote(symbol: str, snap: Any) -> OptionQuote | None:
    try:
        occ = parse_occ(symbol)
    except ValueError:
        return None
    quote = getattr(snap, "latest_quote", None)
    trade = getattr(snap, "latest_trade", None)
    raw_greeks = getattr(snap, "greeks", None)
    return OptionQuote(
        symbol=symbol,
        underlying=occ.root,
        expiry=occ.expiry,
        strike=occ.strike,
        right=Right(occ.right.value),
        bid=float(quote.bid_price) if quote and quote.bid_price else None,
        ask=float(quote.ask_price) if quote and quote.ask_price else None,
        last=float(trade.price) if trade and trade.price else None,
        implied_volatility=_f(getattr(snap, "implied_volatility", None)),
        greeks=Greeks(
            delta=_f(getattr(raw_greeks, "delta", None)),
            gamma=_f(getattr(raw_greeks, "gamma", None)),
            theta=_f(getattr(raw_greeks, "theta", None)),
            vega=_f(getattr(raw_greeks, "vega", None)),
            rho=_f(getattr(raw_greeks, "rho", None)),
        ),
        asof=getattr(quote, "timestamp", None),
    )


def _atm_iv(chain: list[OptionQuote], spot: float) -> float | None:
    if not chain:
        return None
    nearest_expiry = min(q.expiry for q in chain)
    candidates = [
        q for q in chain if q.expiry == nearest_expiry and q.implied_volatility
    ]
    if not candidates:
        return None
    closest = min(candidates, key=lambda q: abs(q.strike - spot))
    return closest.implied_volatility


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
