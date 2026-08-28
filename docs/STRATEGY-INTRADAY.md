# Intraday book — intraday_momentum

**Status:** `enabled: true` since 27 Aug — **this is the primary book.**
Branch `strategy-v2`. Rewired 27 Aug (afternoon) after the gate-funnel diagnosis;
the full change record is in the project doc `claude/strategy-changes-27aug.md`.

## Why it was promoted

The carry book measured **0.30 trades per session** over a 61-session run with
an average hold of **5.78 days**. Over the 5–6 session judged window that is
1.5–1.8 trades, still open at the deadline — a P&L score that is an unrealised
mark on two positions. The carry book's holding period *is* the competition.

This book is flat by 15:15 every session, so everything it does realises inside
the window. `vol_carry` stays on as the resident book; the capital firewall
keeps them apart.

**The original reason for keeping it off still stands and is now live risk:**
judges read the full account history, and a malfunctioning intraday loop firing
junk orders is permanently on the record. Run it in `dev` with `dry_run: true`
until the gate funnel looks sane.

## Thesis, stated honestly

A **momentum strategy expressed through options**. The option is leverage and
defined risk; it is not the source of edge. Presenting it as a vol strategy
invites "which vol premium are you harvesting?", and there isn't one.

|  | Carry book | Intraday book |
|---|---|---|
| Edge | IV–RV premium | directional continuation |
| Option role | the position | leverage + loss bound |
| Hold | 3–10 sessions | minutes to hours, flat by 15:15 |
| Primary risk | short gamma | **spread cost** and signal decay |

Uncorrelated in signal, opposite in vol exposure — the portfolio argument for
running both, and the reason a dry week for one is not a dry week for the book.

## The hard constraint

Target profit is 5–15% of premium ($10–30 on a $2.00 contract). A $0.10-wide
single-name quote costs $20 round trip — the entire target. **Index products
only.** Two symbols, because those are the only two where the arithmetic works.

## Signal stack (`src/oaa/strategies/intraday_momentum.py`)

Three indicators, three *different* questions — only VWAP has an opinion on
direction, so the other two cannot contradict it.

- **VWAP — trigger.** Session VWAP cross (or a bounce inside 0.25×ATR), plus a
  volume z-score computed **within the same time-of-day bucket**, plus 2-bar
  persistence to reject single-bar spikes.
- **Bollinger *width* — filter.** Width, not position: a volatility-regime
  measurement, direction-agnostic. Must be rising over 6 bars.
- **RSI — veto only.** One-sided, 80/20 not 70/30.

**Catalyst confirmation** is the non-generic layer: news (0.50), breadth (0.35),
volume participation (0.15), rank-normalised *within* each source. No catalyst →
veto: a VWAP cross with no mechanism is drift, and drift reverts. Deterministic
by default — an LLM call inside a loop whose signal decays in minutes is a
latency risk.

**Spread gate — mandatory.** `(ask−bid)/mid ≤ 2%` and round-trip ≤ 30% of
target. Five times tighter than the carry book's 10%. Expect it to reject more
than any other gate.

## Timing — REBUILT 27 Aug

This is where the book was broken, and the fix is the most consequential change
of the day.

### It could only ever trade for 75 minutes a day

`momentum.min_bars: 30` on `5Min` bars means the book refuses any session with
fewer than 150 minutes elapsed — nothing before 12:00 ET. Measured against the
four configured scan times:

| scan time | 5Min bars available | passed `min_bars: 30`? |
|---|---|---|
| 09:45 | 4 | no |
| 10:30 | 13 | no |
| 13:45 | 52 | yes |
| 14:30 | 61 | yes |

**Two of the four scan times could never produce a trade.** 152 of 304 replayed
candidates died at the `data` gate. Then `skip_lunch` blocks 11:30–13:30 and
`no_entry_after` blocks past 14:45, leaving a real tradable window of
**13:30–14:45**. The book advertised as entering from 09:45 could not.

### It was an event-driven signal on a four-times-a-day poll

`cross_lookback_bars: 3` on 5-minute bars asks "did price cross VWAP in the last
15 minutes", checked four times a day — roughly 60 of 390 session minutes
observed, with the cross required to land inside one of them. **86% of
candidates died at "no VWAP cross"**, at every configuration tested. That is an
architecture mismatch, not a weak signal.

### What changed

| setting | was | now |
|---|---|---|
| `data.intraday_timeframe` | `5Min` | **`1Min`** — `min_bars` clears by 10:00 |
| `backtest.session_times_et` | 4 times | **12**, every 15 min 10:00–14:45 |
| `momentum.cross_lookback_bars` | 3 | **15** — 15 one-minute bars is exactly one polling gap, so no cross falls between two cycles |

Do not raise `cross_lookback_bars` much beyond the polling interval: past it, the
book starts re-firing on a cross an earlier cycle already acted on.

**Time gate unchanged:** 09:45–14:45 with lunch 11:30–13:30 skipped. It is now
the binding constraint rather than a formality, because `min_bars` no longer is.

## What the backtest needed before this book could run at all

It could not have traded in replay under any settings. Six things were missing
and each produced a silent zero. The first four were fixed in the morning:

1. **Intraday bars were never fetched.** `HistoricalContextSource` accepted
   `intraday_by_symbol` and nothing populated it, so the book vetoed on `data`
   every time. Now fetched, and only when an enabled strategy reads them.
2. **No catalyst engine.** `ctx.catalyst` was unset, so the gate returned
   "no catalyst engine wired into this cycle". Now built from the strategy's own
   `catalyst_gate` params, reading real Alpaca headlines.
3. **No breadth snapshot.** `breadth_agrees` returns **False** when breadth is
   unknown, so the gate vetoed regardless. Now rebuilt each session from the
   replayed universe's own bars.
4. **0–2 DTE contracts were never listed.** `tradable_dte_range` read
   `structures.dte_*`; this book selects by `selection.dte_max`. Range is now
   0–16 DTE.

Two more were found in the afternoon, and both mattered more:

5. **Replay was a strictly harsher strategy than live.** The live provider
   fetches `data.intraday_lookback_days` (5 sessions) of intraday bars; replay
   built each context from the **current day only**. So `min_bars: 30` was
   satisfied instantly live but not until 12:00 ET in replay, and
   `volume_zscore_by_bucket` — which needs at least three same-bucket samples
   from *prior* days — could never be evaluated at all. With
   `require_volume: true` that is a hard veto on a gate that structurally cannot
   pass: 255 of 912 candidates. Replay now carries the same multi-session window
   live does. `vwap_series` restarts on each day boundary, so a multi-day window
   still yields a *session* VWAP.
6. **The synthetic source had no intraday bars whatsoever.** Every offline test
   of this book was reporting an empty fixture as "the strategy doesn't trade".
   `synthetic_intraday_bars()` now expands each daily bar into a Brownian bridge
   rescaled onto that bar's high/low, with a U-shaped volume profile so the
   time-of-day volume gate is at least exercisable. Still a fixture — nothing
   measured on it is a result.

A side effect of (5): the breadth snapshot was reading `intraday_bars[0]["open"]`
as the session open, which with a multi-day window is five days ago. Now sliced
to the latest day.

## Option selection (`src/oaa/options/selection.py`)

Conditioned on expected move (ATR scaled by remaining session), IV level and IV
rank:

| Condition | Selection |
|---|---|
| small move, cheap IV | ATM 0–1 DTE — max gamma |
| small move, rich IV | ATM longer dated — less burn if it stalls |
| large move, cheap IV | slightly OTM — convexity per dollar |
| large move, rich IV | **debit vertical** — caps the expensive wing |
| IV rank ≥ 0.85 | **no trade** — already priced in |

The last row is worth a slide: declining when the option is expensive relative
to the move expected is vol-aware, not naive.

## Sizing and exits

Long options or debit verticals only — max loss is the premium paid, always.
Exits: 10% target / 15% stop / 20-minute time stop / VWAP re-cross / 15:15
firewall cutoff. **The stop is wider than the target on purpose** — premium is
noisy and a tight stop is hit by spread flicker alone. Breakeven hit rate ≈ 60%,
computed and displayed against the actual.

Portfolio caps were raised 27 Aug (self-imposed, not hackathon rules):
`max_positions` 12 → 25, `max_new_positions_per_day` 4 → 12 on judged.

**Two new entry checks guard the raised cycle count** — see
`docs/STRATEGY-CARRY.md` for the mechanism, which applies to both books.
`duplicate_structure` refuses a structure already held leg-for-leg on the same
side; `reentry_cooldown` (60 min, per symbol *and* strategy) refuses a re-entry
inside the window. Without these, twelve cycles a day meant position size scaled
with polling frequency rather than with opportunity.

## Failure modes — now measured, not guessed

Ranked by observed share of rejections in the 15-minute-cycle replay:

| gate | share | read |
|---|---|---|
| `momentum` — "no VWAP cross" | 64% | Still the binding constraint after widening the lookback. On a Brownian-bridge fixture this is expected and uninformative — the fixture has no momentum persistence. **Needs re-measuring on real bars.** |
| `momentum` — volume z-score below floor | 21% | Now a *real* z-score rather than "no baseline yet". The gate works; the floor of 1.0 may still be high. |
| `time_of_day` | 8% | Past 14:45, insufficient runway before the 15:15 cutoff. Working as designed. |
| `catalyst` | 4% | `lookback_minutes: 30` with `min_headlines: 1`. With twelve scans instead of four the odds of a headline in the preceding 30 minutes are better than they were, but this is still a lever: widen the lookback, or `required: false` to make it advisory. |

Still unmeasured because nothing reached them: `spread_gate`, and
`breadth_min: 0.60` against a coarse breadth proxy.

Read the gate funnel before the P&L — `oaa backtest --why 15` prints it by
reason, not just by gate name. If `data` or `catalyst` dominate, that is
plumbing or thresholds, not the strategy.

## Honest assessment for the deck

1. The momentum layer is conventional. Any edge in a bare VWAP cross was
   arbitraged long ago. The originality is the catalyst gate and the
   surface-aware selection.
2. **Paper fills flatter this severely.** Fills at mid, no queue, no partials —
   and spread cost is the primary loss mechanism, which is exactly what paper
   does not simulate. Modelled cost is reported alongside raw P&L.
3. **Still never validated on real bars.** The book now runs its full gate stack
   in replay, which it never did before — but on synthetic data it still produced
   zero trades, and that result says nothing either way. The first real-Alpaca
   run with `intraday_momentum` enabled remains the first genuine test.
   Deliberately **not** tuned further against the fixture.
4. Five sessions is not a sample. Expect 10–20 trades, which cannot distinguish
   edge from luck in either direction.

## Open items

- **Run it on real bars.** Everything above is either arithmetic or synthetic.
- Measure live SPY/QQQ 0–2 DTE quote widths; the 0.02 ceiling is a placeholder
  and the whole strategy depends on it
- 0DTE availability on the Alpaca chain
- Signal-to-fill latency on the CLI write path
- The replay breadth proxy is a handful of symbols against a live movers list of
  hundreds
- The live runner still fires the book on the **schedule in `config/default.yaml`**
  (09:45 / 13:45), not on the 12-point backtest grid. `schedule.cycles` needs the
  same treatment before the two paths agree — see Open items in
  `docs/DECISIONS.md`.

## Note on the defined-risk fix (27 Aug, late)

The pricing fault described in `docs/STRATEGY-CARRY.md` — legs of one
structure priced on different surfaces — applies to this book's **debit
verticals** too, in the opposite direction: a long leg marked from a real print
against a short leg marked from the model can *overstate* the position. Long
single options are immune (one leg, one surface). The engine now prices every
leg of a structure on one surface and clamps the mark to the structure's own
arithmetic; both counters are reported per run.

`exits.max_loss_usd` is a `vol_carry` parameter. This book's max loss is the
premium paid, which is already small and already hard-bounded, so it does not
need one — but if position sizing ever rises, add it here too rather than
relying on `sizing.risk_fraction_per_trade` alone.
