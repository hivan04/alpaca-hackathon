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
| **Options trading** | Every strategy emits an options structure with a computed maximum loss. The pairs strategy is collared on both legs; the intraday strategies are multi-leg spreads |

### The two books

Alpaca grants **4x day-trading buying power** but only **2x Reg T overnight**.
Intraday leverage still on the books at 16:00 is a broker-forced liquidation. A
**temporal firewall** makes the overlap impossible with a sequential lock-and-verify:

```
15:15 ET  intraday HARD CUTOFF   cancel, liquidate, POLL until confirmed flat
15:54 ET  overnight GATE         prove flat, read FRESH Reg T, size, take the lock
15:55 ET  overnight entry        only possible while holding the lock
09:35 ET  overnight exit         liquidate, release, hand capital to the day book
```

Layer 1 is temporal — a book trades only in its own window. Layer 2 is capital —
size is scaled against buying power measured *after* the other book is proven flat,
never a cached number. See [`docs/FIREWALL.md`](docs/FIREWALL.md).

```bash
oaa firewall --at 15:54     # what the gate would say
```

### Discovery and the macro lens

The universe is not a hardcoded list. A pre-market cycle reads Alpaca's
most-actives, movers and news feeds, ranks what the market is actually watching,
and an LLM macro lens turns that into a **regime** — which strategies are live
tonight, how wide the collars sit, and which legs carry too much headline risk
to hold overnight.

The judgement it exists to make is not *"is this name hot"* but **"is it hot for
a reason its pair partner shares?"** A sector-wide move leaves the spread intact;
an idiosyncratic one dislocates it on news that will never mean-revert. Identical
in a volume count, opposite in implication, and only distinguishable by reading.

```bash
oaa discover              # attention ranking + tonight's regime read
oaa discover --no-llm     # deterministic breadth rule, zero token cost
oaa pool                  # the accumulated candidate pool
```

Attention **generates candidates**; cointegration still decides. Nothing from
discovery enters the gap model's features — live snapshots cannot be replayed,
and a feature built on them would silently poison the walk-forward backtest.
Full reasoning in [`docs/DISCOVERY.md`](docs/DISCOVERY.md).

### Shipped strategies

| Strategy | Book | Structure | Fires when |
|---|---|---|---|
| `overnight_pairs` | overnight | cointegrated pair + **collar on both legs** | spread dislocated, gap model shows edge against a tolerable tail |
| `vol_carry_condor` | intraday | iron condor (4 legs, net credit) | IV rank rich, IV/RV > 1.1, no strong trend |
| `momentum_debit_spread` | intraday | vertical debit spread | *(off)* confirmed trend, cheap IV |
| `earnings_calendar` | intraday | calendar spread | *(off)* term-structure inversion into earnings |

**`overnight_pairs`** is the headline strategy — Engle-Granger cointegration offline,
a Kalman filter for the live hedge ratio, and a Huber + quantile-LightGBM ensemble
whose q05/q95 place the protective strikes. Full write-up in
[`docs/STRATEGY_OVERNIGHT.md`](docs/STRATEGY_OVERNIGHT.md).

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
  firewall/              the ET session clock and the two-book capital lock
  discovery/             attention sources, tradability filters, the macro lens
  quant/                 cointegration, Kalman filter, gap-forecast ensemble
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
  backtest/              walk-forward overnight engine + replay harness

docs/ARCHITECTURE.md         the diagram and the reasoning
docs/FIREWALL.md             the two-book capital lock, in detail
docs/DISCOVERY.md            universe discovery and the macro lens
docs/STRATEGY_OVERNIGHT.md   the overnight pairs strategy
docs/DEPLOYMENT.md           PM2, systemd, Docker, VS Code
docs/RUNBOOK.md              day-to-day operation and kill switches
docs/PARTNERS.md             wiring in a technology partner at kickoff
docs/DECISIONS.md            why the non-obvious choices were made
```

---

## Commands

```
oaa doctor              check every dependency and credential
oaa discover            what the market is watching + tonight's regime read
oaa pool                the accumulated candidate pool
oaa firewall            the two-book capital lock: phase, owner, budget
oaa firewall --at 15:54 simulate the gate at any moment
oaa account             account, options level, Reg T vs day-trading power
oaa pairs               the approved cointegrated universe
oaa signal KO/PEP       tonight's Kalman state and gap forecast
oaa chain SPY           the filtered chain a strategy actually sees
oaa agent <cycle>       one AI-assistant-driven cycle over MCP
oaa scan                one dry intraday cycle
oaa run                 the autonomous loop
oaa manage              apply exit rules to open positions
oaa flatten             close everything
oaa backtest-overnight  walk-forward backtest, bars via the Alpaca CLI
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

The trading path — firewall, Kalman, the ML model, cointegration, risk,
execution, backtest — is entirely deterministic and needs no model at all. Set
`agents.llm.provider: null` and the system still trades, using a transparent
heuristic score in place of the critic's reasoning.

Where a model is used, three dials control the spend:

```yaml
agents:
  agent_cycles: ["overnight_signal", "overnight_entry"]   # [] = zero cost
  mcp_read_tools: null      # null = the 7-tool allowlist; schemas re-send per turn
  prompt_caching: true      # system prompt + tool schemas are byte-identical
```

The mechanical cycles — the 15:15 liquidation, verification, the 09:35 exit — are
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
- **Combos roll back.** The pairs trade is four orders: protective options first,
  equity second. Anything that filled before a failure is unwound at market, so no
  partial failure can leave an unhedged overnight short.
- **The macro lens is an overlay.** It can stand a strategy down, halve its size
  or widen a hedge. It cannot approve a trade, and `collar_widening` is bounded
  below at 1.0 — a language model can never narrow protection.
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

Do not run the judged agent on a laptop. The design turns on 15:15 and 15:54 ET
firing on time, and a machine that sleeps misses the cutoff — the exact failure
the firewall exists to prevent. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Development

```bash
make test     # 209 tests, no network required
make lint     # ruff
make check    # both
```

The `sim` broker, a synthetic Black-Scholes chain and a generated cointegrated
series mean the entire pipeline — firewall, quant stack, strategies, risk, combo
execution, telemetry — is tested offline. `.vscode/launch.json` has a debug target
for every entry point.

## Licence

MIT. See `LICENSE`.
