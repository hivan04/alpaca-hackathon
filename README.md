# Eventus — Options Alpha Agents

**An autonomous options trading agent on Alpaca. Built solo, in one week.**

Entry for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
— **Options Alpha Agents** track.

The agent reads Alpaca through its **MCP server**, reasons over what it finds, and
writes through the **Alpaca CLI** — so every order it places is a shell command you
can replay. Between the reasoning and the order sits a deterministic risk engine that
is the only thing in the system allowed to approve a trade.

Three strategy books run against one paper account. They fail in different weather:
one is short volatility and wants nothing to happen, one is long gamma and wants
something to happen fast, and one opens on a **date** rather than on a threshold.

- **Live dashboard:** https://eventus-algo.streamlit.app
- **Judged paper account:** `PA3TSH9YTFJL`
- **Deck:** [`docs/options-agent-strategy.pptx`](docs/options-agent-strategy.pptx) · [PDF](docs/options-agent-strategy.pdf)


* Artefact for those who are interested in more on what the LLM expected for the corporate events for each of the stocks during the trading week: [claude.ai/code/artifact/bc6e99bb-d291-4559-a462-09e3c1fce5f1?open_in_browser=1&amp;via=user_open&amp;org=f4ff8578-fefa-49ea-823f-7669bb70dc51](https://claude.ai/code/artifact/bc6e99bb-d291-4559-a462-09e3c1fce5f1?open_in_browser=1&via=user_open&org=f4ff8578-fefa-49ea-823f-7669bb70dc51)
* Artefact of the algorithm's architecture: [claude.ai/code/artifact/c308a2f1-bd4c-40ea-8995-0ecbfbbfbc68](https://claude.ai/code/artifact/c308a2f1-bd4c-40ea-8995-0ecbfbbfbc68)

---

## Quick start

```bash
git clone <this repo> && cd alpaca-hackathon
make setup                 # venv, deps, .env, config/local.yaml
# fill in .env with BOTH paper key pairs
./scripts/install_tools.sh # alpaca CLI, uvx, MCP server, agent skills
make doctor                # verifies every dependency and credential
make scan                  # a dry cycle: what would the agent do right now?
make run                   # the autonomous loop
```

`make doctor` is the command to trust. It checks config, packages, the `alpaca`
binary, `uvx`, both credential sets, the live connection, your options trading
level, and whether the market is open. **The first diagnostic for any "why is it
not trading" question is which account it is pointed at** — `oaa doctor` prints the
resolved profile, the masked key and the judged account ID.

---

## What it does

| Requirement                | How it is met                                                                                                                                                                                                                                       |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Autonomous agent** | `oaa run` schedules its own session, decides without a human gate, and survives failed cycles and restarts. The runner re-fires any cycle whose time has passed, so a crash at 15:10 does not skip the 15:15 cutoff                               |
| **MCP *and* CLI**  | The agent**reads** through Alpaca's MCP server (account, positions, chains, quotes) behind a 7-tool allowlist, and **writes** through the Alpaca CLI. The MCP server's own order-placing tools are deliberately withheld from the model |
| **Options**          | Every strategy emits an options structure with a computed maximum loss.`risk.allow_undefined_risk: false` — nothing without a computable `max_loss` reaches the broker                                                                         |

### Three books, and the boundary between them

`vol_carry` (carry) and `intraday_momentum` (intraday) share one pool of capital, so
a **capital firewall** keeps them apart rather than good manners. Alpaca grants 4x
day-trading buying power but only 2x Reg T overnight, so the resident carry book
reserves its margin first and the transient intraday book leases only what is left,
measured on a fresh poll.

`earnings_event_directional` (events) is **not a firewall tenant**. A book whose
entire life is one overnight hold does not fit the intraday/carry tenancy model, so
it is bounded by size instead — 1.2% per name, 4% shared across everything opened
that night, and a hard ceiling of 10 contracts that does not scale with equity.

A fourth book, `opportunistic` / `event_premium`, is enabled in config and **cannot
open a position**: `_transient_scan` always acquires the capital lease as `INTRADAY`,
so the opportunistic book's `may_open` check always fails. It is documented here as
enabled because that is what the config says, not because it trades.

### The session, as actually scheduled

`config/default.yaml` → `schedule.cycles`, America/New_York:

```
04:00-16:00  events_watch     HOURLY (13 polls). Read the wire on names reporting this week
09:15        discover         attention ranking + today's regime read
09:45        events_flatten   close last night's earnings spreads into the IV crush
10:00-11:15  intraday_scan    every 15 min
10:00        carry_scan       resident book reserves capital, sells rich premium
11:30-15:10  manage_positions profit target, stop, close-at-DTE (11 passes)
13:30-14:45  intraday_scan    every 15 min  (lunch 11:30-13:30 skipped by the time gate)
13:50, 14:40 carry_scan       two further passes
15:15        HARD CUTOFF      cancel, liquidate TRANSIENT ONLY, poll until flat
15:45        carry_verify     zero transient exposure, fresh Reg T, carry covered
15:50        events_arm       open tonight's earnings spreads, one night only
16:00        -- bell --       the carry book is HELD. There is no nightly exit.
16:10 / 16:20 report          performance report, then the LLM-written daily report
```

Twelve intraday scans, not two. A VWAP cross is an event that lasts minutes: two
scans a day observed roughly 30 of 390 session minutes and required the cross to
land inside one of them. These times mirror `backtest.session_times_et` exactly —
when they diverged, the live agent saw a sixth of what the backtest was tuned
against.

> **Note on the arm time.** `config/strategies/earnings_event.yaml` sets
> `schedule.arm_time: 15:45`; the runner fires `events_arm` at **15:50**, and the
> runner is what actually executes. `no_entry_after: 15:55` bounds it either way.

Layer 1 of the firewall is temporal — a book trades only inside its own window.
Layer 2 is capital. A persisted position ledger maps every leg to its book, which is
what lets 15:15 liquidate the day book without touching a multi-session condor. See
[`docs/FIREWALL.md`](docs/FIREWALL.md).

```bash
oaa firewall --at 15:15     # what each book may do at any boundary
```

### Discovery and the macro lens

The universe is not a hardcoded list. A pre-market cycle reads Alpaca's most-actives,
movers and news feeds, ranks what the market is actually watching, and an LLM macro
lens turns that into a **regime** — which strategies are live, how wide the collars
sit, and which legs carry too much headline risk to hold overnight.

The judgement it exists to make is not *"is this name hot"* but **"is it hot for a
reason the whole sector shares?"** Sector-wide IV elevation with no name-specific
catalyst is exactly the premium the carry book wants to sell. A name repricing on its
own news has a fat tail the model cannot see, and that is a veto. Identical in a
volume count, opposite in implication, and only distinguishable by reading.

```bash
oaa discover              # attention ranking + today's regime read
oaa discover --no-llm     # deterministic breadth rule, zero token cost
oaa pool                  # the accumulated candidate pool
oaa gates                 # the gate-by-gate rejection log
```

Attention **generates candidates**; the hard gates still decide. Nothing from
discovery can approve a trade — the macro lens emits a *regime*, and its size
multiplier is bounded at 1.0 so it can only ever reduce.
[`docs/DISCOVERY.md`](docs/DISCOVERY.md).

---

## The three books

### `vol_carry` — resident, held 3–10 sessions

Sells the IV–RV spread as defined-risk iron condors at 7–14 DTE, so the decay fits
inside the judged window. Four hard gates, then economics:

| Gate    | Threshold                                                      | Why                                                      |
| ------- | -------------------------------------------------------------- | -------------------------------------------------------- |
| Premium | `iv_rank ≥ 0.35` **and** `IV − RV ≥ 3 vol pts`    | Rank is a regime filter;**the spread is the edge** |
| Trend   | `ADX ≤ 25`, trend strength ≤ 0.60                          | Short premium is short movement                          |
| Event   | no earnings in the expiry window, no ex-div under a short call | Pre-earnings IV is elevated*because of* the event      |
| Macro   | sector-wide → sell · idiosyncratic → veto                   | A short condor has no defence against the next headline  |
| Cost    | round-trip spread ≤**20% of the credit**                | A four-leg condor crosses eight half-spreads round trip  |

Exits, checked in this order every cycle: `$450` hard dollar stop → 50% of max profit
→ loss = 1.5× credit → 3 DTE floor → short strike touched (close the tested side) →
macro flag. The 50/1.5 pair sets a breakeven hit rate of **75%** before spread; the
old 30%/2.0× pair needed 87%, against an observed 88% win rate — one point of margin.
Re-entry cooldown is 1440 minutes: one entry per underlying per session.

Universe (14): SPY QQQ IWM DIA XLF XLE TLT GLD XLV XLU EEM EFA FXI SLV.

### `intraday_momentum` — transient, flat by 15:10

Long front-expiry premium (`dte_max: 2`) on a VWAP trigger. Two hard gates, then a
vote:

- **VWAP cross** — HARD. It decides direction; without it there is no trade to make.
  Searched over 15 one-minute bars, exactly one polling gap, so a cross cannot fall
  between two cycles unseen.
- **Spread** — HARD. `≤ 4% of mid` **and** round-trip spread ≤ 30% of target. This is
  economics, not evidence.
- **Three of up to seven votes**: volume z ≥ 0.5 in the same time-of-day bucket ·
  2-bar persistence · Bollinger *width* rising · higher-timeframe agreement ·
  term-structure slope in band · RSI not at 80/20 · catalyst score.

`needed = min(3, possible)` — an unmeasurable vote raises neither side and costs a
candidate nothing. This replaced eight stacked hard vetoes, which at ~70% pass each
was 0.7⁸ ≈ 6%: measured over 864 candidates in 9 sessions, 424 died on the volume
gate alone and **zero** survived the whole chain.

Exits: 10% target, 15% stop (wider on purpose), 20-minute time stop, flat by 15:10 —
five minutes ahead of the firewall cutoff. Selection declines entirely above
`iv_rank 0.85`.

Universe (8): SPY QQQ IWM DIA XLF XLE TLT GLD — index products only. A $0.10-wide
single-name quote costs $20 round trip against a $10–30 target.

### `earnings_event_directional` — one night, bounded by size

This is the book that answers the recurring failure of the other two: *the threshold
is never crossed and the agent opens nothing*. **Its entry condition is a date.**
Broadcom reports on 2 September whether or not any indicator agrees; the gate opens
on schedule and the pipeline only decides whether the trade is worth taking.

- **The model proposes, a file confirms.** Featherless serves open-weight models
  whose weights were frozen months ago; asked to list next week's reporters, one
  answers fluently and sometimes wrongly. A wrong date is not a bad trade, it is a
  position opened against no event at all. `config/events/earnings_calendar.json`
  confirms every candidate, and anything unverified is logged and never armed.
- **Screen:** implied move ≥ 3%, relative spread ≤ 25% (the binding gate), the expiry
  must *contain* the print, top 10 by divergence.
- **The watch.** From three days out, hourly polls of Alpaca news and StockTwits.
  Every item already seen is discarded by hash, so a quiet name makes **no model
  call and writes no note** — cost scales with how much news arrives, not with how
  often the loop runs. Notes are kept at salience ≥ 0.35, aged out at 10 days, capped
  at 40. Triage runs Qwen3-8B (~40 calls/day); the once-per-name direction call runs
  Qwen3-32B.
- **The expression follows the sign of the divergence** (implied move ÷ median
  realised reaction over the last four prints): **≥ 1.35** → iron condor with both
  shorts *outside* the implied move · **≤ 0.80** → debit vertical in the called
  direction · **between** → no trade. That middle band is where the book used to do
  all of its trading, which was measuring volatility and trading direction.
- **The direction call is a tilt, not the thesis.** It picks the side of the vertical
  and pushes the threatened short further out on a condor — it never pulls the other
  one in. Confidence floor 0.55, and confidence sets the size. Evidence must be cited.
  A zero abstention rate logs a warning: a model that never declines is not filtering.

Third-party text is treated as untrusted: fenced, stripped of control characters,
truncated, parsed to a fixed schema, every field clamped.
[`docs/STRATEGY-EVENTS.md`](docs/STRATEGY-EVENTS.md).

---

## Architecture: no model can approve a trade

Find the algorithm's full architecture here: [claude.ai/code/artifact/c308a2f1-bd4c-40ea-8995-0ecbfbbfbc68](https://claude.ai/code/artifact/c308a2f1-bd4c-40ea-8995-0ecbfbbfbc68)

The live agent and the replay run the **same ordered stages, from the same objects**.
A backtest that skips a stage is a backtest of a different system.

```
1  strategy gates    strategies/*.py     -> a TradeIdea, or a rejection carrying the number that stopped it
2  modelled cost     telemetry/costs.py  -> attached before anything judges it
3  critic            agents/critic.py    -> scores and may DECLINE. Cannot approve
4  risk engine       risk/engine.py      -> the ONLY approver. Emits a signed stamp
5  partner veto      partners/           -> veto only, at seven hook points
6  execution         execution/          -> atomic combo, deterministic client_order_id
```

`ExecutionRouter` refuses any ticket without the risk engine's stamp. Stage 4 is
fifteen deterministic checks in a fixed order:

```
firewall -> halted -> market_open -> undefined_risk -> unknown_risk -> leg_count ->
duplicate_legs -> max_positions -> max_new_per_day -> duplicate_structure ->
reentry_cooldown -> concentration -> sizing -> portfolio_risk -> cash
```

`duplicate_structure` and `reentry_cooldown` exist because **brokers net identical
option symbols**: re-opening the same condor doubles one position rather than
creating a second, so every count-based limit was blind to it until 27 Aug.

**Sizing is from max loss, not from capital.** A structure without a computable
`max_loss` is refused outright, which is what makes a stray undefined-risk idea
impossible to execute by accident.

Absence of lookahead in the replay is structural, not a convention: a context stamped
10:00 ET holds that morning's *open* as spot, indicators are computed once as a
series and read at `i-1`, and the wall clock is frozen to the replayed session so
dated gates ask about the replayed date.

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/PIPELINE.md`](docs/PIPELINE.md)

---

## Evidence, and what is wrong with it

The artefact worth reading is **not the equity curve — it is the rejection log**.
Across the Feb–Aug 2026 replay, ~8,600 ideas were generated, 441 were taken, and
**72,753 rejections were recorded with the gate that stopped them and the number it
measured**:

```bash
oaa gates                      # live
oaa backtest --why 15          # replay, grouped by reason, numbers normalised within each
```

The headline replay figures (441 closed trades, real Alpaca bars, spread and fees
charged *inside* every fill) are in the deck. Two things about them matter more than
the number:

1. **Gross P&L was ~$13,973 and the spread cost ~$12,990.** The book paid out roughly
   its own gross profit in execution cost. That is why a cost gate sits in front of
   every trade and why the spread is charged inside the fills rather than added
   beside them.
2. **A credit-to-width audit found a large share of the carry book's P&L in
   structures whose recorded credit exceeded their own width** — arithmetically
   impossible for a condor. The gated re-run is far smaller than the ungated
   headline. `risk.max_credit_to_width` is **not implemented in this tree**
   (`grep` finds it in no source or config file), so a run started from here
   reproduces the ungated number. Quote the gated figure, or state the provenance.

Known and measured limitations, all of them in the repo:

- **Exit code is not shared.** Entry runs the same objects live and in replay; exit is
  reimplemented in the backtest engine, and nothing asserts the two agree.
- **Intraday replay marks are coarse** relative to a 20-minute hold, so that book's
  replay P&L reads for candidate flow, not for edge.
- **Paper fills flatter.** Mid fills, no queue, no partials — and spread is the
  primary loss mechanism, which is exactly what paper does not simulate.
- **The judged window is five sessions.** Expect 10–20 trades. That cannot separate
  edge from luck in either direction.
- **The events book has no usable backtest.** Four prints per name is not a sample.
- **`exit_on_vwap_recross` is still `true`** in `config/strategies/intraday_momentum.yaml`,
  and the A/B measured it at 0 wins in 55 attempts.

---

## Layout

```
config/                  every knob, in YAML
  default.yaml           the master file (schedule, risk, universe, execution)
  dev.yaml               throwaway account overlay
  judged.yaml            the account judges evaluate
  strategies/*.yaml      per-strategy parameters
  events/*.json          the confirmed earnings calendar

src/oaa/
  config/                load, merge, validate; credential resolution
  core/                  domain types, registry, logging
  firewall/              the ET session clock, the capital lock, the position ledger
  discovery/             attention sources, tradability filters, the macro lens
  signals/               catalyst engine, macro calendar, cost and time gates
  options/               OCC symbols, chain filtering, structure builders
  data/                  market data (CLI or SDK), indicators
  strategies/            signal -> structure  (add a file, add a config block)
    events/              the events book: calendar, watch, sentiment, direction,
                         technicals, sizing, engine - its own pipeline
  risk/                  hard limits and sizing  (the only thing that can approve)
  execution/             pricing, idempotency, single orders and rollback combos
  brokers/               cli | mcp | rest | sim, one protocol
  agents/                LLM, structured tools, the assistant, orchestrator, runner
  partners/              sponsor technology adapters
  telemetry/             journal, metrics, HTML report
  app/                   the Streamlit dashboard, operator and public builds
  backtest/              replay harness, honestly labelled

deploy/                  PM2, systemd, Docker, and the always-on host walkthrough
scripts/                 install, verification, probes, publishing, plots
tests/                   the offline suite
public/                  what the deployed page reads: runs/, reports/, events/watch/
archive/                 retired strategies and superseded patches

docs/ARCHITECTURE.md             the full system, outer frame down to each gate
docs/PIPELINE.md                 a cycle end to end, stage by stage
docs/STRATEGY-CARRY.md           vol_carry: gates, Greek caps, the credit-width audit
docs/STRATEGY-INTRADAY.md        intraday_momentum: confirmation scoring, the A/B
docs/STRATEGY-EVENTS.md          the events book, and its live status
docs/FIREWALL.md                 the capital boundary, in detail
docs/DISCOVERY.md                universe discovery and the macro lens
docs/UNIVERSE.md                 the tradable universe and the rule behind it
docs/BACKTEST.md                 the replay harness and what it does not model
docs/DAILY-REPORT.md             the end-of-day report and its evaluator
docs/DEPLOYMENT.md               PM2, systemd, Docker, VS Code
docs/RUNBOOK.md                  day-to-day operation and kill switches
docs/PARTNERS.md                 wiring in a technology partner
docs/DECISIONS.md                why the non-obvious choices were made
docs/options-agent-strategy.pptx the presentation
docs/OAA-pipeline-flowchart.pdf  the operating pipeline as one diagram
```

Only ten things sit at the repo root: `.env.example`, `.gitignore`, `Dockerfile`,
`keep-awake.sh`, `LICENSE`, `Makefile`, `public_dashboard.py`, `pyproject.toml`,
`README.md` and `requirements.txt`. Everything else lives in a folder.

---

## Commands

```
oaa status              is the agent live, and what has it decided today?
oaa status --watch 30   the same, refreshing        (--json for scripts)
oaa doctor              check every dependency and credential
oaa account             account, options level, Reg T vs day-trading power
oaa discover            what the market is watching + today's regime read
oaa pool                the accumulated candidate pool
oaa firewall            the capital boundary: phase, reservations, lease, ledger
oaa firewall --at 15:15 simulate any boundary
oaa chain SPY           the filtered chain a strategy actually sees
oaa agent <cycle>       one AI-assistant-driven cycle over MCP
oaa scan                one dry cycle  (--cycle carry_scan | intraday_scan)
oaa trade               one scan, then route what survives
oaa run                 the autonomous loop
oaa manage              apply exit rules to open positions
oaa flatten             close everything
oaa gates                the gate-by-gate rejection log
oaa journal             recent decisions, including the declined ones
oaa report              performance report -> JSON + self-contained HTML
oaa daily-report        the LLM-written end-of-day report + its evaluator score
oaa switchboard         which books are on, per account
oaa strategies          registered strategies, their book, and how each runs
oaa partners            technology-partner adapters and their stages
oaa mcp-tools           list the tools the Alpaca MCP server exposes
oaa backtest            replay a window against real bars
oaa runs                the backtest history on disk
oaa dashboard           the operator dashboard (Streamlit, six tabs)
oaa serve               preview the FastAPI page locally (NOT the submitted URL -
                        that is https://eventus-algo.streamlit.app)
oaa config-dump         the fully merged configuration
```

The events book has its own verbs, because it is read before a print and acted on
from the terminal — the scheduled cycles inside `oaa run` do the same work unattended:

```
oaa events screen       which confirmed prints land this week
oaa events watch        read the names whose prints are coming
oaa events arm          open tonight's earnings spreads
oaa events flatten      close everything that has now reported
```

---

## Configuration

One YAML file drives everything. Merge order, later wins:

```
config/default.yaml -> config/<profile>.yaml -> config/local.yaml -> OAA_* env vars
```

Environment overrides use double underscores for nesting:

```bash
OAA_RISK__MAX_RISK_PER_TRADE_PCT=0.01 oaa run
```

### Two accounts, always

`profile: dev` uses `ALPACA_DEV_*` keys and runs with `execution.dry_run: true`;
`profile: judged` uses `ALPACA_*` and actually trades. The judged account is the one
whose full history the judges read, so all debugging happens in dev.
`./scripts/verify_accounts.sh` confirms they are genuinely different accounts before
you point the agent at the judged one.

Paper vs live is decided **only** by `broker.paper` in YAML, forced onto every
subprocess. `ALPACA_PAPER_TRADE` in `.env` was a decoy that never had any effect and
has been removed; `oaa doctor` warns if it reappears.

What the judged overlay changes, and why:

| Setting                            | judged    | Reason                                                                                                                                                       |
| ---------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `execution.dry_run`              | `false` | this profile actually trades                                                                                                                                 |
| `execution.chase.enabled`        | `false` | the chase loop cancels without checking the cancel succeeded, then resubmits under a new`client_order_id` — a race that can double the risk-approved size |
| `execution.limit_price_ratio`    | `1.0`   | post at the far touch. At the 0.5 default with chase off, every order rested half a spread from the touch with no second attempt, and simply never filled    |
| `risk.max_risk_per_trade_pct`    | `0.01`  | matched to dev on 30 Aug. While they differed, the same code and data gave +$847 on dev and −$237 on judged for the same week                               |
| `risk.daily_loss_limit_pct`      | `0.03`  | the control that should bind, rather than a position count                                                                                                   |
| `risk.max_new_positions_per_day` | `20`    | at 4/day the 5-session window could open at most 20 positions and, measured, expected fewer than twoicence                                                   |

---

MIT. See [`LICENSE`](LICENSE).
