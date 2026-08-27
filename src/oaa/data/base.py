"""Market data port.

One method matters: `context(symbol)` returns the full MarketContext a strategy
reasons over. Everything else is a building block for it.
"""

from __future__ import annotations

import abc
import datetime as dt
from typing import Any

from oaa.config.schema import Config
from oaa.core import clock
from oaa.core.registry import Registry
from oaa.core.types import MarketContext, OptionQuote


class MarketDataProvider(abc.ABC):
    name = "base"

    def __init__(self, cfg: Config, credentials: Any = None) -> None:
        self.cfg = cfg
        self.credentials = credentials

    @abc.abstractmethod
    def spot(self, symbol: str) -> float: ...

    @abc.abstractmethod
    def bars(
        self,
        symbol: str,
        lookback_days: int = 90,
        timeframe: str = "1Day",
    ) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def option_chain(
        self,
        symbol: str,
        min_dte: int | None = None,
        max_dte: int | None = None,
        strike_low: float | None = None,
        strike_high: float | None = None,
    ) -> list[OptionQuote]: ...

    def quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        raise NotImplementedError

    @abc.abstractmethod
    def context(self, symbol: str, lookback_days: int = 90) -> MarketContext: ...

    # -- shared helpers ---------------------------------------------------- #
    def chain_strike_window(self, spot: float, width_pct: float = 0.20) -> tuple[float, float]:
        """Narrow the chain request. On the free tier a full SPY chain is
        thousands of contracts and will exhaust the 200 req/min budget."""
        return round(spot * (1 - width_pct), 2), round(spot * (1 + width_pct), 2)

    @staticmethod
    def _today() -> dt.date:
        return clock.today()


data_registry: Registry[MarketDataProvider] = Registry("data provider")
