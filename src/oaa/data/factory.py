from __future__ import annotations

from typing import Any

from oaa.config.schema import Config
from oaa.data import alpaca_data, cli_data  # noqa: F401  (register providers)
from oaa.data.base import MarketDataProvider, data_registry


def get_data_provider(cfg: Config, credentials: Any = None) -> MarketDataProvider:
    return data_registry.get(cfg.data.provider)(cfg, credentials)
