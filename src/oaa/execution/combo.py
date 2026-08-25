"""Multi-order execution with rollback.

A pairs trade with an options overlay cannot go out as one order. Alpaca's
multi-leg (MLEG) order class does not accept equity legs, so the structure is
four separate orders:

    1. buy the protective put on the long leg
    2. buy the protective call on the short leg
    3. buy the long equity leg
    4. short the hedge equity leg

**Order matters, and it is not the obvious one.** The instinct is to establish
the position first and hedge it after. That is backwards. If the equity legs
fill and the options then fail, the account is carrying an unhedged short
overnight — the single worst state this system can be in. If the options fill
and the equities fail, the account is carrying two cheap long options, whose
maximum loss is the premium already paid.

So: protection first, exposure second. Every partial failure leaves the book
in a bounded state, and anything that did fill is unwound in reverse.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from oaa.brokers.base import Broker
from oaa.config.schema import Config
from oaa.core.errors import BrokerError
from oaa.core.logging import get_logger
from oaa.core.types import AssetKind, Fill, Leg, OrderTicket, Side, TradeIdea

log = get_logger("execution.combo")


class StepStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNWOUND = "unwound"


@dataclass
class PlanStep:
    """One order in a combo. Ordered by `sequence`, lowest first."""

    label: str
    legs: list[Leg]
    quantity: int = 1
    sequence: int = 0
    order_type: str = "limit"
    limit_price: float | None = None
    #: A protective leg is never skipped and never left dangling. If a step
    #: marked critical fails, the whole plan aborts and unwinds.
    critical: bool = True
    kind: AssetKind = AssetKind.OPTION
    status: StepStatus = StepStatus.PENDING
    fill: Fill | None = None
    error: str | None = None

    @property
    def is_multileg(self) -> bool:
        return len(self.legs) > 1

    def describe(self) -> str:
        legs = ", ".join(f"{leg.side.value} {leg.qty or leg.ratio} {leg.symbol}" for leg in self.legs)
        return f"[{self.sequence}] {self.label}: {legs}"


@dataclass
class TradePlan:
    """An ordered set of orders that together form one economic position."""

    idea: TradeIdea
    steps: list[PlanStep] = field(default_factory=list)
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    def add(self, step: PlanStep) -> TradePlan:
        self.steps.append(step)
        return self

    def ordered(self) -> list[PlanStep]:
        return sorted(self.steps, key=lambda s: s.sequence)

    def describe(self) -> str:
        return f"{self.idea.describe()}\n" + "\n".join(
            f"  {step.describe()}" for step in self.ordered()
        )


@dataclass
class ComboResult:
    plan: TradePlan
    filled_steps: list[PlanStep] = field(default_factory=list)
    failed_step: PlanStep | None = None
    unwound: list[PlanStep] = field(default_factory=list)
    unwind_errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.failed_step is None and bool(self.filled_steps)

    @property
    def clean_rollback(self) -> bool:
        """Did the unwind leave the account genuinely flat on this trade?"""
        return self.failed_step is not None and not self.unwind_errors

    def summary(self) -> str:
        if self.dry_run:
            return f"DRY RUN: {len(self.plan.steps)} step(s) planned, nothing sent"
        if self.ok:
            return f"combo filled: {len(self.filled_steps)}/{len(self.plan.steps)} steps"
        head = f"combo ABORTED at '{self.failed_step.label}'" if self.failed_step else "combo failed"
        tail = (
            f", unwound {len(self.unwound)} step(s)"
            if self.unwound else ", nothing to unwind"
        )
        if self.unwind_errors:
            tail += f" — UNWIND ERRORS: {'; '.join(self.unwind_errors)}"
        return head + tail


class ComboExecutor:
    """Runs a TradePlan, unwinding whatever filled if any critical step fails."""

    def __init__(self, cfg: Config, broker: Broker, journal: Any = None) -> None:
        self.cfg = cfg
        self.broker = broker
        self.journal = journal

    # ------------------------------------------------------------------ #
    def execute(self, plan: TradePlan, risk_stamp: str | None = None) -> ComboResult:
        dry_run = self.cfg.execution.dry_run
        result = ComboResult(plan=plan, dry_run=dry_run)

        if dry_run:
            log.info("DRY RUN combo plan:\n%s", plan.describe())
            for step in plan.ordered():
                step.status = StepStatus.SKIPPED
            self._journal(plan, result, "dry_run")
            return result

        for step in plan.ordered():
            ticket = self._ticket(plan.idea, step, risk_stamp)
            try:
                fill = self.broker.submit(ticket)
            except BrokerError as exc:
                step.status, step.error = StepStatus.FAILED, str(exc)
                log.error("combo step '%s' rejected: %s", step.label, exc)
                fill = None
            except Exception as exc:  # noqa: BLE001
                step.status, step.error = StepStatus.FAILED, str(exc)
                log.exception("combo step '%s' blew up", step.label)
                fill = None

            if fill is not None and fill.status not in {"rejected", "canceled"}:
                step.status, step.fill = StepStatus.FILLED, fill
                result.filled_steps.append(step)
                log.info("combo step '%s' -> %s (%s)", step.label, fill.status, fill.order_id)
                continue

            # Step failed.
            if not step.critical:
                step.status = StepStatus.SKIPPED
                log.warning("non-critical step '%s' skipped: %s", step.label, step.error)
                continue

            result.failed_step = step
            log.critical(
                "CRITICAL combo step '%s' failed — unwinding %d filled step(s)",
                step.label, len(result.filled_steps),
            )
            self._unwind(result)
            break

        self._journal(plan, result, "filled" if result.ok else "aborted")
        return result

    # ------------------------------------------------------------------ #
    def _ticket(self, idea: TradeIdea, step: PlanStep, risk_stamp: str | None) -> OrderTicket:
        return OrderTicket(
            idea_id=idea.id,
            client_order_id=self.broker.client_order_id(idea, suffix=step.label),
            symbol=step.legs[0].symbol if step.legs else idea.symbol,
            legs=step.legs,
            quantity=step.quantity,
            order_type=step.order_type,  # type: ignore[arg-type]
            limit_price=step.limit_price,
            time_in_force=self.cfg.execution.time_in_force,
            risk_stamp=risk_stamp,
            dry_run=False,
        )

    def _unwind(self, result: ComboResult) -> None:
        """Reverse whatever filled, most recent first.

        Market orders, deliberately. An unwind that does not fill is worse than
        an unwind that pays the spread — the whole point is to end the attempt
        with a flat book, not a good price.
        """
        for step in reversed(result.filled_steps):
            reversed_legs = [
                Leg(
                    symbol=leg.symbol,
                    side=leg.side.opposite,
                    ratio=leg.ratio,
                    qty=leg.qty,
                    kind=leg.kind,
                    intent=None if leg.is_equity else _closing_intent(leg.side.opposite),
                )
                for leg in step.legs
            ]
            ticket = OrderTicket(
                idea_id=result.plan.idea.id,
                client_order_id=self.broker.client_order_id(
                    result.plan.idea, suffix=f"unwind-{step.label}"
                ),
                symbol=step.legs[0].symbol,
                legs=reversed_legs,
                quantity=step.quantity,
                order_type="market",
                time_in_force="day",
                dry_run=False,
            )
            try:
                self.broker.submit(ticket)
                step.status = StepStatus.UNWOUND
                result.unwound.append(step)
                log.info("unwound combo step '%s'", step.label)
            except Exception as exc:  # noqa: BLE001
                message = f"{step.label}: {exc}"
                result.unwind_errors.append(message)
                log.critical(
                    "UNWIND FAILED for '%s': %s — MANUAL INTERVENTION REQUIRED",
                    step.label, exc,
                )

    def _journal(self, plan: TradePlan, result: ComboResult, outcome: str) -> None:
        if self.journal is None:
            return
        try:
            self.journal.event(
                "combo",
                idea_id=plan.idea.id,
                symbol=plan.idea.symbol,
                structure=plan.idea.structure.value,
                book=plan.idea.book,
                outcome=outcome,
                steps=[
                    {
                        "label": s.label,
                        "sequence": s.sequence,
                        "status": s.status.value,
                        "order_id": s.fill.order_id if s.fill else None,
                        "error": s.error,
                    }
                    for s in plan.ordered()
                ],
                unwind_errors=result.unwind_errors,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("combo journal write failed: %s", exc)


def _closing_intent(side: Side) -> Any:
    from oaa.core.types import Intent

    return Intent.closing(side)


# --------------------------------------------------------------------------- #
def plan_from_idea(idea: TradeIdea, limit_prices: dict[str, float] | None = None) -> TradePlan:
    """Turn a pairs TradeIdea into an ordered, rollback-safe plan.

    Protective options first (sequence 0-1), equity exposure second (2-3), so
    every partial failure leaves the book in a bounded state.
    """
    prices = limit_prices or {}
    plan = TradePlan(idea=idea)

    option_legs = [leg for leg in idea.legs if not leg.is_equity]
    equity_legs = [leg for leg in idea.legs if leg.is_equity]

    for index, leg in enumerate(option_legs):
        plan.add(PlanStep(
            label=f"hedge-{leg.symbol}",
            legs=[leg],
            quantity=idea.quantity * leg.ratio,
            sequence=index,
            order_type="limit" if leg.symbol in prices or leg.limit_price else "market",
            limit_price=prices.get(leg.symbol, leg.limit_price),
            critical=True,
            kind=AssetKind.OPTION,
        ))

    offset = len(option_legs)
    # Long leg before short leg: an unfilled short is safer than an unfilled long
    # when the protective options are already on.
    equity_legs.sort(key=lambda leg: 0 if leg.side is Side.BUY else 1)
    for index, leg in enumerate(equity_legs):
        plan.add(PlanStep(
            label=f"equity-{leg.symbol}",
            legs=[leg],
            quantity=int(leg.qty or 0),
            sequence=offset + index,
            order_type="market",
            limit_price=prices.get(leg.symbol),
            critical=True,
            kind=AssetKind.EQUITY,
        ))

    return plan
