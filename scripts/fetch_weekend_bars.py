#!/usr/bin/env python3
"""Download BTC/USD bars into the weekend book's cache.

Alpaca's crypto endpoint needs no key and no entitlement, so this runs with the
standard library alone:

    python3 scripts/fetch_weekend_bars.py --days 400

It writes data/cache/weekend/BTCUSD_15Min_<start>_<end>.json in exactly the
shape `oaa.strategies.weekend.data.cached_bars` expects, so a subsequent
`oaa weekend backtest --days 400` reads the cache and never touches the
network. That matters on machines whose egress is filtered - and it means the
replay is reproducible from a file that can be committed if we want the judges
to re-run it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
UTC = dt.timezone.utc


def fetch(symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    out: list[dict] = []
    token: str | None = None
    while True:
        query = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
            "sort": "asc",
        }
        if token:
            query["page_token"] = token
        url = f"{BASE}?{urllib.parse.urlencode(query)}"
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = json.load(response)
        chunk = (payload.get("bars") or {}).get(symbol) or []
        out.extend(
            {
                "t": b["t"],
                "timestamp": b["t"],
                "open": float(b["o"]),
                "high": float(b["h"]),
                "low": float(b["l"]),
                "close": float(b["c"]),
                "volume": float(b["v"]),
                "trades": b.get("n"),
                "vwap": b.get("vw"),
            }
            for b in chunk
        )
        token = payload.get("next_page_token")
        print(f"  {len(out):>7,} bars…", end="\r", flush=True)
        if not token:
            break
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USD")
    parser.add_argument("--timeframe", default="15Min")
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--out-dir", default="data/cache/weekend")
    args = parser.parse_args()

    end = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(days=args.days)
    print(f"fetching {args.symbol} {args.timeframe} {start:%Y-%m-%d} -> {end:%Y-%m-%d}")
    bars = fetch(args.symbol, args.timeframe, start, end)

    directory = Path(args.out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = (
        f"{args.symbol.replace('/', '')}_{args.timeframe}_"
        f"{start:%Y%m%d}_{end:%Y%m%d}.json"
    )
    path = directory / name
    path.write_text(json.dumps(bars), encoding="utf-8")
    span = f"{bars[0]['t']} -> {bars[-1]['t']}" if bars else "empty"
    print(f"\nwrote {len(bars):,} bars to {path}\n{span}")


if __name__ == "__main__":
    main()
