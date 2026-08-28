"""The weekend loop.

One cycle is: read the clock, read the tape, manage what is open, then - and
only inside the window, and only when flat - look for an entry. It runs as its
own process (`oaa weekend run`) because its cadence and its calendar have
nothing in common with the options runner's.

State that must survive a restart
---------------------------------
The stop and the target are computed from the band AT ENTRY. If the process
dies at 02:00 on Sunday and comes back at 02:05, recomputing them from the
current band would move the stop - usually further away, because the band has
widened around the move that is hurting. So the open position, its levels and
the cooldown are persisted to `runs/weekend_state.json` and reloaded verbatim.
A restart that cannot find its state file treats any live crypto position as
unattributed and closes it, which is the same recoverable-error convention the
options ledger uses.

Exits are enforced here rather than resting at the broker on purpose: a resting
stop on a 24/7 venue can be swept by a two-second wick that never trades a
share of size. Polling every 60s costs a little slippage and avoids that.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from oaa.core.logging import get_logger
from oaa.core.types import (
    AssetKind,
    Decision,
    DecisionAction,
    Intent,
    Leg,
    OrderTicket,
    RiskVerdict,
    Side,
)
from oaa.strategies.weekend.clock import WindowPhase
from oaa.strategies.weekend.data import fetch_bars, latest_quote
from oaa.strategies.weekend.params import WeekendParams
from oaa.strategies.weekend.signals import evaluate
from oaa.strategies.weekend.strategy import build_idea

log = get_logger("weekend.engine")
UTC = dt.timezone.utc
STATE_PATH = "runs/weekend_state.json"


@dataclass
class OpenPosition:
    symbol: str
    qty: float
    entry: float
    stop: float
    target: float
    entered_at: str
    idea_id: str
    client_order_id: str
    z: float = 0.0
    sigma: float = 0.0
    adx: float = 0.0

    def hours_held(self, now: dt.datetime) -> float:
        started = dt.datetime.fromisoformat(self.entered_at)
        return (now - started).total_seconds() / 3600.0


@dataclass
class WeekendState:
    position: OpenPosition | None = None
    cooldown_until: str | None = None
    last_cycle: str | None = None

    @classmethod
    def load(cls, path: str | Path = STATE_PATH) -> WeekendState:
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("weekend state at %s unreadable - starting flat", p)
            return cls()
        pos = raw.get("position")
        return cls(
            position=OpenPosition(**pos) if pos else None,
            cooldown_until=raw.get("cooldown_until"),
            last_cycle=raw.get("last_cycle"),
        )

    def save(self, path: str | Path = STATE_PATH) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "position": asdict(self.position) if self.position else None,
                    "cooldown_until": self.cooldown_until,
                    "last_cycle": self.last_cycle,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def cooling_down(self, now: dt.datetime) -> bool:
        if not self.cooldown_until:
            return False
        return now < dt.datetime.fromisoformat(self.cooldown_until)


@dataclass
class CycleReport:
    phase: WindowPhase
    now: dt.datetime
    action: str = "idle"
    detail: str = ""
    signal: dict[str, Any] | None = None

    def line(self) -> str:
        return f"[{self.now:%a %H:%M}Z {self.phase.value}] {self.action}: {self.detail}"


class WeekendEngine:
    def __init__(
        self,
        params: WeekendParams,
        broker: Any,
        journal: Any = None,
        state_path: str | Path = STATE_PATH,
    ) -> None:
        self.params = params
        self.broker = broker
        self.journal = journal
        self.state_path = state_path
        self.state = WeekendState.load(state_path)

    # ------------------------------------------------------------------ #
    def cycle(self, now: dt.datetime | None = None) -> CycleReport:
        now = now or dt.datetime.now(UTC)
        phase = self.params.window.phase(now)
        report = CycleReport(phase=phase, now=now)
        self.state.last_cycle = now.isoformat()

        if phase is WindowPhase.CLOSED:
            report.detail = "outside the weekend window"
            self._persist()
            return report

        if phase is WindowPhase.FLATTEN:
            closed = self.flatten("sunday cutoff")
            report.action = "flatten"
            report.detail = f"hard cutoff - closed {closed} position(s)"
            self._persist()
            return report

        symbol = self.params.symbols[0]
        bars = self._bars(symbol)
        quote = latest_quote(symbol)
        price = quote["mid"] if quote else (float(bars[-1]["close"]) if bars else 0.0)

        # -- manage first ------------------------------------------------- #
        if self.state.position is not None:
            reason = self._exit_reason(self.state.position, price, now, phase)
            if reason:
                self._exit(self.state.position, reason, price)
                report.action = "exit"
                report.detail = f"{reason} at {price:,.0f}"
                self._persist()
                return report
            pos = self.state.position
            report.action = "hold"
            report.detail = (
                f"{pos.symbol} {pos.qty} @ {pos.entry:,.0f} -> {price:,.0f} "
                f"(stop {pos.stop:,.0f} target {pos.target:,.0f}, "
                f"{pos.hours_held(now):.1f}h held)"
            )
            self._persist()
            return report

        # -- entries, only while fully open -------------------------------- #
        if phase is WindowPhase.MANAGE_ONLY:
            report.detail = "past last entry - exits only"
            self._persist()
            return report
        if self.state.cooling_down(now):
            report.detail = f"cooling down until {self.state.cooldown_until}"
            self._persist()
            return report

        signal = evaluate(symbol, bars, self.params)
        report.signal = signal.as_dict()
        if not signal.actionable:
            report.action = "skip"
            report.detail = signal.reason
            self._journal_skip(signal)
            self._persist()
            return report

        entry = quote["ask"] if quote else price
        idea = build_idea(signal, self.params, equity=self._equity(), entry_price=entry)
        if idea is None:
            report.action = "skip"
            report.detail = "sizing produced no order (below the minimum notional)"
            self._persist()
            return report

        fill = self._submit(idea, entry)
        report.action = "entry"
        report.detail = idea.describe() if fill else "order not filled"
        self._persist()
        return report

    # ------------------------------------------------------------------ #
    def _exit_reason(
        self, pos: OpenPosition, price: float, now: dt.datetime, phase: WindowPhase
    ) -> str | None:
        if price <= pos.stop:
            return "stop"
        if price >= pos.target:
            return "target"
        if pos.hours_held(now) >= self.params.exits.max_hold_hours:
            return "time_stop"
        if self.params.window.hours_to_flatten(now) <= 0.25:
            return "window_flatten"
        return None

    def _exit(self, pos: OpenPosition, reason: str, price: float) -> None:
        if self.params.execution.dry_run:
            log.info("DRY RUN exit %s %s (%s) @ %s", pos.symbol, pos.qty, reason, price)
        else:
            try:
                self.broker.close_position(pos.symbol, pos.qty)
            except Exception as exc:  # noqa: BLE001 - an exit must not raise
                log.error("weekend exit failed for %s: %s", pos.symbol, exc)
                return
        pnl = self.params.costs.net_of_costs(
            pos.entry, price, pos.qty, crossing_exit=reason != "target"
        )
        log.info("weekend exit %s %s @ %s (%s) pnl %.2f", pos.symbol, pos.qty, price, reason, pnl)
        self._journal_event("weekend_exit", symbol=pos.symbol, reason=reason, price=price, pnl=pnl)
        self.state.position = None
        if reason == "stop":
            until = dt.datetime.now(UTC) + dt.timedelta(hours=self.params.exits.cooldown_hours)
            self.state.cooldown_until = until.isoformat()

    def flatten(self, reason: str = "manual") -> int:
        """Close every crypto position, whether or not this process opened it.

        Unattributed crypto is treated as ours and closed - the same convention
        the options ledger uses, and the safe direction of the error for a book
        that must not exist during an equity session.
        """
        closed = 0
        try:
            positions = [
                p for p in self.broker.account().positions
                if "/" in p.symbol or (p.asset_class or "").lower() == "crypto"
            ]
        except Exception as exc:  # noqa: BLE001
            log.error("weekend flatten could not read the account: %s", exc)
            positions = []
        for position in positions:
            if self.params.execution.dry_run:
                log.info("DRY RUN flatten %s %s", position.symbol, position.qty)
                closed += 1
                continue
            try:
                self.broker.close_position(position.symbol, abs(position.qty))
                closed += 1
            except Exception as exc:  # noqa: BLE001
                log.error("could not close %s: %s", position.symbol, exc)
        if closed:
            self._journal_event("weekend_flatten", reason=reason, closed=closed)
        self.state.position = None
        return closed

    # ------------------------------------------------------------------ #
    def _submit(self, idea: Any, entry: float) -> Any:
        leg = idea.legs[0]
        ticket = OrderTicket(
            idea_id=idea.id,
            client_order_id=self.broker.client_order_id(idea),
            symbol=idea.symbol,
            legs=[
                Leg(
                    symbol=leg.symbol,
                    side=Side.BUY,
                    kind=AssetKind.CRYPTO,
                    qty=leg.qty,
                    intent=Intent.BUY_TO_OPEN,
                )
            ],
            quantity=1,
            order_type="limit",
            limit_price=round(entry, 2),
            time_in_force="gtc",
            risk_stamp=RiskVerdict.approve(1).stamp,
            dry_run=self.params.execution.dry_run,
        )
        try:
            fill = self.broker.submit(ticket)
        except Exception as exc:  # noqa: BLE001
            log.error("weekend entry rejected: %s", exc)
            self._journal_event("weekend_entry_rejected", symbol=idea.symbol, error=str(exc))
            return None

        self.state.position = OpenPosition(
            symbol=idea.symbol,
            qty=float(leg.qty or 0),
            entry=entry,
            stop=float(idea.meta["stop"]),
            target=float(idea.meta["target"]),
            entered_at=dt.datetime.now(UTC).isoformat(),
            idea_id=idea.id,
            client_order_id=ticket.client_order_id,
            z=float(idea.meta.get("z") or 0),
            sigma=float(idea.meta.get("sigma") or 0),
            adx=float(idea.meta.get("adx") or 0),
        )
        self._journal_decision(idea, fill)
        return fill

    def _bars(self, symbol: str) -> list[dict[str, Any]]:
        lookback_bars = self.params.signal.min_bars + self.params.signal.lookback_bars
        days = max(3, int(lookback_bars / 96) + 2)
        end = dt.datetime.now(UTC)
        return fetch_bars(
            symbol=symbol,
            timeframe=self.params.signal.timeframe,
            start=end - dt.timedelta(days=days),
            end=end,
        )

    def _equity(self) -> float:
        try:
            return float(self.broker.account().equity or 0.0)
        except Exception as exc:  # noqa: BLE001
            log.error("weekend engine could not read equity: %s", exc)
            return 0.0

    def _persist(self) -> None:
        self.state.save(self.state_path)

    # -- journal ------------------------------------------------------------ #
    def _journal_decision(self, idea: Any, fill: Any) -> None:
        if self.journal is None:
            return
        self.journal.record(
            Decision(
                cycle="weekend",
                action=DecisionAction.OPEN,
                symbol=idea.symbol,
                strategy="weekend_crypto_reversion",
                idea=idea,
                fill=fill,
                rationale=idea.thesis,
            )
        )

    def _journal_skip(self, signal: Any) -> None:
        if self.journal is None:
            return
        self.journal.record(
            Decision(
                cycle="weekend",
                action=DecisionAction.SKIP,
                symbol=signal.symbol,
                strategy="weekend_crypto_reversion",
                rationale=signal.reason,
                agent_notes=signal.as_dict(),
            )
        )

    def _journal_event(self, kind: str, **fields: Any) -> None:
        if self.journal is not None:
            self.journal.event(kind, **fields)
