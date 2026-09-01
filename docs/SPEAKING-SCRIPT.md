# Speaking script — Options Agent

**Target: 5 minutes.** Six slides. Timings are cumulative; the appendix is for Q&A only.

Delivery notes before you start:

- The three strongest lines in the deck are *"eight vetoes in a row is not a strategy"*, *"the spread cost as much as the gross profit"*, and *"the agent's job is to decline."* Slow down on those three and let them land.
- Don't read the cards. The judges can read. Say the thing that isn't written on the slide.
- If you're running long, cut the events book down to one sentence on slide 3 and skip the second half of slide 4. Never cut slide 6.

---

## Slide 1 — Title (0:00 – 0:25)

> Three books, one pipeline.
>
> There's a short-volatility carry book that sells options and waits, a long-gamma intraday book that buys momentum and is flat by the close, and an earnings book where a language model reads the news for three days before a print.
>
> They run through one decision pipeline, and the rule that shapes the whole system is this: **no model is allowed to approve a trade.** Not the critic, not the LLM. One deterministic risk engine signs every ticket.
>
> Seven months of backtest on real Alpaca bars: up 13.8%, Sharpe 2.4, three-and-a-half percent drawdown. I'll show you what's wrong with that number later.

*Transition:* "Start with why there are three of them."

---

## Slide 2 — Three bets that fail in different weather (0:25 – 1:20)

> These aren't three variations on one idea. They're deliberately opposite.
>
> **Carry** sells options, because options are usually priced for more movement than actually happens. It wants nothing to happen, and it holds for three to ten sessions.
>
> **Intraday** buys options, because a move with a mechanism behind it tends to keep going. It wants something to happen fast, and it's flat by ten past three every day.
>
> One is short volatility, the other is long it. So a week that kills one of them is often a good week for the other.
>
> **Events** is different again, and it exists because of a specific failure. The other two books wait for a market condition to cross a threshold — and the recurring problem in this project was that the threshold never crossed. The agent runs all week and opens nothing. Here the entry condition is a date. Broadcom reports on the 2nd of September whether or not any indicator agrees.

*Transition:* "So how does each one decide?"

---

## Slide 3 — Every trade clears a named gate (1:20 – 2:20)

> Every book is a sequence of hard gates. Not a score — hard gates.
>
> That distinction is the whole design. A blended score lets a rich volatility reading paper over an earnings date, and for a book that's short premium that's the one trade you must never take. So earnings inside the expiry window is a veto, and no amount of attractive on the other numbers can outvote it.
>
> Carry needs the volatility to be rich *and* richer than what the stock has actually been doing — that second gate is where the edge actually is. Intraday keeps two gates hard, the VWAP trigger because it decides direction and the spread gate because it's economics rather than evidence — and everything else votes. Events needs a confirmed date, a mispriced move, and a model that's willing to say how sure it is.
>
> **And here's the part I'd actually like you to take away.** Every threshold on this slide has been moved at least once, and always because something was measured. The volatility floor used to be 0.70 and it rejected 304 out of 304 candidates. The intraday spread ceiling was 2% — which turned out to be *below the tightest quote in our universe*, so the gate wasn't selecting within the market, it was rejecting the market. And the intraday book had eight hard vetoes stacked in a row: at roughly 70% pass each, that's 0.7 to the eighth — about six percent. **Eight vetoes in a row isn't a strategy, it's an arithmetic guarantee that nothing ever trades.** So now three of seven confirmations vote, and only the two that have to be hard still are.

*Transition:* "All three books feed one pipeline."

---

## Slide 4 — No model can approve a trade (2:20 – 3:10)

> Six stages, and the live agent and the backtest run the same ones from the same objects — a backtest that skips a stage is a backtest of a different system.
>
> A strategy produces an idea or a rejection. Cost is attached before anything judges it, so even a declined trade carries what it would have cost. The critic scores it and can decline it — **it cannot approve.** The LLM in the events book cannot approve either. Stage four is the only approver, and it signs the ticket the execution router demands.
>
> Fifteen deterministic checks, in a fixed order, in Python. Two of them are highlighted because they're a genuinely non-obvious bug: **brokers net identical option symbols.** Opening the same iron condor twice doesn't create a second position — it doubles the quantity on the same four contracts. So every count-based limit in the system was blind to it, and the book was quietly doubling down instead of opening new trades. Nearly invisible at four cycles a day; severe at twelve.
>
> Sizing is from max loss, not from capital — a structure without a computable max loss is refused outright. And the two day-books share capital behind a firewall: carry's margin is reserved first, intraday leases what's left, and at 15:15 the transient books are liquidated and *polled until confirmed flat*.

*Transition:* "So what did it actually do?"

---

## Slide 5 — Evidence (3:10 – 4:10)

> Seven months, real Alpaca bars, 441 closed trades. Up 13.8%, Sharpe 2.4, drawdown under four percent.
>
> **Now read the red number.** Gross profit was $13,973. The spread cost $12,990. The book paid out almost exactly its own gross profit in execution cost — and that's *with* the cost gate in front of every trade. That's why spread is charged inside the fills rather than added beside them, and it's why the intraday book only trades index ETFs: a ten-cent-wide single-name quote costs twenty dollars round trip against a twenty-dollar target.
>
> The bars are every trade grouped by the rule that closed it, and they say the exits are the strategy. Carry's profit target made twenty-nine thousand; short-strike touches gave back twelve — that's the cost of doing business for a defined-risk structure, it's what keeps it defined. On the intraday side the stop is deliberately wider than the target, because option premium is noisy and a tight stop gets hit by spread flicker alone — and you can see the price of that: seventy-five winners at plus $139 average, forty-three stops at minus $244.
>
> And the split on the right is the honest version. **The carry book carries this. The intraday book lost five thousand dollars.** The events book has no usable backtest at all — four earnings prints per name is not a sample you can walk forward, and I'd rather say that than show you a number I don't believe.

*Transition:* "Which brings me to the last slide."

---

## Slide 6 — Honesty, and the argument (4:10 – 5:00)

> Four things I know are wrong with this, all measured, all in the repo.
>
> Entry is shared code between live and replay. **Exit isn't** — it's reimplemented in the backtest engine and nothing asserts the two agree. Intraday marks in replay are far coarser than a twenty-minute hold, so that book's replayed P&L is readable for candidate flow, not for edge. Paper fills flatter — mid fills, no queue — and spread is the primary loss mechanism, which is exactly what paper doesn't simulate. And five sessions is not a sample: expect ten to twenty trades, which can't separate edge from luck in either direction.
>
> So what am I actually claiming?
>
> **The agent's job is to decline.** 8,569 ideas were generated and 441 were taken. The artefact I'd want you to look at isn't the equity curve — it's the rejection log: 72,753 declined trades, each one recorded with the gate that stopped it and the number it measured.
>
> Gates are hard, not scored. No model can approve. And every number in the config carries the measurement that set it — including the ones that are still guesses, which say so.
>
> Happy to go deeper on any of the three books — there's a slide for each.

---

# Q&A — where to jump

| If they ask | Go to | The answer in one line |
|---|---|---|
| "Why 25-delta shorts? That's aggressive." | Appendix, carry gates + carry exits | Breakeven is set by the exit pair, not the strikes; 25Δ roughly doubles credit-to-width against an unchanged bar. It's a hypothesis under measurement — below breakeven it goes back to 14. |
| "Where's the edge in a VWAP cross?" | Appendix, intraday signal stack | There isn't one, and I don't claim it. The option is leverage and defined risk; the originality is the catalyst gate and the surface-aware selection that declines when IV rank is above 0.85. |
| "How do you stop the LLM hallucinating an earnings date?" | Appendix, events — the date slide | The model proposes, a file confirms. A wrong date isn't a bad trade, it's a position against no event at all. CPRT ships unconfirmed and cannot trade. |
| "Isn't prompt injection a risk?" | Appendix, events — the watch | Evidence is fenced, stripped, truncated, parsed to a fixed schema and every field clamped. The worst a poisoned post can do is move a bounded confidence score — and nothing the model returns can authorise a trade. |
| "How do you know the model isn't just rubber-stamping?" | Appendix, events — the watch | Abstention rate is journalled on every run and a zero rate logs a warning. A model that never declines isn't filtering. We shipped that bug once already with a critic that scored eighty candidates and declined none. |
| "Why did the events book only take one trade?" | Appendix, events — divergence | Because it was measuring volatility and trading direction. A directional structure bought at a fair implied move on a coin-flip direction call returns minus the round trip in expectation. The expression now follows the sign of the divergence. |
| "Which gate rejects the most?" | Appendix, rejection funnel | The carry premium gate at 15,334 — but it's a filter, not a block: entries occurred on 43% of sessions. Spread is third, and it should be. |
| "Why these fourteen symbols?" | Appendix, universe rule | Measured spread and *effective independent bets*, not P&L. We cut XLK at 0.96 correlation to QQQ and ten times the spread, and kept XLE despite a wide chain because it's the only negative correlation in the book. |
| "What would you do with another week?" | — | Close the exit-path gap between live and replay, fix intraday mark resolution, then re-measure. In that order — nothing downstream is trustworthy until the marks are. |

## Two questions worth pre-empting

**"Your intraday book loses money — why is it still on?"**
Because its replayed P&L isn't measurable yet: the option marks in replay are far coarser than a twenty-minute hold, so those losses are mostly an artefact of mark resolution, not evidence of a bad signal. I'm not claiming it works — I'm saying it hasn't been fairly tested, and I'd rather show you that than quietly drop it and present a cleaner number.

**"Isn't 13.8% just a bull market?"**
Partly, yes, and the carry book is short volatility so it's structurally exposed to that. The cap that would catch it doesn't exist yet — the position limit is per underlying and blind to correlation, and ten symbols measured as about 2.4 independent bets. That's on the open-items list, not the strengths list.
