# Speaking script — Options Agent

**Target: 3 minutes 30.** Six slides, 575 spoken words. That is ~165 words a minute — brisk but comfortable, and it leaves you about fifteen seconds of slack for slide changes and a breath. Don't add anything back in; the appendix is what the extra material is for.

**Time it once out loud before you present.** If your natural pace is slower than 165, drop the second sentence of slide 4 and the first sentence of slide 6 — that buys you twenty seconds without losing an argument.

Before you start:

- Three lines carry the pitch: *"eight vetoes in a row is not a strategy"*, *"the spread cost as much as the gross profit"*, and *"the agent's job is to decline."* Slow down on those. Everything else can be brisk.
- Don't read the cards. Say the thing that isn't written on the slide.
- If you're behind at slide 5, cut the sentence about the bars. Never cut slide 6.

---

## Slide 1 — Title (0:00 – 0:18)

> Three books, one pipeline.
>
> A carry book that sells options and waits. An intraday book that buys momentum and is flat by the close. An earnings book where a language model reads the news for three days before a print.
>
> One rule shapes all of it: **no model can approve a trade.**

---

## Slide 2 — Three bets that fail in different weather (0:18 – 0:52)

> These are deliberately opposite bets.
>
> Carry sells options, because options are usually priced for more movement than actually happens. Intraday buys them, because a move with a mechanism behind it tends to keep going. One is short volatility, the other long it — a week that kills one is often good for the other.
>
> The events book exists because of a specific failure: the other two wait for a threshold that in practice never crossed, so the agent runs all week and opens nothing. **Here the entry condition is a date.** Broadcom reports on the 2nd of September whether or not any indicator agrees.

---

## Slide 3 — Every trade clears a named gate (0:52 – 1:35)

> Each book is a sequence of hard gates, not a score. A blended score lets a rich volatility reading paper over an earnings date — for a short-premium book, the one trade you must never take.
>
> **The bottom line is the point.** Every threshold here was moved at least once, always by measurement. The volatility floor was 0.70 and rejected 304 of 304 candidates. The intraday spread ceiling sat below the tightest quote in our own universe — it wasn't selecting within the market, it was rejecting it. And the intraday book had eight hard vetoes in a row: at 70% pass each, six percent survive. **Eight vetoes in a row isn't a strategy, it's a guarantee that nothing trades.**

---

## Slide 4 — No model can approve a trade (1:35 – 2:10)

> Six stages, and the live agent and the backtest run the same ones from the same objects — a backtest that skips a stage is a backtest of a different system.
>
> The critic scores an idea and can decline it. It cannot approve; the events model can't either. Stage four is the only approver.
>
> Fifteen deterministic checks in Python. The two highlighted exist because **brokers net identical option symbols** — opening the same condor twice doubles one position rather than creating a second, so every count-based limit was blind to it.

---

## Slide 5 — Evidence (2:10 – 2:55)

> Seven months on real Alpaca bars, 441 closed trades, up 13.8% at a Sharpe of 2.4.
>
> **Now read the red number.** Gross profit was $13,973. Spread cost $12,990 — the book paid out almost its own gross profit in execution cost, *with* a cost gate in front of every trade. That's why the spread is charged inside the fills.
>
> The bars group every trade by the rule that closed it: the exits are the strategy. And the split on the right is the honest version — **carry carries this, the intraday book lost five thousand**, and the events book has no usable backtest at all.

---

## Slide 6 — Honesty, and the argument (2:55 – 3:30)

> Four things I know are wrong with it, all measured. Entry is shared code between live and replay — exit isn't. Intraday marks are too coarse for a twenty-minute hold. Paper fills flatter. Five sessions is not a sample.
>
> So what am I claiming? **The agent's job is to decline.** 8,569 ideas generated, 441 taken. The artefact worth reading isn't the equity curve — it's the rejection log: 72,753 declined trades, each with the gate that stopped it and the number it measured.
>
> Gates are hard. No model can approve. Every number is defended — including the ones still guesses, which say so.

---

# Q&A — where to jump

| If they ask | Go to | The answer in one line |
|---|---|---|
| "Why 25-delta shorts? That's aggressive." | Carry gates + carry exits | Breakeven is set by the exit pair, not the strikes; 25Δ roughly doubles credit-to-width against an unchanged bar. A hypothesis under measurement — below breakeven it goes back to 14. |
| "Where's the edge in a VWAP cross?" | Intraday signal stack | There isn't one, and I don't claim it. The option is leverage and defined risk; the originality is the catalyst gate and a selection layer that declines when IV rank is above 0.85. |
| "How do you stop the LLM hallucinating an earnings date?" | Events — the date slide | The model proposes, a file confirms. A wrong date isn't a bad trade, it's a position against no event at all. CPRT ships unconfirmed and cannot trade. |
| "Isn't prompt injection a risk?" | Events — the watch | Evidence is fenced, stripped, truncated, parsed to a fixed schema, every field clamped. The worst a poisoned post does is move a bounded confidence score — and nothing the model returns can authorise a trade. |
| "How do you know the model isn't rubber-stamping?" | Events — the watch | Abstention rate is journalled every run and a zero rate logs a warning. We shipped that bug once already, with a critic that scored eighty candidates and declined none. |
| "Why did the events book only take one trade?" | Events — divergence | Because it measured volatility and traded direction. A directional structure at a fair implied move on a coin-flip call returns minus the round trip in expectation. The expression now follows the sign of the divergence. |
| "Which gate rejects the most?" | Rejection funnel | The carry premium gate, 15,334 — but it's a filter, not a block: entries occurred on 43% of sessions. Spread is third, and it should be. |
| "Why these fourteen symbols?" | Universe rule | Measured spread and effective independent bets, not P&L. Cut XLK at 0.96 correlation to QQQ and ten times the spread; kept XLE despite a wide chain as the only negative correlation in the book. |
| "What would you do with another week?" | — | Close the exit-path gap between live and replay, fix intraday mark resolution, then re-measure. In that order — nothing downstream is trustworthy until the marks are. |

## Two questions worth pre-empting

**"Your intraday book loses money — why is it still on?"**
Its replayed P&L isn't measurable yet: option marks in replay are far coarser than a twenty-minute hold, so those losses are largely an artefact of mark resolution rather than evidence of a bad signal. I'm not claiming it works — I'm saying it hasn't been fairly tested, and I'd rather show you that than quietly drop it and present a cleaner number.

**"Isn't 13.8% just a bull market?"**
Partly, yes — the carry book is short volatility, so it's structurally exposed. The cap that would catch it doesn't exist yet: the position limit is per underlying and blind to correlation, and ten symbols measured as about 2.4 independent bets. That's on the open-items list, not the strengths list.
