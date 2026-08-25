"""The temporal firewall.

Two books share one account. Without an interlock they compete for the same
capital, and the intraday book's 4x day-trading buying power bleeds into the
overnight book's 2x Reg T limit — which is how an account gets force-liquidated
by the broker at 16:00.

The firewall makes that impossible with a sequential lock-and-verify:

    15:15 ET  intraday hard cutoff   cancel everything, liquidate to cash,
                                     CONFIRM flat, then release the lock
    15:54 ET  overnight verification  re-poll Alpaca, prove zero positions and
                                     zero working orders, read fresh Reg T
                                     buying power, size against it, acquire
                                     the lock
    15:55 ET  overnight entry        only possible while holding the lock

Layer 1 is temporal: a book may only open inside its own window.
Layer 2 is capital: the size is scaled against buying power measured *after*
the other book is provably flat, never against a cached number.
"""

from oaa.firewall.clock import Phase, SessionClock
from oaa.firewall.lock import (
    Book,
    FirewallVerdict,
    LiquidationReport,
    TemporalFirewall,
)

__all__ = [
    "Book",
    "FirewallVerdict",
    "LiquidationReport",
    "Phase",
    "SessionClock",
    "TemporalFirewall",
]
