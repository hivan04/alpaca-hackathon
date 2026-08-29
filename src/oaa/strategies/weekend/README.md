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

## What the evidence says (58 weekends, measured 29 Aug)

`oaa weekend edge --days 400` measures forward returns after every in-window
reading — no entry rule, no stop, no sizing. Overlapping bars inside one
dislocation are collapsed into independent episodes, and the t-statistic is
computed on those alone.

| z bucket | regime | 8h forward | hit | net of 54bp | episodes / weekends |
|---|---|---|---|---|---|
| z ≤ −2.5 | **ranging** | **+79bp** | 88% | **+25bp** | 11 / 11 |
| z ≤ −2.5 | trending | +15bp | 53% | −39bp | 22 / 20 |
| −2.5 < z ≤ −2 | ranging | +52bp | 89% | −2bp | 15 / 15 |
| −2.5 < z ≤ −2 | trending | +2bp | 54% | −52bp | 34 / 28 |

Unconditional forward return over the same bars: **+1bp**. So the z-score
carries information, and it carries it *only in the ranging regime* — the ADX
gate is load-bearing, not decoration.

The end-to-end replay says the same thing as the gate is relaxed:

| ADX gate | trades | hit rate | net P&L | modelled costs |
|---|---|---|---|---|
| ADX < 25 (shipped) | 6 | 83% | **+$283** | $232 |
| ADX < 25, looser bands | 10 | 70% | +$228 | $369 |
| ADX < 30 | 18 | 61% | +$62 | $694 |
| no ADX gate | 35 | 51% | **−$157** | $1,390 |

Hit rate falls monotonically with turnover and the P&L crosses zero. At 35
trades the costs ($1,390) exceed the gross ($1,233): **this strategy is
turnover-bounded, not signal-bounded.** That single table is the argument for
every gate in the stack.

## What this book actually is

Six trades in 58 weekends. +$283 on $100k — **+28bp over thirteen months**, at
0.1 trades per weekend. Uncorrelated with the options books and effectively
free to run, but the expected contribution to any single judged week is
approximately zero, and it should be presented that way rather than as a P&L
engine.

The honest caveats, stated once: the parameters were chosen on the same 58
weekends they are quoted against; six trades is not a distribution; and the
best traded bucket carries t = +1.7 on 11 episodes, which is a direction, not a
proof. The cost model is also the selectivity dial rather than only a
deduction — assuming *higher* costs makes the edge gate stricter and, in this
sample, raises net P&L. Any P&L number here should be read as "the machinery
works and the gates are doing something real", not as an expected return.

## What would still falsify it

1. **Weekend spreads are wider than 4bp.** The whole cost line moves and the
   +25bp net becomes negative. `oaa weekend scan` prints the live half-spread
   against the assumption — measure it before trusting the default.
2. **The ranging edge does not survive another year.** 11 episodes is thin.
3. **The 8-hour horizon is a fit.** The reversion builds monotonically (+11bp
   at 1h → +79bp at 8h), which is reassuring, but 8h is also the longest
   horizon the weekend window allows — the study cannot see past its own edge.
