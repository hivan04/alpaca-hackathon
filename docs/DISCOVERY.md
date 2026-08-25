# Discovery and the macro lens

Four deterministic specialists already run — a volatility lens, a trend lens, an
event lens and a statistical-arbitrage lens. Each reads a feature vector, and
each is independently backtestable, which is why they are Python and not prompts.

The gap they cannot fill is **macro**, because filling it means reading rather
than computing. That is the one job a language model does better than a feature
vector, and it is the only place one is used in the strategy layer.

## The judgement that actually matters

Not *"is this name hot"* — **"is it hot for a reason its pair partner shares?"**

| | What happens | Verdict |
|---|---|---|
| NAND prices jump industry-wide. SNDK, MU and WDC all spike | the SNDK/MU **spread is unchanged** | shared catalyst — relationship intact, more dislocation to trade. **Not flagged** |
| SNDK announces a customer-specific supply deal. MU doesn't move | the spread dislocates on news that **will never mean-revert** | idiosyncratic — **flag that leg** |

Identical attention scores. Opposite implications. A z-score cannot tell them
apart; it requires reading *why*. The overnight book is hedged against market
moves, not against headlines, so asymmetric news is precisely how a
market-neutral pair loses money.

The lens is deliberately biased against flagging. With roughly five overnight
entries in a competition week, standing down twice costs 40% of the sample.
`reduce` (half size) is the honest middle answer and should fire far more often
than `stand_down`.

## Two disciplines, enforced in code

**1. Attention generates candidates; cointegration still decides.**

A name that just became hot is one whose historical relationship may be
breaking. The test itself is unaffected — being hot today does not change a
500-day history — but the *forward* assumption is exactly what attention calls
into question. Screening a buzz list at face value selects for the names most
likely to fail. So discovery feeds the offline screen and nothing else.

**2. Nothing here enters the gap model's feature set.**

`most_actives` and `movers` are live snapshots with no history. A feature built
on them could not be reconstructed for a past date, so it would silently poison
the walk-forward backtest and every result after it. Only `news` is replayable
(the endpoint takes a date range), and even that is kept out of the model.
Discovery is an **overlay** on a model that stays independently testable.

`score_snapshot(..., replayable_only=True)` enforces this if you ever do want to
build a historical series.

## Sources

| Source | Command | Replayable | Weight |
|---|---|---|---|
| News velocity | `alpaca data news` | **yes** | 0.40 |
| Movers | `alpaca data screener movers` | no | 0.35 |
| Most actives | `alpaca data screener most-actives` | no | 0.15 |
| External | any JSON endpoint | depends | 0.10 |

News is weighted highest because it is replayable *and* carries a reason. Volume
is weighted lowest — a mega-cap is always in the most-actives list, and that fact
carries almost no information.

Scores are **rank-normalised within each source** before blending. A stock with
20x the volume of another is first, not "twenty times hotter", and the raw units
(shares, percent, article counts, a vendor's 0–100) are not comparable anyway.

### Why not a scraper

Alpaca answers this question itself, and using it beats scraping on every axis
that matters here: no ToS exposure, no parser that breaks when a page changes,
no rate-limit games, and it scores better on Technology Implementation. A
scraper that dies on day 3 of a 7-day window costs the P&L score.

`HttpJsonSource` is the escape hatch — point it at any JSON endpoint you have
the right to call (a sentiment vendor, a sponsor feed, an official social API):

```yaml
external:
  enabled: true
  url: "https://api.example.com/v1/trending"
  symbol_path: "data[].ticker"
  score_path: "data[].score"
  api_key_env: "PARTNER_SENTIMENT_KEY"
```

## Tradability filters

A buzz list is full of things you cannot trade this strategy on. In rejection
order:

- **not shortable / hard to borrow** — the one that bites silently. The pairs
  trade shorts a leg, so an unborrowable candidate fails at 15:55 with the
  collar already bought
- **leveraged or inverse** — daily-rebalance decay means they cointegrate with
  nothing. They appear in most-actives constantly
- **no listed options** — cannot be collared, so cannot be held overnight
- price outside bounds, insufficient history

## The candidate pool

**Accumulates.** A name hot on Tuesday and quiet on Wednesday is still a
candidate; taking only today's top twenty throws away yesterday.

**Ranks by persistence before intensity.** Seen on four of five days beats one
loud spike — a durable theme beats a headline.

**Additive-only.** Pairs enter the live universe when they pass cointegration
and are never dropped for falling off a buzz list. Churning the tradable set is
chasing, and chasing is what destroys a mean-reversion strategy.

Seeds (hand-picked, economically-linked names) are always screened and never
evicted.

## Multiple testing — read this before widening the pool

N symbols means N×(N−1) ordered cointegration tests. **24 symbols is 552 tests**,
and at a flat `p < 0.05` you would expect roughly 27 "cointegrated" pairs from
pure noise — indistinguishable from the real ones.

`find_pairs.py` applies FDR control by default, tightening the threshold with
the size of the pool. Disable it with `--no-fdr` only if you know why.

Statistical significance alone is not enough. **Require economic rationale too**
— same sector, a real supply-chain link, a spin-off relationship. SNDK/MU passes
on rationale; SNDK/Chipotle might produce a beautiful p-value and be pure data
mining.

## Using it

```bash
oaa discover                  # attention ranking + tonight's regime read
oaa discover --no-llm         # deterministic breadth rule, zero token cost
oaa pool                      # the accumulated candidate pool

python scripts/find_pairs.py --from-pool --additive --write
```

Cadence: `discover` runs pre-market (09:15 ET) for the pool and the morning
regime, and the view is refreshed at 15:45 before the overnight decision — that
is when it actually gets used. Two small model calls a day, trivial next to the
agent cycles.

Screen **weekly, not daily**. Cointegration statistics barely move day to day;
adding one bar to 500 changes the p-value in the fourth decimal. The *pool*
refreshes daily, the *approved universe* does not.

## What it changes, honestly

Over a five-session competition the candidate-generation half will not move
P&L — discovering, screening and trading a new pair enough times for an edge to
show is a three-month feature.

The flagging half is worth more than it looks because it is asymmetric: it can
only prevent trades, and the ones it prevents carry unhedged idiosyncratic gap
risk. It fires rarely, but the magnitude when it does is your worst night.

The larger return is on the other criteria: "demonstrate how your agent
identifies opportunities" is otherwise answered by a hardcoded YAML file, and
*"the agent declined this pair because SNDK was moving on a story MU did not
share"* is the strongest single line available for the demo.
