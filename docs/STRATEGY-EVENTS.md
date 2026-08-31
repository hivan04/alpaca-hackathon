# Events book — earnings_event_directional

**Status:** live on the judged account since 30 Aug. Driven by `oaa run`
(`events_watch` hourly 04:00-16:00 ET, `events_flatten` 09:45, `events_arm`
15:50) and by `oaa events screen | watch | arm | flatten` for manual and dry
runs. The arm cycle sits at **15:50**, ten minutes after this book's own
`schedule.arm_time: "15:45"`, so it does not race `carry_verify` — the day's
sign-off — for the same account snapshot. `config/default.yaml` is what `oaa
run` reads; `arm_time` governs the manual `oaa events arm`.

It is **not a firewall tenant** — see *Capital* below. Config:
`config/strategies/earnings_event.yaml`. Code: `src/oaa/strategies/events/`.

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

## 3b. What the watch reads, and what it does when it cannot (30 Aug)

Three defects on the path the watch depends on were found reading a `make
status` on the Sunday before the week. All three had one shape: **the system
continuing to look healthy while doing less than it claimed.**

| The defect | How it presented |
|---|---|
| `news()` caught `DataError` and returned `[]` | `gather` records a **raised** news failure on the pack's `errors`, and the watch report reads that list to decide whether a name was quiet or unreadable. It cannot record a failure it is never told about — so a dead `alpaca data news` made **all fourteen** watched names report as *quiet*. A name nobody could read was indistinguishable from a name with no news. |
| the events lookback was silently the global one | `alpaca_news_fetcher` called `news(symbol, start=…, limit=…)`; `news` took only `(symbol, limit)`, so the keyword call raised `TypeError`, the fallback re-called `news(symbol)` bare, and this file's `news_lookback_days: 7` / `max_headlines: 25` were replaced by `data.news_lookback_hours: 6` / `news_limit: 20`. |
| a missing reasoning layer announced itself at INFO | `no LLM available - running the deterministic %s cycle`. That INFO line was the *entire* evidence that the week was running on rules — and `telemetry.console: focused` filters INFO, so it would no longer even be on screen. |

`news()` now raises. The one in-package caller that must stay tolerant —
`context()`, built for every symbol in a scan — catches it and logs a WARNING
rather than taking market context down with the feed. **The window is part of
the cache key**: without that, a six-hour discovery fetch and a seven-day events
fetch for the same symbol collide and whichever ran first answers for the other.
The degradation line is now WARNING, names the provider, and is journalled as
`agent_degraded`; `Runner._warn_if_reasoning_is_missing` asks the question at
boot rather than waiting for the 10:00 agent cycle.

The lookback defect barely touched the ongoing hourly polls — they dedupe. **The
first read of a name three days out is where it bit**: six hours of history
instead of a week, which is most of the window in which revisions actually land.

`tests/test_news_feed_is_honest.py`, five cases: a dead feed raises; the watch
records it as an error rather than as a quiet name; the seven-day window and the
25-headline cap reach the command line; the default window is still the global
six hours; two windows do not share a cache entry. 563 pass.

### The failing news pull was not this book's (31 Aug)

`oaa status` on 31 Aug reported:

```
source 'news' failed: alpaca data news --start 2026-08-25 --end 2026-08-28
--limit 200 exited 1
```

That is the **discovery screener's** bare date-range pull, not this book's feed.
`NewsSource.fetch` sends no `--symbols`, and the shown limit of 200 is over
Alpaca's documented maximum of 50, which `NEWS_MAX_LIMIT` is supposed to clamp.
The events book calls a different signature through `CliData.news`
(`--symbols X --limit N --start <iso>`) and **was working** — the dossiers of
30 and 31 Aug carry real headlines. The distinction is worth stating because the
two failures look identical in a status pane and only one of them touches this
book: the earnings book's inputs were intact; the screener's were not.

### The submission backstop was not armed (30 Aug)

`management.submission_flatten_utc: "2026-09-04T13:45:00Z"` closes the whole
book before the deadline, and the GitHub Actions *Safety net* workflow is the
version that works from a phone with no laptop involved. Two of its four runs
failed in ~45s on 30 Aug: the repo secrets `ALPACA_API_KEY` /
`ALPACA_SECRET_KEY` are unset, so `flatten` dies in `brokers/factory.py` —
credentials missing, and the configured fallback is the SIMULATOR while
`execution.dry_run` is false. That guard is doing its job.

Two things made it invisible. The `schedule` trigger supplies no inputs, so it
resolves to `flatten`; the manual runs were dispatched with `report`, which
never touches the broker — **a green manual run is a false green**. And the
doctor step is `oaa doctor --profile judged | tail -25` with no `set -o
pipefail`, so the step exits with `tail`'s status and a `FAIL` row on
`credentials` scrolls past green. The step named "Confirm which account these
keys open" confirms nothing.

This matters to *this* book more than to the others: it is the only one that
holds a position overnight, so it is the only one that can still be open when a
backstop is asked to close it. The three crons for 2026-09-04 (13:45 / 14:05 /
14:25 UTC) all run `flatten` and, as of 30 Aug, all three would fail the same
way. Fix is two secrets and one `pipefail`.

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

`watch.model` is pinned to `Qwen/Qwen3-8B` in `earnings_event.yaml`;
`direction.model` ships as **null**, which inherits `agents.llm` — currently
`Qwen/Qwen3-32B` in `config/default.yaml`. The 32B model is therefore what
answers, but it is inherited rather than pinned: change `agents.llm` and this
book's direction call changes with it.

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

**The honest limit on the ATR stop.** No cycle runs between the 15:50 arm and
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
`oaa run`'s 15:50 `events_arm` cycle. A book whose entire life is one overnight
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
15:50        events_arm       the session before an after-close print
15:55        no_entry_after   enforced since 30 Aug - see below
10:30        hard_exit_time
```

The 09:45 exit is deliberate: the first minutes of the session are the widest
quotes of the day, and the IV crush is the whole reason the cheap-side structure
is a debit vertical rather than a long call.

### `no_entry_after` was dead config until 30 Aug

`schedule.no_entry_after` ("15:55") has been in the params since the book was
written and documented in `earnings_event.yaml` as *"a late fill on a wide quote
is the one execution risk this schedule cannot manage away"*. **Nothing read
it.** A grep across `src/` found the definition and no consumer — the same shape
of defect as `DirectionParams.model` and `.seed`. The global backstop does not
cover it either: `management.entry_cutoff_utc` is null, and deliberately so,
because a global cutoff written for the carry book deletes half of this book's
week (LULU, ZS, DOCU and CIEN all print after the old 2 Sep 20:00 UTC cutoff).

**The failure mode is the sleep path, not the crash path.** `Runner._due` fires
every cycle whose wall-clock time has passed and which has not fired today.
That is correct for what it was written for: a crash at 15:10 must not skip the
15:15 cutoff. A laptop that sleeps through the afternoon and wakes at 17:00
takes exactly the same path — `events_arm` then runs with a clock hours past the
close and, unguarded, tries to open a debit spread into a closed market, on the
judged paper account whose complete history the judges read. pm2 does not help:
a sleeping macOS suspends pm2 with everything else.

`Orchestrator._arm_is_too_late` is checked in `events_arm` after the switchboard
and the risk halt, and before the engine is asked for anything expensive. It
compares `clock.now(ZoneInfo(cfg.schedule.timezone))` against
`params.no_entry_after_at()`; on or before the deadline it proceeds, one minute
past it stands down. The refusal is logged at WARNING — so it survives
`console: focused` — and written to the journal as `events_arm / action: skip /
reason: past no_entry_after`, carrying the clock that caused it. A silent
stand-down is indistinguishable from a dead agent.

**Standing down is the conservative error.** A night not traded costs one
opportunity. An order sent hours after the close is on the record permanently.

**The guard cannot itself become the outage.** If the params or the timezone
cannot be resolved it logs a warning and returns `False` — the arm proceeds. A
safety rail that silently cancels the book's only trade of the night because it
failed to read its own config would be worse than the defect it fixes. Pinned by
`test_a_broken_deadline_does_not_become_an_outage`; the behaviour half is a
parametrised case per clock in `tests/test_events_in_run.py` (15:50 and 15:55
trade, 15:56 and 17:00 refuse, 09:30 trades for a before-open print).

It removes the worst consequence of a sleeping machine; it does not make one
acceptable. The 09:45 flatten and the 15:15 cutoff have no equivalent
protection, and the answer to sleep remains an always-on host.

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

## Operational risk: two runners on one account (31 Aug)

Found during a pre-week check at ~20:45 EDT on Sunday 30 Aug. `oaa status`
showed two unnamed online Python processes. Both were
`.venv/bin/oaa run --profile judged`: pid **12181**, up 1h47m, the intended one;
and pid **99100**, up 8h13m, started ~12:31 EDT, matching four `runner_start`
journal entries between 16:24 and 16:31 UTC. Its terminal had been closed, so
its console output was already unrecoverable. `kill 99100` ended it on the
graceful path — it took a few minutes because the monitor loop sleeps 300s.

**Nothing surfaced it.** `oaa status` renders both as truncated
`/Library/Frameworks/Python.framework/Ver…` rows with no profile and no account,
so a second judged runner is visually indistinguishable from the Streamlit
dashboard. The banner read **UP** either way. The README's warning ("do not run
two agents on one account") is true and unenforced: there is no pidfile, no
lockfile and no singleton guard anywhere in `src/`. Identifying it took
`ps -ww -o pid,command -p <pid>` — the `-ww` matters, because plain `ps`
truncated the line at `--pr`, one character short of the profile that decided
whether it was harmless.

**The cost so far was nothing, and the exposure was specifically this book.**
0 open positions, flat $100k, and the afternoon process had done one
`carry_scan` that stood down on market phase. The next day is where it bites:
two runners both firing `events_arm` at 15:50 ET would have made **two direction
calls on the same names** — two LLM calls, two independent confidence readings,
two sizings — and sent **two orders against one `nightly_risk_budget_pct` of 4%
that each believed it alone was spending**. Nothing downstream reconciles that:
the per-night budget is computed per process, and the position ledger has no
writer arbitration. The 4% cap and the 1.2%-per-name cap are both stated in this
document as bounds on the night; with two runners they are bounds per runner.

The small fix is a pidfile written at `_boot` under `<run_dir>/` with a liveness
check, refusing a second start on the same profile. Failing that, `oaa status`
should name the profile and account per process rather than printing a truncated
interpreter path — the check above is only possible if the operator already
suspects the answer. **Neither is done.** Until one is, the operational control
is procedural: one terminal tab, and `oaa status` read with `ps -ww` when
anything looks doubled.

## Live status — 31 Aug, evening

One runner on the judged account, `oaa run --profile judged`. Journal
`runs/judged/journal.jsonl`; dossiers `runs/events/watch/`.

| | as of the last journal write, 18:30 UTC / 14:30 ET |
|---|---|
| open positions | **0** — `runs/position_ledger.json` is empty |
| `events_watch` cycles fired today | **11** of the 13-cycle grid, hourly and on time from 08:00 to 18:00 UTC (04:00-14:00 ET); 15:00 and 16:00 ET still to come |
| names watched | **14** — every confirmed 1-3 Sep reporter |
| dossiers on disk | 14, holding **124** deduped items and **11** dated notes |
| headlines behind those notes | 47 |
| StockTwits messages behind those notes | **0** (see defects) |
| `events_flatten` | ran 13:45 UTC (09:45 ET), closed nothing — nothing was open |
| `events_arm` | **has not fired against a live print yet.** The first is tonight at 15:50 ET, for the six names that report on 1 Sep |

The dossiers are real reading, not empty scaffolding. MDB carries a 0.8-salience
bullish note dated 30 Aug off three analyst price-target raises (DA Davidson to
$465, Barclays to $460, Wells Fargo to $475) from 8 headlines, and a second
0.8-salience note dated 31 Aug off a fourth (Cantor Fitzgerald to $540). Notes
were written today on ZS, DELL, GTLB, MDB, SNOW, CIEN and NTAP; the remaining
names read genuinely quiet, which is a state this book reports rather than
papers over.

**A restart demonstrated the replay behaviour, harmlessly.** The runner was
restarted at 15:51 UTC (11:51 ET); the eight earlier hourly watch cycles fired
back to back in nine seconds, and every one of them read **zero** new items and
made **zero** model calls. That is the dedupe working exactly as the design
claims. A stale `events_arm` on the same path would now be refused by
`_arm_is_too_late`.

### The confirmed calendar for the week

From `config/events/earnings_calendar.json`. Only `confirmed: true` rows are
ever armed; `reference_implied_move_pct` is the front-weekly straddle as of
28 Aug, kept for reference — the screen prices the live chain.

| Report date | Names (after the close) | Before the open | Reference implied move |
|---|---|---|---|
| Tue 1 Sep | DELL, PANW, MDB, CRDO, GTLB | NIO | 9.4%-16.5% (MDB highest) |
| Wed 2 Sep | AVGO, SNOW, NTAP, AI | — | 8.1%-13.9% (NTAP not quoted) |
| Thu 3 Sep | LULU, ZS, DOCU | CIEN | 10.1%-13.1% |
| Thu 3 Sep | CPRT — **`confirmed: false`, will not trade** | — | not quoted |

Fourteen confirmed rows, one refused. `CPRT` ships unconfirmed because Copart
had not announced its date as of 29 Aug, and an unverified date is not a bad
trade — it is a position opened against no event at all.

Timing decides the entry date, not just the display. Both cases arm into the
close of the **session before** the print — for an `amc` name that is the same
calendar day as the report, for a `bmo` name it is the day earlier. So DELL,
PANW, MDB, CRDO and GTLB arm on 31 Aug alongside NIO, and CIEN arms on 2 Sep
alongside nothing else.

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
- **Nothing enforces one runner per account.** No pidfile, no lockfile, no
  singleton guard — see *Operational risk* above. This is the defect on this
  page with the largest live consequence, because it doubles the nightly risk
  budget without any component reporting an error.
- **StockTwits has contributed nothing live.** All 11 notes written on 30-31 Aug
  were built from news headlines alone; the message count across every note is
  **0**. StockTwits returned 403 from a datacenter IP during verification and
  has produced no messages from the Mac either. `gather` degrades to news-only
  and records the failure in `report.errors` rather than reporting a quiet name,
  so the behaviour is correct — but the "two feeds" in the pipeline diagram is,
  in live evidence so far, one feed.
- **Nothing has measured whether the watch improves the call.** The dossier
  changes what the model reads. Whether that changes P&L is unmeasured, and is
  not claimed.
- **The portfolio Greek caps see only one symbol on this book's path.**
  `RiskEngine.evaluate` takes `contexts` to recover greeks for open positions by
  matching OCC symbols back to a chain, and `strategies/events/engine.py` passes
  `{market.symbol: market}` — the single name it is arming. Coverage is counted
  and carried onto the verdict, so this is visible rather than silent, but a
  cap computed over part of the book is looser than one computed over all of it.
- **The live chain DTE window fix is on a branch, not in the running process.**
  `fix/live-chain-dte-window` derives the live `context()` chain window from
  `tradable_dte_range(cfg)`, which includes this book's runtime-derived 1-9 DTE
  window; without it the live providers request `options.min/max_days_to_expiry`
  (3-45 DTE) regardless of what any book declared. The working tree is on that
  branch and **uncommitted** — a stale `.git/index.lock` blocks git writes — and
  the running agent holds its modules in memory, so the live process is on the
  old window until it is restarted.

## Running it

```bash
oaa events screen                       # what reports this week, and what was proposed
oaa events arm --dry-run                # decide, size, journal - route nothing
oaa events arm --live --profile judged  # the session before a print, before 15:55
oaa events flatten --profile judged     # 09:45 ET the morning after
```
