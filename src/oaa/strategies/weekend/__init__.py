"""The weekend book: BTC/USD mean reversion, live only while the US equity
market is shut.

This package is deliberately self-contained. Everything the weekend book needs
- its clock, its signal stack, its cost model, its sizing, its backtest - lives
under `oaa.strategies.weekend`. It borrows four things from the rest of the
system and nothing else:

    oaa.core.types        the TradeIdea / Leg / OrderTicket contract
    oaa.data.indicators   adx() and atr(), already Wilder-correct and tested
    oaa.brokers.base      the Broker port, so orders go through the same
                          audited execution surface as the options books
    oaa.telemetry.journal the same decision log the judges read

Why the separation is real and not cosmetic
-------------------------------------------
The options books are built on assumptions that are simply false for crypto:
an option chain exists, the session has an open and a close, a position can be
short, and the contract multiplier is 100. Bolting a spot-crypto strategy onto
`Strategy.generate` would mean threading `None` chains and `if is_crypto`
branches through the risk engine and the firewall - the sort of change that
breaks an options book at 14:45 on a Tuesday because of a coin.

So the weekend book runs as its own process (`oaa weekend run`) inside its own
window, and by construction that window cannot overlap an equity session.

The one-sentence thesis
-----------------------
Over the weekend BTC trades with no equity market, no macro prints and thin
participation; flow is dominated by liquidation and retail impulse rather than
information, so displacement from the 24-hour mean reverts more often than it
continues - but ONLY while the tape is ranging, which is what ADX is there to
establish and what makes this a filter-first strategy rather than a dip buyer.
"""

from oaa.strategies.weekend.clock import WeekendWindow, WindowPhase
from oaa.strategies.weekend.costs import CryptoCostModel
from oaa.strategies.weekend.params import WeekendParams, load_params
from oaa.strategies.weekend.signals import WeekendSignal, evaluate

# Imported for its REGISTRATION side effect, and this line is load-bearing.
# `Registry.autoload` walks `oaa.strategies` with pkgutil, which yields this
# subpackage and imports this file - but nothing deeper. Without the import
# below, `@strategy_registry.register("weekend_crypto_reversion")` never runs,
# and the moment an operator flips the Control tab toggle the runner raises
# "unknown strategy" while holding live positions.
from oaa.strategies.weekend.strategy import WeekendCryptoReversion  # noqa: E402

__all__ = [
    "CryptoCostModel",
    "WeekendCryptoReversion",
    "WeekendParams",
    "WeekendSignal",
    "WeekendWindow",
    "WindowPhase",
    "evaluate",
    "load_params",
]
