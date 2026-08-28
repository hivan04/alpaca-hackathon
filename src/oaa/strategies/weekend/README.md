# The weekend book — BTC/USD mean reversion

A fourth book, in its own package, live only while the US equity market is
shut. It exists for one reason: the judged P&L window is a calendar week, and
three of its days are ones the options books cannot trade at all.

```
Fri 16:00 ET ─────────────────────── weekend book ─────────────────── Sun 16:00 ET
             carry book held, transient books flat, crypto book live
```

## The thesis, in one paragraph

Between the Friday equity close and Sunday evening there is no equity market,
no macro print and no institutional desk. What remains in BTC is liquidation
and retail impulse — price-insensitive flow that pushes price away from its
24-hour mean *without carrying information*. Displacement without information
reverts. Displacement **with** information (a trend, a genuine repricing) does
not, and buying it is how mean-reversion books die. ADX is the instrument that
separates the two, and it is checked before the z-score is even consulted.

## The signal stack

Gates run cheapest-first and the first veto ends the evaluation, so every
rejection names exactly one number.

| # | gate | question | veto when |
|---|---|---|---|
| 1 | `data` | enough bars for a 24h mean and a Wilder ADX? | < 120 bars |
| 2 | `regime` | is the tape ranging? | ADX ≥ 25, or ADX rising > 4 over 8 bars |
| 3 | `band` | is the band a real band? | σ < 0.35% or σ > 3.0% |
| 4 | `shock` | are we mid-cascade? | last 15m bar ≤ −1.5% |
| 5 | `displaced` | is there a signal? | z > −2.0 |
| 6 | `edge` | is it worth the friction? | expected move < 2.5 × round trip |

Gate 2 before gate 5 is the design decision. A dip-buyer asks "how far has it
fallen"; this asks "is falling what this tape does right now" first, and only
then how far.

## Why gate 6 is the one that matters

Crypto fees are a **percentage of notional**, not a per-contract charge — the
opposite of the options books, where the spread dominates and the fee is
noise. Alpaca retail crypto is 25bp taker / 15bp maker per side. With a
measured weekend half-spread and slippage the model is **~54bp per round
trip**.

    a 2σ reversion on a 40bp band  =  70bp gross  −  54bp costs  =  16bp
    the same reversion on a 62bp band = 134bp gross − 54bp costs =  80bp

The first is a rounding error with variance attached. `min_edge_multiple: 2.5`
refuses it. Expect this gate to reject more candidates than every other gate
combined — that is the finding, not a bug to tune away.

## Long only, and why the deck should say so

Alpaca does not permit short crypto. The rich side of the band is therefore an
**exit**, never an entry, and the strategy is asymmetric by construction. The
rejection log names the constraint when it refuses a +2σ read, so the asymmetry
is visible in the artefact rather than hidden in the returns.

## Defined risk on an instrument with no strikes

`risk.allow_undefined_risk: false` is a hard rule across this repo, and spot
crypto has no strike to bound a loss. The bound comes from the stop instead:

    max_loss = (entry − stop) × qty,  stop = entry − 1.5 × ATR(14),
               clamped to [0.8%, 3.5%]

That number is computed **before** the order is sent and carried on the
`TradeIdea`, so the same risk arithmetic the options books use applies
unchanged. Sizing takes the smaller of two independent caps: 2% of equity at
risk to the stop, and 10% of equity in gross notional.

Stops are enforced by the engine's poll, not left resting at the broker: a
resting stop on a 24/7 venue gets swept by a two-second wick that trades no
size. 60-second polling costs a little slippage and avoids that.

## The clock is a safety device

| | UTC | why |
|---|---|---|
| opens | Fri 20:05 | after the 15:45 ET carry sign-off releases the transient lease |
| last entry | Sun 12:00 | 8 hours of runway — an entry with no room to work is a coin flip |
| flat | Sun 20:00 | 17.5h before Monday's open, a full night before any gap |

`test_window_never_overlaps_an_equity_session` walks a fortnight in 15-minute
steps and asserts the book is never open inside 09:30–16:00 ET. Because the
book cannot *hold* while an equity session is live, it never competes with the
carry book for Reg T buying power — it needs no lease from the capital
firewall at all, which is why it could be added without touching it.

## Layout

```
weekend/
  clock.py      the window, derived from the equity session so DST cannot drift it
  signals.py    the six gates, z-score on log price, Wilder ADX/ATR
  costs.py      the fee model that gate 6 is built on
  params.py     schema for config/strategies/weekend_crypto.yaml
  strategy.py   signal -> defined-risk TradeIdea (registered, never auto-enabled)
  engine.py     the live loop, with restart-safe state in runs/weekend_state.json
  backtest.py   the replay: next-bar fills, stop-before-target, costs on every fill
  data.py       Alpaca's public crypto bars (no key, no entitlement, no delay)
```

## Commands

```
oaa weekend status                  where the clock is, what is open
oaa weekend scan                    one evaluation, gate by gate, no orders
oaa weekend backtest --days 400     the replay, costs included
oaa weekend run --once              one live cycle (dry run unless --live)
oaa weekend run --live              the loop; needs enabled: true in the YAML too
oaa weekend flatten --live          close every crypto position now
```

History for the replay comes from `scripts/fetch_weekend_bars.py`, which caches
bars to `data/cache/weekend/` so a backtest is reproducible offline.

## What would falsify this

Honest failure modes, in the order they are likely:

1. **The edge gate never fires.** If 400 days of history produce three trades,
   the band is too tight to pay 54bp and the book should not run.
2. **ADX below 25 does not mean reverting.** The regime gate is a hypothesis.
   The backtest reports hit rate conditioned on it; if it is 50%, the gate is
   decoration.
3. **Weekend spreads are wider than 4bp.** Then the cost model is optimistic
   and every number above shifts against the book. `oaa weekend scan` prints
   the live spread — measure it before trusting the default.
