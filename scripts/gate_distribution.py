#!/usr/bin/env python3
"""What value was the binding gate actually measuring, and where should it sit?

A funnel says 75% of candidates died at the IV-rank floor. It does not say
whether they missed by one point or by forty - and those call for opposite
decisions. The rejection records carry the measured value, so the threshold can
be read off the distribution instead of guessed at, and the honest answer is
sometimes "there was no premium in this window and no floor would have found
any".

    python scripts/gate_distribution.py                       # newest run
    python scripts/gate_distribution.py --metric premium.iv_rank
    python scripts/gate_distribution.py --run runs/backtests/2026...

Needs a SAVED run, so drop --no-save from the backtest command.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_RUNS = Path("runs/backtests")

#: gate -> the metric whose threshold that gate is testing
BINDING_METRIC = {
    "premium": "premium.iv_rank",
    "trend": "trend.adx",
    "confirmation": "confirmation.confirmations",
    "momentum": "momentum.volume_z",
    "spread": "spread.relative_spread",
}


def _newest(root: Path) -> Path:
    runs = sorted(p for p in root.glob("*") if (p / "result.json").exists())
    if not runs:
        sys.exit(
            f"no saved runs under {root}. The backtest was probably run with "
            "--no-save; re-run without it."
        )
    return runs[-1]


def _histogram(values: list[float], width: int = 46) -> list[str]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [f"  all {len(values)} at {lo:.3f}"]
    buckets = 10
    step = (hi - lo) / buckets
    counts = Counter(min(buckets - 1, int((v - lo) / step)) for v in values)
    peak = max(counts.values())
    out = []
    for i in range(buckets):
        n = counts.get(i, 0)
        bar = "#" * int(width * n / peak) if peak else ""
        out.append(f"  {lo + i * step:7.3f} - {lo + (i + 1) * step:7.3f} {n:5d} {bar}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS))
    parser.add_argument("--metric", help="dotted metric, e.g. premium.iv_rank")
    parser.add_argument("--gate", help="restrict to one gate")
    parser.add_argument("--top", type=int, default=3, help="how many gates to profile")
    args = parser.parse_args()

    run = Path(args.run) if args.run else _newest(Path(args.runs_root))
    result = json.loads((run / "result.json").read_text())
    rejections = result.get("rejections") or []
    if not rejections:
        sys.exit(f"{run.name} recorded no rejections")

    by_gate: dict[str, list[dict]] = defaultdict(list)
    for row in rejections:
        by_gate[str(row.get("vetoed_by") or "unknown")].append(row)

    ranked = sorted(by_gate.items(), key=lambda kv: -len(kv[1]))
    if args.gate:
        ranked = [(g, r) for g, r in ranked if g == args.gate]
    total = len(rejections)

    print(f"\n{run.name}: {total} rejections\n")
    for gate, rows in ranked[: args.top]:
        metric = args.metric or BINDING_METRIC.get(gate)
        share = len(rows) / total
        print(f"{gate}  -  {len(rows)} rejections ({share:.0%})")
        if not metric:
            print("  no known threshold metric for this gate; pass --metric\n")
            continue
        values = [
            float(r["metrics"][metric]) for r in rows
            if isinstance(r.get("metrics"), dict)
            and isinstance(r["metrics"].get(metric), (int, float))
            and float(r["metrics"][metric]) > -90     # -1/-99 are "not measured"
        ]
        if not values:
            print(f"  {metric} was never measured on these rejections\n")
            continue
        values.sort()
        print(f"  {metric}: n={len(values)}  min {values[0]:.3f}  "
              f"median {statistics.median(values):.3f}  max {values[-1]:.3f}")
        print(*_histogram(values), sep="\n")
        print("  a floor here would have ADMITTED:")
        for q in (0.10, 0.25, 0.50):
            cut = values[int((1 - q) * (len(values) - 1))]
            print(f"    {q:.0%} of them at {cut:.3f}")
        print()

    print("Read it as an economic question, not a tuning one: a floor exists to "
          "refuse trades that are not worth the risk.\nIf the whole distribution "
          "sits well below the floor, the window had nothing to sell and moving "
          "the floor manufactures\ntrades rather than finding them.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
