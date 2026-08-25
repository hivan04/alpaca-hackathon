"""The always-on scheduler.

`oaa run` starts this and walks away. It wakes itself on the cycle times in
config, monitors positions in between, and keeps going across days without
supervision.

Deliberately dependency-free (no APScheduler): one loop, one sleep, easy to
reason about at 2am when something is wrong.
"""

from __future__ import annotations

import datetime as dt
import signal
import time
from typing import Any

from oaa.agents.orchestrator import Orchestrator
from oaa.core.logging import get_logger

__all__ = ["Runner"]

log = get_logger("runner")

_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class Runner:
    def __init__(self, orchestrator: Orchestrator, agent: Any = None) -> None:
        self.orch = orchestrator
        self.cfg = orchestrator.cfg
        self.schedule = self.cfg.schedule
        self.firewall = orchestrator.firewall
        self._stop = False
        self._fired: set[tuple[dt.date, str]] = set()
        self._last_monitor = 0.0
        self._session_date: dt.date | None = None

        # The AI assistant drives the cycles when it is available; the
        # deterministic path runs underneath it either way.
        self.agent = agent
        if self.agent is None and self.cfg.agents.enabled:
            try:
                from oaa.agents.trading_agent import TradingAgent

                candidate = TradingAgent(orchestrator)
                self.agent = candidate if candidate.available else None
            except Exception as exc:  # noqa: BLE001
                log.warning("could not start the trading agent (%s) - running rules-only", exc)
                self.agent = None

    #: Fallback when nothing is configured. The purely mechanical cycles -
    #: verification, the 15:15 liquidation, the 09:35 exit, reporting - are
    #: deliberately absent. There is nothing to reason about in them, and a
    #: language model in the path of a safety-critical liquidation is a failure
    #: mode dressed as a feature. It is also the single largest avoidable cost.
    DEFAULT_AGENT_CYCLES = ("overnight_signal", "overnight_entry")

    @property
    def agent_cycles(self) -> set[str]:
        configured = getattr(self.cfg.agents, "agent_cycles", None)
        if configured is None:
            return set(self.DEFAULT_AGENT_CYCLES)
        return set(configured)

    # -- signals ------------------------------------------------------------ #
    def install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: Any) -> None:
            log.info("signal %s received - finishing current cycle then stopping", signum)
            self._stop = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):  # not on the main thread
                pass

    # -- time --------------------------------------------------------------- #
    def _now(self) -> dt.datetime:
        # One clock for the whole system: the firewall's.
        return self.firewall.clock.now()

    def _is_trading_day(self, now: dt.datetime) -> bool:
        allowed = {_DAY_INDEX[d.lower()[:3]] for d in self.schedule.trading_days}
        return now.weekday() in allowed

    def _due(self, now: dt.datetime) -> list[Any]:
        """Cycles whose time has passed today and which have not yet fired.

        Late-firing is intentional: if the process restarts at 10:15 the 09:45
        scan still runs, rather than being silently skipped for the day.
        """
        due = []
        for cycle in self.schedule.cycles:
            key = (now.date(), cycle.name)
            if key in self._fired:
                continue
            hour, minute = (int(part) for part in cycle.at.split(":"))
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= scheduled:
                due.append(cycle)
        return due

    # -- main loop ----------------------------------------------------------- #
    def run(self, once: bool = False, poll_seconds: int = 20) -> None:
        self.install_signal_handlers()
        log.info(
            "runner started: %d cycles, timezone %s, monitor every %ds",
            len(self.schedule.cycles), self.schedule.timezone,
            self.schedule.monitor_interval_seconds,
        )
        self.orch.journal.event("runner_start", cycles=[c.name for c in self.schedule.cycles])

        while not self._stop:
            now = self._now()

            self._roll_session(now)

            if self._is_trading_day(now):
                for cycle in self._due(now):
                    log.info(
                        "--- cycle '%s' (%s) at %s ---",
                        cycle.name, cycle.action, self.firewall.clock.describe(now),
                    )
                    try:
                        self._fire(cycle)
                    except Exception as exc:  # noqa: BLE001 - a bad cycle must
                        # never kill the process; tomorrow's cycles still matter.
                        log.exception("cycle '%s' failed: %s", cycle.name, exc)
                        self.orch.journal.event("cycle_error", cycle=cycle.name, error=str(exc))
                    self._fired.add((now.date(), cycle.name))

                self._monitor()

            if once:
                break
            time.sleep(poll_seconds)

        log.info("runner stopped")
        self.orch.journal.event("runner_stop")

    def _fire(self, cycle: Any) -> None:
        """Run one cycle, through the assistant where that makes sense."""
        if self.agent is not None and cycle.action in self.agent_cycles:
            self.agent.run_cycle(cycle.action)
            return
        self.orch.run_cycle(cycle.action, cycle.name)

    def _roll_session(self, now: dt.datetime) -> None:
        """Reset the per-day firewall locks when the date turns over.

        Without this the intraday book stays locked out forever after the first
        15:15 cutoff, and the agent quietly stops trading on day two.
        """
        today = now.date()
        if self._session_date == today:
            return
        if self._session_date is not None:
            log.info("session rollover %s -> %s", self._session_date, today)
            self.firewall.reset_day(today)
        self._session_date = today

    def _monitor(self) -> None:
        interval = self.schedule.monitor_interval_seconds
        if interval <= 0 or time.monotonic() - self._last_monitor < interval:
            return
        self._last_monitor = time.monotonic()
        try:
            self.orch.journal.snapshot(self.orch.broker.account())
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot failed: %s", exc)
