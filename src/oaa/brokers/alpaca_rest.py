"""alpaca-py backend. The default path.

Multi-leg options orders go out as OrderClass.MLEG with a signed limit price:
positive = net debit paid, negative = net credit received.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from oaa.brokers.base import Broker, broker_registry
from oaa.core.errors import BrokerError
from oaa.core.logging import get_logger
from oaa.core.types import (
    AccountSnapshot,
    Fill,
    OrderTicket,
    PositionSnapshot,
    Right,
    Side,
)
from oaa.options.occ import is_occ, parse_occ

log = get_logger("brokers.rest")


@broker_registry.register("rest")
class AlpacaRestBroker(Broker):
    name = "alpaca-rest"
    supports_multileg = True

    def __init__(self, cfg: Any, credentials: Any = None) -> None:
        super().__init__(cfg, credentials)
        self._client: Any = None

    # -- lifecycle -------------------------------------------------------- #
    def connect(self) -> None:
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover
            raise BrokerError("alpaca-py is not installed - run `make install`") from exc

        creds = self.credentials
        if not creds or not creds.configured:
            raise BrokerError(
                "Alpaca credentials missing. Copy .env.example to .env and fill "
                "in ALPACA_API_KEY / ALPACA_SECRET_KEY."
            )
        self._client = TradingClient(
            api_key=creds.api_key,
            secret_key=creds.secret_key,
            paper=self.cfg.broker.paper,
        )
        # Fail fast on a bad key rather than at the first order.
        self._client.get_account()

    @property
    def client(self) -> Any:
        if self._client is None:
            self.connect()
        return self._client

    # -- read ------------------------------------------------------------- #
    def account(self) -> AccountSnapshot:
        acct = self.client.get_account()
        positions = [self._to_position(p) for p in self.client.get_all_positions()]
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            open_orders = len(
                self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
            )
        except Exception:  # noqa: BLE001
            open_orders = 0

        return AccountSnapshot(
            account_id=str(getattr(acct, "account_number", "") or getattr(acct, "id", "")),
            equity=float(acct.equity or 0),
            last_equity=float(acct.last_equity or 0),
            cash=float(acct.cash or 0),
            buying_power=float(acct.buying_power or 0),
            options_buying_power=_maybe_float(getattr(acct, "options_buying_power", None)),
            regt_buying_power=_maybe_float(getattr(acct, "regt_buying_power", None)),
            daytrading_buying_power=_maybe_float(
                getattr(acct, "daytrading_buying_power", None)
            ),
            multiplier=_maybe_float(getattr(acct, "multiplier", None)),
            shorting_enabled=getattr(acct, "shorting_enabled", None),
            daytrade_count=_maybe_int(getattr(acct, "daytrade_count", None)),
            pattern_day_trader=getattr(acct, "pattern_day_trader", None),
            options_trading_level=_maybe_int(getattr(acct, "options_trading_level", None)),
            positions=positions,
            open_orders=open_orders,
        )

    def is_market_open(self) -> bool:
        try:
            return bool(self.client.get_clock().is_open)
        except Exception as exc:  # noqa: BLE001
            log.warning("clock unavailable (%s); assuming closed", exc)
            return False

    def options_level(self) -> int | None:
        return self.account().options_trading_level

    def ensure_options_level(self, level: int = 3) -> bool:
        """Paper accounts can raise their own options level. Level 3 is what
        multi-leg spreads require - check this before the first cycle."""
        current = self.options_level()
        if current is not None and current >= level:
            return True
        try:
            cfg = self.client.get_account_configurations()
            cfg.max_options_trading_level = level
            self.client.set_account_configurations(cfg)
            log.info("raised options trading level to %s", level)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("could not raise options level to %s: %s", level, exc)
            return False

    # -- write ------------------------------------------------------------ #
    def submit(self, ticket: OrderTicket) -> Fill:
        from alpaca.trading.enums import (
            OrderClass,
            OrderSide,
            PositionIntent,
            TimeInForce,
        )
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            OptionLegRequest,
        )

        if ticket.dry_run:
            return _dry_fill(ticket)

        tif = TimeInForce.DAY if ticket.time_in_force == "day" else TimeInForce.GTC
        side_map = {Side.BUY: OrderSide.BUY, Side.SELL: OrderSide.SELL}

        if ticket.is_multileg:
            if not 2 <= len(ticket.legs) <= 4:
                raise BrokerError(
                    f"Alpaca multi-leg orders accept 2-4 legs, got {len(ticket.legs)}"
                )
            legs = [
                OptionLegRequest(
                    symbol=leg.symbol,
                    ratio_qty=leg.ratio,
                    side=side_map[leg.side],
                    position_intent=PositionIntent(leg.resolved_intent().value),
                )
                for leg in ticket.legs
            ]
            kwargs: dict[str, Any] = {
                "qty": ticket.quantity,
                "time_in_force": tif,
                "order_class": OrderClass.MLEG,
                "legs": legs,
                "client_order_id": ticket.client_order_id,
            }
            if ticket.order_type == "limit":
                if ticket.limit_price is None:
                    raise BrokerError("limit multi-leg order requires limit_price")
                request = LimitOrderRequest(limit_price=ticket.limit_price, **kwargs)
            else:
                request = MarketOrderRequest(**kwargs)
        else:
            leg = ticket.legs[0]
            if leg.is_crypto and ticket.time_in_force == "day":
                # Alpaca rejects `day` on a 24/7 asset outright. The weekend
                # book sets gtc in its own config; this is the backstop for a
                # ticket built from the shared execution defaults.
                tif = TimeInForce.GTC
            kwargs = {
                "symbol": leg.symbol,
                "qty": leg.qty if leg.is_equity and leg.qty else ticket.quantity * leg.ratio,
                "side": side_map[leg.side],
                "time_in_force": tif,
                "client_order_id": ticket.client_order_id,
            }
            if leg.is_option:
                # position_intent is an options concept; sending it on an equity
                # or crypto order is rejected by Alpaca.
                kwargs["position_intent"] = PositionIntent(leg.resolved_intent().value)
            elif ticket.time_in_force == "day":
                # Equity legs of an overnight pair must be marketable at the
                # close, so GTC would leave a resting order into the next day.
                kwargs["time_in_force"] = tif
            request = (
                LimitOrderRequest(limit_price=ticket.limit_price, **kwargs)
                if ticket.order_type == "limit"
                else MarketOrderRequest(**kwargs)
            )

        try:
            order = self.client.submit_order(order_data=request)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"order rejected for {ticket.symbol}: {exc}") from exc

        return self._to_fill(order, ticket)

    def cancel(self, order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel failed for %s: %s", order_id, exc)
            return False

    def cancel_all(self) -> int:
        try:
            return len(self.client.cancel_orders() or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel_all failed: %s", exc)
            return 0

    def close_position(self, symbol: str, qty: float | None = None) -> Fill | None:
        from alpaca.trading.requests import ClosePositionRequest

        try:
            # Crypto positions are fractional, so the quantity cannot be
            # truncated to an int - `int(0.0431)` closes nothing and leaves the
            # book exposed through Monday's open.
            req = ClosePositionRequest(qty=_qty_str(abs(qty))) if qty else None
            order = self.client.close_position(symbol, req)
            return self._to_fill(order, None)
        except Exception as exc:  # noqa: BLE001
            log.error("close_position failed for %s: %s", symbol, exc)
            return None

    def order_status(self, order_id: str) -> Fill | None:
        try:
            return self._to_fill(self.client.get_order_by_id(order_id), None)
        except Exception:  # noqa: BLE001
            return None

    def order_by_client_id(self, client_order_id: str) -> Fill | None:
        """The safe way to check before retrying an ambiguous submission."""
        try:
            return self._to_fill(
                self.client.get_order_by_client_id(client_order_id), None
            )
        except Exception:  # noqa: BLE001
            return None

    def orders(self, limit: int = 200, after: dt.datetime | None = None) -> list[dict[str, Any]]:
        """Every order this account has placed, newest first.

        Read from Alpaca rather than from the journal on purpose. The journal
        records what the AGENT did; this account is also reachable by hand, by
        an earlier build, and by anything else holding the keys - and at
        submission the judges read the account, not our log of it. Where the
        two disagree, the broker is right.
        """
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            request = GetOrdersRequest(
                status=QueryOrderStatus.ALL, limit=min(int(limit), 500),
                after=after, nested=True,
            )
            raw = self.client.get_orders(request)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read order history: %s", exc)
            return []

        out: list[dict[str, Any]] = []
        for order in raw or []:
            # `nested=True` returns a multi-leg order with its legs attached.
            # Flattening them keeps one row per CONTRACT, which is what a
            # reader counting positions expects to see.
            for item in [order, *(getattr(order, "legs", None) or [])]:
                symbol = str(getattr(item, "symbol", "") or "")
                filled_qty = _maybe_float(getattr(item, "filled_qty", None)) or 0.0
                price = _maybe_float(getattr(item, "filled_avg_price", None))
                out.append({
                    "submitted_at": getattr(item, "submitted_at", None),
                    "filled_at": getattr(item, "filled_at", None),
                    "symbol": symbol,
                    "underlying": parse_occ(symbol).underlying if is_occ(symbol) else symbol,
                    "side": str(getattr(getattr(item, "side", None), "value", "") or ""),
                    "qty": _maybe_float(getattr(item, "qty", None)) or 0.0,
                    "filled_qty": filled_qty,
                    "filled_avg_price": price,
                    "notional": round(price * filled_qty * 100, 2) if price else None,
                    "status": str(getattr(getattr(item, "status", None), "value", "") or ""),
                    "order_type": str(getattr(getattr(item, "order_type", None), "value", "") or ""),
                    "order_id": str(getattr(item, "id", "") or ""),
                    "client_order_id": str(getattr(item, "client_order_id", "") or ""),
                    "leg": item is not order,
                })
        return out

    # -- mapping ---------------------------------------------------------- #
    @staticmethod
    def _to_position(p: Any) -> PositionSnapshot:
        symbol = str(p.symbol)
        underlying = expiry = strike = right = None
        if is_occ(symbol):
            occ = parse_occ(symbol)
            underlying, expiry, strike = occ.root, occ.expiry, occ.strike
            right = Right(occ.right.value)
        return PositionSnapshot(
            symbol=symbol,
            qty=float(p.qty),
            avg_entry_price=float(p.avg_entry_price or 0),
            market_value=float(p.market_value or 0),
            unrealized_pl=float(p.unrealized_pl or 0),
            unrealized_plpc=float(p.unrealized_plpc or 0),
            asset_class=str(getattr(p.asset_class, "value", p.asset_class)),
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,
        )

    @staticmethod
    def _to_fill(order: Any, ticket: OrderTicket | None) -> Fill:
        return Fill(
            order_id=str(order.id),
            client_order_id=getattr(order, "client_order_id", None),
            symbol=str(order.symbol or (ticket.symbol if ticket else "")),
            status=str(getattr(order.status, "value", order.status)),
            filled_qty=float(order.filled_qty or 0),
            filled_avg_price=_maybe_float(order.filled_avg_price),
            submitted_at=order.submitted_at,
            filled_at=order.filled_at,
            legs=[
                {
                    "symbol": leg.symbol,
                    "side": str(getattr(leg.side, "value", leg.side)),
                    "ratio_qty": float(leg.ratio_qty or 0),
                    "filled_avg_price": _maybe_float(leg.filled_avg_price),
                    "status": str(getattr(leg.status, "value", leg.status)),
                }
                for leg in (getattr(order, "legs", None) or [])
            ],
        )


def _qty_str(qty: float) -> str:
    return f"{qty:.9f}".rstrip("0").rstrip(".") if qty % 1 else str(int(qty))


def _dry_fill(ticket: OrderTicket) -> Fill:
    return Fill(
        order_id=f"dry-{ticket.client_order_id}",
        client_order_id=ticket.client_order_id,
        symbol=ticket.symbol,
        status="dry_run",
        filled_qty=0.0,
        filled_avg_price=ticket.limit_price,
        submitted_at=dt.datetime.now(dt.timezone.utc),
        legs=[{"symbol": leg.symbol, "side": leg.side.value, "ratio_qty": leg.ratio}
              for leg in ticket.legs],
    )


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
