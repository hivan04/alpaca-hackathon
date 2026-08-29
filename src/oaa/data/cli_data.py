"""Market data over the Alpaca CLI.

Execution and backtesting both run through the `alpaca` binary, so the data
that feeds them comes from the same place. Two reasons that matters beyond the
hackathon's CLI requirement:

  * **Auditability.** Every fetch is a command you can paste into a terminal and
    rerun. When a backtest disagrees with live, you can replay the exact call.
  * **One code path.** The backtest and the live agent read bars through the
    same provider, so a discrepancy is a data problem, not a plumbing problem.

The CLI is spec-generated and its JSON mirrors the REST API, but the exact
envelope has changed between alpha builds. Everything here normalises
defensively — `alpaca data bars --schema` is the source of truth if a shape
shifts under you.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Sequence
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
from oaa.data.iv_history import IVHistoryStore
from oaa.options.occ import parse_occ

log = get_logger("data.cli")


@data_registry.register("cli")
class AlpacaCliDataProvider(MarketDataProvider):
    """MarketDataProvider backed by `alpaca data ...` subprocess calls."""

    name = "alpaca-cli"

    def __init__(self, cfg: Any, credentials: Any = None) -> None:
        super().__init__(cfg, credentials)
        self.binary = cfg.broker.cli.binary
        self.timeout = max(60, cfg.broker.cli.timeout_seconds)
        self.profile = cfg.broker.cli.profile
        self._cache: dict[str, tuple[float, Any]] = {}
        self._iv_history = IVHistoryStore.open(getattr(cfg.telemetry, "run_dir", None))
        self._calls: deque[float] = deque()

    # ------------------------------------------------------------------ #
    # process plumbing
    # ------------------------------------------------------------------ #
    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        creds = self.credentials
        if creds and getattr(creds, "configured", False):
            env["ALPACA_API_KEY"] = creds.api_key
            env["ALPACA_SECRET_KEY"] = creds.secret_key
        env["ALPACA_LIVE_TRADE"] = "false" if self.cfg.broker.paper else "true"
        env["ALPACA_OUTPUT"] = "json"
        return env

    def _throttle(self) -> None:
        limit = self.cfg.data.rate_limit.requests_per_minute
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= limit:
            sleep_for = 60 - (now - self._calls[0]) + 0.05
            log.debug("CLI rate limit reached, sleeping %.1fs", sleep_for)
            time.sleep(max(0.0, sleep_for))
        self._calls.append(time.monotonic())

    def run(self, args: Sequence[str]) -> Any:
        if shutil.which(self.binary) is None:
            raise DataError(
                f"'{self.binary}' is not on PATH. Install it with:\n"
                "  brew install alpacahq/tap/cli\n"
                "  go install github.com/alpacahq/cli/cmd/alpaca@latest"
            )
        cmd = [self.binary, *args]
        if self.profile:
            cmd += ["--profile", self.profile]
        self._throttle()
        log.debug("$ %s", " ".join(cmd))
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=self.timeout,
            env=self._env(), check=False,
        )
        if proc.returncode != 0:
            raise DataError(
                f"`{' '.join(cmd)}` exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
            )
        text = (proc.stdout or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DataError(f"non-JSON from CLI: {text[:200]}") from exc

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

    # ------------------------------------------------------------------ #
    # prices
    # ------------------------------------------------------------------ #
    def spot(self, symbol: str) -> float:
        def fetch() -> float:
            payload = self.run(["data", "latest-trade", "--symbol", symbol])
            trade = _first(payload, ("trade", "trades"), symbol)
            price = _num(trade.get("p") if isinstance(trade, dict) else None) or _num(
                trade.get("price") if isinstance(trade, dict) else None
            )
            if price is None:
                raise DataError(f"no last trade for {symbol}")
            return price

        return self._cached(f"spot:{symbol}", fetch)

    def bars(
        self,
        symbol: str,
        lookback_days: int = 500,
        timeframe: str = "1Day",
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> list[dict[str, Any]]:
        """Daily (or intraday) bars via `alpaca data bars`, paginated."""

        def fetch() -> list[dict[str, Any]]:
            stop = end or dt.date.today()
            begin = start or (stop - dt.timedelta(days=int(lookback_days * 1.5) + 10))
            rows: list[dict[str, Any]] = []
            page_token: str | None = None

            for _ in range(20):  # hard page cap; 20k bars is plenty
                args = [
                    "data", "bars",
                    "--symbol", symbol,
                    "--timeframe", timeframe,
                    "--start", begin.isoformat(),
                    "--end", stop.isoformat(),
                    "--feed", self.cfg.data.stock_feed,
                    "--adjustment", "all",
                    "--limit", "10000",
                ]
                if page_token:
                    args += ["--page-token", page_token]
                payload = self.run(args)
                rows.extend(_bar_rows(payload, symbol))
                page_token = payload.get("next_page_token") if isinstance(payload, dict) else None
                if not page_token:
                    break

            if not rows:
                raise DataError(f"no bars returned for {symbol} ({begin} to {stop})")
            rows.sort(key=lambda b: b["timestamp"])
            return rows

        key = f"bars:{symbol}:{timeframe}:{lookback_days}:{start}:{end}"
        return self._cached(key, fetch)

    # ------------------------------------------------------------------ #
    # options
    # ------------------------------------------------------------------ #
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

        def fetch() -> list[OptionQuote]:
            today = dt.date.today()
            args = [
                "data", "option", "chain",
                "--underlying-symbol", symbol,
                "--feed", self.cfg.data.option_feed,
                "--expiration-date-gte", (today + dt.timedelta(days=min_dte)).isoformat(),
                "--expiration-date-lte", (today + dt.timedelta(days=max_dte)).isoformat(),
                "--strike-price-gte", f"{strike_low:.2f}",
                "--strike-price-lte", f"{strike_high:.2f}",
            ]
            payload = self.run(args)
            snapshots = payload.get("snapshots", payload) if isinstance(payload, dict) else {}
            quotes = [
                q for q in (
                    _to_quote(sym, snap) for sym, snap in (snapshots or {}).items()
                ) if q is not None
            ]
            log.debug("%s: %d contracts from the CLI chain", symbol, len(quotes))
            return quotes

        key = f"chain:{symbol}:{min_dte}:{max_dte}:{strike_low}:{strike_high}"
        return self._cached(key, fetch)

    def news(self, symbol: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Recent headlines for one symbol via `alpaca data news`.

        Alpaca answers this itself, so there is no scraper to maintain, no ToS
        exposure, and nothing to break on day three of a seven-day window.
        """
        cap = int(limit or self.cfg.data.news_limit)
        hours = int(self.cfg.data.news_lookback_hours)
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)

        def fetch() -> list[dict[str, Any]]:
            args = [
                "data", "news",
                "--symbols", symbol,
                "--limit", str(cap),
                "--start", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ]
            payload = self.run(args)
            rows = payload.get("news", payload) if isinstance(payload, dict) else payload
            return [r for r in (rows or []) if isinstance(r, dict)]

        try:
            return self._cached(f"news:{symbol}:{cap}", fetch)
        except DataError as exc:
            log.debug("%s: no news (%s)", symbol, exc)
            return []

    def option_contracts(self, symbol: str, **flags: Any) -> list[dict[str, Any]]:
        """Reference data via `alpaca option contracts` (open interest, tradability)."""
        args = ["option", "contracts", "--underlying-symbols", symbol]
        for key, value in flags.items():
            args += [f"--{key.replace('_', '-')}", str(value)]
        payload = self.run(args)
        if isinstance(payload, dict):
            return payload.get("option_contracts", []) or []
        return payload if isinstance(payload, list) else []

    # ------------------------------------------------------------------ #
    def context(self, symbol: str, lookback_days: int = 400) -> MarketContext:
        history = self.bars(symbol, lookback_days=lookback_days)
        try:
            spot = self.spot(symbol)
        except DataError:
            spot = float(history[-1]["close"])

        try:
            chain = self.option_chain(symbol)
        except DataError as exc:
            log.debug("%s: no chain (%s) - continuing without an overlay", symbol, exc)
            chain = []

        atm_iv = _atm_iv(chain, spot)
        # One observation per DAY, seeded from the replay's own IV model so the
        # rank means something on the first session rather than after a month
        # of accumulation, and persisted so a restart resumes.
        if atm_iv is not None:
            if self._iv_history.needs_seed(symbol):
                self._iv_history.seed_from_bars(symbol, history)
            self._iv_history.observe(symbol, atm_iv)
            self._iv_history.save()

        intraday: list[dict[str, Any]] = []
        if self.cfg.data.fetch_intraday:
            try:
                intraday = self.bars(
                    symbol,
                    lookback_days=self.cfg.data.intraday_lookback_days,
                    timeframe=self.cfg.data.intraday_timeframe,
                )
            except DataError as exc:
                log.debug("%s: no intraday bars (%s)", symbol, exc)

        headlines = self.news(symbol) if self.cfg.data.fetch_news else []

        return MarketContext(
            symbol=symbol,
            asof=dt.datetime.now(dt.timezone.utc),
            spot=spot,
            prev_close=history[-2]["close"] if len(history) > 1 else None,
            bars=history,
            intraday_bars=intraday,
            news=headlines,
            chain=chain,
            realised_vol=vol_estimator(self.cfg.data.volatility_estimator)(history, 20),
            implied_vol=atm_iv,
            iv_rank=iv_rank(atm_iv, self._iv_history.series(symbol)),
            trend_strength=trend_strength(history),
            adx=adx(history),
            volume_ratio=volume_ratio(history),
        )


# --------------------------------------------------------------------------- #
# normalisation - the CLI envelope has moved between alpha builds
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first(payload: Any, keys: tuple[str, ...], symbol: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    for key in keys:
        node = payload.get(key)
        if isinstance(node, dict):
            return node.get(symbol, node)
        if isinstance(node, list) and node:
            return node[0]
    return payload


def _bar_rows(payload: Any, symbol: str) -> list[dict[str, Any]]:
    """Accept `{"bars": [...]}`, `{"bars": {"SPY": [...]}}`, or a bare list."""
    node: Any = payload
    if isinstance(payload, dict):
        node = payload.get("bars", payload)
        if isinstance(node, dict):
            node = node.get(symbol) or node.get(symbol.upper()) or []
    if not isinstance(node, list):
        return []

    rows: list[dict[str, Any]] = []
    for raw in node:
        if not isinstance(raw, dict):
            continue
        stamp = raw.get("t") or raw.get("timestamp")
        parsed = _parse_ts(stamp)
        if parsed is None:
            continue
        rows.append({
            "timestamp": parsed,
            "open": _num(raw.get("o") if "o" in raw else raw.get("open")) or 0.0,
            "high": _num(raw.get("h") if "h" in raw else raw.get("high")) or 0.0,
            "low": _num(raw.get("l") if "l" in raw else raw.get("low")) or 0.0,
            "close": _num(raw.get("c") if "c" in raw else raw.get("close")) or 0.0,
            "volume": _num(raw.get("v") if "v" in raw else raw.get("volume")) or 0.0,
        })
    return rows


def _parse_ts(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _to_quote(symbol: str, snap: Any) -> OptionQuote | None:
    if not isinstance(snap, dict):
        return None
    try:
        occ = parse_occ(symbol)
    except ValueError:
        return None

    quote = snap.get("latestQuote") or snap.get("latest_quote") or {}
    trade = snap.get("latestTrade") or snap.get("latest_trade") or {}
    greeks = snap.get("greeks") or {}

    return OptionQuote(
        symbol=symbol,
        underlying=occ.root,
        expiry=occ.expiry,
        strike=occ.strike,
        right=Right(occ.right.value),
        bid=_num(quote.get("bp") if "bp" in quote else quote.get("bid_price")),
        ask=_num(quote.get("ap") if "ap" in quote else quote.get("ask_price")),
        last=_num(trade.get("p") if "p" in trade else trade.get("price")),
        implied_volatility=_num(
            snap.get("impliedVolatility") or snap.get("implied_volatility")
        ),
        greeks=Greeks(
            delta=_num(greeks.get("delta")),
            gamma=_num(greeks.get("gamma")),
            theta=_num(greeks.get("theta")),
            vega=_num(greeks.get("vega")),
            rho=_num(greeks.get("rho")),
        ),
        open_interest=int(_num(snap.get("openInterest") or snap.get("open_interest")) or 0) or None,
    )


def _atm_iv(chain: list[OptionQuote], spot: float) -> float | None:
    if not chain:
        return None
    nearest = min(q.expiry for q in chain)
    candidates = [q for q in chain if q.expiry == nearest and q.implied_volatility]
    if not candidates:
        return None
    return min(candidates, key=lambda q: abs(q.strike - spot)).implied_volatility
