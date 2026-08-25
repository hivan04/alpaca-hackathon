"""In-process simulator.

Fills at the ticket's limit price, tracks positions and cash, never touches the
network. This is the fallback backend and the backtest engine's broker, so the
same execution code path is exercised in tests as in production.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from oaa.brokers.base import Broker, broker_registry
from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot, Fill, OrderTicket, PositionSnapshot, Right, Side
from oaa.options.occ import is_occ, parse_occ

log = get_logger("brokers.sim")


@broker_registry.register("sim")
class SimBroker(Broker):
    name = "sim"
    supports_multileg = True

    def __init__(self, cfg: Any, credentials: Any = None, starting_cash: float = 100_000.0) -> None:
        super().__init__(cfg, credentials)
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self._positions: dict[str, PositionSnapshot] = {}
        self._orders: dict[str, Fill] = {}
        self._seen_client_ids: dict[str, Fill] = {}
        self.market_open = True
        self.now: dt.datetime = dt.datetime.now(dt.timezone.utc)

    def account(self) -> AccountSnapshot:
        market_value = sum(p.market_value for p in self._positions.values())
        equity = self.cash + market_value
        return AccountSnapshot(
            account_id="SIM-PAPER",
            equity=round(equity, 2),
            last_equity=self.starting_cash,
            cash=round(self.cash, 2),
            buying_power=round(self.cash, 2),
            options_buying_power=round(self.cash, 2),
            regt_buying_power=round(equity * 2, 2),
            daytrading_buying_power=round(equity * 4, 2),
            multiplier=4.0,
            shorting_enabled=True,
            daytrade_count=0,
            pattern_day_trader=False,
            options_trading_level=3,
            positions=list(self._positions.values()),
            asof=self.now,
        )

    def is_market_open(self) -> bool:
        return self.market_open

    def submit(self, ticket: OrderTicket) -> Fill:
        # Idempotency: the same client_order_id never fills twice.
        if ticket.client_order_id in self._seen_client_ids:
            log.debug("duplicate client_order_id %s - returning original fill",
                      ticket.client_order_id)
            return self._seen_client_ids[ticket.client_order_id]

        price = ticket.limit_price
        if price is None:
            # Market order: mark against what we already hold, or the leg's own
            # last known price. Filling an unwind at zero would silently make
            # every rollback look free.
            price = self._reference_price(ticket)
        order_id = f"sim-{uuid.uuid4().hex[:10]}"

        if ticket.dry_run:
            fill = Fill(order_id=order_id, client_order_id=ticket.client_order_id,
                        symbol=ticket.symbol, status="dry_run",
                        filled_avg_price=price, submitted_at=self.now)
            self._seen_client_ids[ticket.client_order_id] = fill
            return fill

        # Cash: positive limit price = debit paid, negative = credit received.
        # Equity legs move cash at 1x; options at 100x per contract.
        if all(leg.is_equity for leg in ticket.legs):
            shares = sum((leg.qty or ticket.quantity) for leg in ticket.legs if leg.side is Side.BUY)
            shares -= sum((leg.qty or ticket.quantity) for leg in ticket.legs if leg.side is Side.SELL)
            self.cash -= price * shares
        else:
            self.cash -= price * 100 * ticket.quantity

        for leg in ticket.legs:
            units = (leg.qty if leg.is_equity and leg.qty else ticket.quantity * leg.ratio)
            signed = units * (1 if leg.side is Side.BUY else -1)
            leg_price = leg.limit_price or abs(price)
            self._apply(leg.symbol, signed, leg_price, equity=leg.is_equity)

        fill = Fill(
            order_id=order_id,
            client_order_id=ticket.client_order_id,
            symbol=ticket.symbol,
            status="filled",
            filled_qty=float(ticket.quantity),
            filled_avg_price=price,
            submitted_at=self.now,
            filled_at=self.now,
            legs=[
                {"symbol": leg.symbol, "side": leg.side.value, "ratio_qty": leg.ratio,
                 "filled_avg_price": leg.limit_price}
                for leg in ticket.legs
            ],
        )
        self._orders[order_id] = fill
        self._seen_client_ids[ticket.client_order_id] = fill
        log.info("SIM fill %s %s x%d @ %.2f", ticket.symbol, ticket.order_type,
                 ticket.quantity, price)
        return fill

    def _apply(
        self, symbol: str, signed_qty: float, price: float, equity: bool = False
    ) -> None:
        multiplier = 1 if equity else 100
        existing = self._positions.get(symbol)
        if existing is None:
            underlying = expiry = strike = right = None
            if is_occ(symbol):
                occ = parse_occ(symbol)
                underlying, expiry, strike = occ.root, occ.expiry, occ.strike
                right = Right(occ.right.value)
            self._positions[symbol] = PositionSnapshot(
                symbol=symbol, qty=signed_qty, avg_entry_price=price,
                market_value=signed_qty * price * multiplier,
                asset_class="us_equity" if equity else "us_option",
                underlying=underlying, expiry=expiry, strike=strike, right=right,
            )
            return

        new_qty = existing.qty + signed_qty
        if abs(new_qty) < 1e-9:
            del self._positions[symbol]
            return
        if (existing.qty > 0) == (signed_qty > 0):  # adding to the position
            total = existing.avg_entry_price * abs(existing.qty) + price * abs(signed_qty)
            existing.avg_entry_price = round(total / abs(new_qty), 4)
        existing.qty = new_qty
        existing.market_value = new_qty * existing.avg_entry_price * multiplier

    def _reference_price(self, ticket: Any) -> float:
        total = 0.0
        for leg in ticket.legs:
            held = self._positions.get(leg.symbol)
            leg_price = leg.limit_price or (held.avg_entry_price if held else 0.0)
            total += leg_price if leg.side is Side.BUY else -leg_price
        return round(total, 4)

    def mark(self, symbol: str, price: float) -> None:
        """Re-mark a position - the backtest engine calls this each bar."""
        pos = self._positions.get(symbol)
        if pos is None:
            return
        pos.market_value = pos.qty * price * 100
        pos.unrealized_pl = round((price - pos.avg_entry_price) * pos.qty * 100, 2)
        if pos.avg_entry_price:
            pos.unrealized_plpc = round(
                (price - pos.avg_entry_price) / pos.avg_entry_price, 5
            )

    def cancel(self, order_id: str) -> bool:
        return self._orders.pop(order_id, None) is not None

    def cancel_all(self) -> int:
        count = len(self._orders)
        self._orders.clear()
        return count

    def close_position(self, symbol: str, qty: float | None = None) -> Fill | None:
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        closing = -(qty if qty is not None else pos.qty)
        price = pos.avg_entry_price
        self.cash += -closing * price * 100
        self._apply(symbol, closing, price)
        return Fill(order_id=f"sim-close-{uuid.uuid4().hex[:8]}", symbol=symbol,
                    status="filled", filled_qty=abs(closing), filled_avg_price=price)

    def order_status(self, order_id: str) -> Fill | None:
        return self._orders.get(order_id)

    def order_by_client_id(self, client_order_id: str) -> Fill | None:
        return self._seen_client_ids.get(client_order_id)
