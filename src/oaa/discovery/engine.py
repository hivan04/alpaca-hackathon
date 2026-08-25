"""The discovery cycle, end to end.

    sources -> score -> tradability filter -> candidate pool
                     \\
                      -> macro lens -> regime view

Runs pre-market. Cheap: a handful of CLI calls plus at most one model call.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oaa.core.logging import get_logger
from oaa.discovery.filters import FilterVerdict, TradabilityFilter, filter_symbols
from oaa.discovery.macro import MacroLens, MacroView
from oaa.discovery.score import AttentionSnapshot, score_snapshot
from oaa.discovery.sources import build_sources, cli_runner
from oaa.discovery.universe import CandidatePool

log = get_logger("discovery.engine")


@dataclass
class DiscoveryResult:
    snapshot: AttentionSnapshot
    macro: MacroView
    pool: CandidatePool
    tradable: list[str] = field(default_factory=list)
    rejected: list[FilterVerdict] = field(default_factory=list)
    new_symbols: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"attention {len(self.snapshot.symbols)} symbols | "
            f"tradable {len(self.tradable)} | new {len(self.new_symbols)} | "
            f"pool {len(self.pool.entries)} | macro {self.macro.regime}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.as_dict(),
            "macro": self.macro.as_dict(),
            "tradable": self.tradable,
            "new_symbols": self.new_symbols,
            "pool": self.pool.stats(),
            "rejected": [
                {"symbol": v.symbol, "reasons": v.reasons} for v in self.rejected if not v.passed
            ],
        }


class DiscoveryEngine:
    def __init__(
        self,
        settings: Any,
        llm: Any = None,
        journal: Any = None,
        runner: Any = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.config
        self.discovery = self.cfg.discovery
        self.journal = journal
        self.runner = runner or cli_runner(
            self.cfg.broker.cli.binary,
            settings.credentials,
            self.cfg.broker.paper,
        )
        self.sources = build_sources(self.discovery, self.runner)
        self.macro_lens = MacroLens(self.discovery, llm=llm, journal=journal)
        self.pool = CandidatePool.load(
            settings.path(self.discovery.pool.path),
            accumulate_days=self.discovery.pool.accumulate_days,
            max_symbols=self.discovery.pool.max_symbols,
            seeds=self.discovery.pool.seeds,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.discovery.enabled)

    # ------------------------------------------------------------------ #
    def run(
        self,
        strategies: list[str] | None = None,
        pairs: list[tuple[str, str]] | None = None,
        asof: dt.date | None = None,
        apply_filters: bool = True,
        replayable_only: bool = False,
    ) -> DiscoveryResult:
        results = []
        for source in self.sources:
            try:
                results.append(source.fetch(asof))
            except Exception as exc:  # noqa: BLE001 - one bad source must not
                # take the cycle down; discovery is an overlay, not a dependency.
                log.error("source '%s' blew up: %s", source.name, exc)

        snapshot = score_snapshot(
            results,
            weights=self.discovery.weights,
            replayable_only=replayable_only,
        )

        tradable: list[str] = []
        rejected: list[FilterVerdict] = []
        if apply_filters and snapshot.symbols:
            ranked = snapshot.ranked_symbols(self.discovery.max_filter_checks)
            rules = TradabilityFilter.from_config(self.discovery.filters)
            prices = {
                entry.symbol: price
                for entry in snapshot.top(self.discovery.max_filter_checks)
                if (price := (entry.raw.get("movers") or {}).get("price")) is not None
            }
            tradable, verdicts = filter_symbols(ranked, self.runner, rules, prices)
            rejected = [v for v in verdicts if not v.passed]
        else:
            tradable = snapshot.ranked_symbols(self.discovery.max_filter_checks)

        new_symbols = self.pool.observe(
            {s: snapshot.symbols[s].score for s in tradable if s in snapshot.symbols},
            asof=asof,
        )
        self.pool.save()

        macro = self.macro_lens.view(snapshot, strategies or [], pairs or [])

        result = DiscoveryResult(
            snapshot=snapshot, macro=macro, pool=self.pool,
            tradable=tradable, rejected=rejected, new_symbols=new_symbols,
        )
        if self.journal is not None:
            try:
                self.journal.event("discovery", **result.as_dict())
            except Exception as exc:  # noqa: BLE001
                log.debug("discovery journal write failed: %s", exc)
        log.info("discovery: %s", result.summary())
        return result

    # ------------------------------------------------------------------ #
    def candidates(self, limit: int | None = None) -> list[str]:
        return self.pool.candidates(limit)
