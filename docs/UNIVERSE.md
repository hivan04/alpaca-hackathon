# Investment universe

**Eight index and sector ETFs**, traded by both books. Set 28 Aug on measured
liquidity and correlation; supersedes the mixed ETF-and-mega-cap list.

| | ticker | role |
|---|---|---|
| **Broad equity** | SPY | S&P 500 — the deepest option market listed |
| | QQQ | Nasdaq 100 |
| | IWM | Russell 2000 — small caps, a different beta |
| | DIA | Dow 30 |
| **Sectors** | XLF | financials — near-independent of tech (0.08 to SMH, 0.20 to XLK) |
| | XLE | energy — **negatively correlated with everything else** |
| **Decorrelated** | TLT | 20y+ Treasuries — duration, not equity beta |
| | GLD | gold |

Configured in `config/default.yaml` (`universe.symbols`) and per book in
`config/strategies/*.yaml`. `event_premium` uses SPY and QQQ only.

---

## The rule

The universe is defined by a rule, not by a list. Three criteria, applied in
order:

**1. Measured median option spread ≤ 2% of mid.**
Not a tier assumption — the measured number, from
`python scripts/universe_report.py --live`. This is the arithmetic in
`COST_STRUCTURE.md` §5: the intraday book targets 5–15% of premium, roughly
$10–30 on a $2.00 contract, and a $0.10-wide quote costs $20 round trip. A name
whose real spread exceeds the ceiling has no edge left after execution,
whatever its signal looks like.

**2. A name earns its place only if it raises the number of *effective*
independent bets.**
Not the ticker count. Under equal weights, `n / (1 + (n−1)·ρ̄)` — ten names at a
mean pairwise correlation of 0.7 behave like about three. A short-premium book
loses on every position simultaneously when volatility spikes, so effective
breadth is the only thing that limits the damage. Adding a second technology ETF
next to QQQ adds a row to the table and no protection.

**3. No idiosyncratic event risk in a short-volatility book.**
This is what excludes single names. The event gate removes *scheduled* earnings
inside the expiry window, but nothing protects a short condor from a surprise
product headline, a guidance cut, or a downgrade. A basket has no such headline:
the same news moves one constituent and is diluted across the rest.

---

## Why single names went, and why that reasoning is not a P&L fit

NVDA, AMD, TSLA, AAPL and MSFT were removed on 27–28 Aug.

NVDA was the demonstration: 24 trades, −$1,254, and $2,863 of a $4,852 drawdown
concentrated in three days. But **that is evidence for rule 3, not the reason
for it.** Selling a defined-risk condor on the highest-volatility, widest-spread
name available means being short gamma on the most gamma-heavy underlying in the
book — the loss was the predicted consequence of a structural mismatch, and the
prediction holds *a priori*.

The honest test of whether a universe rule is a rule or a curve fit is whether
it also removes the names that made money. **AAPL and MSFT were profitable** in
the same backtest — +$479 and +$93 — and they are gone too, because rule 3
applies to them identically. Keeping the winners and dropping the loser would
have been fitting the last twelve weeks and calling it a strategy.

For the same reason, the list above is not final: it is whatever the rule
returns when the measurements are re-run.

---

## What the measurement said (28 Aug)

`scripts/universe_report.py --live`, 160 sessions to 22 Aug.

| symbol | ann. vol | chain-median option spread |
|---|---|---|
| DIA | 9.8% | 2.59% |
| SPY | 11.4% | 2.62% |
| TLT | 5.4% | 2.68% |
| QQQ | 18.2% | 2.85% |
| IWM | 15.7% | 2.94% |
| GLD | 14.8% | 4.76% |
| XLE | 17.8% | 11.79% |
| XLF | 12.0% | 13.10% |
| ~~SMH~~ | 37.2% | 12.49% |
| ~~XLK~~ | 25.9% | **27.94%** |

Correlation of daily returns, the part that matters:

- **QQQ / XLK 0.96, XLK / SMH 0.93, QQQ / SMH 0.91** — one instrument wearing
  three tickers
- SPY / QQQ 0.92, SPY / DIA 0.84 — broad equity is largely one bet
- **XLE is negative against everything: −0.18 to −0.36**
- TLT 0.14–0.39, GLD 0.06–0.39 — the other genuine diversifiers
- XLF / SMH 0.08, XLF / XLK 0.20 — financials are near-independent of tech

> **Mean pairwise correlation 0.36. Ten symbols behaved like 2.4 independent
> bets.**

### The two cuts

**XLK removed.** The worst name in the universe on *both* criteria at once: the
widest option market by an order of magnitude (27.94%) and 0.96 correlated with
QQQ, which is already held. It contributed nothing QQQ does not, at ten times
the cost.

**SMH removed.** Same argument at 0.91–0.93 and 12.49%.

**XLE kept despite an 11.79% chain-median spread**, because it is the only name
in the table negatively correlated with the rest. A short-premium book's entire
failure mode is every position losing simultaneously, so a negatively correlated
name is worth more than three more equity ETFs. This is rule 1 yielding to rule
2 deliberately, and it is the one place the rules conflict.

### A caveat on the spread figures

Those are **whole-chain medians**, which average in far-OTM strikes nothing
trades and overstate the real cost. The report now measures 3–21 DTE within 10%
of spot — where the books actually go — and reports both. Re-run before quoting
these numbers anywhere: the ranking is trustworthy, the levels are not.

Worth noting against the gates: `intraday_momentum.spread_gate.
max_relative_spread` is **2%**, and the tightest name here measured 2.59% on the
whole chain. If the near-the-money figure is still above 2%, that gate is
unpassable and is a second reason the intraday book has never traded.

---

## Known caveats

**Even at eight names the book is not as diversified as it looks.** SPY, QQQ,
IWM and DIA are all US equity beta and correlate 0.64–0.92 with each other.
XLE, TLT and GLD carry almost all of the genuine breadth. Re-run
`scripts/universe_report.py` after the cuts to see where the effective-bets
figure lands — it should rise from 2.4, and that number, not the ticker count,
is what limits damage in a vol spike.

**Short-dated expiries are uneven.** SPY and QQQ list daily expiries. The other
six list weeklies, so with `selection.dte_max: 2` they are only tradable
Wednesday to Friday and contribute nothing Monday or Tuesday. Raising `dte_max`
to 5 makes the whole universe tradable all week at the cost of less gamma per
dollar — an open decision, not an oversight.

**Wing width is now a fraction of spot** (`structures.wing_width_pct: 0.015`),
because the universe spans roughly a $50 ETF to a $600 one and a flat 5-point
wing meant something different on each. On the cheapest names 1.5% can still
fall below one strike increment, which is why XLF declines some structures with
*"no listed strike sits outside the short strikes"* — safe (it refuses rather
than building a broken condor) but it means XLF contributes less than it should.
A floor of one strike increment is the fix.

**XLF and XLE are the marginal names on cost.** Both measured above 11% on the
whole chain. They are held for breadth, but if the near-the-money measurement
confirms wide quotes, they may be paying for their diversification twice - once
in spread and once in a lower pass rate at the cost gate.

---

## Enforcement

`tests/test_strategies.py::test_no_book_trades_single_names` asserts that every
enabled strategy's universe is `index_etf` tier. A single name cannot be added
back without the test failing, which is deliberate: the rule should be harder to
break than to follow.

## Re-deriving the list

```bash
python scripts/universe_report.py            # correlations, vol, effective bets
python scripts/universe_report.py --live     # adds measured option spreads
```

Bars come from the backtest cache, so the correlation half costs no API calls.
`--live` needs the market open and pulls one chain snapshot per symbol.
