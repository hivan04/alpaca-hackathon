"""Universe discovery and the macro lens.

Two jobs, sharing one input:

**Discovery** finds what the market is actually paying attention to today, and
turns that into a candidate pool for the offline cointegration screen. It stops
the tradable universe being a hardcoded guess.

**The macro lens** reads the same picture and forms a view — the one lens the
deterministic strategies cannot supply, because it means reading unstructured
text rather than computing a number. It emits a *regime*, not a trade: which
strategies should be live tonight, how wide the collars should be, and which
symbols carry too much headline risk to hold overnight.

Two disciplines worth knowing about, both enforced here:

1. **Attention generates candidates; cointegration still decides.** A name that
   is suddenly hot is one whose historical relationship is breaking, which is
   the worst possible input to a mean-reversion strategy if taken at face value.

2. **Nothing from this module enters the gap model's feature set.** Most-actives
   and movers are live snapshots with no history, so a feature built on them
   could not be replayed and would silently poison the walk-forward backtest.
   Discovery is an *overlay* on a model that stays backtestable.
"""

from oaa.discovery.engine import DiscoveryEngine, DiscoveryResult
from oaa.discovery.filters import TradabilityFilter, filter_symbols
from oaa.discovery.macro import MacroLens, MacroView
from oaa.discovery.score import AttentionSnapshot, SymbolAttention, score_snapshot
from oaa.discovery.sources import (
    AttentionSource,
    HttpJsonSource,
    MostActivesSource,
    MoversSource,
    NewsSource,
    build_sources,
)
from oaa.discovery.universe import CandidatePool

__all__ = [
    "AttentionSnapshot",
    "DiscoveryEngine",
    "DiscoveryResult",
    "AttentionSource",
    "CandidatePool",
    "HttpJsonSource",
    "MacroLens",
    "MacroView",
    "MostActivesSource",
    "MoversSource",
    "NewsSource",
    "SymbolAttention",
    "TradabilityFilter",
    "build_sources",
    "filter_symbols",
    "score_snapshot",
]
