"""Execution.

One job: turn a risk-approved TradeIdea into a filled order, or give up
cleanly. Two hard interlocks:

  1. No ticket is built without a risk stamp (when require_risk_approval).
  2. Every ticket carries a deterministic client_order_id, so a retry after an
     ambiguous failure is checked, not re-fired.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from oaa.brokers.base import Broker
from oaa.config.schema import Config
from oaa.core.errors import BrokerError, RiskRejection
from oaa.core.logging import get_logger
from oaa.core.types import Fill, OrderTicket, RiskVerdict, TradeIdea
from oaa.execution.pricer import limit_price_for, slippage_vs_mid

log = get_logger("execution")


@dataclass
class ExecutionResult:
    ticket: OrderTicket
    fill: Fill | None
    attempts: int = 1
    slippage: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.fill is not None and self.fill.status not in {"rejected", "canceled"}


class ExecutionRouter:
    def __init__(self, cfg: Config, broker: Broker) -> None:
        self.cfg = cfg
        self.broker = broker
        self.exec_cfg = cfg.execution

    # -- ticket construction ------------------------------------------------ #
    def build_ticket(
        self, idea: TradeIdea, verdict: RiskVerdict, step: int = 0
    ) -> OrderTicket:
        if self.cfg.broker.require_risk_approval:
            if not verdict.approved or not verdict.stamp:
                raise RiskRejection(
                    "execution refused: idea carries no risk approval stamp",
                    rule="require_risk_approval",
                )

        quantity = verdict.adjusted_quantity or idea.quantity
        price = (
            limit_price_for(idea, self.exec_cfg.limit_price_ratio, step)
            if self.exec_cfg.order_type == "limit"
            else None
        )
        return OrderTicket(
            idea_id=idea.id,
            client_order_id=self.broker.client_order_id(idea, suffix=str(step)),
            symbol=idea.symbol,
            legs=idea.legs,
            quantity=quantity,
            order_type=self.exec_cfg.order_type,
            limit_price=price,
            time_in_force=self.exec_cfg.time_in_force,
            risk_stamp=verdict.stamp,
            dry_run=self.exec_cfg.dry_run,
        )

    # -- the main path ------------------------------------------------------ #
    def execute(self, idea: TradeIdea, verdict: RiskVerdict) -> ExecutionResult:
        chase = self.exec_cfg.chase
        max_steps = chase.steps if chase.enabled else 1
        last_error: str | None = None
        ticket: OrderTicket | None = None

        for step in range(max_steps):
            ticket = self.build_ticket(idea, verdict, step=step)

            # Ambiguity guard: if this exact ticket already exists at the
            # broker, adopt its fill rather than sending it again.
            existing = self._existing_fill(ticket.client_order_id)
            if existing is not None:
                log.info("client_order_id %s already known - not resubmitting",
                         ticket.client_order_id)
                return ExecutionResult(ticket=ticket, fill=existing, attempts=step + 1)

            try:
                fill = self.broker.submit(ticket)
            except BrokerError as exc:
                last_error = str(exc)
                log.warning("submit failed (step %d): %s", step, exc)
                break

            if fill.status == "dry_run":
                log.info("DRY RUN %s @ %s", idea.describe(), ticket.limit_price)
                return ExecutionResult(ticket=ticket, fill=fill, attempts=step + 1)

            if fill.is_filled:
                slip = slippage_vs_mid(idea, fill.filled_avg_price or ticket.limit_price or 0)
                if slip is not None and slip > chase.max_slippage_pct:
                    log.warning("filled %s with %.1f%% slippage vs mid", idea.symbol, slip * 100)
                return ExecutionResult(ticket=ticket, fill=fill, attempts=step + 1, slippage=slip)

            # Not filled ON THE ACKNOWLEDGEMENT - which is the normal case,
            # not a failure. The exchange answers later, so let the order WORK
            # and then ASK before giving up. This settle-and-repoll used to sit
            # inside the `step < max_steps - 1` branch, so with the chase
            # disabled (max_steps = 1) it never ran and every order was
            # cancelled microseconds after being sent. See
            # `claude/order-cancelled-before-it-could-fill.md`.
            settle = self.exec_cfg.fill_settle_seconds
            if chase.enabled and step < max_steps - 1:
                settle = chase.interval_seconds
            if settle > 0:
                time.sleep(settle)
            refreshed = self.broker.order_status(fill.order_id) if fill.order_id else None
            if refreshed and refreshed.is_filled:
                slip = slippage_vs_mid(
                    idea, refreshed.filled_avg_price or ticket.limit_price or 0
                )
                return ExecutionResult(
                    ticket=ticket, fill=refreshed, attempts=step + 1, slippage=slip
                )

            # Still unfilled. Cancel, and walk the price only if a chase step
            # remains - one resting order per step, never two.
            self.broker.cancel(fill.order_id)
            if step < max_steps - 1:
                log.info("chasing %s: step %d -> %d", idea.symbol, step, step + 1)
            else:
                last_error = (
                    f"unfilled after {settle}s at {ticket.limit_price}"
                    if not chase.enabled else "unfilled after chase"
                )

        assert ticket is not None
        return ExecutionResult(ticket=ticket, fill=None, attempts=max_steps, error=last_error)

    def _existing_fill(self, client_order_id: str) -> Fill | None:
        getter = getattr(self.broker, "order_by_client_id", None)
        if getter is None:
            return None
        try:
            return getter(client_order_id)
        except Exception:  # noqa: BLE001
            return None

    # -- exits --------------------------------------------------------------- #
    def close(self, symbol: str, qty: float | None = None) -> Fill | None:
        if self.exec_cfg.dry_run:
            log.info("DRY RUN close %s qty=%s", symbol, qty)
            return None
        return self.broker.close_position(symbol, qty)

    def flatten_all(self) -> int:
        """Close everything. Called before the submission deadline so the
        judged P&L is realised rather than open."""
        if self.exec_cfg.dry_run:
            log.info("DRY RUN flatten_all")
            return 0
        self.broker.cancel_all()
        closed = 0
        for position in self.broker.account().option_positions():
            if self.broker.close_position(position.symbol):
                closed += 1
        log.info("flattened %d positions", closed)
        return closed
