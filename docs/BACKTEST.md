# Backtesting

`oaa backtest` and the Backtesting tab of `oaa dashboard` are the same run: the
CLI and the button call one entry point (`oaa.backtest.runner.run_backtest`), so
a number in the deck and a number in a terminal cannot disagree.

```bash
make dashboard                      # the operator dashboard, both tabs
oaa backtest                        # the configured universe and window
oaa backtest --symbols SPY,QQQ --start 2026-06-01 --end 2026-08-22
oaa backtest --source synthetic     # no keys, no network: a wiring test only
oaa backtest --critic llm           # run the real critic (cached; see below)
```

Every run is written to `runs/backtests/<timestamp>__<label>/` as
`result.json`, `manifest.json` and `equity.csv`, and the dashboard can reopen
any of them without re-running anything.

---

## What is real and what is modelled

This is the part that decides how the output may be used, so it goes first.

| Piece | Source |
|---|---|
| Underlying daily bars | **REAL** - `StockHistoricalDataClient.get_stock_bars` |
| Headlines and their timestamps | **REAL** - `NewsClient.get_news` |
| Strikes and expiries | **REAL** - `TradingClient.get_option_contracts`, expired contracts included |
| Option marks | **REAL** - `OptionHistoricalDataClient.get_option_bars`, where a print exists |
| Contract volume | **REAL** - from the same bar |
| Implied volatility and IV rank | **RECOVERED** - Black-Scholes inverted on the real traded price |
| Greeks | **DERIVED** - analytic, at the recovered vol |
| Open interest | **APPROXIMATE** - the contracts endpoint serves a current snapshot; Alpaca has no historical OI |
| **Bid-ask spread** | **MODELLED** - bars are OHLCV, not quotes. The biggest remaining assumption. |
| Marks on contract-days that never traded | **MODELLED** - counted and reported per run |
| Strategy, risk engine, critic, sizing, routing | **the live code**, not a re-implementation |
| Fees, exchange fees, margin interest | modelled from `COST_STRUCTURE.md` |

Alpaca serves historical option bars back to **February 2024**, and on the free
Basic plan the restriction is the most recent fifteen minutes, not history. So
option prices do not have to be invented. What genuinely is not served
historically is greeks and implied volatility - those are snapshot-only - and
implied vol is recovered by inverting Black-Scholes on the real traded price
instead (`pricing.implied_vol_from_price`). That matters more than it sounds:
IV rank is what the premium gate trades on, so recovering it turns the entry
signal from an assumption into a measurement.

**Coverage is not total**, and the harness says so rather than hiding it. An
option bar exists only on a day the contract actually traded, so wings on
single names are sparse. Those contract-days fall back to the modelled surface,
every mark records which it was, and the run's provenance and the dashboard
both report the ratio. A backtest that silently mixes measured and invented
prices is worse than one that only invents, because it looks trustworthy.

Set `backtest.chain.source: modelled` to switch the whole chain back to the
surface - useful offline, and for isolating what the pricing model contributes.

**A run is still not evidence of edge.** It is evidence that the logic fires
when intended, stays quiet otherwise, sizes correctly and survives its own risk
limits. The judged number is live paper P&L.

### Check the assumptions against your own account

The coverage numbers above depend on the plan and the symbols. Measure them
before quoting any figure:

```bash
python scripts/probe_option_data.py --symbols SPY,QQQ --days 60
```

It reports how many contracts were listed, how many ever printed, the fraction
of contract-days with a bar overall and near the money, and whether historical
option **quotes** are reachable. If they are, the bid-ask spread stops being
modelled - which is the single biggest improvement left in this harness, since
the spread is 50-100x the fee load.

---

## The implied-vol model - the FALLBACK path

With `chain.source: real` (the default) implied vol is recovered from real
traded prices and this model is used only for contract-days with no print, for
the shape of the spread, and for symbols whose option tape is too thin to rank
(`min_iv_observations`, default 20 sessions). It is still documented in full
because it is what runs whenever the real path cannot.

`vol_carry` trades on IV rank and the IV-RV spread, so how those are modelled
decides whether the backtest measures anything at all.

The naive model, `IV = k x RV`, is worthless: the IV-RV spread is then a
constant and the premium gate either always passes or never does.

What is modelled instead reproduces the two properties that generate the signal:

- **IV is sticky.** It is anchored to a slow EWMA of realised vol (45-day
  halflife by default), not to the trailing 20 days. When realised vol
  collapses, IV lags down and the IV-RV spread *widens* - the state the carry
  book wants to sell. When vol spikes, realised vol jumps past the anchor and
  the spread goes *negative*, standing the strategy down into the move.
- **IV is systematic.** The anchor is scaled by the market's own vol level
  relative to its trailing median, so a market-wide vol event lifts every name
  at once and the macro gate's "shared or idiosyncratic" question has something
  real to answer.

IV rank is the percentile of modelled IV within its own trailing year - the same
definition a live feed uses. Fewer than 20 observations produces `None`, which
the premium gate treats as a veto rather than as 0.5.

Knobs live under `backtest.iv_model` in `config/default.yaml`.

**Limitations.** No earnings crush and no event repricing that realised vol
never explains. The term structure is a fixed mild upward slope, so a genuinely
inverted curve never appears. The surface cannot be validated against historical
option prices, because obtaining those is the problem it exists to work around.

---

## The chain model

- **Surface.** ATM vol from the IV model, plus a standard equity put skew and a
  smile in standardised-moneyness units, plus a mild upward term structure. The
  skew makes the puts a short-premium book sells *richer*, which flatters the
  seller - it is the realistic direction, and the spread and liquidity models
  more than pay it back.
- **Spread.** A floor plus a fraction of mid, widened away from the money, per
  liquidity tier (`index_etf`, `mega_cap`, `single_name`, `illiquid`), rounded
  to the penny-pilot tick. Entry and exit both cross it.
- **Liquidity.** Open interest and volume decay with |moneyness| and with time
  to expiry, so `options.min_open_interest` and `options.min_volume` actually
  bind. A chain where everything is liquid lets the strategy trade contracts
  that do not exist.
- **Expiries.** Fridays only; weeklies restricted to the tiers that list them.

Knobs live under `backtest.chain`.

---

## The decision path is the live one

`agents/orchestrator.py` decides in this order, and the replay runs the same
order. A backtest that skips a stage is not a backtest of the live system.

```
modelled cost  ->  CRITIC  ->  risk engine  ->  partner veto  ->  execute
                   scores and             the ONLY thing        risk-stage
                   may decline            that can approve      adapters may
                                                                veto, never
                                                                approve
```

Skipping the critic over-reports trades: every candidate it would have passed
on gets taken instead. It also throws away the reasoning, which is the artefact
worth showing a judge.

### Critic modes - `--critic` / the sidebar / `backtest.critic.mode`

| mode | what it does |
|---|---|
| `heuristic` **(default)** | the real `Critic` class with a null LLM client - the documented degraded path the live system falls back to whenever the provider is unreachable. Deterministic, free, identical code. |
| `llm` | calls the actual model. Every verdict is cached on disk under `data/cache/backtest/critic/`, keyed by a hash of the prompt inputs, so a re-run costs nothing and returns identical verdicts. A hard call budget (`max_llm_calls`, default 250) caps the spend; past it the critic degrades to the heuristic and the run records how many times. |
| `off` | skip the critic. Useful once: diff a run against it to measure what it contributes. |

### Which model, and why the replay does not share the live one

`agents.llm` is the LIVE agent's provider. `backtest.critic.llm` overrides it
for replay only, and by default they are different:

| | provider | model | settings | why |
|---|---|---|---|---|
| live | `featherless` | `Qwen/Qwen3-32B` | temp 0.2, 4000 tokens, seed 11 | a handful of calls a day, on the account being judged; needs tool calling for the MCP cycles |
| backtest | `featherless` | `Qwen/Qwen3-32B` | temp 0.0, 1024 tokens, seed 7 | scores every candidate in every session, gets re-run whenever a parameter moves, and must return the same answer twice |

**These used to be two vendors.** Gemini sat in the backtest slot on a
cost-shape argument: the replay is the heavier caller, so point it at the cheap
model. Featherless dissolved that argument - open-weight inference, and the
Chat plan bills requests flat rather than per token - and a second vendor,
second key and second SDK to keep alive through a seven-day event is a real
cost of its own. Gemini was removed on 28 Aug. One key now: `FEATHERLESS_API_KEY`.

What survives is the *settings* split, which was always the part about
correctness rather than money:

- **temperature 0 and a fixed seed** (`backtest.critic.llm.seed`, default 7) -
  a backtest whose numbers move when you re-run it is not a backtest. The seed
  is the second line of defence; the on-disk verdict cache is the first.
- **1024 tokens** - the replay wants a verdict, not an essay, and the live
  budget of 4000 across thousands of candidates is pure waste.

`oaa doctor` still reports the two rows separately, so a misconfigured backtest
critic cannot be mistaken for a healthy live one. Set `backtest.critic.llm: null`
to make the replay share the live block wholesale.

Model IDs move. `--critic-model moonshotai/Kimi-K2-Instruct` overrides for one
run without touching config; the run artefact records which model produced which
verdicts, and the cache key includes the model so switching cannot silently
replay the previous one's answers.

**Why `llm` is not the default, and the caveat that must travel with any figure
from it:** the model is being asked about a period that may sit inside its
training data. Asked whether to sell QQQ volatility on 4 June 2026, it may not
be reasoning from the prompt alone. That is lookahead no engineering removes.
The dashboard shows a warning banner on any run that used it.

**Use `llm` mode to inspect the quality of the reasoning on a handful of
trades. Do not use it to produce a P&L number for the deck.**

### The critic sees outcomes, as it does live

The live critic is handed recent trade outcomes (`agents/memory.py`). The
replay builds the same store as it runs, from trades **already closed** at that
point in the window, so it can only ever contain the past. It is scoped to one
run and deleted afterwards, so it can never leak into the live agent's memory
or into the next run. `agents/memory.py` now stamps rows with the frozen replay
clock rather than wall time.

### Bug this surfaced

`AnthropicClient.complete` passed `temperature` to `messages.create`. That
parameter left the method in the 1.x SDK, and `pyproject.toml` pins
`anthropic>=0.34`, so a fresh install resolved to 1.x and **every critic call
raised `TypeError`**. `json_complete` swallows exceptions by design, so the
system silently ran on the heuristic fallback with nothing but a `WARNING` line
to show for it - the LLM layer looked wired up and was not. The client now
filters its kwargs against the SDK's real signature, which works on both major
versions. A test pins it.

---

## No lookahead

A context stamped 10:00 ET on 4 June contains:

- every **complete** daily bar up to and including 3 June
- 4 June's **open** as the spot (known at 09:30 - never its close)
- intraday bars for 4 June up to 10:00, if the intraday feed is on
- headlines published **before** 10:00 on 4 June, with their real timestamps

Indicators are computed once as a series over the full history and read at index
`i-1`, which makes the property structural rather than something five call sites
have to remember. `tests/test_backtest.py` asserts it directly.

The wall clock is frozen to the replayed session (`oaa.core.clock`), so
"is there an earnings date inside the expiry window" is asked about June rather
than about today. Without that, dated gates answer June's questions with the
calendar of whenever the backtest happened to be run.

---

## Costs

Charged in cash at the close of each round trip, from the rates in
`COST_STRUCTURE.md`:

- OCC clearing, ORF, FINRA CAT (both sides); FINRA TAF and the SEC fee (sells)
- index exchange fees where the symbol carries them
- margin interest at `cost_model.margin_rate_annual` on the structure's
  requirement for as long as it is held

The **spread** is charged inside the fills on both sides rather than added
beside them - charging it twice would be a different kind of dishonesty from
ignoring it, but still dishonesty. It is reported separately in the cost
attribution so the size of it is visible: it is 50-100x the fee load.

`backtest.slippage_spread_fraction` controls how much of the half-spread each
fill crosses. `0.0` fills at mid, which is what paper trading does and what
flatters every options backtest ever written. The default is `0.5`.

Note that net P&L is **not** monotonic in that fraction, and a test asserting
so would be wrong: a different entry price changes the credit, which changes
when the profit target trips, which changes the exit date. What is monotonic is
the modelled spread bill.

---

## Exits

Positions are not held to the end of the window and marked at a fantasy mid.
Each session the engine reprices every open leg, computes P&L as a fraction of
max profit, and calls the strategy's own `should_exit`. Expiry settles at
intrinsic. Whatever is still open when the window ends is closed at the last
modelled mark, so the final number is realisable.

---

## Which account is this pointed at?

Every page of the dashboard and every `oaa backtest` invocation prints the
resolved profile, the masked API key and **which environment variable it came
from** to the terminal before doing anything.

Related fix: an explicit `--profile` now beats `OAA_PROFILE` in `.env`. It did
not before, so `oaa <cmd> --profile judged` silently kept running on the dev
account - the exact failure the profile split exists to prevent, and one that
left no trace in the output.
