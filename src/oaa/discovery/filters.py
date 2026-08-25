"""Tradability guards.

A buzz list is full of things you cannot actually trade this strategy on:
sub-$5 movers, names with no listed options, leveraged ETFs, and — the one
that bites silently — stocks you cannot borrow. The overnight pairs trade
shorts a leg. A candidate that passes cointegration beautifully and then
cannot be shorted is a trade that fails at 15:55 with the protective options
already bought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oaa.core.logging import get_logger

log = get_logger("discovery.filters")

#: Leveraged and inverse products. Their daily-rebalance decay destroys any
#: long-run relationship, so they cointegrate with nothing and mean-revert
#: against nothing. They show up constantly in most-actives.
LEVERAGED_MARKERS: tuple[str, ...] = (
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SPXU", "UPRO", "SDOW", "UDOW", "TNA", "TZA",
    "SOXL", "SOXS", "LABU", "LABD", "FAS", "FAZ", "NUGT", "DUST", "JNUG", "JDST",
    "UVXY", "SVXY", "VIXY", "TMF", "TMV", "YINN", "YANG", "BOIL", "KOLD",
    "NVDL", "TSLL", "TSLQ", "MSTU", "MSTZ", "CONL", "AMDL",
)


@dataclass
class TradabilityFilter:
    min_price: float = 10.0
    max_price: float = 1500.0
    min_dollar_volume: float = 50_000_000.0
    require_optionable: bool = True
    require_shortable: bool = True
    exclude_leveraged: bool = True
    min_history_days: int = 250
    exclude: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, cfg: Any) -> TradabilityFilter:
        get = (lambda k, d: getattr(cfg, k, d)) if cfg is not None else (lambda k, d: d)
        return cls(
            min_price=float(get("min_price", 10.0)),
            max_price=float(get("max_price", 1500.0)),
            min_dollar_volume=float(get("min_dollar_volume", 50_000_000.0)),
            require_optionable=bool(get("require_optionable", True)),
            require_shortable=bool(get("require_shortable", True)),
            exclude_leveraged=bool(get("exclude_leveraged", True)),
            min_history_days=int(get("min_history_days", 250)),
            exclude={s.upper() for s in (get("exclude", []) or [])},
        )


@dataclass
class FilterVerdict:
    symbol: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    asset: dict[str, Any] = field(default_factory=dict)


def filter_symbols(
    symbols: list[str],
    runner: Any,
    rules: TradabilityFilter,
    price_hint: dict[str, float] | None = None,
) -> tuple[list[str], list[FilterVerdict]]:
    """Check each symbol against the rules. Returns (survivors, all verdicts).

    Asset metadata comes from `alpaca asset get`, which carries `tradable`,
    `shortable`, `easy_to_borrow` and `fractionable` — the fields that decide
    whether the pairs short leg can actually be established.
    """
    survivors: list[str] = []
    verdicts: list[FilterVerdict] = []
    prices = price_hint or {}

    for symbol in symbols:
        verdict = FilterVerdict(symbol=symbol, passed=True)

        if symbol in rules.exclude:
            _fail(verdict, "explicitly excluded")
        if rules.exclude_leveraged and symbol in LEVERAGED_MARKERS:
            _fail(
                verdict,
                "leveraged/inverse product - daily rebalance decay means it "
                "cointegrates with nothing",
            )

        price = prices.get(symbol)
        if price is not None:
            if price < rules.min_price:
                _fail(verdict, f"price {price:.2f} below {rules.min_price:.2f}")
            elif price > rules.max_price:
                _fail(verdict, f"price {price:.2f} above {rules.max_price:.2f}")

        if verdict.passed:
            asset = _asset(runner, symbol)
            verdict.asset = asset
            if not asset:
                _fail(verdict, "no asset record")
            else:
                if not asset.get("tradable", False):
                    _fail(verdict, "not tradable")
                if rules.require_shortable and not asset.get("shortable", False):
                    _fail(verdict, "not shortable - the pairs short leg would fail")
                if rules.require_shortable and not asset.get("easy_to_borrow", True):
                    _fail(verdict, "hard to borrow")

        if verdict.passed and rules.require_optionable:
            if not _has_options(runner, symbol):
                _fail(verdict, "no listed options - cannot be collared")

        verdicts.append(verdict)
        if verdict.passed:
            survivors.append(symbol)
        else:
            log.debug("%s rejected: %s", symbol, "; ".join(verdict.reasons))

    log.info(
        "tradability filter: %d/%d symbols survived", len(survivors), len(symbols)
    )
    return survivors, verdicts


def _fail(verdict: FilterVerdict, reason: str) -> None:
    verdict.passed = False
    verdict.reasons.append(reason)


def _asset(runner: Any, symbol: str) -> dict[str, Any]:
    try:
        payload = runner(["asset", "get", "--symbol", symbol])
    except Exception as exc:  # noqa: BLE001
        log.debug("asset lookup failed for %s: %s", symbol, exc)
        return {}
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


def _has_options(runner: Any, symbol: str) -> bool:
    try:
        payload = runner([
            "option", "contracts", "--underlying-symbols", symbol, "--limit", "1",
        ])
    except Exception as exc:  # noqa: BLE001
        log.debug("option contract lookup failed for %s: %s", symbol, exc)
        return False
    if isinstance(payload, dict):
        contracts = payload.get("option_contracts") or payload.get("contracts") or []
        return bool(contracts)
    return bool(payload)
