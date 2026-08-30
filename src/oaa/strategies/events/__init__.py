"""The events book: one overnight hold across a scheduled earnings print.

Why this book exists
--------------------
The other books wait for a market condition to cross a threshold, and the
recurring failure has been that the threshold is never crossed - the agent runs
all week and opens nothing. This book cannot have that problem, because its
entry condition is a DATE. Broadcom reports on 2 September whether or not any
indicator agrees, so the gate opens on schedule and the only remaining question
is whether the trade is worth taking.

The pipeline
------------
    calendar.py    Featherless proposes next week's reporters; a confirmed
                   calendar file verifies every proposal. Unverified names are
                   logged, never armed.
    volscreen.py   Prices the ATM straddle in the expiry containing the print
                   and sets it against the last four actual reactions. Ranks
                   the top N by divergence.
    sentiment.py   Builds the evidence pack: Alpaca news plus the StockTwits
                   public stream, sanitised and budget-capped.
    direction.py   Featherless reads the pack and returns direction,
                   confidence and the evidence it used. Abstention is a valid
                   and common answer.
    sizing.py      Confidence maps linearly onto position size between a floor
                   and a cap, bounded again by a per-trade limit, a nightly
                   budget shared across names, and an absolute contract cap.
    strategy.py    Builds the vertical debit spread, in the repo's Strategy
                   shape, with three interlocks.
    engine.py      The runner behind `oaa events arm` / `oaa events flatten`.

Separation from the other books
-------------------------------
This package is self-contained. It borrows the TradeIdea/Leg contract, the
structure builders, the Broker port, the RiskEngine, the ExecutionRouter and
the Journal - and nothing else. It has its own params file, its own calendar,
its own capital book ("events") and its own process. No existing strategy
changes behaviour because this exists, and nothing here can open a position
through the intraday or carry scan.
"""

from oaa.strategies.events.calendar import (
    EarningsEvent,
    ScreenResult,
    load_calendar,
    screen_week,
)
from oaa.strategies.events.direction import DirectionCall, predict
from oaa.strategies.events.engine import ArmReport, EventsEngine  # noqa: E402
from oaa.strategies.events.params import EventsParams, load_params
from oaa.strategies.events.sentiment import EvidencePack, gather
from oaa.strategies.events.sizing import SizeDecision, size

# Imported for its REGISTRATION side effect, and this line is load-bearing.
# `Registry.autoload` walks `oaa.strategies` with pkgutil, which imports this
# file but nothing deeper. Without it, `@strategy_registry.register(...)` never
# runs and `oaa events arm` raises "unknown strategy".
from oaa.strategies.events.strategy import EarningsEventDirectional  # noqa: E402
from oaa.strategies.events.volscreen import VolRead, rank, screen_one
from oaa.strategies.events.watch import Dossier, EventWatcher, WatchNote, WatchReport

__all__ = [
    "ArmReport",
    "DirectionCall",
    "Dossier",
    "EarningsEvent",
    "EarningsEventDirectional",
    "EventsEngine",
    "EventWatcher",
    "EventsParams",
    "EvidencePack",
    "ScreenResult",
    "SizeDecision",
    "VolRead",
    "WatchNote",
    "WatchReport",
    "gather",
    "load_calendar",
    "load_params",
    "predict",
    "rank",
    "screen_one",
    "screen_week",
    "size",
]
