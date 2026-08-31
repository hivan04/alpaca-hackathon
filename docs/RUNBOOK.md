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
oaa daily-report               # after the close: the session, evaluated -> reports/
```

The loop runs the cycles in `schedule.cycles` and monitors positions in between. It
survives a failed cycle and fires late cycles after a restart, so a crash at 09:40
does not cost the day's first scan.

**After the close, without you.** The `daily_report` cycle fires at 16:20 ET
inside the same process and writes `reports/<profile>/<date>.md`: the ideas it
priced and declined, the gate funnel, the fills, the day's P&L, and a
bullet-point critique from Featherless. `oaa daily-report --date YYYY-MM-DD`
regenerates any past session from the journal - see `docs/DAILY-REPORT.md`.

## The day, in ET

The live cycle grid, as `config/default.yaml` actually schedules it. The
operating half of this runbook — three terminal tabs, what the console shows,
what a restart replays — lives in the notebook note *Live runbook — competition
week*.

| time | what |
|---|---|
| 04:00-16:00 hourly | `events_watch` - the earnings sentiment read, 13 cycles |
| 09:15 | `discover` |
| 09:45 | `events_flatten` - close what reported overnight, into the IV crush |
| 10:00 | `carry_scan` |
| 10:00-14:45 every 15 min | `intraday_scan`, skipping 11:30-13:30 |
| 11:30-15:10 every 15 min | `manage_positions` |
| 15:15 | `intraday_cutoff` - hard, transient books flat |
| 15:45 | `carry_verify` - the sign-off |
| 15:50 | `events_arm` - the direction call and the order |
| 16:10 | `eod_report` &nbsp; 16:20 `daily_report` |

The live agent fired four intraday cycles a day until 30 Aug while the backtest
scanned twelve; those now agree.

**Mind what a restart replays.** `_fired` is in-process memory, so on start every
cycle whose time has already passed today fires again. That is deliberate - a
crash at 15:10 must not skip the 15:15 cutoff - and it is mostly harmless: the
watch dedupes to zero model calls, `intraday_cutoff` and `events_flatten` are
idempotent, and a stale `events_arm` is refused by `_arm_is_too_late`. What is
not free is `carry_scan` and `intraday_scan`, which can open a position on a
restart. **Prefer to restart outside 09:30-16:00 ET**, and check `make status`
afterwards if you cannot.

## Do not run two agents on one account

There is no pidfile, lockfile or singleton guard in `src/`. A second
`oaa run --profile judged` - a forgotten tab, or pm2 alongside the terminal - is
two runners appending to the same journal and racing the same
`position_ledger.json`, and the ledger has no writer arbitration. An
unattributed leg is swept as transient at 15:15, which may be one leg of a
multi-session condor.

**This has already happened.** On the evening of 30 Aug two judged runners were
live at once. Nothing surfaced it: `oaa status` renders both as truncated
interpreter paths with no profile and no account, and the banner said UP either
way. It cost nothing only because the book was flat - the exposure was the next
day's `events_arm`, where two runners would have made two direction calls on the
same names and sent two orders against a nightly risk budget each believed it
alone was spending. Identifying it needed `ps -ww -o pid,command -p <pid>`; plain
`ps` truncates one character short of the `--profile` that decides whether a
process is harmless.

Use `--profile dev` for anything experimental - a different account and a
different run directory.

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
| `chain` returns nothing, or "no contracts survived the liquidity filter" | **usually not liquidity.** An empty `ChainView` means the requested DTE window contains no listed contract - see the 31 Aug finding in `docs/STRATEGY-INTRADAY.md`. The filter predicates never even run | `oaa chain --why` names the line of config that emptied it. Loosen `options.min_open_interest` / `max_bid_ask_spread_pct` only once you have ruled the window out |
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
