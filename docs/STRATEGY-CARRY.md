# Carry book — vol_carry (supersedes Overnight Anomaly Pairs)

**Last revised 31 Aug, evening.** Every performance figure in this document
postdates the credit-to-width gate of 30 Aug. The pre-gate numbers are in
§"The headline edge was mostly an artefact" and must not be quoted.

**Status:** `enabled: true`, but **no longer the primary book** — see
`docs/STRATEGY-INTRADAY.md`. It measured 0.30 trades per session with a 5.78
day average hold, so over a 5–6 session judged window it expects fewer than two
trades, still open at the deadline. It stays on as the resident book because its
regime (rich IV, no trend) is the complement of the intraday book's — and
because, on the honest weekly numbers of 30 Aug, **carry-only is the smoothest
arm the repo has** (§"Weekly consistency").

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
observed IV rank never exceeded 38%. **That measurement was a fixture result**
(`claude/why-no-trades.md`, corrected 30 Aug): the synthetic source could not
generate a high-IV state, so the gate rejected everything regardless of its
threshold. The conclusion — that 0.70 is too high — survived re-measurement on
real bars; the "coin flip over a judged window" arithmetic attached to it did
not, and should not be quoted.

Re-measured on real bars, current code, judged profile, 160 sessions Jan–Aug
2026:

| | |
|---|---|
| entry days | 69 of 160 (43%) |
| P(≥1 entry in 4 consecutive sessions) | 88.5% |
| expected entries per 4 sessions | 2.3 |
| longest dry spell | 7 sessions |

The premium gate is still the largest single rejector (10,516 of 27,387) but it
is not a hard block. 35% sells less-rich premium, so expect a lower win rate and
a smaller credit per trade. **The IV−RV spread gate is what still has to be
cleared, and it is the one carrying the actual edge** — the IV-rank floor is a
regime filter, not the thesis. `tests/test_config.py` pins the floor to a
0.30–0.50 band rather than asserting a single value: a carry book selling
premium at median richness is not a carry book, so the floor still has to be a
floor.

Note also `claude/iv-rank-divergence.md` (fixed 29 Aug, `8bff6f5`): live and
replay were computing IV rank by two different functions sharing a name —
percentile over a trailing year in replay, min-max over a handful of the same
morning's polls live, and not surviving a restart. One definition now, with a
20-observation floor and a persisted `IVHistoryStore` seeded from the replay's
own model. **Any conclusion about this gate drawn before that commit is not
evidence about the live book.**

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
- **Wings are `structures.wing_width_pct: 0.015` — a fraction of spot**, with
  `wing_width_points: 5` retained only as the fallback when the percentage is
  unset. 1.5% is close to the 5 points this book traded on SPY, so index
  behaviour is roughly unchanged, but it now means the same thing on TLT and GLD
  as on SPY instead of being several percent of a cheap name and under one
  percent of a dear one. This closes the "wing width is a flat 5 points" open
  item carried since 27 Aug.
- `min_credit_to_width: 0.15` as a floor, and **`risk.max_credit_to_width: 0.45`
  as a ceiling** — see the next section. The floor says the credit is paying for
  the tail; the ceiling says the quote is real.
- **7–14 DTE, not 20–45.** At 30 DTE a five-session hold captures ~20–25% of
  remaining decay; at 10 DTE it captures the majority. The cost is gamma — a
  deliberate trade of tail risk for realised P&L inside the window.
- Size from **max loss**, not capital. Note 25Δ *lowers* max loss per structure
  ($350 vs $424), so risk-based sizing trades more contracts for the same equity
  risk — some of any P&L improvement is size, not edge.

## The credit-to-width gate, and the headline edge that wasn't — 30 Aug

`risk.max_credit_to_width: 0.45`, enforced in `RiskEngine.evaluate` immediately
after `unknown_risk` so every strategy inherits it. Credit structures only: on a
debit spread the same ratio is high by construction and means the opposite
thing.

**Why it exists.** A credit structure's payoff is bounded by its width —
credit + max loss = width. A 14Δ condor collects ~17% of width, a 25Δ one ~30%.
Anything collecting far more than that is not a rich trade, it is a chain that
has mispriced a leg. Leg IVs are **recovered by inverting Black-Scholes on each
leg's own print, with no cross-strike consistency check**, so the recovered
smile need not be monotone:

| example | quote | implication |
|---|---|---|
| GLD 2026-03-23 | 385 put at IV 0.603, 383 put at IV 0.395 | a 2-wide put spread priced at **4.34** |
| EEM 2026-06-04 | short 70C at IV 0.396, long 71C at IV 0.215 | **0.81 credit on a 1-wide spread** |

Those are arbitrages no market offers, and the fill model books them happily.
Measured over 2026-02-01 → 08-21: **36 such condors, +$13,353, 71% of the carry
book's entire P&L.** The gate takes that to **zero**.

The gate catches the symptom cheaply. A **smile-monotonicity check on recovered
IVs** would catch the cause and is the better long-term fix; it is not written.

### The headline edge was mostly an artefact

Four controlled replays of 2026-02-01 → 08-21, 14 symbols, `--profile dev`, real
Alpaca bars and news from cache, `--critic heuristic`, one change per rung. The
baseline reproduces the prior long run to the cent, so this is a controlled
comparison, not two samples.

| arm | trades | net | carry | intraday | artefacts | win% | PF | maxDD |
|---|---|---|---|---|---|---|---|---|
| BASE | 441 | +13,764 | **+18,782 (198)** | −5,018 (243) | 36 (+13,353) | 46.5 | 1.397 | −3.62% |
| + credit/width gate | 409 | +924 | **+5,134 (160)** | −4,210 (249) | **0** | 46.7 | 1.032 | −3.28% |
| + drop VWAP re-cross | 409 | +1,704 | +5,250 (160) | −3,545 (249) | 0 | 49.4 | 1.058 | −3.01% |
| + time stop 20→60 min | 408 | +1,183 | +5,134 (160) | −3,951 (248) | 0 | 52.2 | 1.037 | −3.35% |

**The +$18,782 over 198 trades figure was ~93% artefact. Do not quote it, in the
deck or anywhere else.** The honest carry book is:

| | |
|---|---|
| trades | 160 |
| net | **+$5,134** |
| mean per trade | +$32.8 |
| **t-statistic** | **1.62** |
| win rate | 55.0% |
| profit factor | 1.375 |
| top 5 trades | **52% of P&L** |
| top 10 trades | 94% of P&L |

Read that honestly: **positive, not statistically significant over 6.5 months,
and concentrated.** Reward dispersion survives the gate and is now the largest
remaining source of run-to-run spread — risk per trade is well normalised
(median $995, p25–p75 $887–$1,073) while potential reward ranges $182 → $7,420.
Risk parity without reward parity is what lets one trade be a third of a run.

(Bookkeeping note: the per-trade statistics above are quoted from
`claude/exit-ab-and-the-credit-width-gate.md` §4 against its **+$5,250** arm —
+$32.8 × 160 = $5,250. The gate-only arm is +$5,134, mean +$32.1. Carry entries
are identical across the two arms; the difference is an intraday exit change.
Quote +$5,134 with a t of ~1.6 and the distinction does not matter to any claim
made here.)

### Where the gate is tonight — NOT IN THE REPO

`grep -rn max_credit_to_width src/ config/ tests/` returns **nothing** on the
working tree and nothing on `main`. `RiskConfig` has no such field and
`RiskEngine.evaluate` has no `credit_to_width` check. The change exists only as
`oaa_gate.patch` in the repo root, and the same is true of
`exit_on_vwap_recross`, which still reads `true` in
`config/strategies/intraday_momentum.yaml`.

**So a run started from this tree reproduces the artefact-inflated result, not
the honest one.** The +$5,134 figure is a measurement of a patched tree. Either
apply `oaa_gate.patch` before any run whose numbers will be shown, or say
plainly that the quoted figure comes from the patched build. This is the single
most important thing to resolve before presenting.

## Exits — 50% target / 1.5× stop / $450 hard stop

The old 30% / 2.0× pair implied a breakeven hit rate of **87% before paying any
spread**, against 88% observed on 42 real trades — one point of margin. Worse,
the 20% cost ceiling implied a 95.7% breakeven, so the gate was admitting trades
the arithmetic said would lose. 50% / 1.5× moves the idealised breakeven to 75%
and makes the same cost ceiling survivable at 85%.

| Trigger | Action |
|---|---|
| **`exits.max_loss_usd`** ($450) | close — checked FIRST, before anything else |
| **50%** of max profit | close |
| loss = **1.5×** credit | close |
| DTE floor (3) | close regardless of P&L |
| underlying touches a short strike | close the tested side |
| macro lens flags the name mid-hold | close at next open |
| submission flatten | close everything |

`exits.max_hold_days: 0` — the hard time stop is off, which is the default and
the tested setting.

### Why a hard dollar stop was added (27 Aug), and why it is $450 not $900

`loss_multiple_of_credit` is a **ratio**, so a wide structure with a fat credit
is allowed a proportionally fat loss before the stop trips. That is backwards:
the constraint that actually matters is `risk.daily_loss_limit_pct`, which on
the judged profile is **3%** — $3,000 on a $100k account — and is written in
dollars. One trade should not be able to spend a third of it.

**It was set to 900 and lowered to 450 the same day, because at 900 it never
fired.** Measured over 55 real trades, the six largest losses were $780–$868 —
all of them sitting just underneath the stop, and every one exited on the DTE
floor instead, which realises whatever has accumulated rather than capping it.
A stop set above the loss distribution is decoration. `0` disables the check.

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

### The tested-side exit is the largest loss bucket — measured 30 Aug, unresolved

`vol_carry` exits over Feb–Aug (pre-gate run, so the profit-target row is
artefact-inflated; the exit *distribution* is not):

| exit | n | net | median hold |
|---|---|---|---|
| profit target 50% of max profit | 61 | +28,992 | 5.0d |
| 3d to expiry, at/below target | 37 | +2,166 | 6.0d |
| window ended | 5 | +61 | 2.2d |
| loss reached 1.5× credit | 1 | −777 | 6.0d |
| **underlying touched a short strike** | **94** | **−11,661** | **1–2d** |

94 of 198 carry trades — 47% — exit on a short-strike touch, at a **21% win
rate**, with **median MFE 0.00 of premium** and a 1–2 day hold against 5 days
for the winners. The rule closes a *defined-risk* position at maximum adverse
excursion, before theta has done anything, on a normal and expected event.

The counterfactual is instrumented but **still unmeasured**. The 21% win rate
and the 0.00 median MFE are strong priors, not a result. If the measurement
supports it, replace the touch with a delta or premium-multiple rule (close at
2× credit, or short-leg delta > ~0.35). `exits.defensive_mode` is honoured now;
before 30 Aug the key was unread and every value behaved as
`close_tested_side`.

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

**Related, and separate — the vol anchor, fixed 30 Aug.** The same `_leg_marks`
recovered a leg's vol by inverting the daily print *at the current spot* and
then re-priced *at that same spot*, which is an algebraic identity: the mark
never left the daily close and structural delta was ~0 on every intraday mark.
The anchor is now recovered **once per contract per session**, de-skewed to ATM
so the smile is not applied twice, and held. Mark-move ÷ delta-move went from a
median of **0.000 to 0.945**. It matters here because every carry mark before
30 Aug was measuring that defect, and because the anchor is recovered from the
session's *closing* print — mild, non-directional lookahead that should be
stated in the deck rather than discovered by a judge.

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

**Carry overrides the default to `exits.reentry_cooldown_minutes: 1440` — one
entry per underlying per session.** The 60-minute global is sized for the
intraday book; on a 10:00–15:15 scan grid it let the same condor open at 14:00
and again at 15:15.

## Correlation is capped by Greeks, not by a position count — 30 Aug

Every portfolio limit that existed counted *structures*: `max_positions` (25),
`max_new_positions_per_day`, `max_positions_per_underlying` (1),
`duplicate_structure`, `reentry_cooldown`. All five pass twenty-five positions
that are one bet — and the ten-symbol universe measured as **2.4 independent
bets**. `risk.max_net_delta`, `risk.max_net_vega` and
`max_notional_per_trade_pct` were configured, commented and documented, and read
by **no code path**.

`src/oaa/risk/exposure.py` and a gate at step 5b of `RiskEngine.evaluate` now
enforce them, with **coverage of the open book counted and carried onto every
verdict, pass or fail** — a cap computed over half a book silently passes trades
a full one would refuse. Two unit bugs were found by running it rather than
reading it: vega divided by 100 twice, and each leg priced off its own strike as
a spot proxy (worst on a condor with wings).

Calibration mattered in both directions. Against 44 approvals the old
unenforced values were nonsense: `max_net_delta: 0.35` would have refused
essentially every trade the book has ever made (observed median 1.47×, max
5.24×), and `max_net_vega: 50` would never once have fired (observed max $27).
Shipped values sit **just above the highest reading the book has produced** —
`max_net_delta: 5.5`, `max_net_vega: 30.0`, `max_notional_per_trade_pct: 5.5`.
They cannot fire on observed behaviour and they do stop it creeping past it. A
p90 setting was measured and rejected: it removed five trades worth ~$960 on a
window containing no stress day, which is a known cost for an unmeasured
benefit two days before the entry cutoff.

## Live findings — 31 Aug, on the judged account

Three defects surfaced in today's session. None is a market state; all three are
our own code or our own config reporting itself as the market.

### 1. The live chain DTE window — confirmed, fixed, on `main` (`375e59a`)

`config/default.yaml: options.min_days_to_expiry: 3`, and both live providers'
`context()` called `option_chain(symbol)` with **no** `min_dte`, so the live
chain was always 3–45 DTE. `chain_dte_window()` had exactly one consumer,
`backtest/runner.py` — nothing in the live path read it. The intraday book,
which wants 0–2 DTE, therefore filtered an empty shelf and reported *"no
contracts survived the liquidity filter"* on SPY and QQQ, which is impossible
and is what forced the search into the code.

This hit carry indirectly rather than directly — 7–14 DTE sits inside 3–45 — but
it is the reason 25 of 54 rejections carried *"term structure: chain has no two
expiries to compare"*: the front anchor is 1 DTE and was never fetched.

The fix is **27 lines across three files, committed to `main` as `375e59a`**
on the evening of 31 Aug: a `context_chain_window()` on `MarketDataProvider`
returning `tradable_dte_range(cfg)`, called by both live `context()` methods.
The requested window becomes **0–32 DTE**. It is *cheaper*, not dearer — one
`get_option_chain` per symbol either way, and the modelled contract count falls
7,865 → 7,182 (−8.7%) because the 33–45 tail is larger than the 0–2 head on
everything except SPY and QQQ. Five new tests in `tests/test_live_chain_window.py`
failed before the change and pass after it; the 13 pre-existing suite failures
were identical on both trees (Streamlit rendering tests, a Linux artefact of the
checking environment).

**The running process has not restarted into it.** The agent holds its modules
in memory, so the live chain request is still 3–45 DTE until `oaa run` is
restarted. Committing changed the repo, not the process.

Do **not** lower `options.min_days_to_expiry` globally to achieve this: that
widens the carry chain, whose 3 DTE floor is a gamma control.

### 2. The chain filter guts the view a condor selects from — diagnosed, half-fixed

Today's carry scans produced what looked like three unrelated problems:

```
DIA / QQQ / XLF / EEM:  no expiry with DTE in [7, 14]
FXI:                    no puts for 2026-09-11
SLV:                    no puts for 2026-09-09
XLU:  iron condor strikes out of order: [43.0, 43.5, 35.0, 41.5]
```

They are one bug. `ChainView` holds `quotes` (post-tradeability-filter) and
`all_quotes` (the pre-filter wing pool). **Everything the carry book selects
with reads only `quotes`**: `expiries()`, `for_expiry()`, and `by_delta` with
its unguarded `by_moneyness` fallback. `vol_carry` declares no filter overrides,
so it inherits the global `min_open_interest: 250`, `min_option_price: 0.10`,
`max_bid_ask_spread_pct: 0.12`.

**A condor sells OTM options, and far-OTM contracts are cheap, wide and thinly
held — they fail all three filters by construction.** The filter removes exactly
the strikes the structure is built from. The XLU condor is the giveaway: the
short call at 35.0 sits *below* the short put at 43.5, i.e. deep ITM, because
the filtered call ladder for that expiry ended before it reached spot and
`by_moneyness` returned the nearest survivor with no check that it was anywhere
near the money. The wings escape this — `strike_offset(allow_unfiltered=True)`
reaches into `_wing_pool`. **The wing can see the real ladder and the short leg
cannot.** That asymmetry is the bug in one sentence.

**The fix is a risk decision, not a correction, and only half of it should
ship before the cutoff.**

- **A — shipped, on `main`.** `iron_condor_by_delta` now
  asserts the body straddles spot and names the real cause ("the filtered
  ladder for this expiry runs X–Y on calls … one side ends before it reaches the
  money … a filter result, not a listing gap"). Creates no trades; costs
  nothing, because these candidates were already rejected. `ChainFilter` also
  gained `reject_reason()` and the CLI gained **`oaa chain --why`**, which
  tallies which line of config emptied the chain.
- **B — NOT shipped, deliberately.** Letting expiry and short-strike selection
  see the pre-filter pool the way the wings already do would unblock DIA, QQQ,
  XLF, EEM, FXI, SLV and XLU — half the carry universe. It is also how you end
  up **selling an option with 40 open interest and a 30% bid-ask on the judged
  account.** `min_open_interest: 250` is there so the book can get out of what
  it gets into. Widening it two days before the entry cutoff, to raise trade
  count, on the account being scored, is the exact move this project has twice
  written down as the wrong one. If the book is still empty on Wednesday, B is a
  deliberate choice with a per-symbol liquidity floor, not a rushed one.

**The design note worth a slide:** every one of these messages reported the
state of the *filtered view* as though it were the state of the market. A
rejection that cannot distinguish "the market does not offer this" from "our own
filter removed it" is not a diagnostic. That is what `--why` exists to fix.

### 3. IV rank diverges between the two books in the same process

Same cycle, same day, same process:

| book | reading |
|---|---|
| carry, 14:01 | IWM **0%**, SPY **34%**, TLT IV-RV spread 2.1% |
| intraday, 14:00–14:45 | DIA **90%**, GLD **100%** (×3), XLE **100%** (×2), XLF **100%**, TLT **98%** |

An IV rank pinned at exactly 100% on four symbols at once is the same class of
number as 304-of-304 — almost certainly degenerate because it is computed off a
chain with one usable expiry. It cost 10 intraday candidates at the 85% ceiling.
**Re-derive it after the chain-window fix lands, and only then decide whether
any ceiling needs moving.** The carry readings look sane; do not treat the
divergence as evidence about the carry gate in either direction.

### What was genuinely healthy today

Worth stating, because not everything in the journal is a defect. The carry
book's *"ADX 31 > 25 — the underlying is trending and a range bet is the wrong
instrument"* (GLD, XLE, XLV) and *"IV-RV spread 2.1% below the 3.0% floor"*
(TLT) are gates working exactly as designed on real measured inputs. The events
watch ran all seven hourly cycles, the firewall verified with a $15,000 intraday
budget, and the cycle grid was on time (14:00/14:15/14:30/14:45 UTC, no gaps).

## Weekly consistency — carry-only is the smoothest arm

29 weeks over 2026-02-01 → 08-21:

| book | winning weeks | mean | sd | worst | best | mean/sd |
|---|---|---|---|---|---|---|
| BASE as-is | 62% | 475 | 1,290 | −1,079 | 4,992 | 0.368 |
| gate + noVWAP, both books | 52% | 59 | 590 | −955 | 1,247 | 0.100 |
| gate + noVWAP, **carry only** | **59%** | 181 | **473** | **−918** | 1,082 | **0.383** |

BASE's 62% and its +4,992 best week are both artefact-driven and are not a
comparison. Against the honest baseline, **dropping `intraday_momentum` is the
single biggest smoothing lever available**: weekly sd 590 → 473 (−20%), winning
weeks 52% → 59%, mean weekly P&L 59 → 181, and the same risk-adjusted
consistency as the fake baseline — on trades that could actually have been
filled.

That change is **not applied**. It is a live decision, not a finding.

The supporting arithmetic for why a short window says nothing: carry has a 5–6
session median hold and needs a 20-observation trailing IV series, so a 5-day
window takes 0–3 carry trades and the week's P&L is the intraday book. Intraday
per-trade sd is ~$151, so the standard error on a 40-trade weekly sum is
**±$987**. **A one-week result is only informative outside roughly ±$1,000**, in
either direction. Do not judge this book on the judged window's P&L alone; judge
it on whether the gates fired correctly.

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
  *within one structure* is now handled explicitly rather than silently. The
  cost of that recovery is the cross-strike inconsistency the credit-to-width
  gate now catches.
- **Scans moved from 4 a day to 12**, every 15 minutes 10:00–14:45. This book did
  not need that — the intraday book did — but it is what exposed the duplicate
  entries.
- **Marks are evaluated every minute** (`mark_interval_minutes: 1`). This is
  correct and must not be "fixed" again: the intraday stop overshoot it was
  once blamed for is honest 1-minute gap-through.
- **The critic runs in replay**, in the live order: cost → critic → risk engine →
  partner veto. Featherless both sides — temperature 0 in replay, 0.2 live.

## Opportunistic book — `event_premium`

Dormant by default. Compares the implied move in the front expiry against a
committed historical realised distribution. Implied ≥ 1.25× realised median →
sell a defined-risk condor. Otherwise stand down, which is the expected outcome
and accounts for the `scheduled_event` entries dominating the rejection log (8
of them on 31 Aug).

## Submission controls (set in config, not left to memory)

- `exits.entry_cutoff_utc: 2026-09-02T20:00:00Z` in `config/strategies/vol_carry.yaml`
  — no new carry structures. This moved out of `management` on 29 Aug: as a global
  key it also blocked the event book, whose prints run to 3 Sep.
  `management.entry_cutoff_utc` is `null` in `config/default.yaml`, which is
  what makes the per-strategy key the one that binds.
- `management.submission_flatten_utc: 2026-09-04T13:45:00Z` — close everything,
  confirmed-flat poll, checked on **every runner poll**.

## Open items — as of 31 Aug, evening

**Blocking, before anything is shown or run:**

- **The credit-to-width gate is not in the tree.** `oaa_gate.patch` is unapplied
  and `exit_on_vwap_recross` is still `true`. Every honest number in this
  document was measured on a patched build. Apply the patch or state the
  provenance.
- **The live process has not restarted into the chain-window fix.** The code is
  on `main` (`375e59a`); the running agent holds its modules in memory, so until
  `oaa run` is restarted the intraday book still cannot trade and the
  term-structure vote is still dead live. This is now the only thing standing
  between the fix and the account.

*Cleared 31 Aug evening:* the stale `.git/index.lock` that blocked every git
write, and the `fix/live-chain-dte-window` branch — everything is committed on
`main` and the working tree is clean.

**Open, and honest about it:**

- **The book is largely empty live**, and §"Live findings" 2 explains why: our
  own liquidity filter removes the strikes a condor is built from on more than
  half the universe. Fix A is in; fix B is a deliberate risk decision that
  should not be taken to raise trade count.
- **Reward dispersion.** Top 5 trades = 52% of P&L. Risk per trade is
  normalised; reward is not ($182 → $7,420). Nothing caps it.
- **Smile monotonicity on recovered IVs is unchecked.** The 0.45 gate catches
  the symptom; a cross-strike consistency check on the recovered surface is the
  real fix and is not written.
- **The tested-side touch rule is still unmeasured** against its counterfactual —
  94 exits, 21% win rate, median MFE 0.00. Instrumented, not run.
- **Correlated tail.** `max_positions_per_sector` still does not exist
  (confirmed by grep, 31 Aug). The net-delta cap subsumes much of it on an ETF
  universe, but not all, and it is set above the highest reading the book has
  produced rather than at p90.
- **`portfolio_risk` has a unit mismatch.** The new trade is charged at
  `max_loss × quantity` while existing positions are counted at
  `abs(market_value)` — fine for long premium, understates short condors ~2.6×.
  Left deliberately: it changes what this book may hold and should not be
  changed two days out.
- **The live feed's vega convention is unverified** against a live quote. The
  plausibility warning fires if it is per-1.00, but check it.
- **The vol anchor carries mild lookahead** — recovered from the session's
  closing print. Non-directional, accepted by design, but say it before a judge
  finds it.
- Options level 3 on the judged paper account.
- Whether the CLI submits combos atomically (`execution.multileg_mode: atomic`
  is set; that it is honoured end-to-end is not verified).

**Closed since the last revision:** wing width is now a fraction of spot
(`wing_width_pct: 0.015`); the $900 hard stop that never fired is now $450; the
IV-rank live/replay divergence is fixed (`8bff6f5`); the greek caps are
enforced rather than decorative; the intraday vol anchor is fixed.
