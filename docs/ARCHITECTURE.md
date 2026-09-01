# Architecture — Eventus / Options Alpha Agents (`oaa`)

One Alpaca paper account. Three books that trade, one capital boundary between two of
them, and a fourth book that is enabled and cannot fire.

```
                            ┌──────────────────────────────────┐
                            │   ALPACA PAPER ACCOUNT (judged)  │
                            │            PA3TSH9YTFJL          │
                            └──────────────────────────────────┘
                                   ▲            ▲          ▲
        reads (MCP + CLI) ─────────┘            │          └───── writes (CLI / REST)
                                                │
                                     the account is the only
                                     shared resource, and the
                                     firewall is what stops the
                                     books fighting over it
```

Companion documents: [`PIPELINE.md`](PIPELINE.md) for one cycle stage by stage,
[`FIREWALL.md`](FIREWALL.md) for the capital boundary, the three `STRATEGY-*.md` files
for each book in full, and [`OAA-architecture.pdf`](OAA-architecture.pdf) for the
print-ready version of this document (source: `OAA-architecture.html`).

## 0. The outer frame

| Layer | What it is | Where |
|---|---|---|
| **Autonomous loop** | Self-scheduling cycle runner. No human gate; re-fires any cycle whose time has passed, so a crash at 15:10 cannot skip the 15:15 cutoff. | `agents/runner.py` |
| **Orchestrator** | Wires data → strategies → critic → risk → execution → journal for one cycle. | `agents/orchestrator.py` |
| **Books** | `carry` (resident), `intraday` (transient), `events` (size-bounded), `opportunistic` (blocked). | `strategies/` |
| **Capital firewall** | Temporal windows + a reservation/lease split of buying power. | `firewall/` |
| **Deterministic risk** | The only thing that can approve an order. Emits a signed stamp. | `risk/engine.py` |
| **Execution** | Atomic combos by default, rollback-safe legging as a fallback. | `execution/` |
| **Telemetry** | Append-only journal, equity curve, gate rejection log, cost model, daily report. | `telemetry/` |
| **Dashboard** | Operator build and a public read-only build from one `main()`. | `app/dashboard.py`, `public_dashboard.py` |

Three Alpaca surfaces are used, deliberately, for different jobs:

* **MCP server** — the agent's *read* surface. Account, positions, orders, clock,
  quotes, news. Seven tools, allowlisted. `place_option_order` is **withheld**: an
  agent that can place a raw order can bypass the firewall.
* **CLI** — the *write* path, and the backtest data path (`data.provider: cli`).
  Every order is a shell command you can paste into a terminal and replay.
* **Trading API (REST)** — the `judged` profile's broker (`broker.primary: rest`) and
  the only path with websocket streaming.

---

## 1. The books

|  | **carry** | **intraday** | **events** | **opportunistic** |
|---|---|---|---|---|
| Strategy | `vol_carry` | `intraday_momentum` | `earnings_event_directional` | `event_premium` |
| Tenancy | **resident** | transient | **not a firewall tenant** | transient |
| Edge | IV–RV premium | directional continuation | implied vs realised reaction | event premium mispricing |
| Direction | neutral | directional by construction | tilt only | neutral |
| Hold | 3–10 sessions, 7–14 DTE | minutes, 0–2 DTE | one night | 1–3 sessions |
| Window (ET) | 10:00 – 15:00 | 09:45 – 14:45, flat 15:10 | arm 15:50, flat 09:45 | 09:45 – 14:45 |
| Universe | 14 index & sector ETFs | 8 index ETFs | confirmed reporters | SPY, QQQ |
| Vol exposure | short | long | either, by divergence | short |
| Primary risk | short gamma into a move | **spread cost**, signal decay | the gap | the print itself |
| Status | trading | trading | trading | **cannot open** |

Carry and intraday are **uncorrelated in signal and opposite in vol exposure**. That is
the genuine portfolio argument for running both, and it is also why the boundary
between them has to be enforced rather than assumed.

**The events book is not a firewall tenant.** A book whose entire life is one overnight
hold does not fit the intraday/carry tenancy model, so it is bounded by size instead:
1.2% per name, 4% shared across everything opened that night, and an absolute ceiling
of 10 contracts that does not scale with equity.

**`event_premium` is enabled in config and cannot open a position.** `_transient_scan`
always acquires the capital lease as `INTRADAY`, so the opportunistic book's `may_open`
check always fails. It is documented as enabled because that is what the config says,
not because it trades.

---

## 2. The session, end to end

`config/default.yaml` → `schedule.cycles`, 46 cycles, America/New_York. Every one of
them is a firewall boundary.

```
 04:00-16:00  events_watch      HOURLY, 13 polls. The wire on names reporting this week
 09:15        discover          attention sweep + macro regime read (pre-market)
 09:30        ── bell ──        OPEN_SETTLE: nothing opens, quotes are wide
 09:45        events_flatten    close last night's spreads into the IV crush, before
                                the day books compete for the same buying power
 10:00-11:15  intraday_scan     every 15 minutes
 10:00        carry_scan        resident book reserves capital, hunts rich premium
 11:30-15:10  manage_positions  11 passes: target, stop, DTE floor, defensive close
 13:30-14:45  intraday_scan     every 15 minutes (lunch 11:30-13:30 skipped by the
                                strategy's own time gate)
 13:50, 14:40 carry_scan        two further passes
 14:45        ── no new intraday entries ──
 15:00        ── no new carry entries ──
 15:15        intraday_cutoff   HARD: cancel → liquidate TRANSIENT → poll until flat
 15:45        carry_verify      zero transient exposure, fresh Reg T, carry covered
 15:50        events_arm        open tonight's earnings spreads, one night only
 16:00        ── bell ──        the carry book is HELD. There is no nightly exit.
 16:10/16:20  report            performance report, then the LLM-written daily report
```

**Twelve intraday scans, not two.** A VWAP cross is an event that lasts minutes: two
scans a day observed roughly 30 of 390 session minutes and required the cross to land
inside one of them. These times mirror `backtest.session_times_et` exactly — when they
diverged, the live agent saw a sixth of what the backtest was tuned against.

> **Arm time.** `config/strategies/earnings_event.yaml` sets `schedule.arm_time: 15:45`;
> the runner fires `events_arm` at **15:50**, and the runner is what executes.
> `no_entry_after: 15:55` bounds it either way.

Two dated controls fire from the clock rather than from the daily schedule:

```
 2026-09-02 20:00 UTC   vol_carry entry cutoff   stop opening CARRY structures only
                                                 (strategy-level; the global
                                                  entry_cutoff was removed 29 Aug)
 2026-09-04 13:45 UTC   submission_flatten       close EVERYTHING, then refuse every
                                                 book — checked on EVERY runner poll
```

13:45 UTC is 09:45 ET, and the intraday window opens at 09:45. **4 September yields no
new trades; the tradeable sessions are 2 and 3 September.**

---

## 3. The capital firewall

Two layers, both enforced in code and both tested.

### Layer 1 — temporal

`firewall/clock.py` derives every boundary from one ET phase machine.

```
CLOSED → OPEN_SETTLE → INTRADAY → ACTIVE → CARRY_ONLY → WIND_DOWN
       → INTRADAY_CUTOFF → CARRY_VERIFY → CLOSED
```

`Phase.intraday_may_open` and `Phase.carry_may_open` are the only two questions anything
asks it. Boundaries must be strictly increasing and the cutoff must sit at least 15
minutes before verification, both validated on load.

### Layer 2 — capital

```
   Reg T buying power (fresh poll, never a cached number)
        │
        ├── carry requirement ────────►  reserved FIRST, never lent out
        │   live marks on the legs the ledger attributes to `carry`,
        │   × carry_margin_cushion 1.25, capped at carry_max_equity_pct 0.50
        │
        └── what is left  × transient_utilisation 0.50
                          ∧ transient_max_equity_pct 0.15   ──►  transient lease
```

The property, asserted directly in `tests/test_firewall.py`:

> **the resident and transient books never hold conflicting claims on the same
> capital** — for every level of carry usage, `carry_claim + transient_claim ≤ Reg T
> buying power`, and the transient lease shrinks monotonically as the resident book
> grows.

The transient lease is what is *left*, not a share of the whole.

### The position ledger

Once the carry book became resident, "flat" stopped being a property of the account and
became a property of a *book*. `firewall/ledger.py` maps every leg to its owner,
persisted to `runs/position_ledger.json` so a restart at 15:10 cannot cause the 15:15
cutoff to liquidate a multi-session iron condor.

**Unattributed legs are treated as transient.** A leg the ledger has never seen is by
definition not something the system deliberately chose to hold overnight. Closing it is
the recoverable error; carrying it into the close is not.

### What the cutoff actually guarantees

* liquidation is **confirmed, not requested** — a 200 from `close_all_positions` means
  accepted, not filled, so it polls (4 attempts, 5 s apart)
* **working orders count as "not flat"** — a resting order that fills at 15:59 is
  unexpected exposure into the close
* **residual transient *positions* at 15:45 disable the transient books for the
  following session.** This used to trigger on any open order account-wide, and
  `open_orders` is account-wide — so one resting carry entry at the sign-off cost the
  next day's intraday book an entire session. A working order is now cancelled by the
  emergency cutoff and noted, without the ratchet.

---

## 4. Carry book — the signal stack

Four **hard** gates. Not a blended score: a score would let a rich-IV reading paper over
an earnings date, and that is the one trade this must never take.

```
  ┌ 4.1 PREMIUM ─ is vol rich? ─────────────────────────────────┐
  │  IV rank ≥ 0.35          "high for THIS name" — a REGIME    │
  │                          filter, nothing more               │
  │  AND (IV − RV) ≥ 3 vol pts  "paid more than it is moving"   │
  │  The SPREAD is the edge. Both required; either alone is a   │
  │  known false-positive generator.                            │
  └──────────────────────────────┬──────────────────────────────┘
  ┌ 4.2 TREND ─ is it going nowhere? ──────────────────────────┐
  │  ADX ≤ 25  AND  |trend strength| ≤ 0.60                    │
  │  The SAME measurement that fires momentum_debit_spread,    │
  │  which is what makes the two mutually exclusive. A test    │
  │  asserts they never co-fire.                               │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ 4.3 EVENT ─ hard exclusions ───────────────────────────────┐
  │  earnings inside the expiry window   → excluded            │
  │  ex-dividend under a short call      → excluded            │
  │  No scoring. IV is elevated BECAUSE of the event; that     │
  │  premium is fair, and selling it is selling the one thing  │
  │  the market got right.                                     │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ 4.4 MACRO LENS ─ shared or idiosyncratic? ─────────────────┐
  │  sector-wide IV elevation, no name catalyst  → SHARED, sell│
  │  name repricing on its own news              → VETO        │
  │  The question a z-score cannot answer, so it is the one    │
  │  place a language model earns its place.                   │
  └──────────────────────────────┬─────────────────────────────┘
                                 ▼
              structure: iron condor, 0.25Δ shorts,
              wing width 1.5% of spot, credit/width ≥ 0.15
                                 ▼
              cost gate: round-trip spread ≤ 20% of credit
                         relative spread ≤ 10%
                                 ▼
              risk engine → execution → ledger → journal
```

**The IV-rank floor was 0.70, and it rejected 304 of 304 replayed candidates.** Every
threshold in this document was moved at least once, and always by measurement. A gate
that rejects everything is not conservative — it is a book that does not exist. Read the
mix with `oaa backtest --why 15`, which groups by reason and normalises the numbers
within each one.

**Wing width is a fraction of spot** (1.5%), not a flat point count, because the
universe spans a ~$50 ETF to a ~$600 one. On the cheapest names 1.5% can still fall
below one strike increment, which is why XLF sometimes declines with *"no listed strike
sits outside the short strikes"* — safe, but it means XLF contributes less than it
should. A floor of one strike increment is the fix.

### Exits, and the arithmetic that sets them

Breakeven hit rate = `stop ÷ (target + stop)`. It is decided before any trade is taken.

| | needed, before spread | with the 20% cost ceiling |
|---|---|---|
| **Was** — 30% target / 2.0× stop | 87.0% | 95.7% — the gate was admitting trades the arithmetic said would lose |
| **Now** — 50% target / 1.5× stop | **75.0%** | 85% — three points of margin against an observed 88% |

Checked in this order, every cycle:

| Exit trigger | Action |
|---|---|
| `$450` hard dollar stop | close — checked first |
| 50% of max profit | close |
| loss reaches 1.5× credit | close |
| DTE floor (3) | close regardless of P&L — gamma rises sharply into expiry |
| underlying touches a short strike | close the tested side |
| macro lens flags the name mid-hold | close at the next open |
| submission flatten | close everything, unconditionally |

Re-entry cooldown is 1440 minutes: one entry per underlying per session for this book.

**A stop set above the loss distribution is decoration.** The dollar stop was $900 and
never fired — the six largest losses were $780–868, all sitting just underneath it, and
every one exited on the DTE floor instead, which realises whatever has accumulated
rather than capping it.

**Why 7–14 DTE, not the conventional 20–45.** Theta is steeply non-linear into expiry.
At 30 DTE a five-session hold captures ~20–25% of the structure's remaining decay; at 10
DTE it captures the majority. The judged window is the constraint and the structure's
life has to fit inside it. The cost is gamma — a deliberate trade of tail risk for
realised P&L inside the window, right for seven days and wrong for a live book. Three
controls offset it partially, not completely: 0.25Δ short strikes, the position-count
cap, and the defensive exit on a short-strike touch.

---

## 5. Intraday book — the signal stack

Stated honestly: **a momentum strategy expressed through options**. The option is
leverage and defined risk, not the source of edge.

### Eight vetoes in a row is not a strategy

Entry once required eight conjunctive conditions, each passing roughly 70% of
candidates: 0.7⁸ ≈ 6%. Measured over 864 candidates in 9 sessions, **424 died on the
volume gate alone and none survived the whole chain.** Every time one gate was loosened
the next simply became the wall.

The fix is two hard gates and a vote:

```
  ┌ HARD · TRIGGER ────────────────────────────────────────────┐
  │  Session VWAP cross, or price on the band within 2.0× the  │
  │  session's usual dispersion. Searched over the last 15     │
  │  one-minute bars — exactly one polling gap, so a cross     │
  │  cannot fall between two cycles unseen.                    │
  │  It stays hard because it decides DIRECTION.               │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ HARD · SPREAD ─────────────────────────────────────────────┐
  │  (ask−bid)/mid ≤ 4%  AND  round trip ≤ 30% of the target   │
  │  It stays hard because it is economics, not evidence.      │
  │  The ceiling was 2% — below the tightest quote in the      │
  │  universe (2.59%), so the gate was rejecting the market    │
  │  rather than selecting within it.                          │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ VOTES · three of up to seven ──────────────────────────────┐
  │  volume z ≥ 0.5 in the same TIME-OF-DAY bucket             │
  │  persistence: 2 bars, not a single-bar spike               │
  │  Bollinger WIDTH rising — a volatility-regime read         │
  │  higher timeframe (60 min) agreement                       │
  │  term-structure slope inside −0.10 … 0.25                  │
  │  RSI not at 80/20                                          │
  │  catalyst score: news 0.50 · breadth 0.35 · volume 0.15    │
  │  needed = min(3, possible)                                 │
  └──────────────────────────────┬─────────────────────────────┘
  ┌ TIME OF DAY ───────────────────────────────────────────────┐
  │  09:45–14:45, lunch 11:30–13:30 skipped                    │
  └──────────────────────────────┬─────────────────────────────┘
                                 ▼
            SURFACE-AWARE SELECTION (options/selection.py)
     small move + cheap IV   → ATM 0–1 DTE       max gamma, cheap
     small move + rich IV    → ATM longer dated  less burn if it stalls
     large move + cheap IV   → slightly OTM      convexity per dollar
     large move + rich IV    → DEBIT VERTICAL    caps the expensive wing
     IV rank ≥ 0.85          → NO TRADE          already priced in
                                 ▼
        exits: 10% target / 15% stop / 20-min time stop / flat 15:10
```

**"Unmeasurable" is not "failed".** With no time-of-day volume baseline yet, volume
simply does not vote rather than blocking the trade. Demoting the catalyst to a vote
exposed a second bug worth recording: `CatalystEngine.gate()` returned `ok()`
immediately when `required=False`, *before measuring anything* — so the catalyst always
voted yes and the fifth confirmation was free. It now always measures, publishes
`metrics["confirmed"]`, and only `required` decides whether a failure is a veto or a
lost vote.

### Three indicators, three different questions

Only VWAP has an opinion on direction. RSI and Bollinger *position* are mean-reversion
measures and would contradict a VWAP momentum trigger on every signal — price breaking
above VWAP is simultaneously "momentum, buy" and "overbought, fade". Width, not
position: band width is direction-agnostic. RSI is one-sided at 80/20 rather than 70/30,
because strong runs carry elevated RSI for long stretches and a tight threshold rejects
exactly the trades this book exists to take.

### The hard constraint that shapes everything

Target profit is 10% of premium — $10–30 on a $2.00 contract.

| Quote width | Round-trip cost | % of a $20 target |
|---|---|---|
| $0.01 (SPY ATM) | $2 | 10% |
| $0.02 | $4 | 20% |
| $0.05 | $10 | **50%** |
| $0.10 (typical single name) | $20 | **100% — no edge survives** |

**Index products only.** Not a preference — arithmetic. Eight ETFs: SPY, QQQ, IWM, DIA,
XLF, XLE, TLT, GLD.

The stop is *wider* than the target on purpose — option premium is noisy and a tight
stop is hit by spread flicker alone — which implies a breakeven hit rate near 60%,
computed and displayed against the actual.

> **Known and still live:** `exit_on_vwap_recross` is `true` in
> `config/strategies/intraday_momentum.yaml`. The A/B measured that rule at 0 wins in 55
> attempts.

### The macro lens inverts

|  | carry (short vol) | intraday (long vol) |
|---|---|---|
| shared catalyst | safe to sell premium | **tradable** — a broad move continues |
| idiosyncratic | veto — fat tail | barely applies on an index universe |
| no catalyst | fine, quiet is the point | a lost vote |

---

## 6. Events book

`src/oaa/strategies/events/` — its own package, its own params file, its own capital
book, and its own CLI verbs. It borrows only the `TradeIdea`/`Leg` contract, the
structure builders, the `Broker` port, `RiskEngine`, `ExecutionRouter` and the `Journal`,
so every order still carries a signed risk stamp and lands in the decision log.

**Its entry condition is a date.** The recurring failure of the other two books is that
the threshold is never crossed and nothing opens. Broadcom reports on 2 September
whether or not any indicator agrees; the gate opens on the calendar and everything after
it decides whether the trade is worth taking rather than whether to look.

| Module | Does |
|---|---|
| `calendar.py` | Featherless proposes next week's reporters; `config/events/earnings_calendar.json` confirms each one. Unverified proposals are logged, never armed |
| `volscreen.py` | Prices the ATM straddle in the expiry **containing** the print, sets it against the last four actual reactions, ranks top 10 by divergence. Implied move ≥ 3%, relative spread ≤ 25% |
| `watch.py` | Hourly polls from three days out: Alpaca news + StockTwits, deduplicated by hash of timestamp and text. Salience ≥ 0.35, aged out at 10 days, capped at 40 notes |
| `sentiment.py` | Evidence pack, sanitised and budget-capped |
| `direction.py` | One call per name: direction, confidence, and the evidence cited. Abstention is a valid and expected answer |
| `sizing.py` | Confidence maps linearly onto size, bounded per trade (1.2%), per night (4%) and by an absolute cap (10 contracts) |
| `strategy.py` | Builds the structure the divergence calls for, behind three interlocks |
| `engine.py` | `arm()` before the close, `flatten()` the next morning |

**The model proposes; a file confirms.** Featherless serves open-weight models whose
weights were frozen months ago. Asked to list next week's reporters, one answers
fluently and sometimes wrongly — and a wrong date is not a bad trade, it is a position
opened against no event at all. `CPRT` ships `confirmed: false` for exactly this reason
and will not arm.

**The expiry must contain the print.** `screen_one` refuses any expiry earlier than the
reaction session: the cheapest guard against the most expensive mistake available here.

### The correction that mattered

A directional structure bought at a fair-to-rich implied move, on a direction call no
better than a coin flip, returns minus the round trip in expectation. That is arithmetic,
not a sample-size problem. So the **expression follows the sign of the divergence**
(implied move ÷ median realised reaction over the last four prints):

| Ratio | Structure | Where the edge is |
|---|---|---|
| **≥ 1.35** rich | iron condor, both shorts *outside* the implied move | the overpricing itself |
| 0.80 – 1.35 fair | **no trade** | none measurable — and this band is where the book used to do all of its trading |
| **≤ 0.80** cheap | debit vertical, in the called direction | the underpricing |

Thresholds sit well clear of 1.0 because the ratio is computed off four prints — a name
at 1.10 is inside its own sampling error.

**Strikes by distance, not delta** — shorts at spot ± 1.0 × implied move. The
front-weekly surface across a print is too deformed for delta to stand in for distance:
a 16-delta strike lands inside or outside the priced move depending only on how the
wings are skewed. Where the shorts sit relative to that move *is* the thesis, so a
coarse strike ladder that snaps one back inside (`min_shorts_clearance: 0.85`) is
declined, not quietly downgraded.

**The direction call is a tilt, not the thesis.** It picks the side of the debit
vertical, and on a condor it pushes the threatened short further out — never pulls the
other one in, because collecting more premium by moving a short inside the implied move
surrenders the one property the structure is built on.

### What bounds the model

| Control | Value | Why |
|---|---|---|
| watch triage model | `Qwen3-8B`, ~40 calls/day | narrow, schema-bound, usually right to say "immaterial" |
| direction model | `Qwen3-32B`, once per name | running the big model on triage spends the direction budget on noise |
| confidence floor | 0.55, and it sets the size | a marginal call trades small |
| evidence | required | a confident call citing nothing is a guess with a number attached |
| abstention rate | journalled, warns at zero | a model that never declines is not filtering |
| injection surface | contained, not eliminated | third-party text is fenced, stripped of control characters, truncated, schema-parsed and clamped; directive-like text is flagged and logged |

**No new items means no model call.** Cost scales with how much news arrives, not with
how often the loop runs — which is what makes 13 hourly polls affordable.

---

## 7. Opportunistic book

Dormant, and currently **unable to open a position** (§1). Kept in the tree because the
mechanism is sound and the fix is one lease call.

For a dated, scheduled catalyst, the implied move priced into the front expiry (ATM
straddle / spot) is compared against the distribution of *realised* moves for that same
recurring event. Implied materially above realised → sell it as a defined-risk condor on
an index proxy. In line or below → stand down, which is the expected outcome most of the
time.

Both the calendar (`config/macro_events.yaml`) and the historical realised distribution
are **committed files, not live fetches**. A live dependency that fails on the morning of
the print fails at exactly the moment it was needed.

---

## 8. The order path

```
  strategy emits TradeIdea (priced, max_loss computed from the actual strikes)
        │
        ├─► cost model attaches a modelled round trip     telemetry/costs.py
        │   OCC clearing, ORF, FINRA CAT both sides, TAF and SEC on sells,
        │   exchange fees, margin interest — BEFORE anything judges it, so a
        │   rejection still carries what the trade would have cost
        │
        ├─► LLM critic  scores, writes reasoning  ── may DECLINE, CANNOT approve
        │
        ├─► RISK ENGINE ── the ONLY approver ── fifteen checks, fixed order:
        │      firewall → halted → market_open → undefined_risk → unknown_risk →
        │      leg_count → duplicate_legs → max_positions → max_new_per_day →
        │      duplicate_structure → reentry_cooldown → concentration → sizing →
        │      portfolio_risk → cash
        │      → emits a SIGNED STAMP
        │
        ├─► partner adapters may VETO (never approve), at seven hook points
        │
        └─► EXECUTION  refuses any ticket without a stamp
               atomic combo by default — one spread crossed, not four
               deterministic client_order_id + pre-submit existence check
               judged profile posts at the FAR TOUCH (limit_price_ratio 1.0)
        │
        └─► ledger.register(idea, book)   ← what makes 15:15 correct
        └─► journal: the decision, the verdict, the rejection reason
```

**`duplicate_structure` and `reentry_cooldown` exist because brokers net identical
option symbols.** Re-opening the same condor doubles one position rather than creating a
second, so every count-based limit was blind to it. The first refuses a structure whose
every leg is already held on the same side; the second (60 min, per symbol+strategy)
catches the near-duplicate once spot has moved enough to change the strikes.

**Sizing is from max loss, not from capital.** A structure without a computable
`max_loss` is refused outright (`risk.allow_undefined_risk: false`), which is what makes
a stray undefined-risk idea impossible to execute by accident. A quantity of 1 from a
strategy means "no opinion, size me by the risk budget" — not "exactly one".

**Portfolio Greek caps** (`max_net_delta`, `max_net_vega`) are checked across the whole
account, not per trade. A greek block of *exact zeros* is treated as **missing**, not as
a measurement: the indicative feed serves those, and they turned three caps green on a
book that had never been measured. Unmeasurable exposure is recorded and the cycle
continues — deliberately.

**On the judged profile the limit posts at the far touch.** At the 0.5 default with the
chase disabled, `max_steps` is 1 and `step` is always 0, so every order rested half a
spread from the touch with no second attempt and simply never filled. The chase itself
stays off: it cancels without checking the cancel succeeded, then resubmits under a new
`client_order_id` — a race that can double the risk-approved size.

**Legging is a fallback, not the default.** `multileg_mode: atomic` on judged. The
legged path treats a submit acknowledgement as a fill, so a failure later in the sequence
can send unwind orders against legs that never filled. It must not be enabled before the
deadline.

### Marking and exits

Positions are re-marked every cycle and `should_exit` is mechanical — no LLM in the exit
path. **Every leg of a structure is priced on one surface.** `reprice` decides per
contract whether to use a real bar or the model; in a condor the shorts trade and the
wings often do not, so a stressed session marked the short at a real elevated print
against a wing on a calm modelled vol. The vertical's value is then not bounded by its
strike width and the structure stops being defined-risk. Where provenance is mixed,
every leg is now re-priced from the model anchored on the vol the real prints imply, and
`_bounded_gross` clamps the mark to the structure's own arithmetic. Both are counted per
run as `mixed_surface_marks` and `risk_bound_clamps` — **the second should be zero.**

---

## 9. Instrumentation

Built from the first commit, not retrofitted for the deck.

* **Gate-by-gate rejection log** (`oaa gates`, `oaa backtest --why`) — which gate vetoed
  each candidate and every metric it measured. **72,753 rejections** across the Feb–Aug
  replay against 441 trades taken. The highest-value judging artefact: it shows an agent
  declining trades and the number that caused it.
* **Modelled cost per trade**, logged separately from P&L — gross, cost, net — with the
  spread charged *inside* the fills and reported beside them.
* **Equity curve**, per-trade log, win rate, max drawdown.
* **Breakeven hit rate** computed from the target/stop asymmetry and displayed against
  the actual one.
* **Macro lens output archived per cycle**: regime, flagged symbols, themes.
* **Daily report** (`oaa daily-report`) — an LLM-written end-of-day report *and* an
  evaluator score for it. The only page that says what the system thinks is wrong with
  itself, and it is published unguarded on both dashboard builds.
* **Watch dossiers** — dated pre-print notes per name, published to the public Events
  tab. Retired dossiers are deliberately never published: post-print commentary beside
  names still being watched reads as a pre-print call.

---

## 10. Honest limitations

1. **The credit-to-width audit.** A share of the carry book's replayed P&L sits in
   structures whose recorded credit exceeded their own width — arithmetically impossible
   for a condor. 47 of 198 carry trades carried the large majority of the book's P&L.
   `risk.max_credit_to_width` is **not implemented in the working tree**, so a run
   started from it reproduces the ungated number. Quote the gated figure, or state the
   provenance.
2. **Exit code is not shared.** Entry runs the same objects live and in replay; exit is
   reimplemented in the backtest engine, and nothing asserts the two agree.
3. **Intraday replay marks are coarse** relative to a 20-minute hold, so that book's
   replay P&L reads for candidate flow, not for edge.
4. **Paper fills flatter the intraday book severely.** Mid fills, no queue position, no
   partials — and spread cost is exactly what paper does not simulate.
5. **Five sessions is not a sample.** Expect 10–20 trades. That cannot distinguish edge
   from luck in either direction.
6. **The events book has no usable backtest.** Four prints per name is not a sample you
   can walk forward; the divergence ratio is a ranking device, not an edge estimate.
7. **The intraday momentum layer is conventional.** VWAP and volume confirmation ship
   with every charting platform. The originality is the confirmation score, the catalyst
   gate and the surface-aware option selection — not the trigger.
8. **7–14 DTE short premium is a deliberate tail-risk trade** for a seven-day window. It
   would be the wrong call for a live book.

**Operational, and it outranks all of the above:** there is no pidfile, lockfile or
singleton guard. Two runners append to one journal, race one `position_ledger.json`, and
hold separate in-memory caches — so one can serve pre-fix data hours after a fix lands,
and every conclusion drawn from the journal while both are live is suspect. Run exactly
one instance.

Stating these scores better than concealing them. A judge who spots an unacknowledged
paper-fill advantage forms a much worse impression than one who reads that it was
modelled.

---

## 11. Module map

| Module | Responsibility | Depends on |
|---|---|---|
| `oaa.core` | domain types, registry, errors, logging | — |
| `oaa.config` | YAML → typed schema, profile overlays, env | core |
| `oaa.brokers` | `Broker` protocol: CLI, MCP, REST, sim | core, config |
| `oaa.data` | market context, indicators (daily **and** intraday) | core, config |
| `oaa.options` | chain filtering, strike selection, structures, **selection** | core |
| `oaa.signals` | catalyst engine, macro calendar, cost/time gates | core |
| `oaa.strategies` | `vol_carry`, `intraday_momentum`, `event_premium` | options, signals, data |
| `oaa.strategies.events` | calendar, volscreen, watch, sentiment, direction, technicals, sizing, engine | options, brokers, risk |
| `oaa.risk` | the deterministic engine, sizing, exposure | core, config, firewall |
| `oaa.firewall` | phase machine, capital lock, position ledger | core |
| `oaa.execution` | router, combo executor, pricer | brokers, core |
| `oaa.discovery` | attention sweep, tradability filter, macro lens | data, agents |
| `oaa.agents` | orchestrator, runner, critic, tool belt, prompts | everything |
| `oaa.telemetry` | journal, metrics, report, **cost model**, daily report | core |
| `oaa.partners` | sponsor-technology adapters at seven pipeline stages | core |
| `oaa.app` | the Streamlit dashboard, operator and public builds | telemetry |
| `oaa.backtest` | replay harness — same pipeline stages, honestly labelled | everything |
