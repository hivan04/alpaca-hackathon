# Options Alpha Agents

**Built in 48 hours Solo!!!** 

Autonomous options trading agents on Alpaca. Built for the
[Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
— **Options Alpha Agents** track.

An AI assistant queries Alpaca directly through its MCP server, reasons over what it
finds, and acts through structured tools that route via a deterministic risk engine
and out over the Alpaca CLI. Four capital books — **carry**, **intraday**, **events**
and **opportunistic** — share one account and are kept mathematically apart by a
temporal firewall: each trades only inside its own window, and each leases only the
margin the resident book has not already reserved.

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
binary, `uvx`, both credential sets, the live connection, your options trading level,
and whether the market is open.

---

## What it does

| Requirement | How it is met |
|---|---|
| **Autonomous agents** | The assistant drives each cycle over MCP tools and acts without a human gate. `oaa run` schedules itself, survives failed cycles and restarts |
| **MCP *and* CLI** | The agent **reads** through Alpaca's MCP server (account, positions, chains, quotes) and **writes** through the Alpaca CLI — every order is a shell command you can replay. Backtests read bars through the same binary |
| **Options trading** | Every strategy emits an options structure with a computed maximum loss. The carry book sells defined-risk condors and credit verticals; the intraday and events books buy long premium and debit verticals. Nothing without a computable `max_loss` reaches the broker |

### Four books, one boundary

One **resident** book (carry) and three **transient** tenants share a single
account. Alpaca grants 4x day-trading buying power but only 2x Reg T overnight,
so a capital firewall reserves the resident book's margin first and leases out
only what is left:

```
04:00-16:00  events_watch    hourly. Read the wire on names reporting this week
09:15 ET     discover        attention ranking + today's regime read
09:45 ET     events_flatten  close last night's earnings spreads once they print
10:00 ET     carry_scan      resident book reserves capital, sells rich premium
10:00-14:45  intraday_scan   every 15 min; transient books lease the headroom
11:30-15:10  manage_positions profit target, stop, close-at-DTE
15:15 ET     HARD CUTOFF     cancel, liquidate TRANSIENT ONLY, POLL until flat
15:45 ET     carry_verify    zero transient exposure, FRESH Reg T, carry covered
15:50 ET     events_arm      open tonight's earnings spreads, one night only
16:00 ET     ── bell ──      the carry book is HELD. There is no nightly exit.
16:10/16:20  report          performance report, then the LLM-written daily report
```

The events book is the one exception the timeline makes deliberately: it opens
*after* the intraday cutoff has proved the day book flat, holds a single defined-risk
spread across one scheduled print, and closes at 09:45 the next morning. It runs as
scheduled cycles inside `oaa run` — it is not a strategy the options runner loads.

Layer 1 is temporal — a book trades only inside its own window. Layer 2 is
capital — the transient lease is what Reg T leaves *after* the carry
requirement, measured on a fresh poll. A persisted position ledger is what makes
the 15:15 cutoff able to liquidate the day book without touching a
multi-session condor. See [`docs/FIREWALL.md`](docs/FIREWALL.md).

```bash
oaa firewall --at 15:15     # what each book may do at any boundary
```

### Discovery and the macro lens

The universe is not a hardcoded list. A pre-market cycle reads Alpaca's
most-actives, movers and news feeds, ranks what the market is actually watching,
and an LLM macro lens turns that into a **regime** — which strategies are live
tonight, how wide the collars sit, and which legs carry too much headline risk
to hold overnight.

The judgement it exists to make is not *"is this name hot"* but **"is it hot for
a reason the whole sector shares?"** Sector-wide IV elevation with no name-specific
catalyst is exactly the premium the carry book wants to sell. A name repricing on
its own news has a fat tail the model cannot see, and that is a veto. Identical in
a volume count, opposite in implication, and only distinguishable by reading.

```bash
oaa discover              # attention ranking + today's regime read
oaa discover --no-llm     # deterministic breadth rule, zero token cost
oaa pool                  # the accumulated candidate pool
oaa gates                 # the gate-by-gate rejection log
```

Attention **generates candidates**; the four hard premium gates still decide.
Nothing from discovery can approve a trade — the macro lens emits a *regime*, and
its size multiplier is bounded at 1.0 so it can only ever reduce. Full reasoning
in [`docs/DISCOVERY.md`](docs/DISCOVERY.md).

### Shipped strategies

| Strategy | Book | Structure | Fires when |
|---|---|---|---|
| `vol_carry` | **carry** (resident) | iron condor / credit vertical / calendar | IV rank ≥ 0.70 **and** IV−RV ≥ 3 vol pts, ADX ≤ 25, no earnings in the window, macro lens reads the move as shared |
| `earnings_event_directional` | **events** (transient) | vertical debit spread | a *confirmed* print lands tomorrow, a week of watch notes points one way, and the technicals do not veto |
| `intraday_momentum` | intraday (transient) | long option or debit vertical | confirmation score clears its floor — VWAP cross, bucketed volume, expanding band width, term-structure vote — RSI clear, spread gate passed |
| `event_premium` | opportunistic (transient) | iron condor on SPY/QQQ | *(cannot open — see below)* a scheduled print is due **and** implied is ≥ 1.25× the historical realised move |
| `earnings_calendar` | intraday | calendar spread | *(off in config)* term-structure inversion into earnings |

**`vol_carry`** is the resident strategy: it sells the IV–RV spread as
defined-risk structures held 3–10 sessions, at 7–14 DTE so the decay fits inside
the judged window.

**`earnings_event_directional`** is the book that answers the recurring failure of
the other two — *the threshold is never crossed and the agent opens nothing*. Its
entry condition is **a date**, not an indicator. Broadcom reports on 2 September
whether or not any signal agrees; the gate opens on schedule and the pipeline only
decides whether the trade is worth taking. From three days out it re-reads the
Alpaca news and StockTwits feeds hourly, discards every item it has already seen,
and asks the model one narrow question about the *new* items only — so a quiet name
costs no tokens and is reported as quiet. The expression follows the **sign of the
divergence** between what the wire says and what the option chain has priced; three
interlocks (a confirmation gate, an RSI veto, and a `no_entry_after` guard) can each
stand it down on their own. Write-up: [`docs/STRATEGY-EVENTS.md`](docs/STRATEGY-EVENTS.md).

**`event_premium`** is enabled in config and still **cannot open a position**:
`_transient_scan` always acquires the capital lease as `INTRADAY`, so the
opportunistic book's `may_open` check always fails. It is listed here as enabled
because that is what the config says, not because it trades.

Full write-ups: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §4 and §5,
[`docs/STRATEGY-CARRY.md`](docs/STRATEGY-CARRY.md),
[`docs/STRATEGY-INTRADAY.md`](docs/STRATEGY-INTRADAY.md),
[`docs/STRATEGY-EVENTS.md`](docs/STRATEGY-EVENTS.md).

---

## Layout

Only nine things sit at the repo root — `.env.example`, `.gitignore`,
`Dockerfile`, `keep-awake.sh`, `LICENSE`, `Makefile`, `public_dashboard.py`,
`README.md` and `requirements.txt` — plus `pyproject.toml`, which cannot move
because it is the package definition and holds the ruff, pytest and mypy config.
Everything else lives in a folder.

```
config/                  every knob, in YAML
  default.yaml           the master file
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

deploy/                  how it runs somewhere that does not sleep
  ecosystem.config.js    PM2 process definitions (judged, control, dashboard)
  oaa.service            systemd unit
  docker-compose.yml     the container path
  ORACLE.md              the always-on host walkthrough

scripts/                 install, verification, probes, publishing, plots
tests/                   the offline suite
public/                  what the deployed page reads: runs/ and reports/
archive/                 retired strategies and superseded patches
notebooks/  data/  logs/  runs/  reports/

docs/ARCHITECTURE.md             the full system, outer frame down to each gate
docs/PIPELINE.md                 a cycle end to end, stage by stage
docs/STRATEGY-CARRY.md           vol_carry: gates, Greek caps, the credit-width audit
docs/STRATEGY-INTRADAY.md        intraday_momentum: confirmation scoring, the A/B
docs/STRATEGY-EVENTS.md          the events book, and its live status
docs/FIREWALL.md                 the capital boundary, in detail
docs/DISCOVERY.md                universe discovery and the macro lens
docs/UNIVERSE.md                 the tradable universe and its tier caps
docs/BACKTEST.md                 the replay harness and what it does not model
docs/DAILY-REPORT.md             the end-of-day report and its evaluator
docs/DEPLOYMENT.md               PM2, systemd, Docker, VS Code
docs/RUNBOOK.md                  day-to-day operation and kill switches
docs/PARTNERS.md                 wiring in a technology partner at kickoff
docs/DECISIONS.md                why the non-obvious choices were made
docs/OAA-pipeline-flowchart.pdf  the whole operating pipeline as one diagram
docs/OAA-architecture.pdf        the architecture doc, print-ready
docs/options-agent-strategy.pptx the presentation, 20 slides
docs/options-agent-strategy.pdf  the same, exported
```

---

## Commands

```
oaa status              is the agent live, and what has it decided today?
oaa status --watch 30   the same, refreshing
oaa status --json       the same, for scripts
oaa doctor              check every dependency and credential
oaa discover            what the market is watching + today's regime read
oaa pool                the accumulated candidate pool
oaa firewall            the capital boundary: phase, reservations, lease, ledger
oaa firewall --at 15:15 simulate any boundary
oaa account             account, options level, Reg T vs day-trading power
oaa chain SPY           the filtered chain a strategy actually sees
oaa agent <cycle>       one AI-assistant-driven cycle over MCP
oaa scan                one dry cycle  (--cycle carry_scan | intraday_scan)
oaa run                 the autonomous loop
oaa manage              apply exit rules to open positions
oaa flatten             close everything
oaa gates               the gate-by-gate rejection log
oaa report              performance report -> JSON + self-contained HTML
oaa daily-report        the LLM-written end-of-day report + its evaluator score
oaa journal             recent decisions, including the declined ones
oaa switchboard         which books are on, per account
oaa partners            technology-partner adapters and their stages
oaa mcp-tools           list the tools the Alpaca MCP server exposes
oaa strategies          registered strategies, their book, and how each runs
oaa backtest            replay a window against real bars
oaa runs                the backtest history on disk
oaa dashboard           the operator dashboard (Streamlit, six tabs)
oaa config-dump         the fully merged configuration
oaa serve               the public dashboard (the submission's Application URL)
```

The events book has its own verbs, because it is read before a print and acted on
from the terminal — the scheduled cycles inside `oaa run` do the same work
unattended:

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

### Cost control

The trading path — firewall, signal stacks, the macro lens, risk,
execution, backtest — is entirely deterministic and needs no model at all. Set
`agents.llm.provider: null` and the system still trades, using a transparent
heuristic score in place of the critic's reasoning.

Where a model is used, three dials control the spend:

```yaml
agents:
  agent_cycles: ["carry_scan"]        # [] = zero token cost, rules only
  mcp_read_tools: null      # null = the 7-tool allowlist; schemas re-send per turn
  prompt_caching: true      # system prompt + tool schemas are byte-identical
```

The mechanical cycles — the 15:15 liquidation, the 15:45 verification, the
submission flatten — are
deliberately never agent-driven. There is nothing to reason about, and a language
model in the path of a safety-critical liquidation is a failure mode, not a
feature.

### Two accounts, always

`profile: dev` uses `ALPACA_DEV_*` keys; `profile: judged` uses `ALPACA_*`. The judged
account is the one whose full history the judges read, so all debugging happens in
dev. `./scripts/verify_accounts.sh` confirms they are genuinely different accounts
before you point the agent at the judged one.

---

## Safety model

- **The firewall is checked before anything else.** A book that does not hold the
  capital lock cannot open a position, however good the idea.
- **Only `RiskEngine` approves.** It emits a signed stamp; the execution router
  refuses any ticket without one. The assistant reasons and decides *what* to
  trade — it has no latitude over whether the rules apply. The MCP server's own
  order-placing tools are deliberately withheld from the model.
- **Combos roll back.** Where a venue cannot route a multi-leg order atomically,
  the structure is legged **long legs first**, and anything that filled before a
  failure is unwound in reverse at market. No partial failure can leave an
  uncovered short.
- **The macro lens is an overlay.** It can stand a strategy down or halve its
  size. It cannot approve a trade, and `size_multiplier` is bounded at 1.0 — a
  language model can never increase risk.
- **Defined risk enforced in code.** Structures without a computable maximum loss are
  rejected outright.
- **Portfolio Greek caps.** `max_net_delta` and `max_net_vega` are checked across the
  whole account, not per trade, so a book cannot accumulate a directional or vol
  exposure one defensible position at a time.
- **Idempotent orders.** Deterministic `client_order_id`s plus a pre-submit existence
  check, so an ambiguous failure can never double-fill.
- **Self-halting.** Daily loss limit and max drawdown stop trading automatically, and
  the halt is recorded in the journal.
- **Dry run by default.** `execution.dry_run: true` in the dev profile logs the exact
  order payload without sending it.

---

## Extending

**A strategy** — new file in `src/oaa/strategies/`, subclass `Strategy`, decorate with
`@strategy_registry.register("name")`, add a params YAML, add a config block.

**A technology partner** — copy `src/oaa/partners/example_partner.py`, pick one of the
seven pipeline stages, add a config block. See `docs/PARTNERS.md`.

**A broker or data provider** — implement the protocol, decorate with the registry,
change one config line.

Nothing in the core pipeline changes for any of the three.

---

## Running it for real

```bash
pm2 start deploy/ecosystem.config.js --only oaa-judged     # or systemd, or Docker
make pm2-status
```

Do not run the judged agent on a laptop. The design turns on 15:15 and 15:45 ET
firing on time, and a machine that sleeps misses the cutoff — the exact failure
the firewall exists to prevent. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Two dashboards, one codebase

`oaa dashboard` is the **operator** page: six tabs — Backtesting, Live
Trading, Positions, Events, Daily Reports, Control.

`streamlit run public_dashboard.py` is the **public** page — the same `main()`,
with four tabs and nothing that writes:

    Backtesting  ·  Live Trading Positions  ·  Events  ·  Daily Reports

| removed | why |
|---|---|
| Control tab | flips books on and off on a real account. Not hidden — never constructed. |
| Live Trading tab | the operator's instrument panel; every panel on it reads the live chain on our key |
| Run a new backtest | spawns a replay on the host and burns data-API budget |
| Price the live chain | one network round trip per confirmed event, per click |
| Refresh from Alpaca | same, per click. The public page auto-loads once per session instead. |
| dev/judged switch | the public build is the judged account and nothing else |
| the dev account itself | `PUBLIC_ACCOUNTS` is a shorter list, not a filtered one — no code path can put it back |
| identity banner, on the page **and** on stdout | masked key and account id. stdout on a deploy host is a log stream, not a private terminal. |

Positions carries the name **Live Trading Positions** there, because with the
Live Trading tab gone it is the page that answers "is this thing actually
trading" — and it answers it with the broker's own record rather than ours.

**Daily Reports is on both builds, unguarded, on purpose.** It opens files a
finished cycle already wrote — no fetch, no chain read, no broker call — so
there is nothing on it for a guard to protect, and it is the only page that
says what the system thinks is wrong with itself. See
[`docs/DAILY-REPORT.md`](docs/DAILY-REPORT.md).

```bash
make public-dashboard          # preview it locally on :8502
```

The selector is `OAA_PUBLIC`, set by `public_dashboard.py` before it imports
anything and read at call time by `oaa.app.mode` — never cached, because a
cached answer resolved at import is how a Run button survives onto a public
page. `make dashboard` never sets it, so the local page cannot lose its Control
tab by accident. `tests/test_public_mode.py` pins every row of that table.

### Publishing the backtest history

`runs/` is gitignored, and its `result.json` files total **333MB** — a single
wide-universe run is 50MB. No deploy host should clone that. They compress
about **28x**, so the entire store fits in ~12MB:

```bash
python scripts/publish_runs.py --all      # every run, gzipped, into public/runs/
python scripts/publish_runs.py --list     # or choose them by hand
python scripts/publish_reports.py --all   # daily reports, into public/reports/
```

`public/runs/` **is** committed. `load_run` reads `result.json` or
`result.json.gz`, preferring the plain file so a local re-run is never
shadowed by an older published copy. Re-publishing an unchanged run writes a
byte-identical file (`mtime=0` in the gzip header), so git sees no churn.

### Publishing the daily reports

`reports/` is gitignored for the same reason `runs/` is — it is generated, one
pair of files per session, and it dirties the tree every afternoon. A deploy host
clones the repo and gets none of it, so the Daily Reports tab would boot empty with
no error anywhere:

```bash
python scripts/publish_reports.py --all       # or --latest 5, or a single date
```

Both files travel together: the `.json` sidecar is what the page renders and the
`.md` is what the download button hands over. `public/reports/` **is** committed,
and `.gitignore` needs its `!public/reports/` escape for the same
matches-at-any-depth reason `runs/` does.

### Deploying

Point the host at `public_dashboard.py`. Streamlit Community Cloud exposes its
secrets as environment variables, so the judged credentials go in there under
the same names `.env` uses locally. Nothing else is needed — `requirements.txt`
is already at the repo root.

## Development

```bash
make test     # the full suite, no network required
make lint     # ruff
make check    # both
```

The `sim` broker, a synthetic Black-Scholes chain and generated intraday
series mean the entire pipeline — firewall, signal stacks, strategies, risk, combo
execution, telemetry — is tested offline. `.vscode/launch.json` has a debug target
for every entry point.

## Licence

MIT. See `LICENSE`.
