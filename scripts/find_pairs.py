#!/usr/bin/env python3
"""Offline cointegration screen -> config/pairs.yaml.

Run this before the agent trades, and again if the market regime shifts. It is
deliberately a separate script rather than part of the live loop: re-testing
cointegration at 15:45 is how you conjure a pair into existence in the evening
and lose money on it at the open.

    python scripts/find_pairs.py                       # screen the default candidates
    python scripts/find_pairs.py --write               # write config/pairs.yaml
    python scripts/find_pairs.py --from-pool --additive --write   # screen what
                                                       # discovery has found,
                                                       # keeping approved pairs
    python scripts/find_pairs.py --symbols XLE XOP KO PEP --lookback 500 --write
    python scripts/find_pairs.py --provider alpaca     # use the SDK instead of the CLI

Data comes through the configured provider, which defaults to the Alpaca CLI —
the same binary that executes the orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from oaa.config.loader import load_settings  # noqa: E402
from oaa.core.logging import setup_logging  # noqa: E402
from oaa.data.factory import get_data_provider  # noqa: E402
from oaa.quant.cointegration import find_pairs  # noqa: E402

# Economically linked, options-liquid, shortable. The screen decides what
# actually survives; this is only where it starts looking.
DEFAULT_CANDIDATES = [
    "XLE", "XOP", "XLF", "KRE", "XLK", "QQQ", "SPY", "IWM",
    "KO", "PEP", "XOM", "CVX", "HD", "LOW", "MA", "V", "GS", "MS",
    "UPS", "FDX", "CAT", "DE", "T", "VZ",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument(
        "--from-pool", action="store_true",
        help="Screen the discovery candidate pool instead of the built-in list",
    )
    parser.add_argument(
        "--additive", action="store_true",
        help="Merge into the existing pairs.yaml rather than replacing it. "
             "Approved pairs are never dropped for falling off a buzz list.",
    )
    parser.add_argument(
        "--fdr", action="store_true", default=True,
        help="Benjamini-Hochberg correction (default on). Screening N pairs at "
             "a flat p<0.05 finds ~5%% of them 'cointegrated' by chance alone.",
    )
    parser.add_argument("--no-fdr", dest="fdr", action="store_false")
    parser.add_argument("--lookback", type=int, default=500, help="Trading days of history")
    parser.add_argument("--max-pvalue", type=float, default=0.05)
    parser.add_argument("--min-correlation", type=float, default=0.70)
    parser.add_argument("--min-half-life", type=float, default=1.0)
    # 10 days, not 30: over a one-week judged window a slow-reverting pair is a
    # directional bet that has not resolved yet.
    parser.add_argument("--max-half-life", type=float, default=10.0)
    parser.add_argument("--top", type=int, default=10, help="Keep the N strongest pairs")
    parser.add_argument("--provider", default=None, help="Override the data provider (cli|alpaca)")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--write", action="store_true", help="Write config/pairs.yaml")
    parser.add_argument("--out", default="config/pairs.yaml")
    return parser.parse_args()


def resolve_symbols(args, settings) -> list[str]:
    if args.symbols:
        return [s.upper() for s in args.symbols]
    if args.from_pool:
        from oaa.discovery.universe import CandidatePool

        cfg = settings.config.discovery.pool
        pool = CandidatePool.load(
            settings.path(cfg.path), cfg.accumulate_days, cfg.max_symbols, cfg.seeds
        )
        candidates = pool.candidates()
        if not candidates:
            print("The candidate pool is empty. Run `oaa discover` first.")
            return []
        print(f"Screening {len(candidates)} symbols from the discovery pool "
              f"(persistence-ordered).")
        return candidates
    return DEFAULT_CANDIDATES


def main() -> int:
    args = parse_args()
    settings = load_settings(profile=args.profile)
    setup_logging(settings.config.telemetry.log_level, "console")
    if args.provider:
        settings.config.data.provider = args.provider

    symbols = resolve_symbols(args, settings)
    if not symbols:
        return 1
    args.symbols = symbols

    provider = get_data_provider(settings.config, settings.credentials)
    print(f"Fetching {args.lookback}d of daily bars for {len(args.symbols)} symbols "
          f"via '{provider.name}'...")

    closes: dict[str, list[float]] = {}
    for symbol in args.symbols:
        try:
            bars = provider.bars(symbol, lookback_days=args.lookback, timeframe="1Day")
            closes[symbol] = [float(b["close"]) for b in bars]
            print(f"  {symbol:<6} {len(bars):>4} bars")
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol:<6} SKIPPED ({exc})")

    if len(closes) < 2:
        print("\nNot enough symbols with data to screen. Check credentials and the CLI install.")
        return 1

    # Multiple testing. N symbols means N*(N-1) ordered tests: 24 symbols is 552
    # of them, and at a flat p<0.05 you would expect ~27 "cointegrated" pairs
    # from pure noise. Tighten the threshold with the size of the pool.
    n_tests = max(1, len(closes) * (len(closes) - 1))
    threshold = args.max_pvalue
    if args.fdr:
        threshold = min(args.max_pvalue, args.max_pvalue / max(1.0, n_tests / 20.0))
        print(f"\n{n_tests} ordered tests -> tightening p-value "
              f"{args.max_pvalue} to {threshold:.5f} (FDR control)")
        print("  A flat threshold on a pool this size finds noise and calls it edge.")

    print("\nScreening...")
    results = find_pairs(
        closes,
        max_pvalue=threshold,
        min_correlation=args.min_correlation,
        half_life_range=(args.min_half_life, args.max_half_life),
        min_observations=min(250, args.lookback // 2),
        top_n=args.top,
    )

    if not results:
        print("\nNo pair passed the screen. That is a real answer, not a bug —\n"
              "loosen --max-pvalue or widen the half-life range only if you can\n"
              "justify it, and remember a marginal pair is a directional bet.")
        return 2

    print(f"\n{len(results)} pair(s) passed:\n")
    header = f"{'PAIR':<14}{'P-VALUE':>10}{'HEDGE':>9}{'CORR':>8}{'HALF-LIFE':>12}"
    print(header)
    print("-" * len(header))
    for r in results:
        hl = f"{r.half_life_days:.1f}d" if r.half_life_days else "n/a"
        print(f"{r.name:<14}{r.pvalue:>10.5f}{r.hedge_ratio:>9.3f}"
              f"{r.correlation:>8.3f}{hl:>12}")

    if not args.write:
        print("\n(dry run — pass --write to update config/pairs.yaml)")
        return 0

    fresh = {f"{r.left}/{r.right}" for r in results}
    existing: list[dict] = []
    out_path = ROOT / args.out
    if args.additive and out_path.exists():
        try:
            prior = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
            existing = [
                p for p in (prior.get("pairs") or [])
                if f"{p.get('left')}/{p.get('right')}" not in fresh
            ]
            print(f"\nAdditive: keeping {len(existing)} previously-approved pair(s).")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not read the existing file ({exc}) - writing fresh.")

    payload = {
        "screened_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lookback_days": args.lookback,
        "criteria": {
            "max_pvalue": args.max_pvalue,
            "min_correlation": args.min_correlation,
            "half_life_days": [args.min_half_life, args.max_half_life],
            "min_observations": min(250, args.lookback // 2),
        },
        "criteria_applied_pvalue": round(threshold, 6),
        "pairs": existing + [
            {
                "left": r.left,
                "right": r.right,
                "hedge_ratio": round(r.hedge_ratio, 6),
                "pvalue": round(r.pvalue, 6),
                "half_life_days": round(r.half_life_days, 2) if r.half_life_days else None,
                "correlation": round(r.correlation, 4),
                "enabled": True,
                "notes": f"screened {dt.date.today().isoformat()}",
            }
            for r in results
        ],
    }
    header_comment = (
        "# GENERATED by scripts/find_pairs.py - do not hand-edit.\n"
        "# Regenerate with: python scripts/find_pairs.py --write\n"
    )
    out_path.write_text(header_comment + yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    print(f"\nWrote {out_path} ({len(payload['pairs'])} pairs)")

    if args.from_pool:
        from oaa.discovery.universe import CandidatePool

        cfg = settings.config.discovery.pool
        pool = CandidatePool.load(
            settings.path(cfg.path), cfg.accumulate_days, cfg.max_symbols, cfg.seeds
        )
        approved = {r.left for r in results} | {r.right for r in results}
        pool.mark_screened(args.symbols, sorted(approved))
        pool.save()
        print(f"Pool updated: {len(approved)} symbol(s) marked approved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
