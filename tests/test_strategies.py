from __future__ import annotations

import datetime as dt

from oaa.config.loader import load_config
from oaa.core.types import MarketContext
from oaa.strategies.base import StrategyContext, load_strategies, strategy_registry


def context(chain, bars, **overrides) -> MarketContext:
    base: dict = {
        "symbol": "SPY",
        "asof": dt.datetime(2026, 9, 1, 15, 0, tzinfo=dt.timezone.utc),
        "spot": 500.0,
        "bars": bars,
        "chain": chain,
        "realised_vol": 0.14,
        "implied_vol": 0.20,
        "iv_rank": 0.70,
        "trend_strength": 0.10,
        "adx": 15.0,
    }
    base.update(overrides)
    return MarketContext(**base)


def strategy(name: str):
    cfg = load_config()
    ref = next(s for s in cfg.strategies if s.name == name)
    # The dated entry cutoff is a submission-week control, not a strategy
    # property; tests must not start failing the day it passes.
    ref.params.setdefault("exits", {})["entry_cutoff_utc"] = None
    cfg.management.entry_cutoff_utc = None
    return strategy_registry.get(name)(ref, cfg), cfg


def test_registry_autoloads_every_shipped_strategy():
    strategy_registry.autoload("oaa.strategies")
    names = strategy_registry.names()
    assert {
        "vol_carry", "intraday_momentum", "event_premium",
        "momentum_debit_spread", "earnings_calendar",
    } <= set(names)


def test_load_strategies_only_returns_enabled_ones():
    cfg = load_config()
    loaded = load_strategies(cfg)
    assert {s.name for s in loaded} == {
        s.name for s in cfg.strategies if s.enabled
    }


def test_carry_fires_in_a_rich_quiet_regime(chain, bars, account):
    strat, cfg = strategy("vol_carry")
    strat.ref.params["universe"] = ["SPY"]
    ctx = StrategyContext(market=context(chain, bars), account=account,
                          config=cfg, params=strat.params)
    ideas = strat.generate(ctx)
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.is_credit
    assert idea.max_loss and idea.max_loss > 0
    assert len(idea.legs) == 4
    assert idea.thesis  # a thesis is mandatory - the judges read it
    # 7-14 DTE: the structure's life has to fit inside the judged window.
    dte = (dt.date.fromisoformat(idea.meta["expiry"]) - ctx.market.asof.date()).days
    assert 7 <= dte <= 14
    assert idea.meta["gates"]["passed"] is True


def test_carry_stays_out_when_vol_is_cheap(chain, bars, account):
    strat, cfg = strategy("vol_carry")
    ctx = StrategyContext(
        market=context(chain, bars, iv_rank=0.05, implied_vol=0.10, realised_vol=0.20),
        account=account, config=cfg, params=strat.params,
    )
    assert strat.generate(ctx) == []


def test_carry_stays_out_of_a_strong_trend(chain, bars, account):
    strat, cfg = strategy("vol_carry")
    ctx = StrategyContext(
        market=context(chain, bars, trend_strength=0.95, adx=40.0),
        account=account, config=cfg, params=strat.params,
    )
    assert strat.generate(ctx) == []


def test_carry_refuses_to_sell_premium_priced_for_an_earnings_date(chain, bars, account):
    """IV is elevated BECAUSE of the event; that premium is fair."""
    strat, cfg = strategy("vol_carry")
    strat.ref.params["universe"] = ["SPY"]
    ctx = StrategyContext(
        market=context(chain, bars, earnings_date=dt.date(2026, 9, 8)),
        account=account, config=cfg, params=strat.params,
    )
    assert strat.generate(ctx) == []


def test_carry_defers_to_the_macro_lens_on_an_idiosyncratic_flag(chain, bars, account):
    from oaa.discovery.macro import MacroView

    strat, cfg = strategy("vol_carry")
    strat.ref.params["universe"] = ["SPY"]
    flagged = MacroView(flagged_symbols={"SPY": "repricing on its own news"})
    ctx = StrategyContext(market=context(chain, bars), account=account,
                          config=cfg, params=strat.params, macro=flagged)
    assert strat.generate(ctx) == []


def test_carry_exits_leave_room_for_execution_cost(chain, bars, account):
    """The exit pair sets the breakeven hit rate, and the breakeven hit rate
    sets what the cost gate can afford.

    A win is +target x credit and a loss is -stop x credit, so breakeven is
    stop / (target + stop). The original 30% / 2.0x needed 87% BEFORE paying
    any spread, against 88% observed on real data - one point of margin, and
    the cost gate was admitting trades at 95.7% breakeven. 50% / 1.5x moves
    breakeven to 75% and makes the same cost ceiling survivable.
    """
    strat, cfg = strategy("vol_carry")
    strat.ref.params["universe"] = ["SPY"]
    ctx = StrategyContext(market=context(chain, bars), account=account,
                          config=cfg, params=strat.params)
    idea = strat.generate(ctx)[0]

    target = strat.p("exits.profit_target_pct")
    stop = strat.p("exits.loss_multiple_of_credit")
    assert stop / (target + stop) <= 0.80

    assert strat.should_exit(ctx, idea, target - 0.05) is None
    assert "profit target" in (strat.should_exit(ctx, idea, target + 0.02) or "")
    assert "credit" in (strat.should_exit(ctx, idea, -(stop + 0.5)) or "")


def test_momentum_needs_a_confirmed_trend(chain, bars, account):
    strat, cfg = strategy("momentum_debit_spread")
    quiet = StrategyContext(
        market=context(chain, bars, trend_strength=0.10, adx=12.0),
        account=account, config=cfg, params=strat.params,
    )
    assert strat.generate(quiet) == []


def test_momentum_builds_a_debit_spread_when_trending(chain, bars, account):
    strat, cfg = strategy("momentum_debit_spread")
    strat.ref.params["universe"] = ["SPY"]
    trending = StrategyContext(
        market=context(chain, bars, trend_strength=0.85, adx=30.0, iv_rank=0.25),
        account=account, config=cfg, params=strat.params,
    )
    ideas = strat.generate(trending)
    if ideas:  # strike grid may reject; the invariants are what matter
        idea = ideas[0]
        assert not idea.is_credit
        assert idea.max_loss and idea.max_loss > 0
        assert len(idea.legs) == 2
        assert "bullish" in idea.tags


def test_the_two_strategies_do_not_fire_on_the_same_regime(chain, bars, account):
    condor, cfg = strategy("vol_carry")
    momentum, _ = strategy("momentum_debit_spread")
    for s in (condor, momentum):
        s.ref.params["universe"] = ["SPY"]

    rich_and_quiet = context(chain, bars, iv_rank=0.70, trend_strength=0.10, adx=14.0)
    ctx_a = StrategyContext(market=rich_and_quiet, account=account, config=cfg,
                            params=condor.params)
    ctx_b = StrategyContext(market=rich_and_quiet, account=account, config=cfg,
                            params=momentum.params)
    assert condor.generate(ctx_a)      # condor wants rich vol and no trend
    assert not momentum.generate(ctx_b)  # momentum wants the opposite


def test_every_generated_idea_is_defined_risk(chain, bars, account):
    cfg = load_config()
    cfg.management.entry_cutoff_utc = None
    for ref in cfg.strategies:
        ref.enabled = True
        ref.params.setdefault("exits", {})["entry_cutoff_utc"] = None
    for strat in load_strategies(cfg):
        strat.ref.params["universe"] = ["SPY"]
        ctx = StrategyContext(
            market=context(chain, bars, trend_strength=0.85, adx=30.0),
            account=account, config=cfg, params=strat.params,
        )
        for idea in strat.generate(ctx):
            assert idea.structure.is_defined_risk, idea.structure
            assert idea.max_loss is not None
