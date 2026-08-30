# The events book

One overnight hold across a scheduled earnings print. Entry point:
`oaa events arm`. Nothing in `config/default.yaml`'s `strategies:` list names
this book, so the options runner never loads it.

## Why it exists

The other books wait for a market condition to cross a threshold, and the
recurring failure has been that the threshold is never crossed: the agent runs
all week and opens nothing. That cannot happen here, because **the entry
condition is a date**. Broadcom reports on 2 September whether or not any
indicator agrees. The gate opens on schedule; the only question left is whether
the trade is worth taking, which is what the rest of the pipeline decides.

## The pipeline

```
  calendar.py    LLM proposes next week's reporters -> calendar file confirms
  volscreen.py   implied move vs the last four actual reactions -> rank top N
  sentiment.py   Alpaca news + StockTwits -> one sanitised evidence pack
  direction.py   Featherless: direction, confidence, evidence cited
  technicals.py  Bollinger squeeze (setup), RSI (veto), ATR (size and stop)
  universe.py    the week's reporters, derived from the calendar
  sizing.py      confidence -> contracts, bounded three ways
  strategy.py    expression chosen by the sign of the divergence, three interlocks
  engine.py      arm before the close, flatten the next morning
```

## The expression follows the sign of the divergence (30 Aug)

The screen measures ONE thing: how far the option's implied move sits from what
this name actually did on its last four prints. That is a volatility reading.

Until 30 Aug every one of those readings was expressed as a **vertical debit
spread** — a directional bet, whose payoff is orthogonal to the quantity that
was measured. The consequence is arithmetic rather than empirical: a
directional structure bought at a fair-to-rich implied move, on a direction
call no better than a coin flip, returns minus the round trip in expectation.
No sample size fixes that, no gate tightening rescues it, and no improvement to
the direction call is even being tested by it — the edge that was measured is
simply not the edge the structure collects.

The first honest backtest of this book made the point in one trade: 22 ideas,
1 approved, full defined risk lost on DG. The structure was doing exactly what
it was built to do.

So the expression now follows the sign:

| implied / realised | expression | what the edge IS |
|---|---|---|
| `>= rich_ratio_threshold` (1.35) | defined-risk iron condor, both shorts outside the implied move | the overpricing itself |
| `<= cheap_ratio_threshold` (0.80) | vertical debit spread in the called direction | the underpricing |
| between | **no trade** | there isn't one |

That middle band is where the book used to do all of its trading.

The direction call stops being the thesis and becomes a tilt: it picks the side
of the debit vertical, and on the condor it pushes the threatened short further
out — never pulls the other one in, because collecting more premium by moving a
short inside the implied move surrenders the one property the structure is
built on.

**Strikes are placed by distance, not delta.** `iron_condor_outside_move` puts
the shorts at `spot +/- multiple x implied move` and lets the delta fall out.
The front-weekly surface across a print is too deformed for a delta to stand in
for distance — a 16-delta strike can land well outside or well inside the
priced move depending only on how the market has skewed the wings. On a
structure whose whole thesis is "the realised move will be smaller than the
priced one", where the shorts sit relative to that priced move IS the thesis.

`min_shorts_clearance` then checks what the listed ladder actually delivered: a
coarse grid can snap a short back inside the move, and that is a different
trade from the one the screen justified, so it is declined rather than quietly
downgraded.

Set `expression_follows_divergence: false` to restore the old behaviour, so the
two can be measured against each other rather than asserted.

## The four things most likely to go wrong

**1. The model does not know next week's earnings dates.** Featherless serves
open-weight models whose weights were frozen months ago. Asked to list next
week's reporters it answers fluently and sometimes wrongly, and a wrong date is
not a bad trade - it is a position opened against no event at all. So the model
**proposes** and `config/events/earnings_calendar.json` **confirms**. Anything
unverified is logged and never armed. `CPRT` ships with `confirmed: false`
precisely because Copart had not announced its date; it will not trade.

**1b. The global $25 option-price cap distorts rather than blocks.** It is a
per-CONTRACT filter, so on an expensive name it removes the near-the-money
strikes and leaves the cheap far-OTM ones. The chain is not empty and nothing
raises: `atm()` prices an out-of-the-money strike as if it were ATM and
understates the implied move, and `by_delta(0.45)` resolves to whatever delta
survived. `screen.max_option_price` overrides it. On the 1-4 Sep calendar it
bites MDB (ATM leg ~$37) and DELL (~$26); CIEN (~$24) and SNOW (~$20) sit just
under it.

**2. The expiry must contain the print.** An expiry that settles before the
reaction session prices no event whatsoever. `screen_one` refuses any expiry
earlier than `event.exit_date`, which is the cheapest way to avoid the most
expensive mistake available here.

**3. Spread, not fees, is the cost that matters.** A vertical crosses four
half-spreads on a round trip. Worked example: GTLB at $45 with $0.20-wide legs
pays roughly $80 of a ~$120 credit to the market maker, against about $0.40 in
exchange and clearing fees - the 50-100x ratio in `COST_STRUCTURE.md`.
`screen.max_relative_spread` is therefore the gate that removes the most
candidates, and it should be. Low-priced underlyings rank worse on net than on
gross: premium scales with the share price, the spread does not shrink
proportionally.

**4. A model that never abstains is not filtering.** This repo has shipped that
bug once already, with a critic that scored eighty candidates and declined
none. `ArmReport.abstention_rate` is recorded on every run and the engine logs
a warning when it comes back zero. If every call is actionable, the prompt or
the confidence floor is wrong, not the market.

## Interlocks

`generate` returns nothing unless all three hold:

1. the symbol has a **confirmed** calendar row;
2. today is that event's **entry date** (the session before an after-close
   print, or the session before an open-time print);
3. the **engine supplied a direction call**. The strategy never calls an LLM
   from inside a generation loop.

## Prompt injection

The evidence pack is third-party text - press copy and anonymous retail posts -
going into a language model. It arrives inside `<<<EVIDENCE>>>` markers that
the system prompt names as untrusted data, stripped of control characters and
backticks, truncated to a fixed budget. The response is parsed against a fixed
JSON schema and every field clamped, so the worst a poisoned post can do is
move a bounded confidence score. The model is also asked to flag directive-like
text, which is logged. Nothing the model returns can authorise a trade:
`RiskEngine` signs every ticket and `ExecutionRouter` refuses unsigned ones.

## Capital

This book runs in its own process and never leases capital from the temporal
firewall - the same arrangement the weekend book used, and for the same reason:
a book whose entire life is one overnight hold does not fit the
intraday/carry tenancy model. Sizing is bounded three ways instead:
`max_risk_per_trade_pct` per name, `nightly_risk_budget_pct` shared across
every name opened that night, and `max_contracts` as an absolute ceiling that
does not scale with equity.

## The technical layer

Added 29 Aug so the book is not a pure bet on what a model read overnight.
Three indicators, three jobs, none allowed to drift into another's:

| | Role | Rule |
|---|---|---|
| **Bollinger** | the setup | Width, never position. A squeeze = width in the bottom quartile of its own 100-bar range: movement has coiled while a dated catalyst approaches. Says nothing about direction, and is not asked to. |
| **RSI** | the veto | One-sided, extremes only. No short below 20, no long above 80. RSI at 60 blocks nothing. |
| **ATR** | the risk manager | Never an entry gate. Stop at 2x ATR on the underlying; size scaled down when the daily range is already wide. |

**The honest limit on the ATR stop.** No cycle runs between the 15:45 arm and
the 09:45 exit, so a stop level cannot be watched overnight - the gap through
it is the risk the position exists to take. The stop governs the *morning*: if
the underlying is through it, the position closes without waiting for a target.
The protection against the gap is the structure, not the stop.

## Backtesting it

    oaa backtest --strategy earnings_event_directional --symbols earnings-week

`--strategy` runs one strategy in isolation and works for a strategy config
does not list, which this one deliberately is not. `earnings-week` expands to
the confirmed reporters in the current week, read from the same calendar file
the live book arms from - one source of truth, so the universe cannot drift out
of step with the dates.

There is no LLM in the replay loop. Live, the engine always supplies a
direction call - an abstention is still a call - so a context arriving without
one means a backtest, and the direction is derived from the Bollinger midline
instead. Those ideas are tagged `derived` and sized at the confidence floor.
That is what makes the technical layer measurable on its own: run it and you
are asking "does the setup have any edge before the model is added", which is
the only honest way to find out.

## Running it

```bash
oaa events screen                      # what reports this week, and what was proposed
oaa events arm --dry-run               # decide, size, journal - route nothing
oaa events arm --live --profile judged # 15:45 ET the session before a print
oaa events flatten --profile judged    # 09:45 ET the morning after
```

## What is deliberately missing

* **No backtest.** Four quarters of reactions per name is not a sample you can
  walk forward. The ratio is a ranking device, not an edge estimate.
* **No historical option quotes**, so the modelled spread is an assumption
  until `scripts/probe_option_data.py` measures real weekly widths on the
  shortlist. Do that before sizing anything live.
* **No intraday management.** The position is opened once and closed once. If
  it needs managing between those two points, it was too large.
