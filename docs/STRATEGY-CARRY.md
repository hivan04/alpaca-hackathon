# Carry book — vol_carry (supersedes Overnight Anomaly Pairs)

**Status:** `enabled: true`, but **no longer the primary book** — see
`docs/STRATEGY-INTRADAY.md`. It measured 0.30 trades per session with a 5.78
day average hold, so over a 5–6 session judged window it expects fewer than two
trades, still open at the deadline. It stays on as the resident book because its
regime (rich IV, no trend) is the complement of the intraday book's.

The pairs strategy, the Kalman filter, the Huber/LightGBM quantile stack, the
cointegration universe and the nightly capital handoff are **removed from the
repo**.

## Thesis

Index and single-name options persistently trade at an implied volatility above
subsequently realised volatility. The edge is the **IV–RV spread**, collected by
holding short-premium, defined-risk structures over multiple sessions.

- **The option is the position, not an overlay.** Max loss is arithmetic from the
  structure, so `risk.allow_undefined_risk: false` is satisfied by construction.
- **Theta accrues on calendar days**, weekend included. The book is **resident**,
  not flattened each morning.

## Four hard gates (`src/oaa/strategies/vol_carry.py`)

1. **Premium** — IV rank ≥ **0.35** (lowered from 0.70 on 27 Aug) **and**
   IV−RV ≥ 3 vol points. Both required.
2. **Trend** — ADX ≤ 25 and |trend strength| ≤ 0.60. The *same* measurement that
   fires `momentum_debit_spread`, which is what makes them mutually exclusive.
3. **Event** — earnings inside the expiry window excluded; ex-div with a short
   call excluded.
4. **Macro lens** — shared (sector-wide, no name catalyst) → sell. Idiosyncratic
   → veto.

Then a cost gate: round-trip spread ≤ 20% of credit received.

### The IV-rank floor — CHANGED 27 Aug, 0.70 → 0.35

At 0.70 this single gate rejected **304 of 304** replayed candidates, and
observed IV rank never exceeded 38%. A 70% IV rank is a genuinely rare state:
over a 5–6 session judged window it is close to a coin flip whether it occurs at
all, which is a lottery ticket rather than a strategy. This is the same finding
as the earlier "46% probability of zero trades" estimate, now attributed to a
specific gate.

35% sells less-rich premium, so expect a lower win rate and a smaller credit per
trade. **The IV−RV spread gate is what still has to be cleared, and it is the one
carrying the actual edge** — the IV-rank floor is a regime filter, not the thesis.
`tests/test_config.py` pins the floor to a 0.30–0.50 band rather than asserting a
single value: a carry book selling premium at median richness is not a carry
book, so the floor still has to be a floor.

## Structures and sizing — CHANGED 27 Aug

- Iron condor by default; credit vertical on a macro-lens lean; calendar on a
  term-structure kink. **Every leg is an option** — no strategy in the repo emits
  an equity leg.
- **Short strikes at 25Δ, raised from 14Δ.** The breakeven hit rate is set
  entirely by the exit pair and does not move when strikes move, while
  credit-to-width roughly doubles (17% → 30%) — so return on risk per trade
  doubles against an unchanged bar. The cost is that the true hit rate falls as
  the shorts approach the money. **This is a hypothesis under measurement:** if
  the realised hit rate drops below the measured breakeven, it goes back to
  0.20 or 0.14.
- 5-point wings, `min_credit_to_width: 0.15` (now easily met, kept as a floor).
- **7–14 DTE, not 20–45.** At 30 DTE a five-session hold captures ~20–25% of
  remaining decay; at 10 DTE it captures the majority. The cost is gamma — a
  deliberate trade of tail risk for realised P&L inside the window.
- Size from **max loss**, not capital. Note 25Δ *lowers* max loss per structure
  ($350 vs $424), so risk-based sizing trades more contracts for the same equity
  risk — some of any P&L improvement is size, not edge.

## Exits — 50% target / 1.5× stop / $900 hard stop

The old 30% / 2.0× pair implied a breakeven hit rate of **87% before paying any
spread**, against 88% observed on 42 real trades — one point of margin. Worse,
the 20% cost ceiling implied a 95.7% breakeven, so the gate was admitting trades
the arithmetic said would lose. 50% / 1.5× moves the idealised breakeven to 75%
and makes the same cost ceiling survivable at 85%.

| Trigger | Action |
|---|---|
| **`exits.max_loss_usd`** ($900) | close — checked FIRST, before anything else |
| **50%** of max profit | close |
| loss = **1.5×** credit | close |
| DTE floor (3) | close regardless of P&L |
| underlying touches a short strike | close the tested side |
| macro lens flags the name mid-hold | close at next open |
| submission flatten | close everything |

### Why a hard dollar stop was added (27 Aug)

`loss_multiple_of_credit` is a **ratio**, so a wide structure with a fat credit
is allowed a proportionally fat loss before the stop trips. That is backwards:
the constraint that actually matters is `risk.daily_loss_limit_pct` on a $100k
account, which is written in dollars. At 3% the daily halt is $3,000; one trade
should not be able to spend a third of it. `exits.max_loss_usd: 900` caps the
trade in the units the account limit uses. `0` disables it.

**Note the ordering in the log.** Several of the largest losses in the 27 Aug run
exited on *"3d to expiry"*, not on the stop — they bled to roughly −1.4× credit,
just inside the 1.5× trip, and then the DTE floor closed them at whatever the
mark was. The DTE floor is not a risk control; it is a gamma control that happens
to realise whatever loss has accumulated. The hard dollar stop is what closes
that gap.

**The idealised breakeven is not the realised one.** Measured on the 25Δ judged
run, only 5 of 18 trades exited at the profit target — 6 hit the DTE floor and 6
were short-strike touches. That produces avg win $357 against avg loss $546, a
ratio of 0.65 rather than the 0.33 the formula assumes, so the **measured**
breakeven is 60.4% against 72.2% observed. Use the measured number when judging
whether a parameter change worked; the formula is a bound, not a forecast.

## Defined risk has to survive the marks — FOUND AND FIXED 27 Aug

A real run produced a trade at **−170.9% of its own `max_loss`**. That is
impossible for a defined-risk structure and it was not a market outcome — it was
a pricing fault.

**The mechanism.** `RealChainBuilder.reprice` decides *per contract* whether to
mark from a real Alpaca bar or from Black-Scholes. In a condor the short strikes
trade and the far wings often do not, so on a stressed session the short leg is
marked at its **real, elevated traded close** while its own wing is marked on a
**calm modelled vol**. The value of a vertical is then no longer bounded by its
strike width, and the arithmetic that makes an iron condor defined-risk stops
holding.

Measured on a 5-wide put spread with spot at 93, one week to expiry:

| pricing | short 97 | long 92 | spread | vs width 5.00 |
|---|---|---|---|---|
| mixed surface (real short, modelled wing) | 5.262 | 0.574 | **4.688** | 94% of the width |
| one surface (both at the stressed vol) | 5.262 | 2.306 | **2.956** | 59% |

The mix inflates the loss by 58%, and with a wider vol gap or a staler print it
exceeds the width outright — which is where −170.9% came from.

**Two fixes, both in `backtest/engine.py`:**

1. **One surface per structure.** `_leg_marks` checks provenance across the
   legs. Where it is mixed, it recovers the vol the *real* prints imply, and
   re-prices **every** leg from the model anchored on that vol. The real
   information is kept; one surface prices the whole structure. Counted as
   `mixed_surface_marks` and reported per run. A high count means the real
   option tape is thin for this universe — which is a finding, not a fault.
2. **`_bounded_gross` clamps the mark to the structure's own arithmetic.** The
   risk engine approved the trade on a `max_loss` computed from strike widths;
   if the marks say the position lost more than that before costs, the marks are
   wrong. Counted as `risk_bound_clamps` and printed in a red panel. **This
   count should be zero** — a non-zero one is a bug to chase, not a setting.
   Costs are charged *outside* the bound on purpose: `max_loss` is a pre-cost
   concept, and realising slightly worse than defined risk after crossing the
   spread twice is honest.

`oaa backtest` prints both counters in the metrics table.

## Duplicate entries — FIXED 27 Aug

Both the sim broker and Alpaca **net identical option symbols**, so opening the
same condor twice does not create a second position — it doubles the quantity on
the same four contracts. Every count-based limit was therefore blind:
`max_positions` saw 4 positions, `concentration` saw 4 legs. Only
`portfolio_risk` eventually stopped it, meaning the book doubled down until
aggregate exposure tripped.

Nearly invisible at 4 cycles a day; severe at 12. Two checks added to
`RiskEngine.evaluate`:

- **`duplicate_structure`** — every leg already held on the same side → refuse.
  Keyed on side as well as symbol, so holding the *mirror* image (the position
  being closed out) is not mistaken for re-entry.
- **`reentry_cooldown`** — `risk.reentry_cooldown_minutes`, default 60, per
  (symbol, strategy). The backstop for the *near*-duplicate: once spot moves
  between cycles the strikes differ and `duplicate_structure` no longer matches,
  but it is still the same trade.

## What the backtest changed underneath this strategy

- **Realised vol now uses Garman-Klass**, not close-to-close. The free IEX feed
  is ~2% of the tape and its "close" is the last IEX print; close-to-close ran
  1.3–2.3× the range-based estimate on the same bars (MSFT: 56.3% vs 24.1%).
  Inflated RV was making rich premium look fairly priced — MSFT's −19.7% IV-RV
  spread was mostly the estimator. Applies to the live agent too.
- **The option chain is real** where Alpaca has a print: real listed strikes and
  expiries, real daily bars, implied vol **recovered** by inverting Black-Scholes
  on the traded price rather than modelled. Coverage is partial (35.9% on the
  first working run) and reported per run — and, as above, partial coverage
  *within one structure* is now handled explicitly rather than silently.
- **Scans moved from 4 a day to 12**, every 15 minutes 10:00–14:45. This book did
  not need that — the intraday book did — but it is what exposed the duplicate
  entries.
- **The critic runs in replay**, in the live order: cost → critic → risk engine →
  partner veto. Featherless both sides - temperature 0 in replay, 0.2 live.

## Opportunistic book — `event_premium`

Dormant by default. Compares the implied move in the front expiry against a
committed historical realised distribution. Implied ≥ 1.25× realised median →
sell a defined-risk condor. Otherwise stand down, which is the expected outcome
and accounts for the `scheduled_event` entries dominating the rejection log.

## Submission controls (set in config, not left to memory)

- `management.entry_cutoff_utc: 2026-09-02T20:00:00Z` — no new carry structures
- `management.submission_flatten_utc: 2026-09-04T13:45:00Z` — close everything,
  confirmed-flat poll, checked on **every runner poll**

## Open items

- **Re-measure everything on real bars after the defined-risk fix.** Every loss
  figure quoted before 27 Aug late afternoon was computed on mixed-surface marks
  and is overstated by an unknown amount.
- **Correlated tail.** In the 27 Aug run NVDA (−$866) and SPY (−$1,456) both
  blew up on the *same session* — ~2.3% of equity from two positions. The daily
  loss halt catches the next day, not the same one.
  `max_positions_per_sector` still does not exist; the cap is per-underlying and
  blind to correlation.
- **The DTE floor realises whatever loss has accumulated.** Consider closing
  losers earlier — a 1.0× credit stop, or a DTE floor that acts on P&L rather
  than on the calendar alone. Test before adopting.
- Wing width is a flat 5 points and does not fit every strike grid
  (`NVDA: wing width 5 does not fit the strike grid`). Should be a percent of
  spot.
- Options level 3 on the judged paper account.
- Whether the CLI submits combos atomically (`execution.multileg_mode`).
