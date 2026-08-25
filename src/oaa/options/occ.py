"""OCC option symbol handling.

Alpaca uses the unpadded variant: ROOT + YYMMDD + C|P + strike*1000 zero-padded
to 8 digits. e.g. AAPL260918C00250000 = AAPL 2026-09-18 250.00 call.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from oaa.core.types import Right

_OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class OccSymbol:
    root: str
    expiry: dt.date
    right: Right
    strike: float

    def __str__(self) -> str:
        return build_occ(self.root, self.expiry, self.right, self.strike)

    def dte(self, asof: dt.date | None = None) -> int:
        return (self.expiry - (asof or dt.date.today())).days


def build_occ(
    root: str,
    expiry: dt.date | str,
    right: Right | str,
    strike: float,
) -> str:
    if isinstance(expiry, str):
        expiry = dt.date.fromisoformat(expiry)
    cp = right.value if isinstance(right, Right) else str(right)
    cp = "C" if cp.lower().startswith("c") else "P"
    strike_int = int(round(float(strike) * 1000))
    if strike_int <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    return f"{root.strip().upper()}{expiry:%y%m%d}{cp}{strike_int:08d}"


def parse_occ(symbol: str) -> OccSymbol:
    match = _OCC_RE.match(symbol.strip().upper())
    if not match:
        raise ValueError(f"not a valid OCC option symbol: {symbol!r}")
    return OccSymbol(
        root=match["root"],
        expiry=dt.datetime.strptime(match["ymd"], "%y%m%d").date(),
        right=Right.CALL if match["cp"] == "C" else Right.PUT,
        strike=int(match["strike"]) / 1000.0,
    )


def is_occ(symbol: str) -> bool:
    return bool(_OCC_RE.match(symbol.strip().upper()))


def underlying_of(symbol: str) -> str:
    """Root symbol for an option; the symbol itself for equities."""
    try:
        return parse_occ(symbol).root
    except ValueError:
        return symbol.strip().upper()
