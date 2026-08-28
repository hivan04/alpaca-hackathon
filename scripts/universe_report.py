#!/usr/bin/env python3
"""Measure a candidate universe instead of asserting one.

A universe picked from a backtest's per-symbol P&L is overfitting with extra
steps: it selects the names that happened to work in one window. This selects on
two things that are properties of the INSTRUMENT rather than of the sample -

    liquidity   realised spread as a fraction of mid on the live chain, which is
                what decides whether an edge survives execution (COST_STRUCTURE
                S5). Measured, not assumed from a tier map.
    decorrelation
                pairwise correlation of daily returns. A short-premium book
                loses on every position at once when vol spikes, so a universe
                of ten names that are really four bets offers no protection
                from the only event that matters.

    python scripts/universe_report.py --symbols SPY,QQQ,IWM,DIA,XLF,XLE,XLK,SMH,TLT,GLD
    python scripts/universe_report.py --live          # adds measured spreads

Bars come from the backtest cache, so the correlation half costs no API calls.
`--live` pulls one chain snapshot per symbol and needs the market open.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import statistics
import sys


def _returns(bars: list[dict]) -> list[float]:
    closes = [float(b["close"]) for b in bars if b.get("close")]
    return [
        math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0
    ]


def _corr(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 30:
        return None
    a, b = a[-n:], b[-n:]
    sa, sb = statistics.pstdev(a), statistics.pstdev(b)
    if sa == 0 or sb == 0:
        return None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=False)) / n
    return cov / (sa * sb)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="SPY,QQQ,IWM,DIA,XLF,XLE,XLK,SMH,TLT,GLD")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-08-22")
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--live", action="store_true",
                        help="Also measure real bid-ask width from a live chain")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR)
    for name in ("oaa", *[n for n in logging.root.manager.loggerDict
                          if n.startswith("oaa")]):
        logging.getLogger(name).setLevel(logging.ERROR)

    from oaa.backtest.feed import HistoricalFeed
    from oaa.config.loader import load_settings
    from oaa.data.indicators import garman_klass_vol

    settings = load_settings(profile=args.profile)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)

    feed = HistoricalFeed(
        api_key=settings.credentials.api_key,
        secret_key=settings.credentials.secret_key,
        cache_dir=settings.path(settings.config.backtest.cache_dir),
        stock_feed=settings.config.data.stock_feed,
        offline=False,
    )

    bars, rets = {}, {}
    for symbol in symbols:
        try:
            rows = feed.bars(symbol, start, end, "1Day")
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol}: no bars ({exc})")
            continue
        if rows:
            bars[symbol], rets[symbol] = rows, _returns(rows)

    live: dict[str, float] = {}
    live_all: dict[str, float] = {}
    if args.live:
        from oaa.data.factory import get_data_provider

        provider = get_data_provider(settings.config, settings.credentials)
        for symbol in bars:
            try:
                quotes = provider.option_chain(symbol)
            except Exception as exc:  # noqa: BLE001
                print(f"  {symbol}: no chain ({exc})")
                continue
            try:
                spot = float(provider.spot(symbol))
            except Exception:  # noqa: BLE001
                spot = None
            today = dt.date.today()

            def _usable(q, spot=spot, today=today):
                if q.spread_pct is None or not q.bid or q.bid <= 0:
                    return False
                # Only where the books actually trade: 5-20 DTE, within 10% of
                # spot. A whole-chain median averages in far-OTM strikes nobody
                # touches and makes every name look untradable.
                dte = (q.expiry - today).days
                if not 3 <= dte <= 21:
                    return False
                if spot and abs(q.strike / spot - 1.0) > 0.10:
                    return False
                return True

            near = [q.spread_pct for q in quotes if _usable(q)]
            allq = [q.spread_pct for q in quotes
                    if q.spread_pct is not None and q.bid and q.bid > 0]
            if near:
                live[symbol] = statistics.median(near)
            if allq:
                live_all[symbol] = statistics.median(allq)

    # -- per symbol ------------------------------------------------------- #
    print(f"\n{'symbol':8} {'sessions':>9} {'ann. vol':>9} "
          f"{'spread @ traded strikes':>24} {'whole chain':>13}")
    print("-" * 68)
    for symbol, rows in bars.items():
        vol = garman_klass_vol(rows, min(60, len(rows) - 1)) or 0.0
        near = f"{live[symbol]:.2%}" if symbol in live else "not measured"
        whole = f"{live_all[symbol]:.2%}" if symbol in live_all else "-"
        print(f"{symbol:8} {len(rows):9d} {vol:9.1%} {near:>24} {whole:>13}")
    if live:
        print("\n`spread @ traded strikes` is 3-21 DTE within 10% of spot - "
              "where the books\nactually go. The whole-chain median averages in "
              "far-OTM strikes nobody\ntouches and makes every name look "
              "untradable. Compare against the gates:\n"
              "intraday `spread_gate.max_relative_spread` and carry "
              "`options.max_bid_ask_spread_pct`.")

    # -- correlation ------------------------------------------------------ #
    names = list(bars)
    print(f"\ndaily-return correlation\n{'':6}" + "".join(f"{s:>7}" for s in names))
    for a in names:
        row = "".join(
            f"{(_corr(rets[a], rets[b]) or float('nan')):7.2f}" for b in names
        )
        print(f"{a:6}{row}")

    # -- the number that actually matters --------------------------------- #
    pairs = [
        (a, b, _corr(rets[a], rets[b]))
        for i, a in enumerate(names) for b in names[i + 1:]
    ]
    valid = [(a, b, c) for a, b, c in pairs if c is not None]
    if valid:
        mean_corr = statistics.fmean(c for _, _, c in valid)
        # Effective independent bets under equal weights: n / (1 + (n-1) * rho).
        n = len(names)
        effective = n / (1 + (n - 1) * max(mean_corr, 0.0))
        print(f"\nmean pairwise correlation   {mean_corr:.2f}")
        print(f"{n} symbols behave like       {effective:.1f} independent bets")
        print("\nA short-premium book loses on every position at once when vol "
              "spikes.\nThe count above, not the ticker count, is what limits "
              "that damage.")
        worst = sorted(valid, key=lambda t: -(t[2] or 0))[:5]
        print("\nmost redundant pairs (drop one of each to gain breadth cheaply):")
        for a, b, c in worst:
            print(f"  {a:5} / {b:5}  {c:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
