#!/usr/bin/env python3
"""Sweep strategy gate thresholds and report what each setting actually buys.

Loosening a gate is a trade, not a free win: every threshold you relax admits
the marginal candidates the threshold existed to exclude. This script measures
the exchange rate - trades gained against P&L and cost - so the decision is made
on numbers rather than on the feeling that there should be more trades.

    python scripts/sweep_gates.py --only exits.max_hold_days

Defaults to 2026-08-08 -> 2026-08-22 on SPY,QQQ,IWM. Pass --start/--end for a
longer window; ten sessions is a small sample and a sweep over it will rank
settings partly on noise.

The first sweep over a window fetches and caches its bars; later sweeps over the
same window reuse them. Add --offline to refuse the network once it is warm.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import logging
import re
import sys

# Each entry is swept ONE AT A TIME against the baseline, not as a cartesian
# product. A full product over-fits to whichever window you happen to run, and
# with a 5-6 session judged window that is the last thing this book needs.
GRIDS: dict[str, dict[str, list]] = {
    "intraday_momentum": {
        "momentum.volume_zscore_min": [1.0, 0.75, 0.5, 0.25],
        "momentum.require_volume": [True, False],
        "momentum.band_dispersion_mult": [0.5, 1.0, 1.5, 2.0, 3.0],
        "momentum.vwap_band_atr_mult": [0.25, 0.40, 0.60],
        "momentum.cross_lookback_bars": [15, 20, 30],
        "momentum.require_width_rising": [True, False],
        "momentum.persistence_bars": [2, 1],
        "catalyst_gate.required": [True, False],
        "catalyst_gate.lookback_minutes": [30, 60, 120],
        "spread_gate.max_relative_spread": [0.02, 0.03, 0.05],
        "momentum.confirmations_required": [2, 3, 4],
        "momentum.higher_timeframe_minutes": [0, 60],
        # THE EXIT SHAPE - the dials that decide whether the book can be
        # profitable at all, and the ones that were never swept.
        #
        # Breakeven hit rate is stop / (target + stop). At the 10% target and
        # 15% stop this book shipped with, that is 60%: it must be right three
        # times in five just to break even, BEFORE costs. That is a
        # premium-SELLING payoff shape bolted onto a premium-BUYING book.
        # Momentum books earn from a few large winners, which means the target
        # belongs above the stop, not below it.
        #
        # The time stop interacts with both: at 20 minutes (30 in practice -
        # the scan grid is 15 minutes) most trades reach neither level and exit
        # near flat, having paid the round trip. Whatever the target and stop
        # say, a book that always exits on time only ever pays cost.
        "exits.target_pct_of_premium": [0.10, 0.15, 0.20, 0.30],
        "exits.stop_pct_of_premium": [0.15, 0.10, 0.25, 0.40],
        "exits.time_stop_minutes": [20, 45, 90, 180],
    },
    "vol_carry": {
        "premium_gate.iv_rank_min": [0.35, 0.25, 0.15],
        "premium_gate.iv_rv_spread_min": [0.03, 0.02, 0.01],
        "trend_gate.adx_max": [25, 30, 35],
        "exits.profit_target_pct": [0.50, 0.40, 0.30],
        # The DTE floor is a CALENDAR, not a risk control: it closes the
        # position regardless of P&L. Raising it cuts gamma exposure and
        # shortens the hold - which matters for a 5-6 session judged window,
        # where a trade still open at the deadline is a mark rather than a
        # result - but it also forgoes the fastest decay, which is the income.
        "exits.dte_floor": [3, 4, 5, 6],
        "exits.max_hold_days": [0, 2, 3, 4, 5],
        "exits.max_loss_usd": [450, 300, 900, 0],
        "cost.max_spread_cost_vs_credit": [0.20, 0.15, 0.10],
    },
}


def _quiet() -> None:
    """Silence the oaa logger tree.

    `logging.basicConfig` does not do it: `oaa.core.logging.get_logger` creates
    its own loggers, and any created AFTER a basicConfig call keep their own
    level - so a sweep that runs dozens of backtests buries its own table under
    thousands of INFO lines. Re-applied before every run, because each run
    imports and creates more of them.
    """
    for name in ("oaa", *[n for n in logging.root.manager.loggerDict
                          if n.startswith("oaa")]):
        logging.getLogger(name).setLevel(logging.ERROR)


def _norm(reason: str) -> str:
    return re.sub(r"[-+]?\d*\.?\d+", "N", reason or "").strip()[:58]


def _run(args, strategy_name: str, override):
    from oaa.backtest.runner import BacktestRequest, run_backtest
    from oaa.config.loader import load_settings

    _quiet()
    settings = load_settings(profile=args.profile)
    if override is not None:
        dotted, value = override
        section, key = dotted.split(".", 1)
        for ref in settings.config.enabled_strategies():
            if ref.name == strategy_name:
                ref.params.setdefault(section, {})[key] = value

    request = BacktestRequest(
        symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()],
        start=dt.date.fromisoformat(args.start),
        end=dt.date.fromisoformat(args.end),
        strategies=[strategy_name],
        source=args.source,
        use_news=not args.no_news,
        offline=args.offline,
        critic_mode="off",
        label="sweep",
    )
    return run_backtest(settings, request)


def _row(label, value, result) -> str:
    metrics = result.metrics()
    counts = collections.Counter(
        f"{r.vetoed_by}: {_norm(r.reason)}" for r in result.rejections
    )
    top = counts.most_common(1)[0][0] if counts else "-"
    trades = result.closed
    cost_per = (metrics["total_modelled_cost"] / len(trades)) if trades else 0.0
    return (f"{label:34} {str(value):>8} {len(result.trades):7d} "
            f"{metrics['net_pnl']:10.2f} {metrics['total_modelled_cost']:9.2f} "
            f"{cost_per:9.2f}  {top[:38]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default="intraday_momentum", choices=sorted(GRIDS))
    parser.add_argument("--symbols", default="SPY,QQQ,IWM")
    parser.add_argument("--start", default="2026-08-11")
    parser.add_argument("--end", default="2026-08-22")
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--source", default="alpaca")
    # Network ALLOWED by default. The bar cache is keyed on the window plus a
    # 400-day warmup, so changing --start changes the key and a previously warm
    # cache misses: defaulting to offline made the first run of every new window
    # fail. The feed caches what it fetches, so only the first sweep over a
    # given window pays for it.
    parser.add_argument("--offline", action="store_true", default=False,
                        help="Refuse the network. Only works once the cache is "
                             "warm for THIS exact window.")
    parser.add_argument("--no-news", action="store_true")
    parser.add_argument("--only", help="Sweep just this one dotted parameter")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(message)s")

    grid = GRIDS[args.strategy]
    if args.only:
        if args.only not in grid:
            # A parameter belongs to exactly one strategy. If the one asked for
            # lives in a different grid, switch rather than making the caller
            # remember which book owns which knob.
            owners = [name for name, g in GRIDS.items() if args.only in g]
            if len(owners) == 1:
                args.strategy = owners[0]
                grid = GRIDS[args.strategy]
                print(f"note: {args.only} belongs to {args.strategy} - "
                      f"sweeping that strategy instead")
            else:
                print(f"unknown parameter {args.only!r}\n")
                for name, g in GRIDS.items():
                    print(f"  {name}:")
                    for key in g:
                        print(f"    {key}")
                return 1
        grid = {args.only: grid[args.only]}

    print(f"\n{args.strategy}  {args.start} -> {args.end}  "
          f"[{args.symbols}]  source={args.source}\n")
    header = (f"{'parameter':34} {'value':>8} {'trades':>7} {'net P&L':>10} "
              f"{'cost':>9} {'cost/trade':>9}  {'top rejection':<38}")

    from oaa.core.errors import DataError

    try:
        rows = [_row("(baseline)", "", _run(args, args.strategy, None))]
    except DataError as exc:
        print(f"\n{exc}\n\nThe bar cache is keyed on the window plus a "
              "400-day warmup, so a new --start\nis always a cache miss. Drop "
              "--offline for the first run over this window.")
        return 1
    print(header)
    print("-" * len(header))
    print(rows[0], flush=True)
    for dotted, values in grid.items():
        for value in values:
            rows.append(_row(dotted, value, _run(args, args.strategy, (dotted, value))))
            print(rows[-1], flush=True)

    # Reprint contiguously. Anything the run logged despite `_quiet` lands
    # between the rows above; this block is always clean and copy-pasteable.
    print("\n" + "=" * len(header))
    print(f"RESULTS  {args.strategy}  {args.start} -> {args.end}  [{args.symbols}]")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for row in rows:
        print(row)

    print("\nRead cost/trade, not just the trade count: a setting that doubles "
          "trades and\ntriples cost has made the book worse, however much "
          "busier it looks. Each row\nchanges ONE parameter against the "
          "baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
