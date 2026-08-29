# Runbook

## Before kickoff (do this now)

1. **Two paper accounts, not one.**
   - A *judged* account — the ID you submit. Nothing but the working agent touches it.
   - A *dev* account — everything else. Broken loops, fat fingers, experiments.

   Everything in the judged account is on the record. Judges see the full activity
   history, not a summary you curate.

2. Put both key pairs in `.env` (`ALPACA_*` = judged, `ALPACA_DEV_*` = dev) and record
   `ALPACA_JUDGED_ACCOUNT_ID`. Write that ID somewhere outside this repo too.

3. Verify they are genuinely different accounts:

   ```bash
   ./scripts/verify_accounts.sh
   ```

4. Confirm options level 3 (multi-leg spreads need it):

   ```bash
   oaa doctor
   ```

   If the level is below 3, raise it in the Alpaca dashboard, or programmatically via
   `AlpacaRestBroker.ensure_options_level(3)`.

## Is it running? (start here)

One command, any fresh terminal, no other windows needed:

```bash
make status            # or: oaa status --profile judged
oaa status --watch 30  # leave it up on a second monitor
oaa status --json      # for a script or a cron check
```

It answers, in this order:

| line | what it tells you | how it can lie to you |
|---|---|---|
| LIVE / UP - MARKET CLOSED / UP BUT STALE / NOT RUNNING | pm2 (or `ps`) plus journal freshness, read against the session clock | a loop on another host shows as NO PROCESS VISIBLE |
| AI layer | whether the model ran or fell back to rules | — this is the line that was silent on 28 Aug |
| screening | scanned / tradable / pool, and **why** candidates were dropped | — |
| source failures | a data feed that died, e.g. the news 400 | — |
| regime | the macro read and which books stood down | — |
| account | equity, day P&L, open positions | — |

**UP BUT STALE** means a process is alive but nothing has been written to the
journal for 45 minutes *during a session* — a wedged loop, which looks identical
to a healthy one in `pm2 ls`. That distinction is the reason this command exists.

**UP - MARKET CLOSED** is the same silence outside a session, which is the
schedule working: the runner writes nothing between the 16:10 report and the
next morning's discover, and nothing at all over a weekend. It shows what it is
waiting for instead of how long it has been quiet. Note there is no holiday
calendar behind that next-open time — on a market holiday it will name a day the
exchange is shut.

It is read-only: it opens files and asks the process table. It cannot place,
size or cancel anything.

## Daily operation

```bash
oaa doctor                     # 30 seconds, catches everything before it matters
oaa scan                       # dry: what would it do right now?
oaa run --profile judged       # the autonomous loop
oaa report                     # equity curve + decision stats -> HTML
```

The loop runs the cycles in `schedule.cycles` and monitors positions in between. It
survives a failed cycle and fires late cycles after a restart, so a crash at 09:40
does not cost the day's first scan.

## Going live on the judged account

The gate is deliberate and has three parts:

```bash
oaa scan --profile dev               # 1. behaviour looks sane in dev
oaa doctor --profile judged          # 2. judged account reachable, level 3
oaa run --profile judged             # 3. dry_run is false in config/judged.yaml
```

Do not skip step 1 for a day. A misfiring agent writes junk into a history you cannot
delete.

## Kill switches

| Situation | Action |
|---|---|
| Stop opening new positions | `OAA_EXECUTION__DRY_RUN=true` and restart |
| Stop everything, keep positions | Ctrl-C — it finishes the current cycle and exits |
| Close everything now | `oaa flatten --profile judged --yes` |
| Cancel resting orders only | `alpaca order cancel-all` |

The risk engine also halts itself: past `daily_loss_limit_pct` it stops for the day,
past `max_drawdown_halt_pct` it stops entirely. Both are recorded in the journal.

## Before the submission deadline

1. `oaa flatten --profile judged --yes` — realise the P&L rather than leaving it
   marked. Do this with enough time for the fills to settle.
2. `oaa report --profile judged` — the HTML page is deck-ready and self-contained.
3. Screenshot the Alpaca dashboard equity curve as a second, independent source.
4. Check `ALPACA_JUDGED_ACCOUNT_ID` matches the account the report came from.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `chain` returns nothing | liquidity filter too tight for the feed | loosen `options.min_open_interest` / `max_bid_ask_spread_pct` |
| Every idea rejected with `rule=sizing` | `max_loss` exceeds the per-trade cap | widen the cap, or narrow the wings so the structure is cheaper |
| `rule=time_window` | inside the open/close no-trade window | expected; check `no_trade_open_minutes` |
| 403 on a multi-leg order | options level below 3 | `oaa doctor`, raise the level |
| Greeks all `None` | free `indicative` feed | expected — chain selection falls back to a moneyness proxy |
| Data looks 15 minutes stale | free tier withholds recent history | expected; `data.delayed_minutes` accounts for it |
| MCP backend will not start | `uvx` missing or keys unset | `./scripts/install_tools.sh`, then `oaa mcp-tools` |
| Rate-limit warnings | too many symbols in the universe | shorten `universe.symbols` — a full chain is expensive |

## What the judges actually read

- The account history (P&L Performance)
- `journal.jsonl` and the dashboard — including the trades the agent *declined*
  (Presentation & Execution: "the reasoning behind its strategy and results")
- The repo (Technology Implementation)

Point 2 is the one most teams leave empty.
