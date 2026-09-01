"""The always-on scheduler.

`oaa run` starts this and walks away. It wakes itself on the cycle times in
config, monitors positions in between, and keeps going across days without
supervision.

Deliberately dependency-free (no APScheduler): one loop, one sleep.
"""

from __future__ import annotations

import datetime as dt
import signal
import time
from typing import Any

from oaa.agents.orchestrator import Orchestrator
from oaa.core.logging import get_logger, tape

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
        self._last_heartbeat = 0.0
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
    #: the 15:45 verification, the 15:15 liquidation, the submission flatten,
    #: reporting - are deliberately absent. There is nothing to reason about in
    #: them, and a language model in the path of a safety-critical liquidation
    #: is a failure mode dressed as a feature. It is also the single largest
    #: avoidable token cost.
    DEFAULT_AGENT_CYCLES = ("carry_scan",)

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
        # TAPE, not INFO. `telemetry.console: focused` drops INFO, and on a
        # non-trading day there are no cycles and no other tape lines - so at
        # INFO a perfectly healthy agent presents as a blank terminal that is
        # indistinguishable from a hang. That cost an evening on 30 Aug.
        tape().info(
            "READY %d cycles, timezone %s, monitor every %ds - waiting for the "
            "next scheduled cycle",
            len(self.schedule.cycles), self.schedule.timezone,
            self.schedule.monitor_interval_seconds,
        )
        self.orch.journal.event("runner_start", cycles=[c.name for c in self.schedule.cycles])
        self._warn_if_reasoning_is_missing()

        while not self._stop:
            now = self._now()

            self._roll_session(now)

            # The dated submission flatten is checked on every poll, not on the
            # daily schedule: it is a one-off wall-clock deadline, and "remember
            # to trigger it on the day" is exactly the plan that fails.
            self._check_submission_flatten(now)

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

            self._heartbeat(now)
            if once:
                break
            time.sleep(poll_seconds)

        log.info("runner stopped")
        self.orch.journal.event("runner_stop")

    def _warn_if_reasoning_is_missing(self) -> None:
        """Say at START-UP whether the agent can actually reason.

        Waiting until the first agent cycle means the answer arrives at 10:00
        ET, buried in a log nobody is watching yet. The question - "is this
        about to run the whole week on rules?" - is answerable the moment the
        process boots, so it is answered there.
        """
        if self.agent is None:
            return
        try:
            available = self.agent.available
            provider = getattr(self.orch.llm, "provider", "?")
        except Exception as exc:  # noqa: BLE001 - a check must not be the outage
            log.warning("could not determine the reasoning layer's state: %s", exc)
            return
        if available:
            tape().info("READY reasoning layer: %s, tool loop available", provider)
            return
        log.warning(
            "NO REASONING LAYER AT START-UP - provider '%s' cannot drive a tool "
            "loop, so every agent cycle this session will fall back to "
            "deterministic rules. Fix this before the open.", provider,
        )
        self.orch.journal.event("agent_degraded", cycle="startup", reason=str(provider))

    #: Cycle actions that open RESIDENT positions and therefore need the carry
    #: book's capital allocated before the agent can submit anything.
    _CARRY_ACTIONS = frozenset({"carry_scan"})

    def _fire(self, cycle: Any) -> None:
        """Run one cycle, through the assistant where that makes sense."""
        if self.agent is not None and cycle.action in self.agent_cycles:
            # Allocate the carry book's capital FIRST. `Orchestrator.carry_scan`
            # calls `firewall.allocate_carry` before it generates anything, and
            # it is the only caller in the codebase - but `agent_cycles`
            # defaults to ["carry_scan"], so on this profile the agent path
            # replaces that method entirely and the allocation never runs.
            # `state.carry_reserved` therefore stays 0 and `may_open` refuses
            # every resident entry with "carry book has no reserved capital",
            # which is what happened to a fully-priced DIA condor - all four
            # gates passed, $546 of risk against a $1,000 cap - on 1 Sep.
            #
            # This is not a bypass of the firewall. It is the same capacity
            # check the deterministic path runs, and it can still refuse: if
            # the book is at its equity ceiling the verdict fails and the
            # agent does not run.
            if cycle.action in self._CARRY_ACTIONS:
                verdict = self.firewall.allocate_carry(self.orch.broker)
                if not verdict.passed:
                    log.warning(
                        "carry allocation refused - skipping the agent cycle: %s",
                        verdict.summary(),
                    )
                    return
            self.agent.run_cycle(cycle.action)
            return
        self.orch.run_cycle(cycle.action, cycle.name)

    def _check_submission_flatten(self, now: dt.datetime) -> None:
        """Fire the whole-book flatten once, at the configured UTC moment.

        Realised P&L on a flat account is unambiguous evidence; an open book at
        judging asks a judge to trust a mid-price mark on a wide quote. Firing
        early also leaves time to fix a failed close.
        """
        if self.firewall.state.flattened_for_submission:
            return
        from oaa.signals.gates import parse_utc

        deadline = parse_utc(getattr(self.cfg.management, "submission_flatten_utc", None))
        if deadline is None:
            return
        moment = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
        if moment.astimezone(dt.timezone.utc) < deadline:
            return
        log.warning("SUBMISSION FLATTEN due at %s - closing the entire book", deadline)
        try:
            self.orch.run_cycle("submission_flatten", "submission_flatten")
        except Exception as exc:  # noqa: BLE001
            log.exception("submission flatten failed: %s", exc)
            self.orch.journal.event("cycle_error", cycle="submission_flatten", error=str(exc))

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

    def _heartbeat(self, now: dt.datetime) -> None:
        """Say something every half hour, even when there is nothing to say.

        A scheduler is mostly idle by design, and a focused console shows only
        events. The two states "waiting correctly" and "wedged" then look
        identical on screen, which is precisely the confusion `oaa status`
        exists to resolve - but only if you think to run it. A dated line every
        30 minutes makes the healthy state visible without making it noisy.
        """
        if time.monotonic() - self._last_heartbeat < 1800:
            return
        self._last_heartbeat = time.monotonic()
        pending = [c.name for c in self.schedule.cycles if (now.date(), c.name) not in self._fired]
        tape().info(
            "ALIVE %s - %d cycle(s) still to fire today%s",
            self.firewall.clock.describe(now), len(pending),
            f", next: {pending[0]}" if pending else "",
        )

    def _monitor(self) -> None:
        interval = self.schedule.monitor_interval_seconds
        if interval <= 0 or time.monotonic() - self._last_monitor < interval:
            return
        self._last_monitor = time.monotonic()
        try:
            self.orch.journal.snapshot(self.orch.broker.account())
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot failed: %s", exc)
