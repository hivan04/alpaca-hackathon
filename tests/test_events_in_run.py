"""The events book, driven by `oaa run` rather than by a second process.

Until 30 Aug this book only traded if a human remembered to start
`oaa events arm` before the close. That is not an autonomous submission - it is
a manual trade with extra steps - so the two cycles now live in the schedule.

What these tests pin is the awkward part of that move. The events book is the
one book here that is NOT a firewall tenant: it arms at 15:50, after the 15:15
transient cutoff, and holds one night. No firewall phase permits that, and
bending the firewall to admit it would weaken the interlock that keeps the day
books out of the carry book's margin. So the cycles build their own
`RiskEngine(firewall=None)` instead, and the tests below prove that this
bypass does not leak into the books that DO hold a lease.
"""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.brokers.sim import SimBroker
from oaa.config.loader import load_settings
from oaa.core.types import MarketContext
from oaa.data.base import MarketDataProvider

EVENTS_STRATEGY = "earnings_event_directional"


class FakeData(MarketDataProvider):
    name = "fake"

    def __init__(self, cfg, chain, bars):
        super().__init__(cfg)
        self._chain = chain
        self._bars = bars

    def spot(self, symbol): return 500.0

    def bars(self, symbol, lookback_days=90, timeframe="1Day"): return self._bars

    def option_chain(self, symbol, **kwargs): return self._chain

    def context(self, symbol, lookback_days=90):
        return MarketContext(
            symbol=symbol,
            asof=dt.datetime(2026, 9, 1, 15, 0, tzinfo=dt.timezone.utc),
            spot=500.0,
            bars=self._bars,
            chain=self._chain,
            realised_vol=0.14,
            implied_vol=0.20,
            iv_rank=0.70,
            trend_strength=0.10,
            adx=15.0,
        )


@pytest.fixture
def settings(tmp_path):
    s = load_settings(profile="dev")
    s.config.universe.symbols = ["SPY"]
    s.config.agents.llm.provider = None
    s.config.execution.dry_run = True
    s.config.telemetry.run_dir = str(tmp_path)
    s.config.telemetry.journal = str(tmp_path / "journal.jsonl")
    s.config.telemetry.db = str(tmp_path / "oaa.sqlite")
    s.config.telemetry.equity_curve = str(tmp_path / "equity.csv")
    s.config.agents.memory.path = str(tmp_path / "memory.sqlite")
    s.config.firewall.ledger_path = str(tmp_path / "ledger.json")
    s.config.management.entry_cutoff_utc = None
    for ref in s.config.strategies:
        ref.params.setdefault("exits", {})["entry_cutoff_utc"] = None
    return s


def _orchestrator(settings, chain, bars, frozen_clock, at="15:50"):
    from oaa.agents.orchestrator import Orchestrator
    from oaa.firewall.lock import TemporalFirewall

    broker = SimBroker(settings.config)
    firewall = TemporalFirewall(settings.config)
    firewall.clock.freeze(frozen_clock(at))
    orch = Orchestrator(
        settings, broker, FakeData(settings.config, chain, bars), firewall=firewall
    )
    return orch, broker


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_the_shipped_schedule_arms_and_flattens_the_events_book():
    """A book wired into the orchestrator but absent from the schedule would
    never fire - which is the failure this whole change exists to remove."""
    cfg = load_settings(profile="dev").config
    by_action = {c.action: c for c in cfg.schedule.cycles}

    assert "events_arm" in by_action, "no cycle arms the events book"
    assert "events_flatten" in by_action, "no cycle closes the events book"

    arm, flat = by_action["events_arm"], by_action["events_flatten"]
    # The arm must sit after the 15:15 transient cutoff (that is the whole
    # reason it bypasses the firewall) and before the 16:00 close.
    assert "15:15" < arm.at < "16:00", f"events arms at {arm.at}"
    # The flatten must beat the day books to the account: it frees buying power
    # they are about to compete for, and 09:45 is where the IV crush is.
    assert flat.at <= "10:00", f"events flattens at {flat.at}"
    assert flat.at < arm.at


def test_the_events_book_is_configured_and_registered():
    cfg = load_settings(profile="dev").config
    refs = [r for r in cfg.strategies if r.book == "events"]
    assert len(refs) == 1, "exactly one strategy should own the events book"
    assert refs[0].name == EVENTS_STRATEGY
    assert refs[0].enabled
    assert refs[0].params_file, "the events book needs its own params file"


def test_the_events_book_is_not_a_firewall_tenant(settings, chain, bars, frozen_clock):
    """It must not appear in any of the three lists the scan cycles iterate.

    If it did, `_transient_scan` would try to open it inside an intraday lease
    and the 15:15 cutoff would liquidate it the same afternoon.
    """
    orch, _ = _orchestrator(settings, chain, bars, frozen_clock)
    try:
        for book in (orch.carry, orch.intraday, orch.opportunistic):
            assert EVENTS_STRATEGY not in {s.name for s in book}
        # It is still BUILT, so `manage_positions` can route an open events
        # position back to its own exit rules rather than the global ones.
        assert EVENTS_STRATEGY in {s.name for s in orch.strategies}
    finally:
        orch.close()


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def test_both_cycles_dispatch_and_do_not_fall_through(settings, chain, bars, frozen_clock):
    """`run_cycle` raises on an unknown action, so a missing handler is loud.

    The arm is driven through a stub engine: the real one calls Featherless,
    and a test that needed a network key would be skipped exactly when it
    mattered.
    """
    orch, _ = _orchestrator(settings, chain, bars, frozen_clock)
    try:
        armed: dict[str, object] = {}

        class StubReport:
            asof = dt.date(2026, 9, 1)
            screened = ["AVGO", "MDB"]
            unverified = ["CPRT"]
            considered = ["AVGO"]
            calls = []
            opened = []
            declined = {"AVGO": "model abstained"}
            budget = 5_000.0
            budget_used = 0.0
            errors: list[str] = []
            abstention_rate = 1.0

            def summary(self): return "1 event considered, 0 opened"

        class StubEngine:
            def arm(self, asof=None, dry_run=None):
                armed["asof"] = asof
                return StubReport()

            def flatten(self, asof=None):
                return ["AVGO260904C00450000"]

        orch._events_engine_cache = StubEngine()

        result = orch.run_cycle("events_arm", "test")
        assert not result.errors
        assert result.ideas_generated == 1
        assert armed["asof"] is not None
        # An unconfirmed name must be visible on the cycle, not only in a log:
        # arming against a print that never happens is the expensive mistake.
        assert any("CPRT" in note for note in result.notes)

        closed = orch.run_cycle("events_flatten", "test")
        assert closed.positions_closed == 1
        assert not closed.errors
    finally:
        orch.close()


def test_a_model_that_never_abstains_is_called_out(settings, chain, bars, frozen_clock):
    """A judge that declines nothing is not judging. This repo shipped that
    bug once already, in the critic."""
    orch, _ = _orchestrator(settings, chain, bars, frozen_clock)
    try:
        class Call:
            actionable = True

        class StubReport:
            asof = dt.date(2026, 9, 1)
            screened, unverified, considered = ["AVGO"], [], ["AVGO"]
            calls = [Call(), Call()]
            opened, declined, errors = [], {}, []
            budget = budget_used = 0.0
            abstention_rate = 0.0

            def summary(self): return "2 considered, 0 opened"

        orch._events_engine_cache = type("E", (), {"arm": lambda self, asof=None: StubReport()})()
        result = orch.run_cycle("events_arm", "test")
        assert any("abstention rate 0%" in note for note in result.notes)
    finally:
        orch.close()


# --------------------------------------------------------------------------- #
# the switchboard
# --------------------------------------------------------------------------- #
def test_the_toggle_stands_the_arm_down_but_never_the_flatten(
    settings, chain, bars, frozen_clock
):
    """Switching a book off must stop it OPENING, never abandon open risk."""
    orch, _ = _orchestrator(settings, chain, bars, frozen_clock)
    try:
        flattened: list[bool] = []

        class StubEngine:
            def arm(self, asof=None, dry_run=None):
                raise AssertionError("a switched-off book must not arm")

            def flatten(self, asof=None):
                flattened.append(True)
                return []

        orch._events_engine_cache = StubEngine()
        orch.switchboard.set(EVENTS_STRATEGY, False, actor="test")

        result = orch.run_cycle("events_arm", "test")
        assert result.orders_placed == 0
        assert any("switched off" in note for note in result.notes)

        orch.run_cycle("events_flatten", "test")
        assert flattened == [True], "the flatten must run whatever the switch says"
    finally:
        orch.close()


def test_a_bad_night_is_a_logged_cycle_not_a_dead_process(settings, chain, bars, frozen_clock):
    orch, _ = _orchestrator(settings, chain, bars, frozen_clock)
    try:
        class Exploding:
            def arm(self, asof=None, dry_run=None):
                raise RuntimeError("Featherless timed out")

            def flatten(self, asof=None):
                raise RuntimeError("broker unreachable")

        orch._events_engine_cache = Exploding()
        armed = orch.run_cycle("events_arm", "test")
        assert armed.errors and "Featherless timed out" in armed.errors[0]
        closed = orch.run_cycle("events_flatten", "test")
        assert closed.errors and "broker unreachable" in closed.errors[0]
    finally:
        orch.close()


def test_the_watch_cycle_dispatches_and_runs_whatever_the_switch_says(
    settings, chain, bars, frozen_clock
):
    """The watch opens nothing, so standing the ARM down must not also blind
    the book: losing the run-up as a side effect of a risk decision would be an
    accident, and by arm day it is unrecoverable."""
    orch, _ = _orchestrator(settings, chain, bars, frozen_clock, at="13:00")
    try:
        polled: list[object] = []

        class StubReport:
            watching = ["NVDA", "CRM"]
            polled = ["NVDA", "CRM"]
            new_items = {"NVDA": 3, "CRM": 0}
            noted = ["NVDA"]
            quiet = ["CRM"]
            retired = ["OKTA"]
            errors: list[str] = []

            def summary(self): return "2 watched, 3 new, 1 noted, 1 quiet, 1 retired"

        class StubEngine:
            def watch(self, asof=None):
                polled.append(asof)
                return StubReport()

        orch._events_engine_cache = StubEngine()
        orch.switchboard.set(EVENTS_STRATEGY, False, actor="test")

        result = orch.run_cycle("events_watch", "test")
        assert not result.errors
        assert result.symbols_scanned == 2
        assert polled, "the watch must run even with the book switched off"
        assert any("OKTA" in note for note in result.notes), (
            "a name that has reported must be visibly retired"
        )
    finally:
        orch.close()


def test_the_watch_runs_hourly_and_covers_the_arm():
    """One poll a day would be a snapshot with extra steps.

    Until 30 Aug this pinned three reads inside the session and asserted that
    none landed before the open. That guard was inverted deliberately: the
    items this book cares about - estimate revisions, guidance, a supplier's
    read-across - land on the overnight and pre-market wire, and a watch that
    only opened its eyes at 09:55 read them after they were priced. The watch
    is now an hourly grid from 04:00 ET.

    What still has to hold, and is what this test defends:
      * the grid has no hole wider than an hour, or "hourly" is a comment;
      * something reads the pre-market, which is the point of the change;
      * a read lands in the hour before the arm, so the arm sees the day it is
        arming into rather than this morning's picture.
    """
    cfg = load_settings(profile="dev").config
    watches = sorted(c.at for c in cfg.schedule.cycles if c.action == "events_watch")
    arm = next(c for c in cfg.schedule.cycles if c.action == "events_arm")

    def minutes(at: str) -> int:
        hh, mm = at.split(":")
        return int(hh) * 60 + int(mm)

    assert len(watches) >= 8, "the watch has to see more than a few moments in the day"
    assert minutes(watches[0]) < 9 * 60 + 30, (
        "at least one read must land before the open - the overnight wire is "
        "the half of the day the old three-cycle schedule never saw"
    )
    gaps = [minutes(b) - minutes(a) for a, b in zip(watches, watches[1:])]
    assert max(gaps) <= 60, f"a hole of {max(gaps)} minutes is not an hourly watch"
    before_arm = [w for w in watches if minutes(w) <= minutes(arm.at)]
    assert before_arm and minutes(arm.at) - minutes(before_arm[-1]) <= 60, (
        "a watch must land within the hour before the arm, so the arm judges "
        "the session it is arming into"
    )


def test_the_engine_is_built_with_the_firewall_bypassed(settings, chain, bars, frozen_clock):
    """The bypass is the design. It is also the thing most likely to be
    'tidied up' later by someone who reads the firewall docs and not this one,
    so it is pinned."""
    orch, _ = _orchestrator(settings, chain, bars, frozen_clock)
    try:
        engine = orch._events_engine()
        assert engine.risk.firewall is None
        # Everything else is shared with the rest of the loop, so an events
        # order still lands in the journal the judges read.
        assert engine.journal is orch.journal
        assert engine.router is orch.executor
        assert orch._events_engine() is engine, "the engine should be cached"
    finally:
        orch.close()
