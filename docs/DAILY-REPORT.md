# The daily report evaluator

One file per trading session, in `reports/<profile>/<date>.md`, written after
the close by the process that traded the day.

```
reports/
  judged/2026-08-31.md      # and .json alongside it
  dev/2026-08-31.md
```

## What is in it

| Section | What it answers |
|---|---|
| Result | Day P&L, equity open -> close, positions at close, the day's counts |
| Orders filled | Every fill: time, symbol, quantity, price, order id |
| Positions closed | Realised P&L and the exit reason, per position |
| **Potential executions** | Every idea that was **built and priced** and then declined, with the reason. On most sessions this is the report. |
| Gate funnel | Which gate refused, how often, in which book, and the most repeated reasons |
| By strategy | Ideas / opened / closed / declined / risk-approved-but-unsent / realised P&L |
| Session log | Errors, firewall leases and cutoffs, watch notes, macro read |
| Where the algorithm can improve | Featherless, in bullet points |

Two design decisions are worth knowing about.

**The declined trades are the point.** This book is a stack of gates and it
fills nothing on most sessions. A report that counted only fills would be blank
on exactly the days there is most to learn, so the ideas that were priced and
then refused are a first-class section, not an appendix.

**A risk-approved idea that never became an order is called out separately.**
`approved=1` on a `skip` means the risk engine signed the ticket and no order
exists - a downstream veto, or a defect. It is the first row to chase on a
session that filled nothing, so it gets its own column, its own callout and its
own bullet.

## The critique

The `## Where the algorithm can improve` section is written by the same
Featherless model the live loop runs on (`agents.llm`), given a compact JSON
brief of the session: the funnel aggregates, a sample of the declined ideas,
the fills, the P&L and the errors - not the raw 200-line rejection log, which is
mostly near-duplicates.

The section is **always present**, and always says who wrote it:

- `featherless / Qwen/Qwen3-32B` - the model answered.
- `deterministic (fallback - ... failed: ...)` - the provider was unreachable
  or returned prose instead of bullets.
- `deterministic (no reasoning provider)` - no key, or `--no-llm`.

The deterministic critique is arithmetic rather than clever: it restates the
binding constraint out of the funnel. Its job is that the report is never blank
and never silently pretends a model wrote it - the same rule the rest of this
repo applies to degradation.

## Running it

It fires by itself. `daily_report` is a scheduled cycle at **16:20 ET**, ten
minutes after `eod_report`, inside the same `oaa run` process that traded the
day. It opens nothing, and a failure inside it is caught and journalled rather
than allowed to end the cycle loop.

By hand, for any past session:

```bash
oaa daily-report --profile judged                      # today
oaa daily-report --profile judged --date 2026-08-29    # a specific session
oaa daily-report --profile judged --days 5             # the last five
oaa daily-report --profile judged --no-llm             # arithmetic critique only
```

The report reads the **journal**, never the broker, so regenerating a past day
gives that day's numbers, and a second run of the same date corrects the file
rather than adding another. That is also why `reports/` is committed and
`runs/` is not: the reports are the week's readable record.

## Where the numbers come from

| In the report | Source |
|---|---|
| P&L, equity | `equity` table - the broker's own `day_pl` on the last snapshot of the session |
| Fills | `fills` table |
| Potential executions | `decisions` where `action = 'skip'`, with the priced idea attached |
| Gate funnel | `gate_rejection` events in `journal.jsonl` |
| Session log | `cycle_error`, `agent_degraded`, `firewall_*`, `events_*`, `macro_view`, `discovery` events |

The session window is an **exchange day**, not a UTC one: 04:00 ET to 04:00 ET
next day. The events book arms at 15:50 ET, which is the following UTC day for
part of the year, and a UTC window would drop it from every report.
