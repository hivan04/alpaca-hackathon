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

    def context_chain_window(self) -> tuple[int, int]:
        """The DTE band the ENABLED strategies could actually trade.

        `options.min/max_days_to_expiry` is the outer envelope the per-contract
        filter uses, NOT what anything trades. Building the context chain from
        the envelope handed `intraday_momentum` - which buys 0-2 DTE - a chain
        whose minimum DTE was 3, so its `ChainView` was empty on every symbol of
        every cycle and it reported "no contracts survived the liquidity filter"
        forever. See `claude/live-chain-defect-confirmed.md`.

        Replay has always been right here: `tradable_dte_range` is the same
        computation `backtest/runner.py` does, and a test already pins that its
        config-only form agrees with the strategy-aware one. This makes the live
        path ask the same question, so the two stop disagreeing.

        Imported inside the function because `backtest` imports `data`.
        """
        from oaa.backtest.runner import tradable_dte_range

        return tradable_dte_range(self.cfg)

    @staticmethod
    def _today() -> dt.date:
        return clock.today()


data_registry: Registry[MarketDataProvider] = Registry("data provider")
