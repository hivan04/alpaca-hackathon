"""The opportunistic book, and the calendar that wakes it.

The most likely correct behaviour of this module inside a seven-day window is
to do nothing at all. These tests assert that standing down is a first-class
outcome, not a failure.
"""

from __future__ import annotations

import datetime as dt

from oaa.config.loader import load_config
from oaa.core.types import MarketContext
from oaa.signals.catalyst import CatalystEngine, MacroCalendar, MacroEvent
from oaa.strategies.base import StrategyContext, strategy_registry


def build():
    strategy_registry.autoload("oaa.strategies")
    cfg = load_config()
    ref = next(s for s in cfg.strategies if s.name == "event_premium")
    ref.enabled = True
    return strategy_registry.get("event_premium")(ref, cfg), cfg


def market(chain, **overrides) -> MarketContext:
    base: dict = {
        "symbol": "SPY",
        "asof": dt.datetime(2026, 9, 1, 15, 0, tzinfo=dt.timezone.utc),
        "spot": 500.0,
        "chain": chain,
        "iv_rank": 0.6,
        "implied_vol": 0.22,
        "realised_vol": 0.15,
    }
    base.update(overrides)
    return MarketContext(**base)


def engine_with(event: MacroEvent | None) -> CatalystEngine:
    return CatalystEngine(calendar=MacroCalendar(events=[event] if event else []))


def context(strat, cfg, market_ctx, catalyst, account):
    return StrategyContext(
        market=market_ctx, account=account, config=cfg,
        params=strat.params, catalyst=catalyst,
    )


# --------------------------------------------------------------------------- #
# the calendar
# --------------------------------------------------------------------------- #
def test_the_shipped_calendar_parses():
    """A committed file, deliberately: a live calendar feed that fails on the
    morning of a print fails at exactly the moment it was needed."""
    calendar = MacroCalendar.load("config/macro_events.yaml")
    assert calendar.events
    assert all(e.when.tzinfo is not None for e in calendar.events)
    assert {"cpi", "fomc", "nfp", "pmi"} & {e.kind for e in calendar.events}


def test_a_missing_calendar_is_inert_not_fatal():
    assert MacroCalendar.load("config/does_not_exist.yaml").events == []


def test_events_are_found_inside_a_window():
    now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)
    calendar = MacroCalendar.load("config/macro_events.yaml")
    assert any(e.kind == "nfp" for e in calendar.within(now, minutes_before=60))


# --------------------------------------------------------------------------- #
# the book
# --------------------------------------------------------------------------- #
def test_it_stands_down_when_nothing_is_scheduled(chain, account):
    """The expected outcome most of the time, and reported as such."""
    strat, cfg = build()
    ctx = context(strat, cfg, market(chain), engine_with(None), account)
    assert strat.generate(ctx) == []


def test_it_stands_down_when_the_premium_is_in_line(chain, account):
    """Implied in line with the historical realised move means there is nothing
    to sell. Selling anyway is paying the spread to take a coin flip."""
    strat, cfg = build()
    soon = MacroEvent(
        name="US CPI", kind="cpi",
        when=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12),
    )
    # Push the historical distribution above whatever the chain implies.
    strat.ref.params["event"]["realised_moves"]["cpi"] = [0.05] * 8
    ctx = context(strat, cfg, market(chain), engine_with(soon), account)
    assert strat.generate(ctx) == []


def test_it_refuses_without_enough_history_to_claim_a_mispricing(chain, account):
    strat, cfg = build()
    soon = MacroEvent(
        name="US CPI", kind="cpi",
        when=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12),
    )
    strat.ref.params["event"]["realised_moves"]["cpi"] = [0.001, 0.002]
    ctx = context(strat, cfg, market(chain), engine_with(soon), account)
    assert strat.generate(ctx) == []


def test_it_sells_a_defined_risk_structure_when_the_premium_is_rich(chain, account):
    strat, cfg = build()
    strat.ref.params["structures"]["target_dte"] = [7, 14]
    strat.ref.params["event"]["realised_moves"]["cpi"] = [0.001] * 8
    soon = MacroEvent(
        name="US CPI", kind="cpi",
        when=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12),
    )
    ctx = context(strat, cfg, market(chain), engine_with(soon), account)
    ideas = strat.generate(ctx)
    assert ideas
    idea = ideas[0]
    assert idea.is_credit
    assert idea.structure.is_defined_risk
    assert idea.max_loss and idea.max_loss > 0
    assert idea.book == "opportunistic"
    assert "US CPI" in idea.thesis


def test_the_macro_lens_can_veto_it(chain, account):
    from oaa.discovery.macro import MacroView

    strat, cfg = build()
    strat.ref.params["event"]["realised_moves"]["cpi"] = [0.001] * 8
    soon = MacroEvent(
        name="US CPI", kind="cpi",
        when=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12),
    )
    ctx = context(strat, cfg, market(chain), engine_with(soon), account)
    ctx.macro = MacroView(guidance={"event_premium": "reduce"})
    assert strat.generate(ctx) == []


def test_it_is_index_only():
    """Single-name spreads are too wide to pay for a short hold."""
    strat, _ = build()
    assert set(strat.universe()) <= {"SPY", "QQQ", "XSP"}
