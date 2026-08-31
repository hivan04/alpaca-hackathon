# v2 — "selective carry"  *(ARCHIVED — reference only, not for live trading)*

Parked 30 Aug 2026. This is a research strategy, kept because the evidence
behind it is worth having on the record, **not** because it is a candidate for
the judged account.

## Status

- **Not used live.** A variant can only be selected by an explicit
  `--variant` on `oaa backtest`. There is no `OAA_VARIANT` environment
  variable and no `variant:` key in `config/default.yaml`, and `oaa run` never
  passes the argument. `tests/test_config.py` pins this.
- **Still runnable**, so every number below can be reproduced:

  ```bash
  oaa backtest --variant v2 --start 2026-02-01 --end 2026-08-21
  ```

  The loader resolves a variant from `config/variants/<name>.yaml` **or**
  `archive/strategies/<name>/<name>.yaml`, so archiving it did not break it.

## What it changes, and the evidence for each

Measured on 2026-02-01 → 2026-08-21, 409 trades, with the credit/width gate
and the sizing fix in place. Full working in
`claude/exit-ab-and-the-credit-width-gate.md` and the win-rate scan.

| # | change | evidence |
|---|---|---|
| 1 | `intraday_momentum` off | 249 trades, −$2,777, 41.0% win against a ~47% breakeven, per-trade t = −2.09. Confirmation votes are non-monotonic (3 → 38.8%, 4 → 45.6%, 5 → 35.4%), so the signal does not discriminate. Removing it takes blended win rate 46.5% → 55.0% and weekly sd −20%. |
| 2 | `premium_gate.iv_max: 0.25` | carry win rate by **absolute** IV tertile: 62.3% / 56.6% / 46.3%, monotonic. The baseline gates on IV *rank*, which is relative to each name's own history and was the weakest predictor scanned. |
| 3 | `structures.min_probability_of_profit: 0.50` | realised win rate by modelled-PoP tertile: 47.2% / 52.8% / 64.8%. PoP was computed on every idea and gated on by nothing. |
| 4 | `exits.defensive_mode: hold` | 69 of 160 carry trades exited on a short-strike touch at a **17% win rate for −$10,801**; every other exit reason averaged 84%. `loss_multiple_of_credit: 1.5` still bounds the downside. |

## Why it is archived rather than adopted

**These thresholds were fitted on Feb–Aug 2026 and were never validated out of
sample.** They were found by scanning ~25 metrics, where a single median split
at z ≈ 2 is not significant once corrected for that many comparisons. Changes 2
and 3 are monotonic across three buckets, which is better evidence than one
split — but monotonic-in-sample is still in-sample.

The honest next step, if this is ever revisited, is: fit on Feb–May, confirm on
Jun–Aug, and only then treat any of it as real.

There is also a presentational consequence worth stating: v2 drops the only
directional book, so what it presents is a pure short-volatility strategy. That
is coherent and it is the part with a measurable edge, but the "agent reads the
tape and takes a view" narrative goes with it.

## What v2 does NOT contain

The two changes that are *not* fitted parameters — the credit-to-width gate and
the `single_long` sizing fix — are not part of this variant. They are defect
fixes rather than strategy choices, and they live in `oaa_gate.patch` and
`fix2.patch` at the repo root. Both were reverted from the working tree on
30 Aug at the user's instruction; see `claude/offline-flag-changes-the-result.md`
for what that restores.

## Files

- `v2.yaml` — the variant definition, patching strategies by name
- this README
