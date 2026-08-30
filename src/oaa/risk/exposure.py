"""Aggregate option exposure: what the whole book is actually betting on.

Why this module exists
----------------------
`risk.max_net_delta`, `risk.max_net_vega` and `risk.max_notional_per_trade_pct`
were configured, commented and documented, and enforced NOWHERE - they appeared
only in `config/schema.py`. Every portfolio limit that did exist counted
STRUCTURES: `max_positions`, `max_new_positions_per_day`,
`max_positions_per_underlying`, `duplicate_structure`. All four are blind to the
thing that actually hurts a small book, which is twenty-five positions that are
one bet.

That is not hypothetical here. Measured 28 Aug, the ten-symbol intraday universe
behaved like **2.4 independent bets** - four broad equity ETFs that move
together, and only TLT and GLD genuinely decorrelated. And in the 27 Aug carry
run NVDA (-$866) and SPY (-$1,456) blew up on the SAME session, about 2.3% of
equity from two positions. The daily loss halt catches the next day, not that
one.

A count-based cap cannot see any of that. A Greek aggregate can: correlated
longs stack delta, and stacked short premium stacks vega, whatever the names on
the tickets.

What is measurable, and what is not
-----------------------------------
The new idea is always measurable - its legs carry their own `OptionQuote` with
greeks, straight from the chain that priced them.

The OPEN BOOK is the hard half. `PositionSnapshot` carries symbol, quantity,
strike, expiry and right - and no greeks, because the broker does not send any.
So the greeks have to be recovered by matching each open contract back to a
chain in this cycle's contexts. That match can fail: a position on a symbol that
is not in this cycle's universe, or a contract that has fallen out of the
strike window.

**A partial measurement reported as a total is worse than no measurement**,
because a cap computed on half the book passes trades that a full one would
refuse, and it does so silently. So coverage is counted, returned, and carried
onto the verdict. What the engine does with incomplete coverage is the engine's
decision - see `risk/engine.py` - but it cannot claim not to have known.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from oaa.core.types import AccountSnapshot, MarketContext, OptionQuote, Side, TradeIdea

log = logging.getLogger(__name__)

#: One option contract is 100 shares.
CONTRACT_MULTIPLIER = 100


@dataclass
class Exposure:
    """Aggregate first-order risk, in dollars.

    `dollar_delta` is the share-equivalent delta times spot: what the book makes
    or loses on a $1 move in the underlying, summed. Signed - a long call and a
    long put on the same name net off, which is the whole point.

    `vega` is dollars of P&L per ONE volatility POINT (1.0 = 100%, so the raw
    Black-Scholes vega is divided by 100). Signed the same way: long premium is
    positive, short premium negative.
    """

    dollar_delta: float = 0.0
    vega: float = 0.0
    #: Gross notional controlled: sum of |delta-equivalent shares| x spot.
    gross_notional: float = 0.0
    matched: int = 0
    unmatched: int = 0
    #: Underlyings whose greeks could not be recovered this cycle.
    missing: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        total = self.matched + self.unmatched
        return 1.0 if total == 0 else round(self.matched / total, 4)

    @property
    def complete(self) -> bool:
        return self.unmatched == 0

    def __add__(self, other: Exposure) -> Exposure:
        return Exposure(
            dollar_delta=self.dollar_delta + other.dollar_delta,
            vega=self.vega + other.vega,
            gross_notional=self.gross_notional + other.gross_notional,
            matched=self.matched + other.matched,
            unmatched=self.unmatched + other.unmatched,
            missing=sorted(set(self.missing) | set(other.missing)),
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "dollar_delta": round(self.dollar_delta, 2),
            "vega": round(self.vega, 2),
            "gross_notional": round(self.gross_notional, 2),
            "greek_coverage": self.coverage,
        }


#: Above this, a per-share vega cannot be quoted per vol POINT.
#:
#: Per point, an ATM option's per-share vega is roughly
#: `spot * pdf(d1) * sqrt(T) / 100` - about 0.74 for a 30-day SPY option at
#: 640, and smaller for everything shorter. Per 1.00 of vol the same number is
#: 74. So the two conventions are two orders of magnitude apart and a single
#: threshold separates them cleanly.
#:
#: This is a WARNING, never a silent rescale. `bs_greeks` in this repo divides
#: by 100 and returns per-point; what Alpaca's live feed returns has NOT been
#: verified against a live quote, and a risk cap that quietly divides by 100
#: when it dislikes a number is worse than one that is wrong loudly.
IMPLAUSIBLE_PER_SHARE_VEGA = 5.0


def _contract_exposure(
    quote: OptionQuote, signed_contracts: float, spot: float
) -> tuple[float, float, float]:
    """(dollar_delta, vega, gross_notional) for a signed contract count.

    Vega convention: `greeks.vega` is per share, per one volatility POINT -
    which is what `backtest/pricing.bs_greeks` returns (it divides by 100
    itself) and what a risk limit is written in.

    An earlier version of this function divided by 100 a SECOND time. The whole
    book then measured a net vega of $0.00 per point on every single
    evaluation, which read as "no vol exposure" rather than as a unit error,
    and the cap sitting on top of it could never have fired. Found only because
    the calibration run reported exactly 0.000 at every percentile.
    """
    delta = quote.greeks.delta
    vega = quote.greeks.vega
    shares = (delta or 0.0) * signed_contracts * CONTRACT_MULTIPLIER
    dollar_delta = shares * spot
    if vega is not None and abs(vega) > IMPLAUSIBLE_PER_SHARE_VEGA:
        log.warning(
            "%s: per-share vega %.2f is too large to be per vol POINT - the feed "
            "is probably quoting per 1.00 of vol, which overstates net vega 100x. "
            "Verify against a live quote before trusting risk.max_net_vega.",
            quote.symbol, vega,
        )
    vega_dollars = (vega or 0.0) * signed_contracts * CONTRACT_MULTIPLIER
    return dollar_delta, vega_dollars, abs(dollar_delta)


def idea_exposure(
    idea: TradeIdea, quantity: int, spot: float | None = None
) -> Exposure | None:
    """First-order exposure the idea would ADD, at the size risk sized it to.

    ONE spot for the whole structure. Every leg of a vertical or a condor is on
    the same underlying at the same instant, so pricing each leg against its own
    strike-as-spot proxy is not an approximation, it is an error that grows with
    the strike separation - it made a 500/510 debit vertical measure $9,700 of
    delta instead of $10,000, and on a 5-wide condor with wings it compounds.
    This is the same defect in miniature as the mixed-surface marking fixed on
    27 Aug: one structure, one surface, one spot.

    `spot` should come from the cycle's MarketContext. The fallback - the median
    leg strike, applied to every leg - is a proxy for callers that have no
    context, and it is a SINGLE value rather than a per-leg one so that the
    netting stays correct even when the level is a little off.

    None when any leg arrived without greeks: the free indicative feed does
    serve quotes with no greeks, and a structure half-measured is not measured.
    """
    option_legs = [leg for leg in idea.legs if leg.is_option]
    if not option_legs:
        return None
    for leg in option_legs:
        if leg.quote is None or leg.quote.greeks.delta is None or leg.quote.greeks.vega is None:
            return None

    reference = spot if spot and spot > 0 else _spot_proxy(option_legs)
    if reference is None or reference <= 0:
        return None

    out = Exposure()
    for leg in option_legs:
        sign = 1.0 if leg.side is Side.BUY else -1.0
        contracts = sign * leg.ratio * quantity
        dd, vg, gn = _contract_exposure(leg.quote, contracts, reference)
        out.dollar_delta += dd
        out.vega += vg
        out.gross_notional += gn
        out.matched += 1
    return out


def _spot_proxy(legs: list[Any]) -> float | None:
    """The median leg strike - one number for the whole structure.

    A structure is built around the money, so the median strike is within the
    chain's strike window of spot by construction. Used only when no context
    was supplied, and only for scaling; the netting between legs is unaffected
    because every leg gets the same value.
    """
    strikes = sorted(
        float(leg.quote.strike) for leg in legs
        if leg.quote is not None and leg.quote.strike
    )
    if not strikes:
        return None
    mid = len(strikes) // 2
    if len(strikes) % 2:
        return strikes[mid]
    return (strikes[mid - 1] + strikes[mid]) / 2


def book_exposure(
    account: AccountSnapshot,
    contexts: dict[str, MarketContext] | None,
) -> Exposure:
    """Exposure of the ALREADY OPEN book, recovered from this cycle's chains.

    Positions whose contract is not in any context chain are counted as
    `unmatched` rather than dropped. Dropping them would report a book that is
    smaller than it is, and a cap on an understated book is a cap that does not
    bind.
    """
    out = Exposure()
    index: dict[str, tuple[OptionQuote, float]] = {}
    for context in (contexts or {}).values():
        for quote in context.chain:
            index[quote.symbol.upper()] = (quote, context.spot)

    for position in account.option_positions():
        found = index.get(position.symbol.upper())
        if found is None:
            out.unmatched += 1
            name = position.underlying or position.symbol
            if name not in out.missing:
                out.missing.append(name)
            continue
        quote, spot = found
        if quote.greeks.delta is None or quote.greeks.vega is None:
            out.unmatched += 1
            continue
        # `qty` is already signed by the broker: negative for a short leg.
        dd, vg, gn = _contract_exposure(quote, float(position.qty), spot)
        out.dollar_delta += dd
        out.vega += vg
        out.gross_notional += gn
        out.matched += 1
    return out


def normalised(exposure: Exposure, equity: float) -> dict[str, float]:
    """Exposure as fractions of equity, which is how the limits are written.

    `delta_ratio` 0.35 means: a 1% move in the underlyings moves the book by
    0.35% of equity. NOTE this is a redefinition in the only sense that
    matters - `max_net_delta`'s old comment read "portfolio delta per $1k
    equity", but the limit was never enforced, so there is no live behaviour to
    preserve and nothing was ever calibrated against that reading. The
    fraction-of-equity form is the one that can be compared across account
    sizes and stated in a sentence.
    """
    if equity <= 0:
        return {"delta_ratio": 0.0, "vega_ratio": 0.0, "notional_ratio": 0.0}
    return {
        "delta_ratio": round(exposure.dollar_delta / equity, 4),
        #: Vega per $10k of equity: dollars lost per vol point, scaled. A raw
        #: dollar cap is meaningless without the account size beside it.
        "vega_ratio": round(exposure.vega / (equity / 10_000), 4),
        "notional_ratio": round(exposure.gross_notional / equity, 4),
    }


def describe(exposure: Exposure, equity: float) -> dict[str, Any]:
    out: dict[str, Any] = exposure.as_dict()
    out.update(normalised(exposure, equity))
    out["greek_coverage"] = exposure.coverage
    if exposure.missing:
        out["greeks_missing_for"] = ",".join(exposure.missing[:6])
    return out
