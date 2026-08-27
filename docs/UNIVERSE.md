# Investment universe

One line per symbol, per book. Every symbol faces its book's full gate stack —
the tiers govern **how many** of a kind can be open at once, not how easy it is
to get in.

> Status: proposed. `config/default.yaml` still carries the old universe until
> this is wired.

---

## Carry book — resident, held 3–10 sessions

### Core · index anchor

| Symbol | Why |
|---|---|
| `SPY` | Deepest option market there is. Low IV rank, so it rarely clears the premium gate — it's the anchor, not the earner |
| `QQQ` | Tech beta at index-tier spreads |
| `IWM` | Higher structural IV than SPY and genuinely decorrelated from QQQ — the index most likely to actually fire |

### Tilt · sector ETFs — where the risk budget goes

| Symbol | Why |
|---|---|
| `SMH` | Semis. Most of the tech variance premium, ETF spreads, no earnings date |
| `XLK` | Broad tech. Tighter than SMH, lower IV — the conservative version of the same trade |
| `XLE` | Energy. Uncorrelated with tech, frequently high IV rank |
| `XLF` | Financials. Uncorrelated. Low price — needs its own wing width |
| `XBI` | Biotech. Highest IV of the set and the best diversifier a short-vol book can hold |

### Satellite · single names

| Symbol | Why |
|---|---|
| `AAPL` | Deep chain, weeklies, no earnings in the judged window |
| `MSFT` | As above |
| `GOOGL` | As above |
| `AMZN` | As above |
| `META` | As above |
| `NVDA` | Reported 26 Aug — IV crushed, so the premium gate will reject it all week. Kept in deliberately: the rejection is good evidence |

### Position caps

| Cap | Value |
|---|---|
| Concurrent positions, whole book | 5 |
| Tilt tier | ≤ 3 |
| Satellite tier | ≤ 2 |
| Per sector, across all tiers | ≤ 2 |
| Risk per position | 1.5% of equity |
| Aggregate risk | 8% of equity |

A legal book: `SMH + XLE + XBI + AAPL + IWM`.
Never legal: five semis names, or five single names.

### Excluded, deliberately

| Symbol | Why |
|---|---|
| `AVGO` | Reports 2 Sep — dead centre of the judged window |
| `CRM` `MRVL` `CRWD` `ZS` `DELL` | Off-cycle reporters clustered around the same dates |
| `TSLA` | IV rank chronically elevated, so it passes the premium gate almost always and would dominate a five-position book — with the worst gamma profile in mega-cap tech |
| Any single name intraday | See below |

---

## Intraday book — transient, flat by 15:15

| Symbol | Why |
|---|---|
| `SPY` | Penny-wide 0–2 DTE market |
| `QQQ` | Same |

**Nothing else, ever.** Not a safety call — arithmetic. The target is 5–15% of
premium, so $10–30 on a $2.00 contract. A $0.10-wide single-name quote costs
$20 round trip, which is the entire target. The edge doesn't shrink on single
names, it's gone.

| Cap | Value |
|---|---|
| Concurrent positions | 2 |
| Trades per day | 6 |
| Risk per trade | 0.5% of equity |
| Daily loss limit | 2% — book disabled for the session on breach |

---

## Opportunistic book — dormant unless a dated print is due

| Symbol | Why |
|---|---|
| `SPY` | Index proxy, tight enough for a short hold |
| `QQQ` | Same |

| Cap | Value |
|---|---|
| Max risk | 2% of equity |
| Never opens while | the carry book is at its aggregate cap |
| Requires | macro lens returning `guidance: trade` |

---

## Verify at kickoff

- [ ] All 14 carry symbols are optionable on the Alpaca chain, with **weekly
      expiries** — a monthly-only symbol can't reliably hit 7–14 DTE
- [ ] Live quote widths on the sector ETFs — the cost gate is a placeholder
      until measured
- [ ] Ex-dividend dates per symbol. The SPDR September 2026 schedule runs 1, 11,
      21 and 25 Sep; the first two land inside or against the window
- [ ] Whether historical IV is available. If not, lean on the IV−RV spread and
      treat IV rank as advisory until ~15 sessions of snapshots have accumulated

## Changes this needs in code

- Wing width as a **percent of spot** with a one-strike floor, not a flat 5
  points — `0.008` gives 5pts on SPY, 2 on IWM, 1 on XLF
- `max_positions_per_sector` — the current cap is per-underlying and blind to
  correlation
- Tier caps on the carry universe
- Populate `ex_dividend_date`; that half of the event gate is currently inert
