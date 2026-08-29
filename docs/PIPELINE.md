# The decision pipeline — end to end

How a bar becomes an order, and where every gate sits. Current as of **27 Aug**,
branch `strategy-v2`. Companion docs: `docs/ARCHITECTURE.md` for layout,
`docs/BACKTEST.md` for the harness,
`docs/DECISIONS.md` for what moved today.

## One pipeline, two entry points

The replay and the live agent run the **same** ordered stages. That is the whole
design: a backtest that skips a stage is a backtest of a different system.

```
                    ┌── live:  agents/orchestrator.py  (oaa run)
  MarketContext ────┤
                    └── replay: backtest/engine.py     (oaa backtest)

  1. strategy gates      strategies/*.py        -> TradeIdea or a RejectionRecord
  2. modelled cost       telemetry/costs.py     -> attached to the idea
  3. CRITIC              agents/critic.py       -> scores, may decline
  4. RISK ENGINE         risk/engine.py         -> the ONLY thing that approves
  5. partner veto        partners/              -> veto only, never approve
  6. execution           execution/             -> combo order, signed stamp
```

The critic cannot authorise and the partners cannot authorise. Only
`RiskEngine.evaluate` emits the signed stamp `ExecutionRouter` demands.

## Stage 0 — building the context

`MarketContext` is everything a strategy is allowed to see. No strategy makes a
live call; that is what keeps them deterministic and replayable.

| field | live | replay |
|---|---|---|
| `bars` | daily, `lookback_days` | complete prior sessions only |
| `intraday_bars` | `data.intraday_timeframe` × `intraday_lookback_days` | **same** (fixed 27 Aug — replay previously carried one day, making the backtest a strictly harsher strategy than live) |
| `chain` | live Alpaca chain snapshot | real option bars where a print exists, modelled elsewhere |
| `news` | Alpaca news stream | Alpaca headlines published before the replayed moment |
| `iv_rank`, `realised_vol` | computed live (Garman-Klass) | computed at `i-1`, same estimator |

**No lookahead is structural, not a convention.** A context stamped 10:00 ET
holds that morning's *open* as spot, never the day's close; indicators are
computed once as a series and read at `i-1`; and the wall clock
(`core/clock.py`) is frozen to the replayed session so dated gates ask about the
replayed date rather than about today.

## Stage 1 — strategy gates

Each strategy returns either a `TradeIdea` or a rejection carrying the gate that
stopped it and the metric it measured. That log is the highest-value judging
artefact — an agent declining trades, with the number that caused it.

| book | strategy | gates in order |
|---|---|---|
| carry | `vol_carry` | premium (IV rank ≥ 0.35 **and** IV−RV ≥ 3pts) → trend → event → macro lens → cost |
| intraday | `intraday_momentum` | time of day → data sufficiency → momentum (VWAP / volume-z / persistence / BB width / RSI veto) → catalyst → macro → selection → structure → spread |
| opportunistic | `event_premium` | scheduled catalyst → implied vs realised move |

Read it with `oaa backtest --why 15`, which groups by **reason** and normalises
the numbers inside each one, so "IV rank 19% below the 70% floor" and "IV rank 4%
below the 70% floor" count as one finding rather than two.

## Stage 2 — modelled cost

`telemetry/costs.py` attaches a full round-trip estimate to the idea *before*
anything judges it, so a rejection still carries what the trade would have cost.
OCC clearing, ORF, FINRA CAT both sides, TAF and SEC on sells, index exchange
fees, and margin interest on the structure's requirement for the hold.

The **spread** is charged inside the fills on both entry and exit rather than
added beside them — and reported separately so its size stays visible.
`backtest.slippage_spread_fraction` (0.5) sets how much of the half-spread each
fill crosses; 0.0 is what paper trading does.

## Stage 3 — the critic

Scores the idea and writes reasoning. It can decline; it cannot approve.

- `heuristic` (default) — the real `Critic` class with a null LLM client, which
  is the documented degraded path the live system falls back to. Deterministic,
  free, identical code.
- `llm` — Featherless both sides: temp 0 + seed 7 in replay, temp 0.2 live.
  Verdicts cached on disk keyed by the
  prompt inputs **and the model**. Not the default in replay because the model
  may have the replayed period in its training data — lookahead no engineering
  removes. Inspect reasoning with it; do not quote P&L from it.
- `off` — diff against it to measure what the critic contributes.

## Stage 4 — the risk engine

The only approver. Checks in order, each recorded on the trade:

```
firewall (book x time) -> halted -> market_open -> undefined_risk ->
unknown_risk -> leg_count -> duplicate_legs -> max_positions ->
max_new_per_day -> duplicate_structure -> reentry_cooldown ->
concentration -> sizing -> portfolio_risk -> cash
```

Two of those are new on 27 Aug and exist because **brokers net identical option
symbols**: re-entering the same structure doubles one position rather than
opening a second, so every count-based limit was blind to it.

- `duplicate_structure` — every leg already held on the same side → refuse.
- `reentry_cooldown` — 60 min, per (symbol, strategy). Catches the near-duplicate
  once spot has moved enough to change the strikes.

**Sizing is from max loss, not from capital.** A structure without a computable
`max_loss` is refused outright (`risk.allow_undefined_risk: false`), which is
also what makes a stray non-defined-risk idea impossible to execute by accident.

## Stage 5 — partners

`PartnerHub.run(stage, payload)` at seven points. Adapters registered at the
`risk` stage may **veto only**. Nothing a partner returns can turn a rejection
into an approval.

## Stage 6 — execution

- **Deterministic `client_order_id`** plus a pre-submit existence check, so a
  retry cannot double-fill.
- **Atomic combo by default** — one spread crossed, not four. `legged` is the
  fallback for venues that cannot route combos: long/protective legs first,
  unwound in reverse, so a partial failure never leaves a naked short.
- **Every leg is an option.** `AssetKind.EQUITY` exists in `execution/combo.py`
  for a future hedged structure, but **no strategy in the repo emits an equity
  leg** — every builder in `options/structures.py` takes `OptionQuote` objects
  and every leg defaults to `AssetKind.OPTION`. A bare equity leg would have no
  computable `max_loss` and would be refused at stage 4 anyway.
- The CLI is the **write** path, so every order is a replayable shell command;
  MCP is the agent's **read** surface behind a 7-tool allowlist.

## Marking and exits

Positions are re-marked every cycle, and `should_exit` is mechanical — no LLM in
the exit path.

**Every leg of a structure is priced on ONE surface** (fixed 27 Aug). `reprice`
decides per contract whether to use a real bar or the model; in a condor the
shorts trade and the wings often do not, so a stressed session marked the short
at a real elevated print against a wing on a calm modelled vol. The vertical's
value is then not bounded by its strike width and the structure stops being
defined-risk — measured, a 5-wide spread marked 4.69 mixed against 2.96 on one
surface, and a −170.9%-of-max-loss trade appeared in the log. Where provenance is
mixed, every leg is now re-priced from the model anchored on the vol the real
prints imply.

`_bounded_gross` then clamps the mark-to-market to the structure's own
arithmetic, because the risk engine approved the trade on that number. Costs are
charged outside the bound — `max_loss` is a pre-cost concept.

Both are counted and reported per run as `mixed_surface_marks` and
`risk_bound_clamps`. **The second should be zero.**

## Session schedule (America/New_York)

```
09:15 discover        candidate pool + regime read
09:45 intraday_scan   transient books lease the headroom
10:00 carry_scan      resident book looks for rich premium
12:00 manage          exit rules
13:45 intraday_scan
14:30 manage
15:15 intraday_cutoff HARD CUTOFF - transient books only
15:45 carry_verify    the sign-off
16:10 report
```

The carry book is **held overnight** — there is no morning exit.

**Known divergence:** the *backtest* now scans 12 times a day (every 15 min,
10:00–14:45) because a VWAP cross is an event that a 4-times-a-day poll cannot
see. `schedule.cycles` above still fires the live agent 4 times. Until those
agree, the live intraday book sees far less of the session than the replayed one
does. This is the top open item.

## Dated controls, fired from the clock

- `management.entry_cutoff_utc: null` — removed 29 Aug. It gated every book
  when its reasoning applied only to carry; the carry book now carries its own
  cutoff in `config/strategies/vol_carry.yaml`
- `management.submission_flatten_utc: 2026-09-04T13:45:00Z` — checked on **every**
  runner poll, not once a day

## Running it

```bash
make bt          # terminal backtest: fetches and caches bars, reasons + trades
make bt-offline  # same, refuses the network - needs a warm cache
make bt-wiring   # synthetic smoke test: plumbing only, NOT a result
make dashboard   # Streamlit: backtesting + live trading tabs
make run-judged  # the autonomous loop against the judged account
```
