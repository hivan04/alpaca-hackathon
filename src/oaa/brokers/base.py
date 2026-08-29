"""The broker port.

Anything implementing this can be dropped in via config. Strategies, agents
and the risk engine never import a concrete broker.
"""

from __future__ import annotations

import abc
import hashlib
from typing import Any

from oaa.config.schema import Config
from oaa.core.registry import Registry
from oaa.core.types import AccountSnapshot, Fill, OrderTicket, TradeIdea


class Broker(abc.ABC):
    """Minimal surface: know the account, place orders, cancel them."""

    name: str = "base"
    supports_multileg: bool = True

    def __init__(self, cfg: Config, credentials: Any = None) -> None:
        self.cfg = cfg
        self.credentials = credentials

    # -- lifecycle -------------------------------------------------------- #
    def connect(self) -> None:  # pragma: no cover - overridden where needed
        return None

    def close(self) -> None:  # pragma: no cover
        return None

    def __enter__(self) -> Broker:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- read ------------------------------------------------------------- #
    @abc.abstractmethod
    def account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    def is_market_open(self) -> bool: ...

    # -- write ------------------------------------------------------------ #
    @abc.abstractmethod
    def submit(self, ticket: OrderTicket) -> Fill: ...

    @abc.abstractmethod
    def cancel(self, order_id: str) -> bool: ...

    def cancel_all(self) -> int:  # pragma: no cover - optional
        return 0

    def close_position(self, symbol: str, qty: float | None = None) -> Fill | None:
        raise NotImplementedError(f"{self.name} broker cannot close positions directly")

    def order_status(self, order_id: str) -> Fill | None:
        raise NotImplementedError

    def orders(self, limit: int = 200, after: Any = None) -> list[dict[str, Any]]:
        """Order history as plain rows, newest first. Signature matches the
        REST backend's, which is the one that can actually answer it.

        Optional by design: a backend that cannot read history returns nothing
        rather than raising, so a dashboard panel degrades to an empty table
        instead of taking the page down with an AttributeError.
        """
        return []

    # -- helpers shared by every backend ---------------------------------- #
    @staticmethod
    def client_order_id(idea: TradeIdea, suffix: str = "") -> str:
        """Deterministic idempotency key.

        The same idea retried after an ambiguous failure produces the same id,
        so the broker de-duplicates instead of double-filling.
        """
        raw = f"{idea.id}|{idea.symbol}|{idea.structure.value}|{idea.quantity}|{suffix}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
        return f"oaa-{digest}"

    def health(self) -> dict[str, Any]:
        try:
            acct = self.account()
            return {
                "broker": self.name,
                "ok": True,
                "equity": acct.equity,
                "options_level": acct.options_trading_level,
                "positions": len(acct.positions),
            }
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return {"broker": self.name, "ok": False, "error": str(exc)}


broker_registry: Registry[Broker] = Registry("broker")
