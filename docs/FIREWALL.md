# The capital firewall

One account, three books, one boundary. This document is the operational
reference; `docs/ARCHITECTURE.md` §3 has the summary.

## What changed, and why

The previous design handed the whole account back and forth nightly: the
intraday book traded, went flat at 15:15, and an overnight book took the capital
at 15:55 and returned it at 09:35.

The carry book is now **resident**. It holds short-premium structures for 3–10
sessions because theta accrues on calendar days — including the weekend inside
the judged window — and a nightly round trip would pay the spread twice for no
change in thesis. So the firewall's purpose changed from *nightly handoff* to
*protecting the resident book's margin from the transient tenants*.

## The two layers

### Layer 1 — temporal (`firewall/clock.py`)

| Boundary (ET) | Phase entered | Who may open |
|---|---|---|
| 09:30 | `OPEN_SETTLE` | nobody — quotes are widest here |
| 09:45 | `INTRADAY` | intraday |
| 10:00 | `ACTIVE` | intraday + carry |
| 14:45 | `CARRY_ONLY` | carry |
| 15:00 | `WIND_DOWN` | nobody |
| 15:15 | `INTRADAY_CUTOFF` | nobody |
| 15:45 | `CARRY_VERIFY` | nobody |
| 16:00 | `CLOSED` | nobody — the carry book is **held** |

Validated on load: boundaries strictly increasing, and at least 15 minutes
between the cutoff and verification so liquidations have time to settle.
`TZ=America/New_York` is pinned everywhere, and the process must not run on a
laptop that sleeps.

### Layer 2 — capital (`firewall/lock.py`)

```python
carry_reserve  = max(live carry marks, equity × carry_max_equity_pct)
transient_lease = min(
    (regt_buying_power − carry_reserve) × transient_utilisation,
    equity × transient_max_equity_pct,
)
```

Reg T is read on a **fresh poll** every time, never from a cached number. The
carry requirement is measured from live marks on the legs the ledger attributes
to `carry`, not from a figure computed at entry.

## The position ledger (`firewall/ledger.py`)

`symbol → book`, persisted as JSON beside the journal.

* **Persisted** so a restart at 15:10 cannot cause the cutoff to liquidate a
  resident condor.
* **Unattributed legs are transient.** A leg the ledger has never seen is not
  something the system deliberately chose to hold overnight. Closing it at 15:15
  is the recoverable error.
* Reconciled against live positions on every firewall call, so closed positions
  drop out on their own.

## 15:15 — the transient cutoff

1. Poll the account, split resident from transient.
2. `cancel_all()` **first**, so nothing fills into the liquidation.
3. Close each transient position, then **poll until flat** (4 attempts, 5 s apart).
4. Only if flat is *confirmed*: lock the transient books for the rest of the day
   and release the lease.

Two properties that are easy to get wrong and are tested directly:

* a 200 from `close_all_positions` means **accepted, not filled**
* **working orders count as "not flat"** — a resting order that fills at 15:59 is
  unexpected exposure into the close

## 15:45 — the carry verification

Zero transient positions, zero working orders, fresh Reg T read, carry margin
covered with cushion, `leverage_headroom ≤ 1.0`.

On failure: emergency-liquidate if configured, then **disable the transient
books for the following session**. A book that needed rescuing does not get
fresh leverage the next morning.

## Submission flatten

`management.submission_flatten_utc` is checked on **every runner poll**, not on
the daily schedule — it is a one-off wall-clock deadline, and "remember to
trigger it on the day" is exactly the plan that fails. It closes the entire
book, resident included, with the same confirmed-flat discipline, then refuses
all further entries.

`management.entry_cutoff_utc` stops new carry structures once the remaining
window is shorter than one can meaningfully decay.

## The property under test

`tests/test_firewall.py::test_the_two_books_never_hold_conflicting_claims_on_the_same_capital`

For every level of carry usage: `carry_claim + transient_claim ≤ Reg T`, and the
transient lease shrinks monotonically as the resident book grows.

## Operating it

```bash
oaa firewall                 # current phase, reservations, lease, ledger
oaa firewall --at 15:20      # simulate a boundary
oaa gates --book intraday    # what the gates refused, and why
```
