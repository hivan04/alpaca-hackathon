# Intraday book — intraday_momentum

**Status as of 31 Aug 2026, evening:** `enabled: true`. Everything below is on
`main` as of `375e59a`; the working tree is clean.

**Two things a judge should know before anything else in this file:**

1. **Measured on real Alpaca bars, Feb–Aug 2026, this book loses money after
   costs and the loss is not noise.** n=243, **−$5,018**, mean **−$20.7** per
   trade, **t = −2.09**. No exit change tested is individually significant. The
   honest reading is that the *signal* has no edge after costs on this universe
   — not that the exits are mis-set. §"The measured verdict" has the ladder.
2. **On the judged account today it cannot trade at all.** The live data
   providers request a 3–45 DTE chain; this book selects 0–2 DTE, so it is
   handed an empty shelf and reports "no contracts survived the liquidity
   filter". Confirmed live 31 Aug. §"The live chain-window defect" has the
   evidence and the fix.

Everything below is written on the assumption that both of those are true and
that the book is presented as a measurement, not as a product.

This document was rewritten 31 Aug. Where a claim was made earlier and has since
been measured, the earlier claim is marked rather than deleted.

## Why it was promoted (27 Aug) — and why that argument is now weaker

The carry book measured **0.30 trades per session** over a 61-session run with
an average hold of **5.78 days**. Over the 5–6 session judged window that is
1.5–1.8 trades, still open at the deadline — a P&L score that is an unrealised
mark on two positions. The carry book's holding period *is* the competition.

This book is flat by 15:10 every session, so everything it does realises inside
the window. That argument is still sound and it is the only argument left for
running this book: it produces realised, in-window trades.

**What has changed since 27 Aug is that we now know what those trades are worth.**
Trade *count* was never the objective; realised P&L is. A book that reliably
delivers 40 trades a week at −$20 each is worse than a book that delivers two.
See §"Dropping this book" — it is on the table and the arithmetic favours it.

The original reason for keeping it off also still stands: judges read the full
account history, and a malfunctioning intraday loop firing junk orders is
permanently on the record.

## Thesis, stated honestly

A **momentum strategy expressed through options**. The option is leverage and
defined risk; it is not the source of edge. Presenting it as a vol strategy
invites "which vol premium are you harvesting?", and there isn't one.

|  | Carry book | Intraday book |
|---|---|---|
| Edge | IV–RV premium | directional continuation |
| Option role | the position | leverage + loss bound |
| Hold | 3–10 sessions | minutes to hours, flat by 15:10 |
| Primary risk | short gamma | **spread cost** and signal decay |
| Measured, Feb–Aug 2026 | +$5,250 / 160, t = 1.62 | **−$5,018 / 243, t = −2.09** |

The portfolio argument for running both is signal-uncorrelated and
vol-opposite. The measured argument for running both is now one-sided.

## The hard constraint

Target profit is 5–15% of premium ($10–30 on a $2.00 contract). A $0.10-wide
single-name quote costs $20 round trip — the entire target. **Index and sector
ETFs only.**

The universe is eight names: `SPY QQQ IWM DIA XLF XLE TLT GLD`. XLK and SMH
were removed 28 Aug on measured evidence, not on P&L — XLK's chain-median
spread was **27.94%** against QQQ's 2.85%, at **0.96** correlation to QQQ. XLE
is kept at a wide 9.80% because it is the only negatively correlated name in
the table. Measured 28 Aug, the ten symbols behaved like **2.4 independent
bets**. (The header comment in `config/strategies/intraday_momentum.yaml` still
says "ten"; the list itself is eight.)

## Signal stack (`src/oaa/strategies/intraday_momentum.py`) — REWRITTEN 28–30 Aug

The "three indicators, all hard" description in earlier versions of this
document is **obsolete**. The book now has **two hard gates and a confirmation
score**.

### Why the vetoes were replaced by votes (28 Aug)

Entry used to require eight conjunctive conditions. Eight vetoes in a row, each
passing ~70% of candidates, is 0.7^8 ≈ **6%**: the book was arithmetically
designed almost never to fire. Measured over 864 candidates in one 9-session
window, **424 died on volume alone and none survived the chain**. Every time one
gate was loosened, the next became the wall.

They ask genuinely different questions, so demanding unanimity is a tickbox
exercise rather than a confluence.

### What is still hard

- **VWAP — the trigger.** It is the only signal with an opinion on *direction*;
  without it there is no trade to make. A session-VWAP cross within
  `cross_lookback_bars: 15` (fifteen 1-minute bars = exactly one polling gap),
  or price sitting on the VWAP band.
  - The band is now expressed as `band_dispersion_mult: 2.0` — a multiple of how
    far price *normally* sits from VWAP this session, not of one bar's ATR. The
    old dollar-denominated version came to **±$0.08 on SPY**, 0.013% of spot,
    against a typical session dispersion of 0.1–0.3%: it was 10–20× too tight
    and "no VWAP cross / not on the band" became **75% of every rejection this
    book ever produced**. Swept on real bars at 0.5/1.0/1.5/2.0/3.0: at 0.5 and
    1.0 the top rejection was still the VWAP test; at 1.5 and above it moved to
    the volume gate. **2.0 is where the band stops being the wall.**
- **Spread gate — economics, not evidence.** `max_relative_spread: 0.04` and
  round trip ≤ 30% of target.

### What now votes — `confirmations_required: 3` of up to seven

`confirmations` is a sum over votes that passed; `needed` is
`min(confirmations_required, possible)`. **Unmeasurable is not failed and is not
true** — a vote that cannot be evaluated does not enter `possible` either.

| vote | question | config |
|---|---|---|
| volume | is participation real for *this time of day*? | `volume_zscore_min: 0.5`, 30-min buckets |
| persistence | did the move survive more than one bar? | `persistence_bars: 2` |
| band width | is the volatility regime expanding? | Bollinger **width**, rising over 6 bars |
| higher timeframe | is this with the hour or a retrace inside it? | 1-min bars resampled to 60 min, 3 bars back |
| term structure | does the *surface* expect anything from this session? | band `[−10%, +25%]` on `(front_iv − back_iv)/back_iv` |
| RSI | exhaustion? | one-sided veto-vote, 80/20 not 70/30 |
| catalyst | is there a mechanism behind the move? | `required: false` — demoted to a vote 28 Aug |

**`volume_zscore_min` was lowered 1.0 → 0.5.** A z of 1.0 demands the top ~16%
of volume for that bucket, which is a demanding bar for a gate that only
*confirms* a move it did not choose. 0.5 is the top ~31%. It was the
second-largest rejector at ~24% of intraday candidates.

**The term-structure vote (30 Aug) is the only signal in this book that does not
read the price series.** Volume, persistence, band width and hourly drift are
four different questions asked of one source, so when that source is
uninformative they fall silent together. The chain is a second source and the
book was reading one scalar out of it (ATM IV and its rank).

The band is **a prior, not a fit** — nothing in it was swept. Below −10% (deep
contango) the surface expects nothing from the session and front gamma is cheap
because nothing is priced to happen. Above +25% (steep backwardation) the move
is already priced and we would be paying up for our own forecast — the argument
`selection.iv_rank_no_trade_above: 0.85` makes at the *level*, made at the
*slope*.

The vote **cannot narrow the entry set**, by arithmetic rather than by promise:
it raises `possible` 6 → 7 while `needed` stays at 3. Pinned by
`test_the_vote_can_never_turn_a_trade_into_a_rejection`.

`require_measured: true` is **non-negotiable in replay**. The modelled chain's
term structure is `backtest.chain.term_slope`, a constant — a slope read off it
would vote identically on every candidate forever and look like a signal doing
work. Both anchors must come from real prints.

A/B on one cached window, `term_structure.enabled` the only difference:

| | off | on |
|---|---|---|
| ideas generated | 136 | **151** |
| `confirmation` rejections | 56 | **29** |
| trades | 42 | 44 |

**The first two rows are the result.** The P&L rows from that run (net $480 →
$960) are one sample of 44 trades and are not evidence of edge; they are not
quoted here for that reason.

Two defects the first real run of the vote produced, both found by running it:
`front_dte` was a target rather than a floor, so a session listing only 0 DTE
and 32 DTE picked the 0 and returned slopes of **+257.9% (TLT)** and **+621.4%
(XLF)** — Black-Scholes inverted on a contract with hours of life. `front_dte`
is now a floor, and `|slope_pct| > 1.0` is reported as *unmeasurable* rather
than as an extreme slope.

## Timing — rebuilt 27 Aug, unchanged since

`data.intraday_timeframe: 1Min`, `min_bars: 30` (clears by 10:00),
`cross_lookback_bars: 15`. `backtest.session_times_et` runs every 15 minutes
from 10:00 to 15:00 plus a 15:10 sweep. Most of that grid is **management-only**:
the strategy's own `time_gate` refuses entries before 09:45, from 11:30 to
13:29, and after 14:45. The lunch and 15:10 cycles exist so that an open
position is still marked, stopped and time-stopped — without them a position
opened at 11:00 ran 150 minutes unmanaged and anything opened at 14:45 ran to
expiry.

The pre-27-Aug configuration (`5Min` bars, `min_bars: 30`, four scans a day,
`cross_lookback_bars: 3`) could only trade 13:30–14:45 and asked an event-driven
question of a four-times-a-day poll; **86% of candidates died at "no VWAP
cross"** at every configuration tested. That was an architecture mismatch, not a
weak signal. It is fixed, and the signal is still not profitable — which is the
point of recording it here.

Do not raise `cross_lookback_bars` much beyond the polling interval: past it the
book re-fires on a cross an earlier cycle already acted on.

## Option selection (`src/oaa/options/selection.py`)

Conditioned on expected move (ATR scaled by remaining session), IV level and IV
rank:

| Condition | Selection |
|---|---|
| small move, cheap IV | ATM 0–1 DTE — max gamma |
| small move, rich IV | ATM longer dated — less burn if it stalls |
| large move, cheap IV | slightly OTM — convexity per dollar |
| large move, rich IV (≥ 0.60) | **debit vertical** — caps the expensive wing |
| IV rank ≥ 0.85 | **no trade** — already priced in |

The last row is worth a slide. It is also, live today, the **second-largest
rejector** — 10 candidates on 31 Aug — and that is suspicious rather than
virtuous: see the IV-rank note under the chain defect.

`structure.max_option_price: 25.00` is this book's only premium ceiling since
the global $25 chain-filter cap was removed on 29 Aug. Without it the selection
can land on a deep-ITM contract at $75+.

## Sizing — and the `single_long` denominator defect (found 30 Aug, OPEN)

Long options or debit verticals only, so max loss is the premium paid, always —
**by construction, and the construction has a leak.**

`build_single_long` sets `max_loss = debit * MULTIPLIER` at **idea-build** time
and never refreshes it against the **fill**. Two consequences:

1. **Sizing.** `size_by_risk` divides the risk budget by `max_loss`. On BT0038
   (QQQ 0 DTE put) the recorded `max_loss` was **$93/contract** against a
   premium actually paid of **$411/contract**: q10 booked as $930 against a
   $1,000 cap and was approved, while the position actually risked **$4,114 —
   4.1% of equity against a 1% limit.**
2. **Exit levels.** `pnl_pct = gross / (max_profit or max_loss)`. `single_long`
   has `max_profit = None`, so **every intraday exit percentage is measured
   against `max_loss`**, not against the premium the label promises. BT0038's
   exit reads "target 10% of premium reached (75%)" — 75% of a $930 denominator,
   which is **17% of the $4,114 actually at risk**. The label and the arithmetic
   agree only when `max_loss` equals the premium.

Across the 243 `single_long` trades in the Feb–Aug window, premium paid ÷
recorded `max_loss`:

| | |
|---|---|
| median / p75 | **1.01** — correct in the normal case |
| > 1.5× | **15 trades (6%)** |
| > 3× | 6 trades |
| max | **3.61×** |

It is a tail, not a uniform error, **and the tail is where this book's extremes
live**: the largest single loss (BT0104 GLD, −$1,023, 3.3× oversized, $3,471 at
risk) and the largest single win (BT0038, +$700, 4.4× oversized) are both in it.
190 of 243 trades carried more than the $1,000 per-trade cap.

**This contaminates the measured expectancy in both directions.** The −$20.7
per trade below includes positions the risk engine would have refused had it
priced them correctly. It does not rescue the result — the mean is negative and
the largest *win* is one of the oversized trades — but it is a known impurity
and it should be stated rather than glossed.

The fix is to re-derive `max_loss` from the fill before the risk engine sizes
the trade, or to re-run the risk verdict against the filled debit. Cheap interim
guard: reject when the filled debit exceeds the idea's `max_loss` by more than a
small tolerance. Separately, either rename `exits.target_pct_of_premium` /
`stop_pct_of_premium` to say `of_max_loss`, or compute them against the premium
the name promises.

`vol_carry` is unaffected — its structures set both `max_profit` and `max_loss`
from strike widths, so the denominator is a width and cannot go stale.

**There is no per-strategy sizing.** The `sizing:` block in this book's YAML was
removed 30 Aug because not one of its keys was read by any code path. What
actually bounds the book lives in `risk:`, globally: `max_positions: 25`,
`max_positions_per_underlying: 1`, `max_net_delta: 5.5`, `max_net_vega: 30.0`,
and per profile `max_risk_per_trade_pct` / `max_new_positions_per_day` /
`daily_loss_limit_pct` (judged: **1% / 20 / 3%**).

## Exits, and what happened when they were swept

`target_pct_of_premium: 0.10`, `stop_pct_of_premium: 0.15`,
`time_stop_minutes: 20`, `flat_by: "15:10"`. The stop is wider than the target
on purpose: option premium is noisy and a tight stop is hit by spread flicker
alone. The stated cost is a breakeven hit rate of ~60% — **realised it is worse
than that**, because the stop slips further than the target does:

| | measured, Feb–Aug 2026 |
|---|---|
| win rate | **40.3%** |
| avg win / avg loss | $116 / −$113 → payoff **1.03** |
| **breakeven win rate required** | **49.3%** |
| median hold | 14 minutes |
| median MFE | **+4% of premium** (against a +10% target) |
| p75 / p90 MFE | +11% / +14% |
| stop exits (n=43) | target −15%, realised median −18%, mean −23%, **worst −96%** |

The median trade never approaches its target and dies on the time stop.

## The measured verdict — 30 Aug (this supersedes every earlier P&L claim)

Four full replays of **2026-02-01 → 2026-08-21**, 14 symbols across all books,
`--profile dev`, **real Alpaca bars and news** from cache, one change per rung.
The baseline reproduces its own prior run to the cent, so the ladder is a
controlled comparison.

`intraday_momentum` alone, per-trade mean and t-statistic:

| arm | n | net | mean | t |
|---|---|---|---|---|
| BASE | 243 | −5,018 | −20.7 | **−2.09** |
| + credit/width gate | 249 | −4,210 | −16.9 | −1.92 |
| + drop VWAP re-cross | 249 | −3,545 | −14.2 | −1.57 |
| + time stop 20 → 60 min | 248 | −3,951 | −15.9 | −1.62 |

**Reliably negative. No exit change is individually significant (|t| ≤ 0.28 on
the deltas). The exits are not the problem — the signal has no edge after costs
on this universe.**

Before spread the book lost **−$1,742** and paid **$3,254** of modelled spread.
That is not "costs ate a real edge": the pre-cost number is negative too.

### The VWAP re-cross rule — removed, and be honest about why

Intraday −$4,210 → −$3,545 (+$665), win rate 41.4% → 45.8%.

**That delta is inside noise**: +$2.67 per trade against a standard error of
$12.61, **t = 0.21**. The justification is the mechanism, not the improvement —
the rule produced **0 wins in 55 attempts, −$3,725**. Released, those 55 trades
went: 5 more reached the target, 15 became stops, 35 became time stops. The rule
was mostly exiting at a worse moment than doing nothing, not saving anything.

### The time stop — measured and REFUTED

`claude/exit-shape-arithmetic.md` argued a momentum book needs its target above
its stop and that a 20-minute time stop cuts moves off before they develop. Both
are now testable and both are wrong.

At the 20-minute stop, only **30.5%** of trades ever touch +10% and **7.4%**
ever touch +15%. There is no room to raise the target — the move has not
happened when the exit fires.

Lengthening the hold to 60 minutes grows the distribution **symmetrically**:

| | reach +10% | reach −15% |
|---|---|---|
| 20-minute stop | 33.7% | 15.7% |
| 60-minute stop | 44.4% | 23.4% |
| growth | ×1.32 | ×1.49 |

Targets hit rose 85 → 114 and win rate 45.8% → 50.4%; stops rose 61 → 90 and
average loss went −$112 → −$147. Breakeven rose to 56.5% against a 50.4% win
rate. **Net −$406.** The extra time helps the losses slightly more than the
wins, which is what a zero-edge signal looks like on a longer leash.

**Do not lengthen the time stop and do not sweep it further.** Do not sweep the
target either. Both are measured and refuted.

### Why a one-week re-run says nothing

Intraday per-trade sd is ~$151, so the standard error on a 40-trade weekly sum
is **±$987**. **A one-week intraday result is only informative outside roughly
±$1,000.** Nothing inside that band should change a decision in either
direction — including a good week during judging.

## Dropping this book — the weekly-consistency argument

29 weeks in the Feb–Aug window, after the pricing-artefact gate:

| book | winning weeks | mean | sd | worst | mean/sd |
|---|---|---|---|---|---|
| both books | 52% | 59 | 590 | −955 | 0.100 |
| **carry only** | **59%** | **181** | **473** | **−918** | **0.383** |

**Dropping `intraday_momentum` is the single biggest weekly-smoothing lever
available**: weekly sd −20%, winning weeks 52% → 59%, mean weekly P&L 59 → 181.

**What argues for keeping it anyway,** stated plainly so the trade-off is the
judge's to make:

- It is the only book that produces **realised, in-window** P&L. Carry takes
  ~0.3 trades a session and holds ~5.8 days; over a 5–6 session judged window
  the account's score would be an unrealised mark on one or two positions.
- The intraday book is where every diagnostic in this project was found — the
  frozen marks, the vol-anchor fixed point, the confirmation arithmetic, the
  live chain window. Removing it removes the demonstration.
- Uncorrelated in signal and opposite in vol exposure to the resident book.

**What argues against:** a measured, statistically reliable negative expectancy,
a per-trade mean of −$20.7, and a t-statistic that will not improve with a
5-session sample.

The middle path — and the one this document recommends — is to **present the
measurement rather than the P&L**: run the book if the chain fix lands and the
diagnostics are worth showing, and be first to say it is negative. Do not tune
it further to make the number look better; every dial that could be moved has
been moved and measured.

## The live chain-window defect — CONFIRMED on the judged account, 31 Aug

`runs/judged/journal.jsonl`, 2026-08-31, cycles 10:00–10:45 ET. **12 intraday
`structure` rejections**, all of the form:

```
QQQ: no contracts survived the liquidity filter   (4x)
TLT (3x)   SPY (3x)   XLF (1x)   DIA (1x)
```

**SPY and QQQ options cannot fail a liquidity filter.** That impossibility is
the tell.

The chain of causation, in the code:

- `config/default.yaml` — `options.min_days_to_expiry: 3`.
- `alpaca_data.py` and `cli_data.py` call `option_chain(symbol)` with **no**
  `min_dte`, so both live providers default to 3 and Alpaca is asked for
  `expiration_date_gte = today + 3 days`.
- `intraday_momentum.chain_dte_window()` returns **(0, 2)**.
- `grep chain_dte_window src/` — **one consumer, `backtest/runner.py`.** Nothing
  in the live path reads it.

So the live book filters a 3–45 DTE chain for 0–2 DTE, gets an empty `ChainView`,
and the builder raises. Every cycle, every day. The message reads as liquidity
and is an **empty shelf**.

Corroborating: 25 of 54 rejections carried `term structure: chain has no two
expiries to compare` — the front anchor is also below 3 DTE and also absent, so
the term-structure vote is **dead live and alive in replay**.

### Loosening the liquidity filter is the wrong fix — do not do it

The obvious reading is "the filter is too strict". The arithmetic refutes it
before any run does.

- **The book already passes a permissive filter**: its own `ChainFilter` sets
  `min_volume=0`, OI relaxed from the global 250 to 100, spread ceiling doubled.
- **The liquidity predicates never execute.** `ChainFilter.accepts` tests DTE
  first and returns `False`; price, spread, OI and volume are never reached.
  With `max_dte=2` against a chain whose minimum DTE is 3, **100% of quotes die
  on the first line**. Setting `min_open_interest=0` and `max_spread_pct=inf`
  changes the outcome by exactly zero contracts.
- **The asymmetry between the two books proves it on today's data.** Carry wants
  7–14 DTE, inside the global window, and fails with `no expiry with DTE in
  [7, 14]` — a non-empty view. Intraday, outside the window, fails with the
  liquidity message. If liquidity were binding, carry would fail the same way.
- **The filters are load-bearing.** `max_price: 25.00` is this book's only
  premium cap since 29 Aug; removing it two days before the entry cutoff would
  convert "no trades" into "bad trades on the judged account".

Also do **not** lower `options.min_days_to_expiry` globally — that widens the
carry chain, whose 3 DTE floor is a gamma control.

### The fix, and its state

**27 lines across three files**, committed to `main` as `375e59a` on the evening
of 31 Aug.

One new method on `MarketDataProvider` returning `tradable_dte_range(cfg)` — the
function replay already uses — and both live `context()` methods passing that
window to `option_chain`. No config change, and the liquidity filter is
untouched.

| judged profile | requested chain |
|---|---|
| before | 3–45 DTE |
| after | **0–32 DTE** |

Both term-structure anchors land inside the window (front 1 DTE, back 30 DTE).

**It is cheaper, not dearer.** Request count is unchanged — one `get_option_chain`
per symbol either way, only the date bounds move. Modelled contract count across
the 14 live symbols **falls 7,865 → 7,182 (−8.7%)**: only SPY and QQQ grow,
because they list daily expiries, and everything else loses the 33–45 tail. And
the point of the exercise, same model, SPY on 31 Aug:

| window | 0–2 DTE contracts |
|---|---|
| 3–45 | **0** |
| 0–32 | **324** |

Five new tests in `tests/test_live_chain_window.py`; **5 failed before the
change, 5 pass after it**, and the full-suite failure set was identical on both
trees (13 pre-existing Streamlit rendering failures in the checking
environment). It is **committed but not restarted into** — the running agent
holds its modules in memory, so the live process is still requesting 3–45 DTE
until `oaa run` is restarted.

### An IV-rank divergence the same run surfaced

Same cycle, same day: carry reported IWM **0%** and SPY **34%**; intraday
reported GLD **100%** (×3), XLE **100%** (×2), XLF **100%**, DIA 90%, TLT 98%.
An IV rank pinned at exactly 100% on four symbols at once is a degenerate
number, most likely computed off a chain with one usable expiry. It cost 10
candidates today at the 85% ceiling. **Re-derive it after the chain fix lands,
and only then decide whether the ceiling needs moving.**

## A history of the marks, because it voids earlier numbers

Everything this book produced in replay before 30 Aug measured the marking path,
not the strategy. Recorded here so no stale figure is quoted by accident.

| date | state |
|---|---|
| 29 Aug | **Alpaca option bars are daily.** Every intraday mark of a contract inside one session returned the same daily close. 53 of 53 legs had `entry_mark == exit_mark`; MFE +0.0% on every trade; net loss equal to the modelled spread **to the cent**. |
| 29 Aug (fix 1) | Marks re-priced from the model against the intraday spot. Symptom attenuated, not fixed. |
| 30 Aug | **The half-landed state:** `real_mark_fraction` **0.00** across 434 intraday trades, median leg mark change 0.6%, and **0 of 322** time-stopped trades ever reached the +10% target — against a pricer whose own vol implies ~49% premium swing per 1σ 30-minute move. ~80× too small. |
| 30 Aug (root cause) | **An algebraic fixed point.** `_leg_marks` inverted implied vol from the daily bar *at the current spot*, then re-priced at *the same spot* with that vol. That is an identity: the mark never leaves the daily close and delta ≈ 0 structurally. Mark move ÷ Black-Scholes delta move, median **0.000**. |
| 30 Aug (fixed) | Same ratio on the fixed path: median **0.945**, mean 0.950. The residual 5% is theta plus smile. `mark_interval_minutes: 1` is on, so a 29-minute hold is observed on minutes rather than on the 15-minute scan grid. |

**The Feb–Aug numbers in §"The measured verdict" are post-fix.** Anything from a
run stamped before 30 Aug is not.

**Do not touch mark cadence.** `mark_interval_minutes: 1` is correct, and a
run containing an intraday book that reports zero `fine_marks` or zero
`intraday_model_marks` has regressed.

Stated limitation: vol is frozen within the session, so an intraday mark
responds to spot and to time, never to a vol repricing. That is right for a
delta-driven book and wrong for one whose thesis is intraday vol. This book is
the former.

## Failure modes — live, 31 Aug

54 gate evaluations across 14 symbols in four intraday cycles on the judged
account, with real measured inputs (VWAP, ATR, RSI, volume z-scores, IV–RV
spreads). The candidate search runs to completion every cycle.

| gate | count | read |
|---|---|---|
| `structure` | **12** | The empty shelf. Not liquidity. Blocks the book outright. |
| `selection` (IV-rank ≥ 0.85) | 10 | Suspect — the rank is degenerate on a one-expiry chain. Re-measure after the fix. |
| `momentum` | 7 | The trigger. Working as designed. |
| `confirmation` | 3 | Down from being the binding constraint on nearly half of candidates before the term-structure vote. |

The replay rejection mix is different, and both are worth showing: there, 64%
of rejections were "no VWAP cross" and 21% the volume z-score before the band
and floor were swept.

Read the gate funnel before the P&L — `oaa backtest --why 15` prints it by
reason, not by gate name. If `data`, `structure` or `catalyst` dominate, that is
plumbing or thresholds, not the strategy.

## Cost model — what is measured and what is assumed

**Alpaca serves no historical option quotes.** There is no historical bid/ask
endpoint; option bars are OHLCV. So the spread in every backtest is **modelled
by construction** and cannot be made real retroactively. The tier constants that
set it are Python literals in `backtest/chain.py`, not config, and were never
calibrated.

On the free tier, live option quotes are the **Indicative** feed, not OPRA, so
the widths measured 28 Aug at the strikes this book trades —

```
SPY 2.84%   DIA 2.84%   QQQ 3.22%   TLT 3.52%
IWM 4.13%   GLD 5.32%   XLE 9.80%   XLF 11.85%
```

— may overstate or understate the tradable market. `max_relative_spread` was
raised 0.02 → 0.04 on 28 Aug because **0.02 was below the tightest quote in the
universe**: the gate was rejecting the market, not selecting within it. The
binding test is now `spread_cost_fraction_of_target: 0.30`, which is the
economically meaningful one — at a 10% target, a 3% round trip is 30% of the
target, right at the ceiling.

Costs are **not** double counted: the spread is charged inside the fills on both
sides, and `net_pnl = gross − fees − interest`. `total_modelled_cost` is a memo
line, already inside gross, and printing it beneath net P&L makes it read as a
third subtraction — a labelling defect, not an accounting one.

## Honest assessment for the deck — 31 Aug

1. **The book is measured negative and the measurement is the deliverable.**
   n=243, −$5,018, mean −$20.7, t = −2.09, on real bars over 6.5 months with the
   marking defect fixed. Say it before a judge finds it.
2. **The exits are not the problem.** Two exit changes were tested properly. The
   VWAP re-cross rule was removed on a mechanism (0 wins in 55) whose P&L delta
   was t = 0.21. The time stop was lengthened and **refuted** — the excursion
   distribution grows symmetrically, ×1.32 up against ×1.49 down. Every dial
   that could be moved has been moved and measured.
3. **The momentum layer is conventional.** Any edge in a bare VWAP cross was
   arbitraged long ago. The originality is the catalyst gate, the confirmation
   score, the term-structure vote and the surface-aware selection — and none of
   them turned the sign.
4. **The book could not trade live today**, for a reason that turned out to be a
   27-line fix rather than a strategy failure. The distinction between an empty
   shelf and a strict filter is the interesting part. The fix is committed; the
   process still needs a restart.
5. **Sizing has a known open defect.** 6% of `single_long` trades were 1.5×+
   oversized and both the largest win and the largest loss are in that tail. The
   measured expectancy is contaminated in both directions by it.
6. **Paper fills flatter this severely.** Fills at mid, no queue, no partials —
   and spread cost is the primary loss mechanism, which is exactly what paper
   does not simulate. Modelled cost is reported alongside raw P&L.
7. **Five sessions is not a sample.** Expect 10–20 trades. The standard error on
   a 40-trade week is ±$987, so nothing inside ±$1,000 during judging means
   anything, in either direction.
8. **Replay and live differ in management resolution on purpose.** Replay marks
   every minute; live manages on the 15-minute cycle grid, because raising the
   live cadence must happen in the same change that makes the live exit
   structure-aware. Say so rather than letting a judge find it.

## Open items — 31 Aug

**Before the entry cutoff (`2026-09-02T20:00:00Z`), in order:**

1. **Restart the live process.** The chain-window fix is on `main` (`375e59a`)
   but the running agent holds its modules in memory, so until it restarts the
   book still cannot trade at all.
2. **Decide whether this book runs at all on the judged account.** The weekly
   table argues for dropping it; §"Dropping this book" states both sides. This
   is a decision, not a measurement, and it has not been made.
3. **Re-derive the intraday IV rank** once the chain has more than one expiry,
   and only then revisit the 85% ceiling.

**Known open defects, not fixed:**

- `single_long` `max_loss` goes stale between build and fill — sizing *and*
  every intraday exit percentage.
- `exits.target_pct_of_premium` / `stop_pct_of_premium` are computed against
  `max_loss`, not premium. Rename them or change the arithmetic.
- The `term_structure` band was never swept. It is a prior. Sweeping it now
  would be fitting on a book already known to be negative.
- The replay breadth proxy is a handful of symbols against a live movers list of
  hundreds.
- The universe comment in the strategy YAML says "ten" against a list of eight.
- Tier spread constants are Python literals, unreachable from YAML, and
  uncalibrated. Sweeping `backtest.slippage_spread_fraction` at 0 / 0.25 / 0.5 /
  1.0 would turn "is the pricer why I am losing" into a measured curve.

**Do not do these:**

- Do not loosen the chain liquidity filter. It is not the constraint.
- Do not lower `options.min_days_to_expiry` globally. Carry's 3 DTE floor is a
  gamma control.
- Do not sweep the time stop, the target or the stop further. Measured,
  symmetric, refuted.
- Do not tune entry gates against the pre-30-Aug runs. Their marks were frozen.
- Do not judge any of this on a 5-day window.

## Repo/document discrepancies as of 31 Aug — resolve before presenting

Recorded here because they change numbers a judge might be shown.

1. **The credit/width gate is not in the repo.** The 30 Aug A/B ladder above is
   run with `risk.max_credit_to_width: 0.45`, and every "after the gate" figure
   quoted for the *carry* book depends on it. `grep -rn credit_to_width src/`
   finds no such risk check on `main` or on the working branch; the patch sits
   unapplied at `oaa_gate.patch` in the repo root. **The rungs of the ladder are
   not what the repo would run today.**
2. **`exit_on_vwap_recross` is still `true`.** The A/B doc records it as set to
   `false`; the YAML on both branches has `true`. The rule with 0 wins in 55
   attempts is currently live.
3. **The sizing comment in the strategy YAML says the judged profile risks 1.5%
   per trade and allows 20 new positions per day.** `config/judged.yaml` has
   `max_risk_per_trade_pct: 0.01` and `max_new_positions_per_day: 20`. The
   position-count claim is right; the 1.5% is not.

## Note on the defined-risk fix (27 Aug)

The pricing fault described in `docs/STRATEGY-CARRY.md` — legs of one structure
priced on different surfaces — applies to this book's **debit verticals** in the
opposite direction: a long leg marked from a real print against a short leg
marked from the model can *overstate* the position. Long single options are
immune (one leg, one surface). The engine now prices every leg of a structure on
one surface and clamps the mark to the structure's own arithmetic; both counters
are reported per run.

`exits.max_loss_usd` is a `vol_carry` parameter. This book's max loss is the
premium paid — subject to the denominator defect above, which is exactly the
argument for adding one here rather than relying on the risk engine's view of
`max_loss`.
