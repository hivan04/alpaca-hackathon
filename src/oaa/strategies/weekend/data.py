"""Crypto bars, straight from Alpaca.

`/v1beta3/crypto/us/bars` needs no key and no entitlement - crypto has no SIP,
no OPRA and no 15-minute delay, which removes the single largest data caveat
the options books carry. The same endpoint serves the backtest and the live
loop, so a replay reads exactly what the agent read.

Bars come back as the repo's plain dict shape (`open/high/low/close/volume/t`)
so `oaa.data.indicators` works on them unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from oaa.core.logging import get_logger

log = get_logger("weekend.data")

BASE = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
Bar = dict[str, Any]
UTC = dt.timezone.utc


def _headers() -> dict[str, str]:
    """Auth is optional here, but sending it when present keeps this request on
    the same rate-limit bucket as the rest of the account's traffic."""
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if key and secret:
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    return {}


def fetch_bars(
    symbol: str = "BTC/USD",
    timeframe: str = "15Min",
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    limit: int = 10000,
    timeout: float = 30.0,
    max_pages: int = 200,
) -> list[Bar]:
    """Ascending bars in [start, end). Follows pagination to completion."""
    end = end or dt.datetime.now(UTC)
    start = start or (end - dt.timedelta(days=30))
    params: dict[str, Any] = {
        "symbols": symbol,
        "timeframe": timeframe,
        "start": _iso(start),
        "end": _iso(end),
        "limit": limit,
        "sort": "asc",
    }
    out: list[Bar] = []
    token: str | None = None
    with httpx.Client(timeout=timeout, headers=_headers()) as client:
        for page in range(max_pages):
            if token:
                params["page_token"] = token
            response = client.get(BASE, params=params)
            if response.status_code == 429:  # be a good citizen, then retry
                time.sleep(2.0)
                continue
            response.raise_for_status()
            payload = response.json()
            chunk = (payload.get("bars") or {}).get(symbol) or []
            out.extend(_normalise(b) for b in chunk)
            token = payload.get("next_page_token")
            if not token:
                break
            if page == max_pages - 1:
                log.warning("stopped paginating %s at %d pages", symbol, max_pages)
    log.info("fetched %d %s %s bars", len(out), symbol, timeframe)
    return out


def latest_quote(symbol: str = "BTC/USD", timeout: float = 10.0) -> dict[str, float] | None:
    """Best bid/ask, for the spread gate and for limit pricing."""
    url = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"
    try:
        with httpx.Client(timeout=timeout, headers=_headers()) as client:
            response = client.get(url, params={"symbols": symbol})
            response.raise_for_status()
            quote = (response.json().get("quotes") or {}).get(symbol)
    except Exception as exc:  # noqa: BLE001 - a missing quote is a rejection
        log.warning("quote unavailable for %s: %s", symbol, exc)
        return None
    if not quote:
        return None
    bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    return {"bid": bid, "ask": ask, "mid": mid, "spread_bp": (ask - bid) / mid * 1e4}


# --------------------------------------------------------------------------- #
def _normalise(bar: dict[str, Any]) -> Bar:
    return {
        "t": bar.get("t"),
        "timestamp": bar.get("t"),
        "open": float(bar.get("o", 0)),
        "high": float(bar.get("h", 0)),
        "low": float(bar.get("l", 0)),
        "close": float(bar.get("c", 0)),
        "volume": float(bar.get("v", 0)),
        "trades": bar.get("n"),
        "vwap": bar.get("vw"),
    }


def _iso(ts: dt.datetime) -> str:
    ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def bar_time(bar: Bar) -> dt.datetime:
    raw = bar.get("t") or bar.get("timestamp")
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)


# --------------------------------------------------------------------------- #
# a tiny disk cache, so a backtest re-run costs nothing
# --------------------------------------------------------------------------- #
class InsufficientHistory(RuntimeError):
    """The cache cannot cover the range asked for.

    Raised rather than logged. A study that asks for 400 days, silently
    receives 10, and prints a 100%-hit-rate table with a t-stat of 14 is worse
    than one that fails: the numbers look like evidence and are one weekend.
    """


def cached_bars(
    symbol: str,
    timeframe: str,
    start: dt.datetime,
    end: dt.datetime,
    cache_dir: str | Path = "data/cache/weekend",
    refresh: bool = False,
    min_coverage: float = 0.6,
) -> list[Bar]:
    """Bars for [start, end), served from disk when disk can cover it.

    The lookup is deliberately tolerant: any cached file for this symbol and
    timeframe whose span contains the request is used and sliced. Requiring an
    exact filename match meant a backtest run the day after the download
    silently went back to the network - which on a filtered egress is not a
    slow path, it is a failed one.

    `scripts/fetch_weekend_bars.py` writes files in this shape.
    """
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol.replace('/', '')}_{timeframe}"
    path = directory / f"{stem}_{start:%Y%m%d}_{end:%Y%m%d}.json"

    best: tuple[float, str, str, str] | None = None   # (coverage, name, from, to)
    if not refresh:
        # Try EVERY candidate before giving up. Filenames sort by their start
        # date, so the longest history is not necessarily first - an early
        # `raise` here meant a freshly downloaded 410-day file was skipped in
        # favour of a 10-day one that happened to sort later.
        for candidate in [path, *sorted(directory.glob(f"{stem}_*.json"), reverse=True)]:
            if not candidate.exists():
                continue
            try:
                bars = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("cache file %s unreadable, skipping", candidate)
                continue
            if not bars:
                continue
            sliced = [b for b in bars if start <= bar_time(b) <= end]
            if len(sliced) < 100:
                continue
            covered_from, covered_to = bar_time(bars[0]), bar_time(bars[-1])
            asked = (end - start).total_seconds()
            have = (min(covered_to, end) - max(covered_from, start)).total_seconds()
            coverage = have / asked if asked > 0 else 1.0
            if coverage < min_coverage:
                if best is None or coverage > best[0]:
                    best = (
                        coverage, candidate.name,
                        f"{covered_from:%Y-%m-%d}", f"{covered_to:%Y-%m-%d}",
                    )
                continue
            log.info(
                "served %d bars from %s (%s -> %s)",
                len(sliced), candidate.name, covered_from.date(), covered_to.date(),
            )
            return sliced

    if best is not None:
        coverage, name, covered_from, covered_to = best
        asked_days = (end - start).total_seconds() / 86400
        raise InsufficientHistory(
            f"asked for {symbol} {timeframe} from {start:%Y-%m-%d} to {end:%Y-%m-%d} "
            f"({asked_days:.0f} days); the best cache file ({name}) covers "
            f"{covered_from} to {covered_to} ({coverage * asked_days:.0f} days). "
            f"Refusing to substitute the shorter window silently - a study run on it "
            f"would report one weekend as if it were a distribution.\n\n"
            f"    python3 scripts/fetch_weekend_bars.py --days {int(asked_days) + 10}\n"
        )

    try:
        bars = fetch_bars(symbol=symbol, timeframe=timeframe, start=start, end=end)
    except Exception as exc:  # noqa: BLE001 - a blocked egress is the common case
        raise RuntimeError(
            f"no cached {symbol} {timeframe} bars in {directory} and the fetch "
            f"failed ({exc}). On a machine whose egress does not reach "
            f"data.alpaca.markets, run:\n\n"
            f"    python3 scripts/fetch_weekend_bars.py --days 400\n\n"
            f"on a host that does, then re-run the backtest - it reads the cache."
        ) from exc
    path.write_text(json.dumps(bars), encoding="utf-8")
    return bars


def only_weekend(bars: Iterable[Bar], window: Any) -> list[Bar]:
    """Keep the bars that fall inside the tradeable window. Used by the
    backtest so a weekend strategy is never scored on weekday behaviour."""
    from oaa.strategies.weekend.clock import WindowPhase

    keep: list[Bar] = []
    for bar in bars:
        if window.phase(bar_time(bar)) in {WindowPhase.OPEN, WindowPhase.MANAGE_ONLY}:
            keep.append(bar)
    return keep
