# Overnight Anomaly Pairs

**Book:** overnight · **Entry:** 15:55 ET · **Exit:** 09:35 ET · **Holding period:** one night

## Thesis

A structural premium accrues between the US close and the next open. It is not
free money: the overnight session is where gap risk lives, which is exactly why
the premium exists and why most retail books cannot hold it.

This strategy harvests it while staying market-neutral, and caps the gap risk
contractually rather than hoping.

## Architecture — three timeframes, run in sequence

### 1. Universe — daily bars, 1–2 years, **offline**

Engle-Granger cointegration (`statsmodels.tsa.stattools.coint`) plus practical
filters: mean-reversion half-life inside a tradable range, a correlation floor,
a positive hedge ratio, and both legs liquid enough to short *and* to hedge with
options. Output goes to `config/pairs.yaml`.

```bash
python scripts/find_pairs.py --write
oaa pairs                       # inspect the approved universe
```

This is deliberately **not** in the live loop. Re-screening cointegration at
15:45 is how you conjure a pair into existence in the evening and lose money on
it at the open.

### 2. State — Kalman filter, live

A two-state random walk on `[alpha, beta]`, observed through the price
relationship. A static OLS hedge ratio is stale the moment the relationship
drifts, and an overnight pair lives or dies on the hedge being right at 15:55.

The filter is seeded from a regression of **differences**, not levels: over a
short window a random-walk price barely moves, so a level regression is
near-collinear with its own intercept and hands beta a value it then spends
hundreds of observations walking back out. (`tests/test_quant.py` asserts
recovery of a known beta to within 0.15, and that the filter tracks a beta that
drifts from 1.0 to 2.0.)

The standardised residual is the **z-score** — the number the strategy trades on.

### 3. Forecast — two-stage ensemble, 15:45

**Stage 1: Huber regression.** Overnight gaps have fat tails and occasional
enormous outliers (a gap-down on news is a different process from a normal
night). OLS chases those outliers and produces a baseline that is wrong on the
95% of nights that matter. Huber's loss is quadratic near zero and linear in the
tails, so the fit describes the typical night and lets outliers be outliers.

**Stage 2: quantile LightGBM on the residuals.** The linear baseline cannot
express "the tail is wide tonight because realised vol has doubled". A quantile
model on stage 1's residuals can — and each quantile does a different job:

| Quantile | Job |
|---|---|
| **q50** | the directional edge, and therefore the position size |
| **q05** | the bad night — sets the **protective put strike** |
| **q95** | the good night — sets the **protective call strike** |

That is the point of the ensemble: it does not predict a number, it predicts the
*shape of the distribution*, and the option strikes are read straight off the
tails rather than picked by a human.

Falls back to sklearn's quantile booster if LightGBM is absent, and to empirical
quantiles if there is not enough history to fit anything. Day one still trades,
sized small.

## The options overlay — load-bearing, not decoration

A long/short equity pair has **unbounded** risk on the short leg. A takeover bid
overnight is unrecoverable. So the default overlay is a **collar on the pair**:

```
long put  on the long leg   -> floor  = strike, loss capped at (spot - K) + premium
long call on the short leg  -> ceiling = strike, loss capped at (K - spot) + premium
```

Both legs bounded means the whole position has a **contractual maximum loss**,
which is what lets the deterministic risk engine approve it at all.

Strikes are placed at the worse of (that leg's own empirical 5th/95th percentile
gap) and (the pair's modelled q05/q95) — because this trade loses in two
distinct ways, and the hedge should be priced for a bad night in *either* the leg
or the relationship. Distance is bounded at both ends: never more than 10% away
(a lottery ticket, not a floor) and never closer than 1.5% (an ATM overnight
hedge costs about as much as the move it insures against).

`overlay.mode: put_only` exists for completeness. It leaves the short leg naked,
so `risk.allow_undefined_risk` must be true for it to pass — and it is off by
default for a reason.

## Sizing — and why round lots fight neutrality

Share counts are forced to **multiples of 100** so every share is coverable by
whole option contracts. A partially covered short leg is not a defined-risk
position, it is an unhedged one with paperwork.

That constraint collides with dollar-neutrality harder than it looks. Take SNDK
at 177.93 against MU at 106.79 — a price ratio of 1.666. Sizing the long leg to
the budget and then rounding the short lands on 200/300 shares:

```
long   200 sh x 177.93 = $35,586
short  300 sh x 106.79 = $32,037
net exposure           = $ 3,550     -> 10% residual DIRECTIONAL exposure
```

Ten percent net long inside a position whose entire premise is neutrality. The
hedge was never going to cover that.

So the sizer **searches the lot grid** rather than taking the first fit, testing
both neighbouring short-lot counts at every long size and picking the smallest
residual that still fits the budget:

```
 long lots  short lots      long $     short $     gross $   hedge err
         1           2      17,793      21,358      39,151      20.0%
         2           3      35,586      32,037      67,623      10.0%   <- naive pick
         3           5      53,379      53,395     106,774       0.0%   <- searched
```

The (3,5) combination is essentially exact but needs $107k gross. If no
combination gets inside `risk.max_hedge_error_pct` (default 5%), **the pair is
refused**. A pair that cannot be built neutrally at your account size is not a
pairs trade, it is a directional bet with a hedge attached, and the honest
response is to decline it rather than dress it up.

Two practical consequences:

- **Lower-priced legs with closer price ratios hedge far more cleanly.** This is
  another argument for ETF pairs over high-priced single names — XLE/XOP will
  find a neutral lot combination at a fraction of the notional SNDK/MU needs.
- **Account size is a real constraint on which pairs are tradable at all.** At
  $100k with 50% max overnight exposure, awkward-ratio pairs are simply out of
  reach. That is information, not a bug.

The budget itself comes from the firewall's 15:54 verification, never a cached
number, and the achieved neutrality is reported in `meta.hedge_error_pct`.

## Execution — four orders, in this order

Alpaca's MLEG order class does not accept equity legs, so the structure is four
separate orders. **The ordering is the safety property:**

```
1. buy the protective put   on the long leg
2. buy the protective call  on the short leg
3. buy the long equity leg
4. short the hedge equity leg
```

The instinct is to establish the position first and hedge after. That is
backwards. If the equity legs fill and the options then fail, the account holds
an unhedged short overnight. If the options fill and the equities fail, it holds
two cheap long options whose maximum loss is the premium already paid.

Anything that filled before a failure is **unwound in reverse, at market** —
certainty of exit beats price. `tests/test_combo.py` covers each partial-failure
path, including the case where the unwind itself fails (which halts trading and
demands a human).

## Backtesting

```bash
oaa backtest-overnight KO/PEP --start 2025-01-01 --out runs/backtests/kopep.csv
oaa backtest-overnight                      # every enabled pair
```

Bars come through the Alpaca CLI — the same binary that executes the orders.

**What is exact:** the equity legs, the hedge ratio, the z-score, and the gap
that followed. Daily bars contain both ends of a close-to-open hold, so the P&L
is reconstructed rather than approximated.

**What is modelled:** the options overlay, because no historical option chain
with greeks exists on the free tier. Every assumption in `backtest/pricing.py`
is deliberately pessimistic — implied vol marked up over realised, half the
spread crossed on entry, and the exit valued at intrinsic only, surrendering
whatever time value remains. Errors point against the strategy.

**Walk-forward discipline:** on each simulated evening the model is fitted only
on nights that had already happened, and the entry gates are the same objects
the live path uses. A backtest that trades on looser rules than production is
measuring a different strategy.

## Timeline

| ET | What happens |
|---|---|
| 15:15 | intraday book liquidated and **confirmed** flat |
| 15:45 | `overnight_signal` — Kalman + model compute, nothing routed |
| 15:54 | firewall gate — prove flat, read fresh Reg T, take the lock |
| 15:55 | `overnight_entry` — the four-order combo goes out |
| 16:00–09:30 | hold |
| 09:35 | `overnight_exit` — market orders, book flat, lock released |

## Inspecting it live

```bash
oaa pairs                    # approved universe and screen stats
oaa signal KO/PEP            # tonight's Kalman state and gap forecast
oaa firewall --at 15:54      # what the gate would say
oaa agent overnight_signal   # let the assistant reason over the whole universe
```
