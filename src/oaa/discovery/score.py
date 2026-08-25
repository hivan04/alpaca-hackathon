"""Turning raw source metrics into one comparable attention score.

Sources measure incompatible things — share volume, percentage moves, article
velocity, a vendor's 0-100 sentiment number. Blending them requires putting
them on the same footing first, and the honest way to do that is by *rank*
within each source rather than by value. A stock with 400m shares traded is not
"twenty times hotter" than one with 20m; it is first instead of tenth.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from oaa.core.logging import get_logger
from oaa.discovery.sources import SourceResult

log = get_logger("discovery.score")

#: How much each source contributes. News is weighted highest because it is the
#: only replayable one and the only one carrying a *reason*; volume is weighted
#: lowest because a mega-cap is always in the most-actives list and that fact
#: carries almost no information.
DEFAULT_WEIGHTS: dict[str, float] = {
    "news": 0.40,
    "movers": 0.35,
    "most_actives": 0.15,
    "external": 0.10,
}


@dataclass
class SymbolAttention:
    symbol: str
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    headlines: list[str] = field(default_factory=list)
    percent_change: float | None = None
    direction: str | None = None
    news_velocity: float | None = None

    @property
    def is_news_driven(self) -> bool:
        """Attention with a story behind it, rather than just volume.

        This is the distinction that matters for the overnight book: a name
        moving on heavy volume is normal, a name moving because something
        *happened* is a name that can gap again tonight.
        """
        return (self.news_velocity or 0) >= 2.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "percent_change": self.percent_change,
            "direction": self.direction,
            "news_velocity": self.news_velocity,
            "news_driven": self.is_news_driven,
            "headlines": self.headlines[:3],
        }


@dataclass
class AttentionSnapshot:
    asof: dt.datetime
    symbols: dict[str, SymbolAttention] = field(default_factory=dict)
    breadth: dict[str, int] = field(default_factory=dict)
    source_errors: dict[str, str] = field(default_factory=dict)
    replayable_only: bool = False

    def top(self, n: int = 20) -> list[SymbolAttention]:
        return sorted(self.symbols.values(), key=lambda s: -s.score)[:n]

    def ranked_symbols(self, n: int = 20) -> list[str]:
        return [s.symbol for s in self.top(n)]

    def news_driven(self, min_velocity: float = 2.0) -> list[SymbolAttention]:
        return [
            s for s in self.symbols.values()
            if (s.news_velocity or 0) >= min_velocity
        ]

    @property
    def breadth_ratio(self) -> float | None:
        """Gainers as a fraction of all movers. 0.5 = balanced tape."""
        gainers = self.breadth.get("gainers", 0)
        losers = self.breadth.get("losers", 0)
        total = gainers + losers
        return round(gainers / total, 4) if total else None

    def as_dict(self, top: int = 25) -> dict[str, Any]:
        return {
            "asof": self.asof.isoformat(),
            "breadth": self.breadth,
            "breadth_ratio": self.breadth_ratio,
            "source_errors": self.source_errors,
            "symbols": [s.as_dict() for s in self.top(top)],
        }


def _rank_scores(values: dict[str, float]) -> dict[str, float]:
    """Rank-normalise to 0..1. Highest raw value scores 1.0."""
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda kv: -kv[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 1.0}
    return {symbol: 1.0 - (index / (n - 1)) for index, (symbol, _) in enumerate(ordered)}


def score_snapshot(
    results: Sequence[SourceResult],
    weights: dict[str, float] | None = None,
    asof: dt.datetime | None = None,
    replayable_only: bool = False,
) -> AttentionSnapshot:
    """Blend source results into one ranked snapshot.

    `replayable_only` drops every non-replayable source. Use it when the output
    will feed anything that has to be reconstructed for a past date — otherwise
    you have quietly introduced data that cannot be backtested.
    """
    snapshot = AttentionSnapshot(
        asof=asof or dt.datetime.now(dt.timezone.utc),
        replayable_only=replayable_only,
    )
    weights = weights or DEFAULT_WEIGHTS
    usable = []

    for result in results:
        if not result.ok:
            snapshot.source_errors[result.name] = result.error or "unknown error"
            log.warning("source '%s' failed: %s", result.name, result.error)
            continue
        if replayable_only and not result.replayable:
            log.debug("skipping non-replayable source '%s'", result.name)
            continue
        usable.append(result)

        breadth = result.detail.get("__breadth__")
        if isinstance(breadth, dict):
            snapshot.breadth.update({k: int(v) for k, v in breadth.items()})

    # Only weight the sources that actually returned something, then renormalise
    # so a failed source does not silently shrink every score.
    active = {r.name: weights.get(r.name, 0.1) for r in usable if r.values}
    total_weight = sum(active.values()) or 1.0

    for result in usable:
        weight = active.get(result.name, 0.0) / total_weight
        if weight <= 0:
            continue
        for symbol, normalised in _rank_scores(result.values).items():
            entry = snapshot.symbols.setdefault(symbol, SymbolAttention(symbol=symbol))
            entry.components[result.name] = round(normalised, 4)
            entry.score += weight * normalised

            detail = result.detail.get(symbol)
            if not isinstance(detail, dict):
                continue
            entry.raw[result.name] = detail
            if result.name == "news":
                entry.news_velocity = detail.get("velocity")
                entry.headlines = detail.get("headlines", []) or entry.headlines
            elif result.name == "movers":
                entry.percent_change = detail.get("percent_change")
                entry.direction = detail.get("direction")

    for entry in snapshot.symbols.values():
        entry.score = round(min(1.0, entry.score), 4)

    log.info(
        "attention snapshot: %d symbols from %d source(s)%s",
        len(snapshot.symbols), len(usable),
        " (replayable only)" if replayable_only else "",
    )
    return snapshot
