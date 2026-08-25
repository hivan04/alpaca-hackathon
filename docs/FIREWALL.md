# The temporal firewall

## The failure it prevents

Alpaca grants **4x day-trading buying power** but only **2x Reg T overnight**. If
intraday leverage is still on the books at 16:00 ET, the broker liquidates the
account at whatever price it likes. Inside a one-week judged window that is not
a setback — it is the end of the submission.

Two books sharing one account will do this to you eventually. Not because the
strategies are bad, but because "the intraday liquidation didn't fill" and "the
overnight sizing used a cached buying-power number" are both ordinary Tuesday
events.

## The mechanism

A **sequential lock-and-verify**. One capital lock, two books, and a book cannot
open a position unless it holds it.

```
09:35  overnight_exit      liquidate the overnight book
                           POLL until confirmed flat -> release the lock
10:00  intraday acquires   confirm nothing survived 09:35, take the lock,
                           budget = 50% of day-trading buying power
15:00  last intraday entry
15:15  INTRADAY CUTOFF     cancel all working orders
                           liquidate every position
                           POLL until confirmed flat (4 attempts, 5s apart)
                           lock the day book out for the rest of the day
                           release the capital lock
15:45  overnight_signal    Kalman + ML compute. Nothing is routed.
15:54  THE GATE            re-poll Alpaca
                           confirm zero positions AND zero working orders
                           read FRESH regt_buying_power
                           size against it -> acquire the lock
15:55  overnight_entry     only possible while holding the lock
16:00  close
```

### Layer 1 — temporal

`may_open(book, now)` returns false unless the session phase matches the book.
The intraday book cannot open at 15:56; the overnight book cannot open at 11:00.
The phase machine lives in `firewall/clock.py` and is driven by one ET clock
that the whole system shares — the runner, the risk engine and the agent all
ask it what time it is, so there is no second clock to disagree.

### Layer 2 — capital

The overnight size is scaled against `regt_buying_power` **read after the
intraday book has been proven flat**, never a cached figure and never the
day-trading number. Two further caps apply: `overnight_regt_utilisation` (0.95)
and `overnight_max_equity_pct` (0.50, a hard ceiling on gross overnight
exposure regardless of what margin permits).

## The three details that actually matter

**1. Liquidation is confirmed, not requested.**
`close_all_positions` returning 200 means the orders were *accepted*. An
unfilled liquidation at 15:15 looks identical to a successful one in the
response body. The cutoff therefore liquidates, polls, and liquidates again —
up to four rounds — and reports `confirmed_flat` based on what the account
actually shows.

**2. A rescue at 15:54 still aborts the night.**
If positions are found at verification, they are liquidated (`emergency_liquidate`)
*and the overnight entry is abandoned anyway*. A book that needed rescuing
ninety seconds before the close does not then get handed fresh leverage. This
is the part most implementations get wrong: they fix the problem and carry on.

**3. Positions AND working orders both count as "not flat".**
A resting limit order at 15:54 can fill at 15:59 and put the account into an
overnight position nobody sized for.

## Verifying it

```bash
oaa firewall                 # phase, lock owner, budget, both books' permissions
oaa firewall --at 15:54      # simulate the gate
oaa firewall --at 15:20      # neither book may open during the cutoff
```

The test suite asserts the property directly — see
`tests/test_firewall.py::test_the_two_books_never_hold_capital_simultaneously`,
which walks a full day and checks the lock is never held by both, plus 33 others
covering the phase boundaries, failed liquidations, oversized targets and the
emergency path.

## Configuration

Every boundary is a config value (`firewall.times` in `config/default.yaml`) and
they are validated on load: the times must be strictly increasing, and there
must be at least 15 minutes between the cutoff and verification so liquidations
have room to settle. A config that violates either fails at startup rather than
at 15:15.

To disable it entirely — for a backtest, or a single-book setup:

```yaml
firewall:
  enabled: false
```

Do not do this on the judged account.

## What the judges see

Every cutoff and every verification is written to `journal.jsonl` as a
`firewall_cutoff` / `firewall_verify` event with its full check dictionary. That
log is the evidence that the risk layer is real rather than decorative, and it
is the most convincing thing to put on screen in the demo video: an agent that
*declined* to trade at 15:54 because it found a stray position is a better
story than one that simply worked.
