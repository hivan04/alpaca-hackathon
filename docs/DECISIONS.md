# Design decisions

Short record of the choices that are not obvious, and why. Useful for the deck —
"Creativity & Originality" is scored on the thinking, not the line count.

### One capital lock, not two risk budgets

The obvious way to run two books is to give each a share of equity. It does not
work: buying power is a property of the *account*, and Alpaca's 4x intraday
allowance evaporates at 16:00. Two books sizing independently will eventually
both be right and jointly over the overnight limit.

So there is one lock, held by one book at a time, and the handover is a
liquidate-confirm-verify sequence rather than a clock tick. See `docs/FIREWALL.md`.

### Liquidation is confirmed, never assumed

`close_all_positions` returning 200 means the orders were accepted. An unfilled
liquidation at 15:15 looks identical to a successful one in the response body.
The cutoff therefore polls the account and retries — up to four rounds — and
reports `confirmed_flat` based on what the account actually shows. Most
implementations of this pattern skip the poll, and the skip is invisible until
the day it costs the account.

### A rescue at 15:54 still aborts the night

If verification finds rogue positions it liquidates them *and abandons the
overnight entry anyway*. The tempting behaviour is to fix the problem and carry
on. A book that needed rescuing ninety seconds before the close has already
demonstrated that something upstream is wrong; handing it fresh leverage is how
a small bug becomes a margin call.

### Protective options are bought BEFORE the equity legs

The pairs trade is four orders and MLEG cannot mix equity with options. The
instinct is to establish the position and hedge after. That is backwards: if the
equity legs fill and the options fail, the account carries an unhedged overnight
short. If the options fill and the equities fail, it carries two cheap long
options. Every partial failure should leave a bounded state, so protection goes
first and anything that filled is unwound at market on a failure.

### The agent reads over MCP and writes over the CLI

Two different tool surfaces, deliberately. The model queries Alpaca directly
through the MCP server — real autonomy, not a scripted pipeline with an LLM
bolted on. But `place_option_order` and friends are withheld from it: every
write goes through a first-party tool that routes via the firewall and the risk
engine, and out over the Alpaca CLI, so each order is an auditable shell command.
An agent that can place a raw order can bypass the 15:54 gate, and the entire
point of the gate is that nothing can.

### The Kalman filter is seeded from differences, not levels

Over a short window a random-walk price barely moves, so a level regression of
y on x is near-collinear with its own intercept. It hands alpha an absurd value
that beta then has to cancel, and the filter spends hundreds of observations
walking it back out — during which the z-score, the number the whole strategy
trades on, is wrong. Regressing `diff(y)` on `diff(x)` removes the intercept
entirely. Recovery of a known beta went from ±0.17 to ±0.01.

### Quantiles set the strikes, not a human

The q05 and q95 of the gap model are not decoration on the forecast — they are
the put and call strikes. That is what makes the overlay adaptive: on a quiet
night the hedge sits close and cheap; on a volatile one it widens automatically.
Bounded at both ends, because a strike 20% away is a lottery ticket and an ATM
overnight hedge costs about as much as the move it insures against.

### Round lots only

Option contracts cover 100 shares. A 250-share leg hedged with 2 contracts is
not a defined-risk position, it is a 50-share naked exposure with paperwork. So
share counts are forced to multiples of 100 and the resulting dollar-neutrality
error is *reported* in `meta.hedge_error_pct` rather than quietly ignored.

### Attention generates candidates; cointegration still decides

The intuitive use of a "hottest stocks" feed is to trade what it surfaces. For a
mean-reversion strategy that is close to backwards: a name that just became hot
is one whose historical relationship may be breaking, so ranking by buzz selects
for the candidates most likely to fail forward. The feed populates the offline
screen and touches nothing else.

### The macro lens judges shared versus idiosyncratic, not loud versus quiet

A sector-wide move leaves a pair's spread intact and actually improves the
trading environment. An idiosyncratic move on one leg dislocates the spread on
news that will never mean-revert. Both look identical in an article count. The
overnight book is hedged against market moves and not against headlines, so this
distinction is the entire value of the lens — and it is the one thing in the
strategy layer that genuinely needs reading rather than computing.

### Nothing from discovery enters the model's features

`most_actives` and `movers` are live snapshots with no history. A feature built
on them cannot be reconstructed for a past date, so it would silently invalidate
the walk-forward backtest and every number derived from it. Only `news` is
replayable, and discovery is kept as an overlay on a model that stays
independently testable.

### The LLM is never on a mechanical path

The 15:15 liquidation, the 15:54 verification and the 09:35 exit are never
agent-driven. There is nothing to reason about in them, a language model in the
path of a safety-critical liquidation is a failure mode rather than a feature,
and it is also the single largest avoidable token cost. `collar_widening` is
bounded below at 1.0 for the same reason: the lens may widen protection, never
narrow it.

### Defined risk only, enforced in code

`risk.allow_undefined_risk` defaults to false and the risk engine refuses any
structure whose `max_loss` is not computable. P&L is scored over a single week; one
uncapped loss ends the run. Every shipped strategy returns a structure with a
calculated maximum loss, and the engine checks rather than trusts.

### The LLM never approves anything

The critic scores ideas and writes the reasoning. The deterministic risk engine emits
a signed stamp, and execution refuses tickets without one. If the LLM is down, the
loop degrades to a transparent heuristic score and keeps trading — uptime is worth
more than prose in a P&L-scored week.

### Two strategies that fire on opposite regimes

`vol_carry_condor` wants rich IV and no trend. `momentum_debit_spread` wants a
confirmed trend and cheap IV. They are near-mutually-exclusive by construction, so
the book gets diversification from one universe rather than from two correlated bets.
There is a test asserting exactly this.

### Sizing from max loss, not from capital

`size_by_risk` divides the per-trade risk budget by the structure's own maximum loss.
Every position therefore risks the same fraction of equity regardless of how wide the
spread is — which is the property that makes a hit-rate strategy survive a bad day.

### Deterministic client order IDs

`client_order_id = sha256(idea.id | symbol | structure | qty | step)`. A retry after
an ambiguous failure produces the same ID, and the router checks for an existing
order before resubmitting. Double-fills are the classic way an autonomous agent
destroys a paper account overnight.

### Chase the fill, but bound the slippage

Multi-leg limits at mid frequently do not fill. The router walks the price toward the
far touch over `chase.steps`, cancelling between attempts, and logs a warning past
`max_slippage_pct`. Filling at mid in the backtest and crossing the spread live is
how a strategy looks profitable on paper and is not.

### Four broker backends behind one protocol

The event rewards MCP *or* CLI usage. Rather than pick one and bolt the other on for
the demo, all four (`rest`, `cli`, `mcp`, `sim`) implement the same `Broker`
protocol, so any of them can run the real loop. `sim` also means the entire pipeline
is testable offline.

### The journal records rejections

Most agent projects log what they did. The interesting artefact is what the system
*refused* to do and which rule stopped it — that is the evidence that the risk layer
is real, and it is the most compelling thing to put on screen in the demo video.

### The overnight backtest is real; the intraday one is a replay harness

The overnight strategy's holding period is close-to-open, and daily bars contain
both ends of it, so its P&L can be reconstructed exactly — walk-forward, with
the same entry gates the live path uses. The options overlay is *modelled*
because no historical chain with greeks exists on the free tier, and every
assumption in `backtest/pricing.py` points against the strategy: implied vol
marked up over realised, half the spread crossed on entry, exit valued at
intrinsic only.

The intraday engine is a replay harness rather than a backtest, and says so. It
catches sizing and risk-limit bugs; it is not evidence of edge. The judged
number is live paper P&L either way.

### Partner adapters can only veto

Sponsor technology plugs into seven named pipeline stages. At the `risk` stage an
adapter can block a trade but never approve one. A third-party SDK cannot widen the
risk envelope — which is what makes it safe to integrate one under time pressure on
day one of the event.
