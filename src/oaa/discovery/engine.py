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


def _price_screen(
    symbols: list[str],
    rules: TradabilityFilter,
    prices: dict[str, float],
) -> tuple[list[str], list[FilterVerdict]]:
    """Split ranked symbols into (eligible, priced-out) using the free hint.

    A symbol with no price hint is eligible: unknown is not a rejection, and
    `filter_symbols` will look it up properly.
    """
    eligible: list[str] = []
    rejected: list[FilterVerdict] = []
    for symbol in symbols:
        price = prices.get(symbol)
        if price is None:
            eligible.append(symbol)
        elif price < rules.min_price:
            rejected.append(FilterVerdict(
                symbol=symbol, passed=False,
                reasons=[f"price {price:.2f} below {rules.min_price:.2f}"],
            ))
        elif price > rules.max_price:
            rejected.append(FilterVerdict(
                symbol=symbol, passed=False,
                reasons=[f"price {price:.2f} above {rules.max_price:.2f}"],
            ))
        else:
            eligible.append(symbol)
    return eligible, rejected


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
            rules = TradabilityFilter.from_config(self.discovery.filters)
            checks = self.discovery.max_filter_checks

            # Price first, across EVERYTHING, and only then take the top N.
            #
            # The old order took the top N by attention and priced them
            # afterwards. On 28 Aug that meant all 25 checks were spent on
            # sub-$1 squeeze names (FNGR +129% at $0.40, CHAI, CYAB), every one
            # rejected on price, while 39 further symbols were never looked at
            # - so the pool could discover nothing on precisely the days it is
            # most likely to matter. The price hint is free (it rides along in
            # the movers payload); `asset get` and `option contracts` are a
            # request each. Spend the cheap check on all of them, the expensive
            # checks on the survivors.
            prices = {
                entry.symbol: price
                for entry in snapshot.symbols.values()
                if (price := (entry.raw.get("movers") or {}).get("price")) is not None
            }
            eligible, priced_out = _price_screen(snapshot.ranked_symbols(len(snapshot.symbols)),
                                                 rules, prices)
            ranked = eligible[:checks]
            tradable, verdicts = filter_symbols(ranked, self.runner, rules, prices)
            # A name priced out is still a rejection worth journalling - it is
            # the record of why the pool stayed empty.
            rejected = priced_out + [v for v in verdicts if not v.passed]
            if priced_out:
                log.info(
                    "price screen: %d/%d symbols eligible, %d checked",
                    len(eligible), len(snapshot.symbols), len(ranked),
                )
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
