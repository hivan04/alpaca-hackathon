# Options Alpha Agents

Autonomous options trading agents on Alpaca. Built for the
[Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
— **Options Alpha Agents** track.

An AI assistant queries Alpaca directly through its MCP server, reasons over what it
finds, and acts through structured tools that route via a deterministic risk engine
and out over the Alpaca CLI. Two capital books — intraday and overnight — share one
account and are kept mathematically apart by a temporal firewall.

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
| **Options trading** | Every strategy emits an options structure with a computed maximum loss. The carry book sells defined-risk condors and credit verticals; the intraday book buys long premium and debit verticals. Nothing without a computable `max_loss` reaches the broker |

### Three books, one boundary

One **resident** book and two **transient** tenants share a single account.
Alpaca grants 4x day-trading buying power but only 2x Reg T overnight, so a
capital firewall reserves the resident book's margin first and leases out only
what is left:

```
09:45 ET  intraday_scan       transient books lease the remaining headroom
10:00 ET  carry_scan          resident book reserves capital, sells rich premium
15:15 ET  HARD CUTOFF         cancel, liquidate TRANSIENT ONLY, POLL until flat
15:45 ET  carry_verify        zero transient exposure, FRESH Reg T, carry covered
16:00 ET  ── bell ──          the carry book is HELD. There is no nightly exit.
```

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
| `intraday_momentum` | intraday (transient) | long option or debit vertical | *(off by default)* VWAP cross + bucketed volume + expanding band width, RSI clear, catalyst confirmed, spread gate passed |
| `event_premium` | opportunistic (transient) | iron condor on SPY/QQQ | *(dormant)* a scheduled print is due **and** implied is ≥ 1.25× the historical realised move |
| `momentum_debit_spread` | intraday | vertical debit spread | *(off)* confirmed trend, cheap IV — the opposite-regime strategy |
| `earnings_calendar` | intraday | calendar spread | *(off)* term-structure inversion into earnings |

**`vol_carry`** is the headline strategy: it sells the IV–RV spread as
defined-risk structures held 3–10 sessions, at 7–14 DTE so the decay fits inside
the judged window. **`intraday_momentum`** is deliberately built second and
disabled by default — judges see the full history of the submitted account, and a
malfunctioning intraday loop firing junk orders is permanently on the record.

Full write-ups: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §4 and §5.

---

## Layout

```
config/                  every knob, in YAML
  default.yaml           the master file
  dev.yaml               throwaway account overlay
  judged.yaml            the account judges evaluate
  strategies/*.yaml      per-strategy parameters

src/oaa/
  config/                load, merge, validate; credential resolution
  core/                  domain types, registry, logging
  firewall/              the ET session clock, the capital lock, the position ledger
  discovery/             attention sources, tradability filters, the macro lens
  signals/               catalyst engine, macro calendar, cost and time gates
  options/               OCC symbols, chain filtering, structure builders
  data/                  market data (CLI or SDK), indicators
  strategies/            signal -> structure  (add a file, add a config block)
  risk/                  hard limits and sizing  (the only thing that can approve)
  execution/             pricing, idempotency, single orders and rollback combos
  brokers/               cli | mcp | rest | sim, one protocol
  agents/                LLM, structured tools, the assistant, orchestrator, runner
  partners/              sponsor technology adapters
  telemetry/             journal, metrics, HTML report
  app/                   read-only public dashboard
  backtest/              replay harness, honestly labelled

docs/ARCHITECTURE.md         the full system, outer frame down to each gate
docs/OAA-pipeline-flowchart.pdf  the whole operating pipeline as one diagram
docs/OAA-architecture.pdf        the architecture doc, print-ready
docs/FIREWALL.md             the capital boundary, in detail
docs/DISCOVERY.md            universe discovery and the macro lens
docs/DEPLOYMENT.md           PM2, systemd, Docker, VS Code
docs/RUNBOOK.md              day-to-day operation and kill switches
docs/PARTNERS.md             wiring in a technology partner at kickoff
docs/DECISIONS.md            why the non-obvious choices were made
```

---

## Commands

```
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
oaa journal             recent decisions, including the declined ones
oaa partners            technology-partner adapters and their stages
oaa mcp-tools           list the tools the Alpaca MCP server exposes
oaa strategies          registered strategies
oaa config-dump         the fully merged configuration
oaa serve               the public dashboard (the submission's Application URL)
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
pm2 start ecosystem.config.js --only oaa-judged     # or systemd, or Docker
make pm2-status
```

Do not run the judged agent on a laptop. The design turns on 15:15 and 15:45 ET
firing on time, and a machine that sleeps misses the cutoff — the exact failure
the firewall exists to prevent. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Development

```bash
make test     # 181 tests, no network required
make lint     # ruff
make check    # both
```

The `sim` broker, a synthetic Black-Scholes chain and generated intraday
series mean the entire pipeline — firewall, signal stacks, strategies, risk, combo
execution, telemetry — is tested offline. `.vscode/launch.json` has a debug target
for every entry point.

## Licence

MIT. See `LICENSE`.
