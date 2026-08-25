# Architecture

## The day

```
 09:35        10:00 ─────────────────── 15:00   15:15      15:45   15:54  15:55   16:00
   │            │      INTRADAY BOOK       │      │          │       │      │       │
   ▼            ▼                          ▼      ▼          ▼       ▼      ▼       ▼
overnight   acquire                    last    HARD      signal    GATE  entry   close
  exit       lock                      entry  CUTOFF     compute         ────────────▶
   │            └──────── lock held ──────────┘  │          │       │      └ OVERNIGHT
   └─ lock released                              └ confirmed flat,  │        BOOK HELD
                                                   lock released    └ lock acquired
```

One capital lock. One book holds it at a time. The handover is a
liquidate-confirm-verify sequence, not a clock tick.

## The pipeline, within a cycle

```
                    ┌──────────────────────────────────────────┐
                    │            config/*.yaml                  │
                    │  one file drives every layer below        │
                    └──────────────────┬───────────────────────┘
                                       │
  DISCOVERY (pre-market)
    most-actives + movers + news ──▶ attention score ──▶ tradability filter
                                            │                    │
                                            ▼                    ▼
                                    MACRO LENS (LLM)      candidate pool
                                    regime · flagged        (additive)
                                    legs · collar x            │
                                            │                  ▼
                                            │           find_pairs.py
                                            │           (FDR-corrected)
                                            ▼
  universe ──▶ data provider ──▶ [partner: data_enrichment] ──▶ MarketContext
    (Alpaca CLI)                                                     │
                          ┌──────────────────────────────────────────┤
                          ▼                     ▼                    ▼
                 overnight_pairs        vol_carry_condor      momentum_debit_spread
                 (portfolio mode:       (per-symbol)          (per-symbol)
                  Kalman + Huber +
                  quantile LightGBM)
                          └──────────────────┬─────────────────────┘
                                             ▼
                                    [partner: signal]
                                             ▼
                        AI assistant / critic          MCP reads, structured writes
                                             ▼
                        ══ TEMPORAL FIREWALL ══        wrong book, wrong minute -> stop
                                             ▼
                        ══ RISK ENGINE ══              deterministic; can only REJECT
                                             ▼
                                    [partner: risk veto]
                                             ▼
                    execution router  │  combo executor       single ticket │ 4-order combo
                     (idempotent)         (rollback-safe)                    with unwind
                                             ▼
                     broker: cli │ mcp │ rest │ sim
                                             ▼
                        journal.jsonl + sqlite + equity.csv
                                             ▼
                                  dashboard  /  report
```

## Why it is split this way

**Strategies are pure.** A strategy receives a `MarketContext` snapshot and returns
`TradeIdea`s. It does not size the position, does not price the order, and never
touches the broker. That is what lets the same strategy file run live and inside the
backtest replay without a single conditional.

**The risk engine is the only thing that can approve.** It is plain Python — no model,
no LLM, no discretion. It emits a signed stamp, and the execution router refuses any
ticket that does not carry one. The LLM can be wrong, unavailable, or prompt-injected
by a news headline; none of those paths reach an order.

**Four brokers, one protocol.** `rest` (alpaca-py), `cli` (the `alpaca` binary),
`mcp` (Alpaca's MCP server) and `sim` all implement `Broker`. Switching is a config
line. The hackathon asks for MCP *or* CLI usage; this repo genuinely runs on either,
and `sim` means the whole pipeline is testable with no network at all.

**Everything is registered, nothing is imported by the core.** Strategies, brokers,
data providers and partner adapters all go through `core.registry.Registry`. Adding
one is a decorator plus a config block.

## Layer map

| Package | Responsibility | Depends on |
|---|---|---|
| `oaa.config` | YAML load, merge, validate; credential resolution | — |
| `oaa.core` | domain types, registry, logging, errors | config |
| `oaa.firewall` | ET session clock, phase machine, the two-book capital lock | core |
| `oaa.discovery` | attention sources, tradability filters, candidate pool, macro lens | core, config |
| `oaa.quant` | cointegration screen, Kalman filter, gap-forecast ensemble | core |
| `oaa.options` | OCC symbols, chain filtering, structure builders | core |
| `oaa.data` | market data providers, indicators | core |
| `oaa.strategies` | signal → structure | core, options |
| `oaa.risk` | hard limits, position sizing | core |
| `oaa.execution` | pricing, idempotency, order routing, rollback combos | core, brokers |
| `oaa.brokers` | rest / cli / mcp / sim | core |
| `oaa.agents` | LLM, structured tools, the assistant, orchestrator, runner | everything |
| `oaa.partners` | sponsor technology adapters | core, config |
| `oaa.telemetry` | journal, metrics, report | core |
| `oaa.app` | read-only public dashboard | telemetry |
| `oaa.backtest` | walk-forward overnight engine, replay harness | quant, strategies, risk |

Dependencies point one way. `oaa.strategies` never imports `oaa.brokers`.

## Data flow in one cycle

1. `Orchestrator._gather_contexts` fetches market data for the union of every enabled
   strategy's universe, in a small thread pool bounded by the API rate limiter.
2. Partner adapters at `data_enrichment` decorate each `MarketContext.enrichment`.
3. Each strategy sees each context and returns zero or more `TradeIdea`s. Every idea
   carries a computed `max_loss` from the actual selected strikes.
4. Candidates are ranked by `strategy.weight × idea.confidence`.
5. The critic scores each one and writes the paragraph of reasoning that ends up in
   the journal and on the dashboard.
6. The risk engine runs its checks in cheapest-first order and returns a stamped
   `RiskVerdict`, or a rejection naming the exact rule.
7. Approved ideas go to the execution router, which builds a deterministic
   `client_order_id`, checks whether that order already exists at the broker, and
   submits — walking the limit price toward the touch if unfilled.
8. Every step, including every rejection, is appended to `journal.jsonl`.

## Autonomy

There is no approval step. `oaa run` starts `agents.runner.Runner`, which wakes on the
cycle times in `schedule.cycles`, fires late cycles if the process restarted, monitors
positions on `monitor_interval_seconds`, and survives a failed cycle without dying.

Where an LLM is configured, the runner hands the reasoning cycles to
`agents.trading_agent.TradingAgent`, which queries Alpaca over MCP and acts through
structured tools. The mechanical cycles — verification, exit, reporting — stay
deterministic: there is nothing to reason about, and a language model in that path is
only a failure mode. If the assistant errors, the deterministic handler runs instead.

## The instrumentation is the deliverable

`journal.jsonl` records the trades the system *declined* as well as the ones it took,
with the rule that stopped each one. That file is simultaneously the debugging tool,
the demo footage, and the answer to "why did it trade that".
