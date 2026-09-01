"""Chain filtering and strike selection.

The chain is the raw material for every structure. Selection here is
deterministic and testable, so a strategy never has to hand-roll strike maths.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace

from oaa.core import clock
from oaa.core.errors import DataError
from oaa.core.logging import get_logger
from oaa.core.types import OptionQuote, Right

log = get_logger("options.chain")


@dataclass
class ChainFilter:
    min_dte: int = 3
    max_dte: int = 45
    min_open_interest: int = 250
    min_volume: int = 0
    max_spread_pct: float = 0.12
    min_price: float = 0.10
    #: None means no ceiling. A per-CONTRACT price cap does not refuse a
    #: trade - it removes the near-the-money strikes on an expensive name and
    #: leaves the cheap far-OTM ones, so `atm()` then prices an OTM strike as
    #: ATM and `by_delta()` resolves to whatever delta survived. A distorted
    #: structure rather than a refused one, which is why this defaults to off.
    max_price: float | None = None
    rights: tuple[Right, ...] = (Right.CALL, Right.PUT)
    require_greeks: bool = False

    def reject_reason(self, q: OptionQuote, asof: dt.date) -> str | None:
        """Why this contract is not tradeable, or None if it is.

        `accepts` is the hot path and only needs the bit. This exists because a
        chain that empties out reports "no contracts survived the liquidity
        filter" and nothing else, which twice this week sent us looking at the
        market when the answer was our own config. `oaa chain --why` reads it.
        """
        dte = q.dte(asof)
        if not (self.min_dte <= dte <= self.max_dte):
            return f"outside the {self.min_dte}-{self.max_dte} DTE window ({dte}d)"
        if q.right not in self.rights:
            return "right not requested"
        mid = q.mid
        if mid is None:
            return "no two-sided quote"
        if mid < self.min_price:
            return f"mid {mid:.2f} below min_price {self.min_price:.2f}"
        if self.max_price is not None and mid > self.max_price:
            return f"mid {mid:.2f} above max_price {self.max_price:.2f}"
        if q.spread_pct is not None and q.spread_pct > self.max_spread_pct:
            return (
                f"spread {q.spread_pct:.1%} above max_spread_pct "
                f"{self.max_spread_pct:.1%}"
            )
        if q.open_interest is not None and q.open_interest < self.min_open_interest:
            return f"open interest {q.open_interest} below {self.min_open_interest}"
        if self.min_volume and q.volume is not None and q.volume < self.min_volume:
            return f"volume {q.volume} below {self.min_volume}"
        if self.require_greeks and q.greeks.delta is None:
            return "no delta"
        return None

    def accepts(self, q: OptionQuote, asof: dt.date) -> bool:
        return self.reject_reason(q, asof) is None


@dataclass
class ChainView:
    """A filtered chain with the selection helpers strategies actually need."""

    symbol: str
    spot: float
    quotes: list[OptionQuote]
    asof: dt.date = field(default_factory=dt.date.today)
    #: The same expiries and rights BEFORE the per-contract price, open
    #: interest and spread filters. A defined-risk wing is a cheap option BY
    #: CONSTRUCTION - that is what makes it a hedge - so `min_price` strips
    #: exactly the strikes a condor needs to buy, and on a low-priced
    #: underlying it strips every strike beyond the short. The structure then
    #: cannot be built at all. Short legs are still chosen from `quotes`; only
    #: the protective leg may reach into this pool. Empty means "same as
    #: quotes", so a directly-constructed view behaves as before.
    all_quotes: list[OptionQuote] = field(default_factory=list)

    # -- construction --------------------------------------------------- #
    @classmethod
    def from_quotes(
        cls,
        symbol: str,
        spot: float,
        quotes: list[OptionQuote],
        chain_filter: ChainFilter | None = None,
        asof: dt.date | None = None,
    ) -> ChainView:
        day = asof or clock.today()
        cf = chain_filter or ChainFilter()
        kept = [q for q in quotes if cf.accepts(q, day)]
        # The wing pool keeps the structural filters (DTE window, rights) and
        # drops the tradeability ones. See `all_quotes` above for why.
        wing_cf = replace(
            cf,
            min_price=0.0,
            max_price=None,
            min_open_interest=0,
            min_volume=0,
            max_spread_pct=float("inf"),
            require_greeks=False,
        )
        pool = [q for q in quotes if wing_cf.accepts(q, day)]
        _warn_if_chain_misses_spot(symbol, kept or pool, spot)
        return cls(symbol=symbol, spot=spot, quotes=kept, asof=day, all_quotes=pool)

    def __len__(self) -> int:
        return len(self.quotes)

    @property
    def is_empty(self) -> bool:
        return not self.quotes

    # -- slicing --------------------------------------------------------- #
    def expiries(self) -> list[dt.date]:
        return sorted({q.expiry for q in self.quotes})

    def for_expiry(self, expiry: dt.date, right: Right | None = None) -> list[OptionQuote]:
        out = [q for q in self.quotes if q.expiry == expiry]
        if right is not None:
            out = [q for q in out if q.right is right]
        return sorted(out, key=lambda q: q.strike)

    def nearest_expiry(self, target_dte: int) -> dt.date:
        expiries = self.expiries()
        if not expiries:
            raise DataError(f"{self.symbol}: no expiries left after filtering")
        return min(expiries, key=lambda e: abs((e - self.asof).days - target_dte))

    def expiry_in_range(self, dte_range: tuple[int, int]) -> dt.date:
        """Pick the expiry closest to the middle of an acceptable DTE window."""
        lo, hi = dte_range
        candidates = [e for e in self.expiries() if lo <= (e - self.asof).days <= hi]
        if not candidates:
            raise DataError(f"{self.symbol}: no expiry with DTE in [{lo}, {hi}]")
        target = (lo + hi) / 2
        return min(candidates, key=lambda e: abs((e - self.asof).days - target))

    # -- strike selection ------------------------------------------------ #
    def by_delta(
        self, expiry: dt.date, right: Right, target_delta: float
    ) -> OptionQuote:
        """Closest contract to a target delta.

        Put deltas are negative; pass the signed value you mean (-0.16 for a
        16-delta put) and this does the right thing either way.
        """
        # A delta of exactly 0.0 is not a delta. The free indicative feed fills
        # the greek block with zeros rather than omitting it, so an
        # `is not None` test passes on every contract and this method then
        # picks by |0.0 - target| - a tie across the whole chain, resolved by
        # list order, i.e. the lowest strike listed. Requiring a NON-ZERO
        # delta is what makes the fallback below actually reachable on the
        # feed the judged account runs on.
        candidates = [
            q for q in self.for_expiry(expiry, right)
            if q.greeks.delta is not None and abs(q.greeks.delta) > 1e-9
        ]
        if not candidates:
            # Greeks missing (common on the free indicative feed) - fall back
            # to a moneyness proxy rather than failing the whole cycle.
            return self.by_moneyness(expiry, right, _delta_to_moneyness(target_delta, right))
        target = abs(target_delta)
        return min(candidates, key=lambda q: abs(abs(q.greeks.delta or 0.0) - target))

    def by_moneyness(
        self, expiry: dt.date, right: Right, moneyness: float
    ) -> OptionQuote:
        """moneyness = strike / spot. 1.0 = at the money.

        Refuses when the nearest listed strike is nowhere near the one asked
        for. A truncated chain - one page of an unpaged fetch, ordered by OCC
        symbol so it stops below spot - makes this method silently return its
        top strike, which is a deep-ITM contract priced at pure intrinsic. On
        1 Sep that produced six "ATM" ideas at $2,400+ of premium against a
        $1,000 per-trade cap, and all six died at the sizing gate. Failing here
        turns that into a named rejection instead of a bad trade dressed as a
        good one.
        """
        candidates = self.for_expiry(expiry, right)
        if not candidates:
            raise DataError(f"{self.symbol}: no {right.value}s for {expiry}")
        target_strike = self.spot * moneyness
        best = min(candidates, key=lambda q: abs(q.strike - target_strike))
        drift = abs(best.strike - target_strike)
        tolerance = max(2.0 * _strike_spacing(candidates), 0.005 * self.spot)
        # Only refuse when the chain fails to reach SPOT. A sparse chain that
        # brackets spot is a thin market and the nearest strike is the honest
        # answer; a chain whose whole strike range sits to one side of spot is
        # a truncated fetch. Discriminating on spot rather than on the target
        # keeps legitimate far-OTM wing requests working.
        lo = min(q.strike for q in self.quotes) if self.quotes else best.strike
        hi = max(q.strike for q in self.quotes) if self.quotes else best.strike
        brackets_spot = lo <= self.spot <= hi
        looks_like_a_real_page = len(self.quotes) >= _TRUNCATION_MIN_CONTRACTS
        if drift > tolerance and not brackets_spot and looks_like_a_real_page:
            raise DataError(
                f"{self.symbol}: nearest listed {right.value} strike to "
                f"{target_strike:.2f} is {best.strike:.2f} ({drift:.2f} away, "
                f"tolerance {tolerance:.2f}) - the chain does not reach the "
                f"strike this structure needs. Strikes seen: "
                f"{lo:.2f}-{hi:.2f}, spot {self.spot:.2f}. "
                "A chain that stops short of spot is a truncated fetch, not a "
                "thin market - check option_chain paging."
            )
        return best

    def by_strike(self, expiry: dt.date, right: Right, strike: float) -> OptionQuote:
        candidates = self.for_expiry(expiry, right)
        if not candidates:
            raise DataError(f"{self.symbol}: no {right.value}s for {expiry}")
        return min(candidates, key=lambda q: abs(q.strike - strike))

    def atm(self, expiry: dt.date, right: Right = Right.CALL) -> OptionQuote:
        return self.by_moneyness(expiry, right, 1.0)

    def strike_offset(
        self,
        expiry: dt.date,
        right: Right,
        from_strike: float,
        points: float,
        must_clear: bool = False,
        allow_unfiltered: bool = False,
    ) -> OptionQuote:
        """The listed strike nearest `from_strike + points`.

        Used for wings: never assume the grid is evenly spaced.

        `must_clear` restricts the answer to strikes strictly on the far side of
        `from_strike`. Without it, a ladder that is coarse relative to the wing -
        or one thinned by the chain filter until few strikes survive - snaps the
        wing back ONTO the short strike, and the caller can only report that the
        width "does not fit the strike grid". A wing one strike out is a
        narrower condor; a wing on top of the short is not a condor at all, so
        picking the nearest valid strike is strictly better than refusing.
        """
        target = from_strike + points
        candidates = self.for_expiry(expiry, right)
        if not candidates:
            raise DataError(f"{self.symbol}: no {right.value}s for {expiry}")
        beyond = (
            (lambda strike: strike > from_strike) if points > 0
            else (lambda strike: strike < from_strike)
        )
        if must_clear:
            side = [q for q in candidates if beyond(q.strike)]
            if not side and allow_unfiltered:
                # Nothing listed beyond the short AFTER the tradeability
                # filters. Before giving up, look in the pre-filter pool: on a
                # low-priced underlying the wing is usually there and was
                # removed by `min_price` for being cheap, which is the one
                # property a wing is supposed to have.
                side = [
                    q for q in self._wing_pool(expiry, right)
                    if beyond(q.strike)
                ]
            if side:
                candidates = side
        return min(candidates, key=lambda q: abs(q.strike - target))

    def _wing_pool(self, expiry: dt.date, right: Right) -> list[OptionQuote]:
        """Pre-filter quotes for this expiry and right; `quotes` if unset."""
        pool = self.all_quotes or self.quotes
        return sorted(
            (q for q in pool if q.expiry == expiry and q.right is right),
            key=lambda q: q.strike,
        )

    def atm_iv(self, expiry: dt.date | None = None) -> float | None:
        exp = expiry or (self.expiries()[0] if self.expiries() else None)
        if exp is None:
            return None
        ivs = [
            q.implied_volatility
            for q in (self.atm(exp, Right.CALL), self.atm(exp, Right.PUT))
            if q.implied_volatility
        ]
        return round(sum(ivs) / len(ivs), 4) if ivs else None


def _delta_to_moneyness(target_delta: float, right: Right) -> float:
    """Crude delta -> moneyness proxy for when the feed gives no greeks.

    Not a pricing model - just a monotone mapping that lands in the right
    neighbourhood so a strategy degrades instead of dying.
    """
    d = min(max(abs(target_delta), 0.01), 0.99)
    offset = (0.50 - d) * 0.20  # 50d -> ATM, 16d -> ~7% OTM
    return 1.0 + offset if right is Right.CALL else 1.0 - offset


#: Below this, a one-sided or off-spot chain is a thin name or a test
#: fixture. At or above it, it is an unpaged fetch: the 1 Sep SPY chain
#: arrived as 129 contracts, all calls, topping out 25 points below spot.
_TRUNCATION_MIN_CONTRACTS = 20


def _strike_spacing(candidates: list[OptionQuote]) -> float:
    """Median gap between adjacent listed strikes; 1.0 when it cannot be read."""
    strikes = sorted({q.strike for q in candidates})
    if len(strikes) < 2:
        return 1.0
    gaps = sorted(b - a for a, b in zip(strikes, strikes[1:], strict=False) if b > a)
    if not gaps:
        return 1.0
    return gaps[len(gaps) // 2]


def _warn_if_chain_misses_spot(symbol: str, quotes: list[OptionQuote], spot: float) -> None:
    """A chain that does not bracket spot, or has only one right, is truncated.

    Both are the signature of an unpaged fetch: snapshots come back ordered by
    OCC symbol, so a single page is all calls, lowest strikes first. Silent for
    a whole competition week until someone reads the premiums.
    """
    if len(quotes) < _TRUNCATION_MIN_CONTRACTS or spot <= 0:
        # A handful of contracts is a hand-built fixture or a genuinely thin
        # name, not a truncated page. Only a chain big enough to BE a page can
        # be diagnosed as one.
        return
    lo = min(q.strike for q in quotes)
    hi = max(q.strike for q in quotes)
    if not (lo <= spot <= hi):
        log.warning(
            "%s: chain strikes %.2f-%.2f do not bracket spot %.2f - "
            "the fetch looks truncated, not the market",
            symbol, lo, hi, spot,
        )
    rights = {q.right for q in quotes}
    if len(rights) < 2:
        only = next(iter(rights)).value if rights else "none"
        log.warning(
            "%s: chain came back with %ss only (%d contracts) - "
            "a real chain has both rights; the fetch looks truncated",
            symbol, only, len(quotes),
        )
