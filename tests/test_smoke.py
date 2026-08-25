"""End-to-end smoke: the whole pipeline against the simulator, no network."""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.brokers.sim import SimBroker
from oaa.config.loader import load_settings
from oaa.core.types import MarketContext
from oaa.data.base import MarketDataProvider


class FakeData(MarketDataProvider):
    """Serves the synthetic chain fixture so the loop can run offline."""

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
    s.config.agents.llm.provider = None      # rules-only, no API key needed
    s.config.execution.dry_run = True
    s.config.telemetry.run_dir = str(tmp_path)
    s.config.telemetry.journal = str(tmp_path / "journal.jsonl")
    s.config.telemetry.db = str(tmp_path / "oaa.sqlite")
    s.config.telemetry.equity_curve = str(tmp_path / "equity.csv")
    s.config.agents.memory.path = str(tmp_path / "memory.sqlite")
    return s


def _intraday_orchestrator(settings, chain, bars, frozen_clock):
    """An orchestrator whose clock sits inside the intraday window.

    The firewall now gates every cycle on the ET session phase, so a smoke test
    that runs at an arbitrary wall-clock time would be blocked - correctly.
    """
    from oaa.agents.orchestrator import Orchestrator
    from oaa.firewall.lock import TemporalFirewall

    broker = SimBroker(settings.config)
    firewall = TemporalFirewall(settings.config)
    firewall.clock.freeze(frozen_clock("11:00"))
    orch = Orchestrator(
        settings, broker, FakeData(settings.config, chain, bars), firewall=firewall
    )
    return orch, broker


def test_full_cycle_runs_dry_without_network(settings, chain, bars, frozen_clock):
    for strategy in settings.config.strategies:
        strategy.params["universe"] = ["SPY"]

    orch, broker = _intraday_orchestrator(settings, chain, bars, frozen_clock)
    try:
        result = orch.run_cycle("scan_and_trade", "smoke")
        assert result.symbols_scanned == 1
        assert result.ideas_generated >= 1
        assert not result.errors
        # Dry run: decisions recorded, no cash moved.
        assert orch.journal.counts()["decisions"] >= 1
        assert broker.cash == broker.starting_cash
    finally:
        orch.close()


def test_the_intraday_book_takes_the_lock_before_trading(settings, chain, bars, frozen_clock):
    from oaa.firewall.lock import Book

    for strategy in settings.config.strategies:
        strategy.params["universe"] = ["SPY"]

    orch, _ = _intraday_orchestrator(settings, chain, bars, frozen_clock)
    try:
        assert orch.firewall.holder() is None
        orch.run_cycle("scan_and_trade", "smoke")
        assert orch.firewall.holder() is Book.INTRADAY
        assert orch.firewall.budget_for(Book.INTRADAY) > 0
    finally:
        orch.close()


def test_a_cycle_outside_its_window_is_blocked_not_executed(settings, chain, bars, frozen_clock):
    """The firewall refusing a cycle is the system working, not failing."""
    orch, _ = _intraday_orchestrator(settings, chain, bars, frozen_clock)
    try:
        orch.firewall.clock.freeze(frozen_clock("15:56"))   # overnight entry window
        result = orch.run_cycle("scan_and_trade", "smoke-blocked")
        assert result.orders_placed == 0
        assert result.firewall_passed is False
    finally:
        orch.close()


def test_the_full_daily_sequence_runs_end_to_end(settings, chain, bars, frozen_clock):
    """09:35 exit -> 10:00 intraday -> 15:15 cutoff -> 15:45/15:54/15:55 overnight."""
    from oaa.firewall.lock import Book

    for strategy in settings.config.strategies:
        strategy.params["universe"] = ["SPY"]

    orch, broker = _intraday_orchestrator(settings, chain, bars, frozen_clock)
    try:
        sequence = [
            ("09:35", "overnight_exit"),
            ("10:00", "scan_and_trade"),
            ("15:15", "intraday_cutoff"),
            ("15:45", "overnight_signal"),
            ("15:54", "overnight_verify"),
            ("15:55", "overnight_entry"),
            ("16:10", "report"),
        ]
        for time, action in sequence:
            orch.firewall.clock.freeze(frozen_clock(time))
            result = orch.run_cycle(action, action)
            assert not result.errors, f"{action}: {result.errors}"

        # After the cutoff the day book is locked out; the night book verified.
        assert orch.firewall.state.intraday_disabled_until is not None
        assert orch.firewall.holder() in (Book.OVERNIGHT, None)
        assert broker.cash == broker.starting_cash      # still a dry run
    finally:
        orch.close()


def test_status_reports_the_wiring(settings, chain, bars, frozen_clock):
    orch, _ = _intraday_orchestrator(settings, chain, bars, frozen_clock)
    try:
        status = orch.status()
        assert status["profile"] == "dev"
        assert status["dry_run"] is True
        assert "vol_carry_condor" in status["strategies"]["intraday"]
        assert "overnight_pairs" in status["strategies"]["overnight"]
        assert status["firewall"]["enabled"] is True
        assert status["regt_buying_power"] is not None
    finally:
        orch.close()


def test_manage_cycle_closes_a_position_at_its_profit_target(settings, chain, bars, frozen_clock):
    from oaa.agents.orchestrator import Orchestrator
    from oaa.core.types import PositionSnapshot
    from oaa.firewall.lock import TemporalFirewall

    settings.config.execution.dry_run = False
    broker = SimBroker(settings.config)
    broker._positions["SPY260918C00500000"] = PositionSnapshot(
        symbol="SPY260918C00500000", qty=1, avg_entry_price=5.0,
        market_value=800.0, unrealized_pl=300.0, unrealized_plpc=0.60,
        underlying="SPY", expiry=dt.date(2026, 9, 18),
    )
    firewall = TemporalFirewall(settings.config)
    firewall.clock.freeze(frozen_clock("13:00"))
    orch = Orchestrator(
        settings, broker, FakeData(settings.config, chain, bars), firewall=firewall
    )
    try:
        result = orch.run_cycle("manage_positions", "smoke-manage")
        assert result.positions_closed == 1
    finally:
        orch.close()
