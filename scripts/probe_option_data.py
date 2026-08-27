#!/usr/bin/env python3
"""Measure what Alpaca actually serves this account, before trusting a backtest.

The harness assumes historical option bars are available on the free Basic
plan back to February 2024, and that the free-tier restriction is the most
recent fifteen minutes rather than history. That is what the documentation
says. This script checks it against the account you actually have, on the
symbols you actually trade, and reports:

  * how many contracts are listed for the window, expired ones included
  * how many of them ever printed a bar
  * what fraction of contract-DAYS have a bar - the number that decides how
    much of a backtest is measured and how much is modelled
  * the same fraction restricted to near-the-money contracts, which is where
    the strategy actually trades and where coverage is much better
  * whether historical option QUOTES are reachable - if they are, the bid-ask
    spread stops being modelled, and spread is the dominant cost in a
    short-premium book

Run it before quoting any backtest figure:

    python scripts/probe_option_data.py --symbols SPY,QQQ --days 60
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from collections import defaultdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="SPY,QQQ")
    parser.add_argument("--days", type=int, default=60, help="how far back to look")
    parser.add_argument("--profile", default=None, help="dev | judged")
    parser.add_argument("--moneyness", type=float, default=0.06,
                        help="near-the-money band for the second coverage number")
    parser.add_argument("--sample", type=int, default=300,
                        help="how many contracts to pull bars for. This is a "
                             "MEASUREMENT, not a fetch - a few hundred contracts "
                             "spread across strikes and expiries measures coverage "
                             "as well as ten thousand and takes seconds instead of "
                             "minutes. 0 means all of them.")
    parser.add_argument("--band", type=float, default=0.10,
                        help="strike band around the underlying's range to list")
    parser.add_argument("--verbose", action="store_true",
                        help="show the feed's own INFO logging (one line per "
                             "API batch); off by default so the report is the output")
    args = parser.parse_args()

    from oaa.app.identity import print_banner, resolve
    from oaa.backtest.feed import HistoricalFeed
    from oaa.config.loader import load_settings
    from oaa.core.errors import DataError
    from oaa.core.logging import setup_logging

    # The report below IS the output. Without this the feed logs a line per
    # 100-contract batch, which for SPY is dozens of lines and buries it.
    setup_logging("INFO" if args.verbose else "WARNING", "console")

    settings = load_settings(profile=args.profile)
    print_banner(resolve(settings, "Option data probe"))

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=args.days)

    feed = HistoricalFeed(
        api_key=settings.credentials.api_key,
        secret_key=settings.credentials.secret_key,
        cache_dir=settings.path(settings.config.backtest.cache_dir) / "probe",
        stock_feed=settings.config.data.stock_feed,
    )

    print(f"\nwindow: {start} -> {end}\n")
    overall_ok = True

    for symbol in symbols:
        print(f"=== {symbol} " + "=" * (60 - len(symbol)))

        # 1. the underlying, so we know where the money is
        try:
            bars = feed.bars(symbol, start, end, "1Day")
        except DataError as exc:
            print(f"  underlying bars FAILED: {exc}\n")
            overall_ok = False
            continue
        if not bars:
            print("  no underlying bars returned\n")
            overall_ok = False
            continue
        closes = {b["timestamp"].date(): float(b["close"]) for b in bars}
        sessions = sorted(closes)
        low = min(closes.values()) * (1 - args.band)
        high = max(closes.values()) * (1 + args.band)
        print(f"  underlying: {len(sessions)} sessions, "
              f"{min(closes.values()):.2f}..{max(closes.values()):.2f}")

        # 2. the contract ladder, expired contracts included
        try:
            contracts = feed.option_contracts(
                symbol, start, end + dt.timedelta(days=60), low, high
            )
        except DataError as exc:
            print(f"  CONTRACT LISTING FAILED: {exc}")
            print("  -> the harness cannot use the real chain for this symbol\n")
            overall_ok = False
            continue
        if not contracts:
            print("  contract listing returned NOTHING - check the strike/expiry "
                  "window, or whether expired contracts are visible on this plan\n")
            overall_ok = False
            continue
        expiries = sorted({c["expiry"][:10] for c in contracts})
        with_oi = sum(1 for c in contracts if c.get("open_interest"))
        print(f"  contracts:  {len(contracts)} across {len(expiries)} expiries "
              f"({expiries[0]} .. {expiries[-1]})")
        print(f"              {with_oi} carry an open-interest figure")

        # 3. the bars - the number that actually matters
        wanted = _sample(contracts, closes[sessions[-1]], args.sample)
        if len(wanted) < len(contracts):
            print(f"  sampling:   {len(wanted)} of {len(contracts)} contracts "
                  f"(nearest the money; --sample 0 for all)")
        try:
            option_bars = feed.option_bars(
                [c["symbol"] for c in wanted], start, end, "1Day"
            )
        except DataError as exc:
            print(f"  OPTION BARS FAILED: {exc}")
            print("  -> historical option bars are not reachable on this plan;\n"
                  "     set backtest.chain.source: modelled and say so in the deck\n")
            overall_ok = False
            continue

        printed = {k: v for k, v in option_bars.items() if v}
        by_day: dict[str, set] = defaultdict(set)
        for contract, rows in printed.items():
            for row in rows:
                by_day[str(row["timestamp"])[:10]].add(contract)

        sampled = {c["symbol"] for c in wanted}
        near = {
            c["symbol"] for c in contracts
            if c["symbol"] in sampled
            and abs(float(c["strike"]) / closes[sessions[-1]] - 1.0) <= args.moneyness
        }
        near_printed = sum(1 for s in near if s in printed)

        contract_days = max(1, len(wanted) * len(sessions))
        bar_days = sum(len(v) for v in printed.values())
        print(f"  bars:       {len(printed)}/{len(wanted)} sampled contracts ever printed")
        print(f"              {bar_days}/{contract_days} contract-days have a bar "
              f"({bar_days / contract_days:.1%} overall coverage)")
        print(f"              near the money (+/-{args.moneyness:.0%}): "
              f"{near_printed}/{len(near)} contracts printed "
              f"({near_printed / max(1, len(near)):.0%})")
        if bar_days:
            first = next(iter(printed.values()))[0]
            print(f"  sample bar: close {first['close']} volume {first['volume']:.0f} "
                  f"on {str(first['timestamp'])[:10]}")

        # 4. quotes - the open question
        print("  quotes:     ", end="")
        print(_probe_quotes(settings, next(iter(printed), wanted[0]), start, end))
        print()

    print("=" * 66)
    print(
        "Read the near-the-money coverage number. That is the band the carry\n"
        "book trades, and it is what decides whether a run is mostly measured\n"
        "or mostly modelled. The dashboard reports the same figure per run."
    )
    return 0 if overall_ok else 1


def _sample(contracts: list, spot: float, limit: int) -> list:
    """A representative slice: nearest the money, spread across expiries.

    Coverage is a ratio, so it does not need every contract to measure - and
    pulling every SPY strike for a 60-day window is thousands of contracts and
    dozens of API round trips for a number a few hundred would give.
    """
    if limit <= 0 or len(contracts) <= limit:
        return list(contracts)
    by_expiry: dict = {}
    for contract in contracts:
        by_expiry.setdefault(contract["expiry"][:10], []).append(contract)
    per_expiry = max(2, limit // max(1, len(by_expiry)))
    out = []
    for expiry in sorted(by_expiry):
        ranked = sorted(by_expiry[expiry], key=lambda c: abs(float(c["strike"]) - spot))
        out.extend(ranked[:per_expiry])
    return out[:limit]


def _probe_quotes(settings, contract: str, start: dt.date, end: dt.date) -> str:
    """Is /v1beta1/options/quotes reachable? The SDK does not expose it."""
    import json
    import urllib.error
    import urllib.request

    url = (
        "https://data.alpaca.markets/v1beta1/options/quotes"
        f"?symbols={contract}&start={start}T14:00:00Z&end={start}T15:00:00Z&limit=1"
    )
    request = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": settings.credentials.api_key,
            "APCA-API-SECRET-KEY": settings.credentials.secret_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        return (
            f"HTTP {exc.code} - not available on this plan. "
            "The bid-ask spread stays MODELLED."
        )
    except Exception as exc:  # noqa: BLE001
        return f"probe failed: {exc}"

    quotes = (payload.get("quotes") or {}).get(contract) or []
    if not quotes:
        return "reachable, but no quotes in the sampled hour (try a busier contract)"
    first = quotes[0]
    return (
        f"AVAILABLE - bid {first.get('bp')} ask {first.get('ap')}. "
        "Real spreads are reachable; tell Claude to wire them in and the "
        "biggest remaining assumption goes away."
    )


if __name__ == "__main__":
    _ = os  # noqa: F841
    raise SystemExit(main())
