# Events book — earnings_event_directional

**Status:** driven by `oaa run` (`events_watch` hourly, `events_arm` 15:45,
`events_flatten` 09:45) and by `oaa events screen | watch | arm | flatten` for
manual and dry runs. It is **not a firewall tenant** — see *Capital* below.
Config: `config/strategies/earnings_event.yaml`. Code:
`src/oaa/strategies/events/`.

This is the only book in which a language model is load-bearing, and the only
one whose entry condition is a **date** rather than a threshold.

## Why it exists

The other two books wait for a market condition to cross a threshold, and the
recurring failure has been that the threshold is never crossed: the agent runs
all week and opens nothing. That cannot happen here. Broadcom reports on
2 September whether or not any indicator agrees. The gate opens on schedule and
the only question left is whether the trade is worth taking — which is what the
rest of the pipeline decides.

## Thesis

The screen measures **one** quantity: how far the option market's implied move
for a print sits from what the name actually did on its last four prints.

```
ratio = implied_move / median(|realised reaction|, last 4 prints)
```

That is a **volatility** reading, and the structure that expresses it must be a
volatility structure. See *The expression follows the sign of the divergence*.

## The pipeline

```
calendar.py    LLM proposes next week's reporters -> calendar file confirms
volscreen.py   implied move vs the last four actual reactions -> rank top N
sentiment.py   Alpaca news + StockTwits -> one sanitised evidence pack
watch.py       the same feeds, read hourly from three days out,
               judged once per new batch -> a dated dossier per name
direction.py   Featherless: direction, confidence, evidence cited
technicals.py  Bollinger squeeze (setup), RSI (veto), ATR (size and stop)
universe.py    the week's reporters, derived from the calendar
sizing.py      confidence -> contracts, bounded three ways
strategy.py    expression chosen by the sign of the divergence, three interlocks
engine.py      watch through the week, arm before the close, flatten at 09:45
```

## 1. The calendar — the model proposes, a file confirms

Featherless serves open-weight models whose weights were frozen months ago.
Asked to list next week's reporters, a model answers fluently and sometimes
wrongly — and a wrong date is not a bad trade, it is **a position opened against
no event at all**.

So the model **proposes** and `config/events/earnings_calendar.json`
**confirms**. Anything unverified is logged and never armed. `CPRT` ships with
`confirmed: false` precisely because Copart had not announced its date; it will
not trade.

## 2. The volatility screen

| Gate | Value | Why |
|---|---|---|
| `min_implied_move_pct` | 3.0 | Below a 3% implied move there is no event priced in, and the structure still crosses four half-spreads on the way in and four on the way out. |
| `max_relative_spread` | 0.25 | **The binding gate.** Worked example: GTLB at $45 with $0.20-wide legs pays ~$80 of a ~$120 credit to the market maker, against ~$0.40 in exchange and clearing fees — the 50–100× ratio in `COST_STRUCTURE.md`. |
| `min_option_price` | 0.30 | |
| `max_option_price` | `null` | Per-contract ceiling for this book alone. See the note on the removed global cap below. |
| `top_n` | 10 | Ten names is one LLM call each and at most ten positions sharing one night's risk budget. Beyond that the budget per name cannot clear a single contract's debit. |
| expiry containment | — | `screen_one` refuses any expiry earlier than `event.exit_date`. An expiry that settles before the reaction session prices no event whatsoever. |

Ranked by `abs_divergence`, so rich and cheap events rank together: the screen
asks whether there **is** an event, not which way to lean.

**Low-priced underlyings rank worse on net than on gross.** Premium scales with
the share price; the spread does not shrink proportionally.

## 3. The watch — days, not a minute (30 Aug)

Until this change the direction model met a name exactly once, at 15:50 on the
afternoon it armed, and judged the print from whatever happened to be on the
wire in that minute. That is the wrong shape for the job. An estimate revision
lands on the Tuesday, a supplier guides down on the Wednesday, the retail stream
crowds one way over three days. A snapshot sees the last of those and calls it
the picture.

From `watch.lookahead_days` (3) before a **confirmed** report, `oaa run` polls
the same two feeds hourly, 04:00–16:00 ET. Each poll:

1. gathers the pack, then **discards every item it has already read** — each is
   keyed by a hash of its timestamp and text;
2. if nothing new arrived, stops there. **No model call, no note, no tokens.** A
   quiet name is a real state and is reported as one;
3. otherwise asks the model one narrow question about the **new items only** —
   is this material to how the stock trades on the print, and which way does it
   point — and keeps the answer as a dated note when its salience clears
   `min_salience` (0.35).

Bounds: `max_new_items_per_poll` 40, `max_notes` 40, `note_ttl_days` 10,
`max_seen_keys` 2000.

**It stops at the print.** The day after a name reports, its dossier moves to
`runs/events/watch/reported/` and it is never polled again. Reading a name after
its print spends tokens on information that cannot inform the trade — and
post-print commentary left in a live dossier would be read by a later cycle as
*pre*-print evidence, which is the most expensive kind of quiet error this book
can make.

**The dossier cannot act.** A note is evidence handed to the same bounded
direction call as everything else. Salience is clamped, notes age out, and
`Dossier.lean()` is recorded in the journal but **never enforced** — a week of
bearish notes ending in a bullish call is logged as the disagreement it is, not
overruled.

## 4. The direction call

One call per name, on the whole accumulated week. Its confidence sets the
position size.

| Parameter | Value | Note |
|---|---|---|
| `min_confidence` | 0.55 | **The single most important number in the file.** At 0.55 the book trades marginal calls at minimum size; at 0.70 it trades rarely and only on genuine conviction. Move it after reading a run's abstention rate, not before. |
| `require_evidence` | true | A confident call citing nothing is a guess with a number attached. |
| `expected_abstention_rate` | 0.40 | `ArmReport.abstention_rate` is recorded on every run and the engine logs a **warning when it comes back zero**. |
| `temperature` / `seed` | 0.1 / 11 | |
| `max_tokens` | 1200 | |

**A model that never abstains is not filtering.** This repo has shipped that bug
once already, with a critic that scored eighty candidates and declined none. If
every call is actionable, the prompt or the confidence floor is wrong, not the
market.

### Two roles, two models, one key (30 Aug)

| Role | Frequency | Question | Model |
|---|---|---|---|
| watch triage | ~40 calls/day | is this batch material, and which way? | `Qwen/Qwen3-8B` |
| direction call | once per name | which way does this print go, and how sure? | `Qwen/Qwen3-32B` |

The triage is narrow, schema-bound and usually right to answer "immaterial".
Running the 32B model on it spends the direction call's budget on noise, so
`watch.model` and `direction.model` are separate and `llm_roles.role_llm` builds
a client per role.

**A key is authentication, not model selection.** A second Featherless key
reaches the same catalogue on the same account, so it changes nothing about
which model answers — `model` does that. `api_key_env` exists per role anyway,
because separate metering and separate revocation are fair reasons to want one.

Two behaviours worth knowing:

- A role naming **neither** a model nor a key returns the shared client rather
  than building an identical second one.
- A role naming a key that resolves to nothing falls back to the shared client
  and logs a WARNING. Left mute, the watch would retain batches unjudged and the
  direction call would abstain — which from the outside is indistinguishable
  from a careful model on a quiet week.

Until 30 Aug `DirectionParams.model` and `.seed` were declared, documented, and
read by nothing. That is the same failure as the unnoticed Anthropic key:
config that looks configured and is not.

### Prompt injection

The evidence pack is third-party text — press copy and anonymous retail posts —
going into a language model. It arrives inside `<<<EVIDENCE>>>` markers that the
system prompt names as untrusted data, stripped of control characters and
backticks, truncated to `sentiment.max_chars` (12,000). The response is parsed
against a fixed JSON schema and **every field clamped**, so the worst a poisoned
post can do is move a bounded confidence score. The model is also asked to flag
directive-like text, which is logged.

Nothing the model returns can authorise a trade: `RiskEngine` signs every ticket
and `ExecutionRouter` refuses unsigned ones.

## 5. The technical layer

Added 29 Aug so the book is not a pure bet on what a model read overnight. Three
indicators, three jobs, none allowed to drift into another's.

| | Role | Rule |
|---|---|---|
| **Bollinger** | the setup | **Width, never position.** A squeeze is width in the bottom quartile of its own 100-bar range: movement has coiled while a dated catalyst approaches. Says nothing about direction, and is not asked to. |
| **RSI** | the veto | One-sided, extremes only. No short below 20, no long above 80. RSI at 60 blocks nothing, and should not. |
| **ATR** | the risk manager | Never an entry gate. Stop at 2× ATR on the underlying; size scaled down when the daily range is already wide (full size at `atr_reference_pct` 0.02, floor at 0.40×). |

**The honest limit on the ATR stop.** No cycle runs between the 15:45 arm and
the 09:45 exit, so a stop level cannot be watched overnight — the gap through it
is the risk the position exists to take. The stop governs the *morning*: if the
underlying is through it, the position closes without waiting for a target.
**The protection against the gap is the structure, not the stop.**

## 6. The expression follows the sign of the divergence (30 Aug)

Until 30 Aug every reading from the screen was expressed as a **vertical debit
spread** — a directional bet, whose payoff is orthogonal to the quantity that
was measured. The consequence is arithmetic rather than empirical: a directional
structure bought at a fair-to-rich implied move, on a direction call no better
than a coin flip, returns **minus the round trip in expectation**. No sample size
fixes that, no gate tightening rescues it, and no improvement to the direction
call is even being *tested* by it — the edge that was measured is simply not the
edge the structure collects.

The first honest backtest of this book made the point in one trade: 22 ideas,
1 approved, full defined risk lost on DG. The structure was doing exactly what it
was built to do.

| implied / realised | Expression | What the edge IS |
|---|---|---|
| ≥ `rich_ratio_threshold` (1.35) | defined-risk **iron condor**, both shorts outside the implied move | the overpricing itself |
| ≤ `cheap_ratio_threshold` (0.80) | **vertical debit spread** in the called direction | the underpricing |
| between | **no trade** | there isn't one |

**That middle band is where the book used to do all of its trading.** The
thresholds sit well clear of 1.0 in both directions because the ratio is
computed off four prints, so a name at 1.10 is inside its own sampling error.

The direction call stops being the thesis and becomes a **tilt**: it picks the
side of the debit vertical, and on the condor it pushes the threatened short
further out (`condor_direction_tilt` 0.25) — never pulls the other one in,
because collecting more premium by moving a short inside the implied move
surrenders the one property the structure is built on.

### Strikes are placed by distance, not delta

`iron_condor_outside_move` puts the shorts at `spot ± shorts_at_implied_move ×
implied_move` (1.0) and lets the delta fall out. The front-weekly surface across
a print is too deformed for a delta to stand in for distance — a 16-delta strike
can land well outside or well inside the priced move depending only on how the
market has skewed the wings. On a structure whose whole thesis is "the realised
move will be smaller than the priced one", **where the shorts sit relative to
that priced move IS the thesis**.

`min_shorts_clearance` (0.85) then checks what the listed ladder actually
delivered: a coarse grid can snap a short back inside the move, and that is a
different trade from the one the screen justified, so it is **declined rather
than quietly downgraded**.

Other structural floors: wings at `condor_wing_pct` 4% of **spot** (not points —
the universe spans $4 to $600), `min_credit_to_width` 0.18 on the condor,
`max_debit_to_width` 0.45 and `min_reward_risk` 1.2 on the vertical, DTE window
1–9.

Set `expression_follows_divergence: false` to restore the old behaviour, so the
two can be measured against each other rather than asserted.

## 7. Interlocks

`generate` returns nothing unless all three hold:

1. the symbol has a **confirmed** calendar row;
2. today is that event's **entry date** — the session before an after-close
   print, or the session before an open-time print;
3. the **engine supplied a direction call**. The strategy never calls an LLM from
   inside a generation loop.

## Sizing and capital

Confidence maps to contracts, bounded three ways:

| Bound | Value |
|---|---|
| `max_risk_per_trade_pct` | 1.2% — one name, at full confidence |
| `nightly_risk_budget_pct` | 4% — shared across every name opened that night |
| `max_contracts` | 10 — an absolute ceiling that does **not** scale with equity |

Confidence at the floor still earns a position, at `min_size_multiple` 0.30 of
full size: the gate is where marginal calls die, not the sizing curve.

This book **never leases capital from the temporal firewall**
(`RiskEngine(firewall=None)`), whether armed by `oaa events arm` or by
`oaa run`'s 15:45 `events_arm` cycle. A book whose entire life is one overnight
hold does not fit the intraday/carry tenancy model.

The trade-off is stated rather than hidden: the position ledger treats anything
not booked to `carry` as transient, so an events leg still open at a later 15:15
cutoff would be swept. The 09:45 flatten is what makes that unreachable in the
normal case, and a sweep is the conservative error if it ever is reached.

## Schedule (America/New_York)

```
04:00-16:00  events_watch     hourly, only on names inside the 3-day window
09:45        events_flatten   close into the post-print IV collapse, BEFORE the
                              day books compete for the same buying power
15:45        events_arm       the session before an after-close print
15:55        no_entry_after
10:30        hard_exit_time
```

The 09:45 exit is deliberate: the first minutes of the session are the widest
quotes of the day, and the IV crush is the whole reason the cheap-side structure
is a debit vertical rather than a long call.

## Backtesting it

```bash
oaa backtest --strategy earnings_event_directional --symbols earnings-week
```

`--strategy` runs one strategy in isolation and works for a strategy the config
does not list. `earnings-week` expands to the confirmed reporters in the current
week, read from the same calendar file the live book arms from — one source of
truth, so the universe cannot drift out of step with the dates.

**There is no LLM in the replay loop.** Live, the engine always supplies a
direction call — an abstention is still a call — so a context arriving without
one means a backtest, and direction is derived from the Bollinger midline
instead. Those ideas are tagged `derived` and sized at the confidence floor.
That is what makes the technical layer measurable on its own: run it and you are
asking *"does the setup have any edge before the model is added"*, which is the
only honest way to find out.

## Known defects and deliberate omissions

- **No usable backtest.** Four quarters of reactions per name is not a sample you
  can walk forward. The ratio is a **ranking device, not an edge estimate**.
- **No historical option quotes**, so the modelled spread is an assumption until
  `scripts/probe_option_data.py` measures real weekly widths on the shortlist.
  Do that before sizing anything live.
- **No intraday management.** The position is opened once and closed once. If it
  needs managing between those two points, it was too large.
- **The removed global $25 option-price cap distorted rather than blocked.** It
  was a per-**contract** filter, so on an expensive name it removed the
  near-the-money strikes and left the cheap far-OTM ones. The chain is not empty
  and nothing raises: `atm()` then prices an out-of-the-money strike as if it
  were ATM and **understates the implied move**, and `by_delta(0.45)` resolves to
  whatever delta survived. `screen.max_option_price` overrides it per book.
- **The ATR stop cannot be watched overnight**, as above.

## Running it

```bash
oaa events screen                       # what reports this week, and what was proposed
oaa events arm --dry-run                # decide, size, journal - route nothing
oaa events arm --live --profile judged  # 15:45 ET the session before a print
oaa events flatten --profile judged     # 09:45 ET the morning after
```
