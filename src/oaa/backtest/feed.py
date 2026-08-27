"""Historical market data for the replay harness, straight from Alpaca.

Five real Alpaca endpoints feed the backtest:

    StockHistoricalDataClient.get_stock_bars     daily bars    - the price path
    ... same, 5Min                               intraday      - session VWAP
    NewsClient.get_news                          headlines     - the catalyst gate
    TradingClient.get_option_contracts           the ladder    - real listed
                                                                 strikes and
                                                                 expiries, expired
                                                                 contracts included
    OptionHistoricalDataClient.get_option_bars   option prices - what the contract
                                                                 actually traded at

That last pair is the difference between a replay of the strategy and a replay
of a pricing model. Alpaca serves historical option bars back to February 2024
on the free Basic plan - the free-tier restriction is the most recent fifteen
minutes, not history - so option marks do not have to be invented. What is
still absent is historical greeks and implied vol, which are snapshot-only; the
harness recovers implied vol by inverting Black-Scholes on the real traded
price (`pricing.implied_vol_from_price`) rather than modelling it.

A bar exists only on days the contract actually traded, so coverage is not
total. `realchain.py` falls back to the model for those contract-days and every
mark records which it was.

Everything is cached to disk on first fetch, keyed by symbol, timeframe and
window, so a re-run of the same backtest makes no network calls at all and the
numbers are reproducible offline.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oaa.core.errors import DataError
from oaa.core.logging import get_logger

log = get_logger("backtest.feed")


def _iso(value: Any) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


@dataclass
class HistoricalFeed:
    """Bars and news over a fixed historical window, cached to disk."""

    api_key: str
    secret_key: str
    cache_dir: Path
    stock_feed: str = "iex"
    #: seconds; a cached window older than this is still used - history does
    #: not change. Only an incomplete trailing window is re-fetched.
    offline: bool = False

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stock: Any = None
        self._news: Any = None
        self._option: Any = None
        self._trading: Any = None

    # -- cache ----------------------------------------------------------- #
    def _path(self, kind: str, key: str) -> Path:
        safe = key.replace("/", "_").replace(":", "-")
        return self.cache_dir / f"{kind}__{safe}.json"

    def _cached(self, kind: str, key: str, produce: Any) -> Any:
        path = self._path(kind, key)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                log.warning("corrupt cache %s - refetching", path.name)
        if self.offline:
            raise DataError(
                f"offline and no cached {kind} for {key}. Run once with network "
                "access to populate data/cache/backtest."
            )
        payload = produce()
        path.write_text(json.dumps(payload, default=str))
        return payload

    def cache_manifest(self) -> list[str]:
        return sorted(p.name for p in self.cache_dir.glob("*.json"))

    # -- clients --------------------------------------------------------- #
    def _stock_client(self) -> Any:
        if self._stock is None:
            from alpaca.data.historical.stock import StockHistoricalDataClient

            if not (self.api_key and self.secret_key):
                raise DataError(
                    "no Alpaca keys resolved - the backtest needs them for "
                    "historical bars. Check .env and the active profile."
                )
            self._stock = StockHistoricalDataClient(self.api_key, self.secret_key)
        return self._stock

    def _news_client(self) -> Any:
        if self._news is None:
            from alpaca.data.historical.news import NewsClient

            self._news = NewsClient(self.api_key, self.secret_key)
        return self._news

    # -- bars ------------------------------------------------------------ #
    def bars(
        self,
        symbol: str,
        start: dt.date,
        end: dt.date,
        timeframe: str = "1Day",
    ) -> list[dict[str, Any]]:
        key = f"{symbol}_{timeframe}_{start}_{end}_{self.stock_feed}"

        def fetch() -> list[dict[str, Any]]:
            from alpaca.data.requests import StockBarsRequest

            client = self._stock_client()
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=_timeframe(timeframe),
                start=dt.datetime.combine(start, dt.time(0, 0), tzinfo=dt.timezone.utc),
                end=dt.datetime.combine(end, dt.time(23, 59), tzinfo=dt.timezone.utc),
                feed=self.stock_feed,
            )
            try:
                resp = client.get_stock_bars(request)
            except Exception as exc:  # noqa: BLE001
                raise DataError(f"bar fetch failed for {symbol}: {exc}") from exc
            rows = resp.data.get(symbol, []) if hasattr(resp, "data") else []
            out = [
                {
                    "timestamp": _iso(b.timestamp),
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume or 0),
                }
                for b in rows
            ]
            log.info("%s %s: %d bars %s -> %s", symbol, timeframe, len(out), start, end)
            return out

        rows = self._cached("bars", key, fetch)
        for row in rows:
            row["timestamp"] = _parse(row["timestamp"])
        return rows

    # -- news ------------------------------------------------------------ #
    def news(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Every headline for these symbols over the window, oldest first.

        The catalyst gate asks "was there news behind this move, and was it
        about THIS name". Answering it in replay needs the real headline
        timestamps, which is exactly what this endpoint gives.
        """
        joined = "-".join(sorted(s.upper() for s in symbols))
        key = f"{joined}_{start}_{end}"

        def fetch() -> list[dict[str, Any]]:
            from alpaca.data.requests import NewsRequest

            client = self._news_client()
            collected: list[dict[str, Any]] = []
            token: str | None = None
            for _ in range(40):  # hard page cap; 40 x 50 is far more than a week needs
                request = NewsRequest(
                    symbols=",".join(sorted(s.upper() for s in symbols)),
                    start=dt.datetime.combine(start, dt.time(0, 0), tzinfo=dt.timezone.utc),
                    end=dt.datetime.combine(end, dt.time(23, 59), tzinfo=dt.timezone.utc),
                    limit=limit,
                    sort="asc",
                    include_content=False,
                    exclude_contentless=False,
                    page_token=token,
                )
                try:
                    resp = client.get_news(request)
                except Exception as exc:  # noqa: BLE001
                    log.warning("news fetch failed: %s", exc)
                    break
                items = getattr(resp, "data", {}).get("news", []) if hasattr(resp, "data") else []
                for article in items:
                    collected.append(
                        {
                            "id": getattr(article, "id", None),
                            "created_at": _iso(getattr(article, "created_at", None)),
                            "updated_at": _iso(getattr(article, "updated_at", None)),
                            "headline": getattr(article, "headline", "") or "",
                            "summary": getattr(article, "summary", "") or "",
                            "source": getattr(article, "source", "") or "",
                            "symbols": list(getattr(article, "symbols", []) or []),
                            "url": getattr(article, "url", "") or "",
                        }
                    )
                token = getattr(resp, "next_page_token", None)
                if not token:
                    break
            log.info("news: %d headlines for %s", len(collected), joined)
            return collected

        return self._cached("news", key, fetch)


    # -- option contracts ------------------------------------------------- #
    def _trading_client(self) -> Any:
        if getattr(self, "_trading", None) is None:
            from alpaca.trading.client import TradingClient

            if not (self.api_key and self.secret_key):
                raise DataError("no Alpaca keys resolved - cannot list option contracts")
            self._trading = TradingClient(self.api_key, self.secret_key, paper=True)
        return self._trading

    def option_contracts(
        self,
        underlying: str,
        expiry_from: dt.date,
        expiry_to: dt.date,
        strike_low: float | None = None,
        strike_high: float | None = None,
        page_limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Every listed contract in the window, EXPIRED ONES INCLUDED.

        `status=inactive` is what makes a historical backtest possible: by the
        time you replay June, June's contracts have expired and an active-only
        query returns nothing. Both statuses are fetched and merged.
        """
        key = f"{underlying}_{expiry_from}_{expiry_to}_{strike_low}_{strike_high}"

        def fetch() -> list[dict[str, Any]]:
            from alpaca.trading.requests import GetOptionContractsRequest

            client = self._trading_client()
            found: dict[str, dict[str, Any]] = {}
            for status in ("active", "inactive"):
                token: str | None = None
                for _ in range(page_limit):
                    request = GetOptionContractsRequest(
                        underlying_symbols=[underlying],
                        status=status,
                        expiration_date_gte=expiry_from,
                        expiration_date_lte=expiry_to,
                        strike_price_gte=str(strike_low) if strike_low else None,
                        strike_price_lte=str(strike_high) if strike_high else None,
                        limit=10_000,
                        page_token=token,
                    )
                    try:
                        response = client.get_option_contracts(request)
                    except Exception as exc:  # noqa: BLE001
                        raise DataError(
                            f"contract listing failed for {underlying}: {exc}"
                        ) from exc
                    for contract in getattr(response, "option_contracts", []) or []:
                        found[contract.symbol] = {
                            "symbol": contract.symbol,
                            "underlying": contract.underlying_symbol,
                            "expiry": _iso(contract.expiration_date),
                            "strike": float(contract.strike_price),
                            "type": str(getattr(contract.type, "value", contract.type)),
                            "style": str(getattr(contract.style, "value", contract.style)),
                            "size": int(contract.size or 100),
                            # A CURRENT snapshot, not the OI on the replayed day -
                            # Alpaca serves no historical open interest. Used only
                            # as a liquidity hint and labelled as one.
                            "open_interest": (
                                int(contract.open_interest) if contract.open_interest else None
                            ),
                            "open_interest_date": _iso(contract.open_interest_date)
                            if contract.open_interest_date else None,
                        }
                    token = getattr(response, "next_page_token", None)
                    if not token:
                        break
            rows = sorted(found.values(), key=lambda c: (c["expiry"], c["strike"], c["type"]))
            log.info("%s: %d contracts expiring %s..%s", underlying, len(rows),
                     expiry_from, expiry_to)
            return rows

        return self._cached("contracts", key, fetch)

    # -- option bars ------------------------------------------------------- #
    def option_bars(
        self,
        symbols: list[str],
        start: dt.date,
        end: dt.date,
        timeframe: str = "1Day",
        batch: int = 100,
    ) -> dict[str, list[dict[str, Any]]]:
        """Daily OHLCV per contract. The endpoint caps at 100 symbols a call.

        A contract only produces a bar on a day it TRADED, so the result is
        sparse for anything away from the money. That sparsity is data, not an
        error: a contract with no prints is one the strategy could not have got
        a sensible fill on.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        ordered, rejected = _standard_occ(symbols)
        if rejected:
            # Adjusted contracts - a leading digit marks a strike or deliverable
            # changed by a corporate action - are listed by the contracts
            # endpoint but REJECTED by the bars endpoint's symbol regex. One of
            # them in a batch used to fail the whole fetch, which silently sent
            # the entire run back to the modelled chain. They are dropped here
            # and counted, because their deliverable is not 100 shares and the
            # strategy should not be trading them anyway.
            log.info(
                "dropped %d adjusted/non-standard contracts (e.g. %s)",
                len(rejected), rejected[0],
            )
        for index in range(0, len(ordered), batch):
            chunk = ordered[index : index + batch]
            key = f"{start}_{end}_{timeframe}_{_digest(chunk)}"

            def fetch(chunk: list[str] = chunk) -> dict[str, list[dict[str, Any]]]:
                from alpaca.data.historical.option import OptionHistoricalDataClient
                from alpaca.data.requests import OptionBarsRequest

                if self._option is None:
                    self._option = OptionHistoricalDataClient(self.api_key, self.secret_key)
                request = OptionBarsRequest(
                    symbol_or_symbols=chunk,
                    timeframe=_timeframe(timeframe),
                    start=dt.datetime.combine(start, dt.time(0, 0), tzinfo=dt.timezone.utc),
                    end=dt.datetime.combine(end, dt.time(23, 59), tzinfo=dt.timezone.utc),
                )
                try:
                    response = self._option.get_option_bars(request)
                except Exception as exc:  # noqa: BLE001
                    raise DataError(f"option bar fetch failed: {exc}") from exc
                data = getattr(response, "data", {}) or {}
                payload: dict[str, list[dict[str, Any]]] = {}
                for symbol, bars in data.items():
                    payload[symbol] = [
                        {
                            "timestamp": _iso(b.timestamp),
                            "open": float(b.open),
                            "high": float(b.high),
                            "low": float(b.low),
                            "close": float(b.close),
                            "volume": float(b.volume or 0),
                            "trade_count": int(getattr(b, "trade_count", 0) or 0),
                            "vwap": float(getattr(b, "vwap", 0) or 0),
                        }
                        for b in bars
                    ]
                log.info(
                    "option bars: %d/%d contracts returned data (%s..%s)",
                    len(payload), len(chunk), start, end,
                )
                return payload

            try:
                payload = self._cached("optbars", key, fetch)
            except DataError as exc:
                # One bad batch must not cost the other ninety-nine. Coverage
                # drops, and the coverage number is reported, so a partial
                # fetch degrades visibly instead of silently.
                log.warning(
                    "option bars: batch %d-%d failed (%s) - those contracts "
                    "will be modelled",
                    index, index + len(chunk), str(exc)[:160],
                )
                continue
            for symbol, rows in payload.items():
                for row in rows:
                    row["timestamp"] = _parse(row["timestamp"])
                out[symbol] = rows
        return out


# --------------------------------------------------------------------------- #
_OCC = __import__("re").compile(r"^[A-Z]{1,5}\d{6}[CP]\d{8}$")


def _standard_occ(symbols: list[str]) -> tuple[list[str], list[str]]:
    """Split standard OCC symbols from adjusted ones the bars endpoint refuses."""
    good, bad = [], []
    for symbol in sorted({s.upper() for s in symbols}):
        (good if _OCC.match(symbol) else bad).append(symbol)
    return good, bad


def _digest(symbols: list[str]) -> str:
    import hashlib

    return hashlib.sha256("|".join(symbols).encode()).hexdigest()[:16]


def _timeframe(spec: str) -> Any:
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    table = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1Day": TimeFrame(1, TimeFrameUnit.Day),
    }
    if spec not in table:
        raise DataError(f"unsupported timeframe {spec}")
    return table[spec]


def _parse(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
