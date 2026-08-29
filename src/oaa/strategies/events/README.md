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
  sizing.py      confidence -> contracts, bounded three ways
  strategy.py    vertical debit spread, three interlocks
  engine.py      arm before the close, flatten the next morning
```

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
