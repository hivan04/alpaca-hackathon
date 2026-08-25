"""Broker selection with an automatic fallback.

If the primary backend cannot connect, we fall back rather than halting - a
week-long P&L window punishes downtime more than it punishes a degraded path.
"""

from __future__ import annotations

from typing import Any

from oaa.brokers.base import Broker, broker_registry
from oaa.config.schema import Config
from oaa.core.errors import BrokerError
from oaa.core.logging import get_logger

log = get_logger("brokers")

# Import for side effects: each module registers itself.
from oaa.brokers import alpaca_cli, alpaca_mcp, alpaca_rest, sim  # noqa: E402,F401


def get_broker(
    cfg: Config,
    credentials: Any = None,
    backend: str | None = None,
    allow_fallback: bool = True,
) -> Broker:
    chosen = backend or cfg.broker.primary
    try:
        broker = broker_registry.get(chosen)(cfg, credentials)
        broker.connect()
        log.info("broker ready: %s (paper=%s)", broker.name, cfg.broker.paper)
        return broker
    except Exception as exc:  # noqa: BLE001
        if not allow_fallback or not cfg.broker.fallback or cfg.broker.fallback == chosen:
            raise BrokerError(f"could not start broker '{chosen}': {exc}") from exc
        log.warning("broker '%s' unavailable (%s); falling back to '%s'",
                    chosen, exc, cfg.broker.fallback)
        broker = broker_registry.get(cfg.broker.fallback)(cfg, credentials)
        broker.connect()
        return broker
