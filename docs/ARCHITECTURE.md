# Architecture — Options Alpha Agents (`oaa`)

One Alpaca paper account. Three books. One capital boundary between them.

```
                            ┌──────────────────────────────────┐
                            │   ALPACA PAPER ACCOUNT (judged)  │
                            └──────────────────────────────────┘
                                   ▲            ▲          ▲
        reads (MCP + CLI) ─────────┘            │          └───── writes (CLI)
                                                │
                                     the account is the only
                                     shared resource, and the
                                     firewall is what stops the
                                     books fighting over it
```

## 0. The outer frame

| Layer | What it is | Where |
|---|---|---|
| **Autonomous loop** | Self-scheduling cycle runner. No human gate, survives failed cycles and restarts. | `agents/runner.py` |
| **Orchestrator** | Wires data → strategies → critic → risk → execution → journal for one cycle. | `agents/orchestrator.py` |
| **Books** | `carry` (resident), `intraday` (transient), `opportunistic` (transient). | `strategies/` |
| **Capital firewall** | Temporal windows + a reservation/lease split of buying power. | `firewall/` |
| **Deterministic risk** | The only thing that can approve an order. Emits a signed stamp. | `risk/engine.py` |
| **Execution** | Alpaca CLI, atomic combos or rollback-safe legging. | `execution/` |
| **Telemetry** | Append-only journal, equity curve, gate rejection log, cost model. | `telemetry/` |
| **Dashboard** | Public read-only URL — the submission's Application URL field. | `app/server.py` |

Three Alpaca surfaces are used, deliberately and for different jobs:

* **MCP server** — the agent's *read* surface. Account, positions, orders, clock,
  quotes, news. Seven tools, allowlisted. `place_option_order` is **withheld**:
  an agent that can place a raw order can bypass the firewall.
* **CLI** — the *write* path. Every order is a shell command you can paste into
  a terminal and replay. Also the data path, so backtest and live read through
  one auditable route.
* **Trading API (REST)** — the `judged` profile's broker and the only path with
  websocket streaming.

---

## 1. The three books

|  | **carry** | **intraday** | **opportunistic** |
|---|---|---|---|
| Tenancy | **resident** | transient | transient |
| Edge | IV–RV premium | directional continuation | event premium mispricing |
| Direction | neutral | directional by construction | neutral |
| Option role | *is* the position | leverage + loss bound | *is* the position |
| Hold | 3–10 sessions | minutes to hours | 1–3 sessions |
| Entry window (ET) | 10:00 – 15:00 | 09:45 – 14:45 | 09:45 – 14:45 |
| Exit | 30% of max profit / DTE floor / short-strike touch | target / stop / time stop / VWAP re-cross | 50% of max profit |
| Primary risk | short gamma into a move | **spread cost** and signal decay | the print itself |
| Vol exposure | short | long | short |

The carry book and the intraday book are **uncorrelated in signal and opposite
in vol exposure**. That is the genuine portfolio argument for running both, and
it is also why the boundary between them has to be enforced rather than assumed.

---

## 2. The session, end to end

```
 09:15  discover           attention sweep + macro regime read (pre-market)
 09:30  ── bell ──         OPEN_SETTLE: nothing opens, quotes are wide
 09:45  intraday_scan      transient books lease the headroom, momentum book trades
 10:00  carry_scan         resident book reserves capital, hunts rich premium
 12:00  manage_positions   mechanical exits, routed to the owning strategy
 13:45  intraday_scan      second intraday pass
 14:30  manage_positions
 14:45  ── no new intraday entries ──   runway before the cutoff
 15:00  ── no new carry entries ──
 15:15  intraday_cutoff    HARD: cancel → liquidate TRANSIENT → poll until flat
 15:45  carry_verify       zero transient exposure, fresh Reg T, carry covered
 16:00  ── bell ──         the carry book is HELD. There is no nightly exit.
```

Plus one dated, one-off cycle checked on every poll rather than on the daily
schedule:

```
 2026-09-04 13:45 UTC   submission_flatten   close EVERYTHING, confirmed flat
 2026-09-02 20:00 UTC   vol_carry cutoff     stop opening CARRY structures only
                                             (strategy-level; the global
                                              entry_cutoff was removed 29 Aug)
```

---

## 3. The capital firewall

Two layers. Both are enforced in code and both are tested.

### Layer 1 — temporal

`firewall/clock.py` derives every boundary from one ET phase machine.

```
CLOSED → OPEN_SETTLE → INTRADAY → ACTIVE → CARRY_ONLY → WIND_DOWN
       → INTRADAY_CUTOFF → CARRY_VERIFY → CLOSED
```

`Phase.intraday_may_open` and `Phase.carry_may_open` are the only two questions
anything asks it. Boundaries must be strictly increasing and the cutoff must sit
at least 15 minutes before verification, both validated on load.

### Layer 2 — capital

```
   Reg T buying power (fresh poll, never a cached number)
        │
        ├── carry requirement ────────►  reserved FIRST, never lent out
        │   (live marks on the legs the ledger attributes to `carry`)
        │
        └── what is left  × transient_utilisation
                          ∧ transient_max_equity_pct   ──►  transient lease
```

The property, asserted directly in `tests/test_firewall.py`:

> **the resident and transient books never hold conflicting claims on the same
> capital** — for every level of carry usage, `carry_claim + transient_claim ≤
> Reg T buying power`, and the transient lease shrinks monotonically as the
> resident book grows.

### The position ledger

Once the carry book became resident, "flat" stopped being a property of the
account and became a property of a *book*. `firewall/ledger.py` maps every leg
to its owner, persisted to disk so a restart at 15:10 cannot cause the 15:15
cutoff to liquidate a multi-session iron condor.

**Unattributed legs are treated as transient.** A leg the ledger has never seen
is by definition not something the system deliberately chose to hold overnight.
Closing it is the recoverable error; carrying it into the close is not.

### What the cutoff actually guarantees

* liquidation is **confirmed, not requested** — a 200 from `close_all_positions`
  means accepted, not filled, so it polls (4 attempts, 5 s apart)
* **working orders count as "not flat"** — a resting order that fills at 15:59 is
  unexpected exposure into the close
* failure at 15:45 **disables the transient books for the following session**. A
  book that needed rescuing does not get fresh leverage the next morning.

---

## 4. Carry book — the signal stack

Four **hard** gates. Not a blended score: a score would let a rich-IV reading
paper over an earnings date, and that is the one trade this must never take.

```
  ┌ 3.1 PREMIUM ─ is vol rich? ─────────────────────────────────┐
  │  IV rank ≥ 0.70          "high for THIS name"               │
  │  AND (IV − RV) ≥ 3 vol pts  "paid more than it is moving"   │
  │  Both required. Either alone is a false-positive generator. │
  └──────────────────────────────┬──────────────────────────────┘
  ┌ 3.2 TREND ─ is it going nowhere? ──────────────────────────┐
  │  ADX ≤ 25  AND  |trend strength| ≤ 0.60                    │
  │  The SAME measurement that fires momentum_debit_spread,    │
  │  which is what makes the two mutually exclusive.           │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ 3.3 EVENT ─ hard exclusions ───────────────────────────────┐
  │  earnings inside the expiry window   → excluded            │
  │  ex-dividend with a short call       → excluded            │
  │  No scoring. IV is elevated BECAUSE of the event; that     │
  │  premium is fair, and selling it is selling the one thing  │
  │  the market got right.                                     │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ 3.4 MACRO LENS ─ shared or idiosyncratic? ─────────────────┐
  │  sector-wide IV elevation, no name catalyst  → SHARED, sell│
  │  name repricing on its own news              → VETO        │
  │  The question a z-score cannot answer, so it is the one    │
  │  place a language model earns its place.                   │
  └──────────────────────────────┬─────────────────────────────┘
                                 ▼
                    structure selection
       neutral → iron condor | mild lean → credit vertical
       term-structure kink → calendar (front 7 / back 14–21)
                                 ▼
                    cost gate: round-trip spread ≤ 20% of credit
                                 ▼
                    risk engine → execution → ledger → journal
```

**Why 7–14 DTE, not the conventional 20–45.** Theta is steeply non-linear into
expiry. At 30 DTE a five-session hold captures ~20–25% of the structure's
remaining decay; at 10 DTE it captures the majority. The judged window is the
constraint and the structure's life has to fit inside it. The cost is gamma —
this is a deliberate trade of tail risk for realised P&L inside the window,
right for seven days and wrong for a live book. Three controls offset it
partially, not completely: 10–16Δ strike selection, the position-count cap, and
the defensive exit on a short-strike touch.

**Why a 30% profit target, not 50%.** A target that is rarely reached leaves
positions open at judging, which makes the P&L an unrealised mark on wide option
spreads — noisy, unflattering, and marked at a mid the account could not trade
at. 30% converts decay into realised gains inside the window and produces more
*closed* trades. Win rate, average hold and per-trade attribution only exist for
closed trades, and the equity curve becomes a curve rather than one flat line
with a single mark at the end. The cost is EV per trade; over a longer horizon
50% is better.

---

## 5. Intraday book — the signal stack

Stated honestly: **a momentum strategy expressed through options**. The option
is leverage and defined risk, not the source of edge.

### The hard constraint that shapes everything

Target profit is 5–15% of premium — $10–30 on a $2.00 contract.

| Quote width | Round-trip cost | % of a $20 target |
|---|---|---|
| $0.01 (SPY ATM) | $2 | 10% |
| $0.02 | $4 | 20% |
| $0.05 | $10 | **50%** |
| $0.10 (typical single name) | $20 | **100% — no edge survives** |

**Index products only.** Not a preference — arithmetic. This is a two-symbol
system because those are the only two symbols where the sums work.

### Three indicators, three different questions

The critical design decision is that **only VWAP has an opinion on direction**.
RSI and Bollinger *position* are mean-reversion measures and would contradict a
VWAP momentum trigger on every single signal — price breaking above VWAP is
simultaneously "momentum, buy" and "overbought, fade". A naive consensus gate
across all three either never fires or fires incoherently.

```
  ┌ 3.1 MOMENTUM ──────────────────────────────────────────────┐
  │  VWAP           TRIGGER  which direction, and is now it?   │
  │                 + volume z-score BY TIME-OF-DAY BUCKET     │
  │                   (09:45 volume ≠ 12:30 volume)            │
  │                 + persistence: 2 bars, not a single spike  │
  │  Bollinger      FILTER   is this real, or is it chop?      │
  │   *WIDTH*                width is a volatility-regime read │
  │                          and is direction-agnostic;        │
  │                          position would fight VWAP         │
  │  RSI            VETO     has it already run too far?       │
  │                          one-sided, 80/20 not 70/30 —      │
  │                          strong runs carry elevated RSI    │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ 3.2 CATALYST ─ the non-generic layer ──────────────────────┐
  │  news 0.50   the only factor with a MECHANISM attached     │
  │  breadth 0.35  load-bearing on an index universe           │
  │  volume 0.15   participation, independent of the bars      │
  │  Rank-normalised WITHIN each source before blending.       │
  │  No catalyst → VETO. A VWAP cross with no reason behind    │
  │  it is drift, and drift reverts.                           │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ 3.3 SPREAD ─ mandatory ────────────────────────────────────┐
  │  (ask−bid)/mid ≤ 2%  AND  round trip ≤ 30% of the target   │
  │  Expect this to reject more than any other gate. That is   │
  │  the finding, not a bug.                                   │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ 3.4 TIME OF DAY ───────────────────────────────────────────┐
  │  09:45–14:45, lunch 11:30–13:30 skipped                    │
  └──────────────────────────────┬─────────────────────────────┘
                                 ▼
            4. SURFACE-AWARE SELECTION (options/selection.py)
     small move + cheap IV   → ATM 0–1 DTE       max gamma, cheap
     small move + rich IV    → ATM longer dated  less burn if it stalls
     large move + cheap IV   → slightly OTM      convexity per dollar
     large move + rich IV    → DEBIT VERTICAL    caps the expensive wing
     IV rank ≥ 0.85          → NO TRADE          already priced in
                                 ▼
        exits: 10% target / 15% stop / 20-min time stop / VWAP re-cross
        (the stop is WIDER than the target on purpose — premium is noisy
         and a tight stop is hit by spread flicker alone. Breakeven hit
         rate ≈ 60%, computed and displayed against the actual.)
```

The polarity of the macro lens **inverts** relative to the carry book:

|  | carry (short vol) | intraday (long vol) |
|---|---|---|
| shared catalyst | safe to sell premium | **tradable** — a broad move continues |
| idiosyncratic | veto — fat tail | barely applies on an index universe |
| no catalyst | fine, quiet is the point | **veto** — the move has no mechanism |

---

## 6. Opportunistic book

Dormant by default, and may not fire at all inside the judged week. **That is an
acceptable outcome and is reported as such** — an agent that correctly stands
down is demonstrating judgement.

For a dated, scheduled catalyst, the implied move priced into the front expiry
(ATM straddle / spot) is compared against the distribution of *realised* moves
for that same recurring event. Implied materially above realised → sell it as a
defined-risk condor on an index proxy. In line or below → stand down, which is
the expected outcome most of the time.

Both the calendar (`config/macro_events.yaml`) and the historical realised
distribution are **committed files, not live fetches**. A live dependency that
fails on the morning of the print fails at exactly the moment it was needed.

---

## 7. The order path

```
  strategy emits TradeIdea (priced, max_loss computed from the actual strikes)
        │
        ├─► cost model attaches modelled round-trip cost   telemetry/costs.py
        │
        ├─► LLM critic  scores, writes reasoning  ── CANNOT authorise
        │
        ├─► RISK ENGINE  ─ firewall FIRST, then limits ─ the ONLY approver
        │      0 firewall (wrong book, wrong minute → categorical refusal)
        │      1 session state / halts
        │      2 defined risk, computable max loss, 1–4 legs, no dupes
        │      3 portfolio shape: position caps, per-underlying, per-day
        │      4 sizing FROM MAX LOSS, never from capital
        │      5 aggregate exposure
        │      6 cash buffer
        │      7 time-of-day window
        │      → emits a SIGNED STAMP
        │
        ├─► partner adapters may VETO (never approve)
        │
        └─► EXECUTION  refuses any ticket without a stamp
               atomic multi-leg where the venue routes combos
               otherwise legged: LONG legs first, unwind in reverse
               deterministic client_order_id + pre-submit existence check
               limit at mid with a widening ladder, never market orders
        │
        └─► ledger.register(idea, book)   ← what makes 15:15 correct
        └─► journal: the decision, the verdict, the rejection reason
```

---

## 8. Instrumentation

Built from the first commit, not retrofitted for the deck.

* **Equity curve**, per-trade log, win rate, max drawdown
* **Gate-by-gate rejection log** (`oaa gates`) — which gate vetoed each candidate
  and every metric it measured. The highest-value artefact for judging: it shows
  an agent *reasoning*, not just an agent trading.
* **Modelled cost per trade**, logged separately from P&L — gross, cost, net.
  Paper charges no fees and fills at mid, which flatters the intraday book
  severely because spread cost *is* its primary loss mechanism.
* **Breakeven hit rate** computed from the target/stop asymmetry and displayed
  against the actual one.
* **Macro lens output archived per cycle**: regime, flagged symbols, themes.

---

## 9. Honest limitations — these belong in the deck

1. **The intraday momentum layer is conventional.** VWAP and volume confirmation
   ship with every charting platform. Any edge in a bare VWAP cross was
   arbitraged long ago. The originality is the catalyst gate and the
   surface-aware option selection, not the trigger.
2. **Paper fills flatter the intraday book severely.** Fills at mid, no queue
   position, no partials — and spread cost is exactly what paper does not
   simulate. Modelled cost is reported alongside raw P&L so the gap is visible.
3. **Five sessions is not a sample.** With gates that reject most candidates,
   expect perhaps 10–20 intraday trades total. That cannot distinguish edge from
   luck in either direction.
4. **7–14 DTE short premium is a deliberate tail-risk trade** for a seven-day
   window. It would be the wrong call for a live book.

Stating these scores better than concealing them. A judge who spots an
unacknowledged paper-fill advantage forms a much worse impression than one who
reads that the team modelled it.

---

## 10. Module map

| Module | Responsibility | Depends on |
|---|---|---|
| `oaa.core` | domain types, registry, errors, logging | — |
| `oaa.config` | YAML → typed schema, profile overlays, env | core |
| `oaa.brokers` | `Broker` protocol: CLI, MCP, REST, sim | core, config |
| `oaa.data` | market context, indicators (daily **and** intraday) | core, config |
| `oaa.options` | chain filtering, strike selection, structures, **selection** | core |
| `oaa.signals` | catalyst engine, macro calendar, cost/time gates | core |
| `oaa.strategies` | `vol_carry`, `intraday_momentum`, `event_premium`, … | options, signals, data |
| `oaa.risk` | the deterministic engine and sizing | core, config, firewall |
| `oaa.firewall` | phase machine, capital lock, position ledger | core |
| `oaa.execution` | router, combo executor, pricer | brokers, core |
| `oaa.discovery` | attention sweep, tradability filter, macro lens | data, agents |
| `oaa.agents` | orchestrator, runner, critic, tool belt, prompts | everything |
| `oaa.telemetry` | journal, metrics, report, **cost model** | core |
| `oaa.partners` | sponsor-technology adapters at seven pipeline stages | core |
| `oaa.app` | the public dashboard | telemetry |
