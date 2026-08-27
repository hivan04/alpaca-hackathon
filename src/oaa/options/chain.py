"""Chain filtering and strike selection.

The chain is the raw material for every structure. Selection here is
deterministic and testable, so a strategy never has to hand-roll strike maths.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from oaa.core import clock
from oaa.core.errors import DataError
from oaa.core.types import OptionQuote, Right


@dataclass
class ChainFilter:
    min_dte: int = 3
    max_dte: int = 45
    min_open_interest: int = 250
    min_volume: int = 0
    max_spread_pct: float = 0.12
    min_price: float = 0.10
    max_price: float = 25.0
    rights: tuple[Right, ...] = (Right.CALL, Right.PUT)
    require_greeks: bool = False

    def accepts(self, q: OptionQuote, asof: dt.date) -> bool:
        dte = q.dte(asof)
        if not (self.min_dte <= dte <= self.max_dte):
            return False
        if q.right not in self.rights:
            return False
        mid = q.mid
        if mid is None or not (self.min_price <= mid <= self.max_price):
            return False
        if q.spread_pct is not None and q.spread_pct > self.max_spread_pct:
            return False
        if q.open_interest is not None and q.open_interest < self.min_open_interest:
            return False
        if self.min_volume and q.volume is not None and q.volume < self.min_volume:
            return False
        if self.require_greeks and q.greeks.delta is None:
            return False
        return True


@dataclass
class ChainView:
    """A filtered chain with the selection helpers strategies actually need."""

    symbol: str
    spot: float
    quotes: list[OptionQuote]
    asof: dt.date = field(default_factory=dt.date.today)

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
        return cls(symbol=symbol, spot=spot, quotes=kept, asof=day)

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
        candidates = [q for q in self.for_expiry(expiry, right) if q.greeks.delta is not None]
        if not candidates:
            # Greeks missing (common on the free indicative feed) - fall back
            # to a moneyness proxy rather than failing the whole cycle.
            return self.by_moneyness(expiry, right, _delta_to_moneyness(target_delta, right))
        target = abs(target_delta)
        return min(candidates, key=lambda q: abs(abs(q.greeks.delta or 0.0) - target))

    def by_moneyness(
        self, expiry: dt.date, right: Right, moneyness: float
    ) -> OptionQuote:
        """moneyness = strike / spot. 1.0 = at the money."""
        candidates = self.for_expiry(expiry, right)
        if not candidates:
            raise DataError(f"{self.symbol}: no {right.value}s for {expiry}")
        target_strike = self.spot * moneyness
        return min(candidates, key=lambda q: abs(q.strike - target_strike))

    def by_strike(self, expiry: dt.date, right: Right, strike: float) -> OptionQuote:
        candidates = self.for_expiry(expiry, right)
        if not candidates:
            raise DataError(f"{self.symbol}: no {right.value}s for {expiry}")
        return min(candidates, key=lambda q: abs(q.strike - strike))

    def atm(self, expiry: dt.date, right: Right = Right.CALL) -> OptionQuote:
        return self.by_moneyness(expiry, right, 1.0)

    def strike_offset(
        self, expiry: dt.date, right: Right, from_strike: float, points: float
    ) -> OptionQuote:
        """The listed strike nearest `from_strike + points`.

        Used for wings: never assume the grid is evenly spaced.
        """
        return self.by_strike(expiry, right, from_strike + points)

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
