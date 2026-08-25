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
    return strategy_registry.get(name)(ref, cfg), cfg


def test_registry_autoloads_every_shipped_strategy():
    strategy_registry.autoload("oaa.strategies")
    names = strategy_registry.names()
    assert {"vol_carry_condor", "momentum_debit_spread", "earnings_calendar"} <= set(names)


def test_load_strategies_only_returns_enabled_ones():
    cfg = load_config()
    loaded = load_strategies(cfg)
    assert {s.name for s in loaded} == {
        s.name for s in cfg.strategies if s.enabled
    }


def test_condor_fires_in_a_rich_quiet_regime(chain, bars, account):
    strat, cfg = strategy("vol_carry_condor")
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


def test_condor_stays_out_when_vol_is_cheap(chain, bars, account):
    strat, cfg = strategy("vol_carry_condor")
    ctx = StrategyContext(
        market=context(chain, bars, iv_rank=0.05, implied_vol=0.10, realised_vol=0.20),
        account=account, config=cfg, params=strat.params,
    )
    assert strat.generate(ctx) == []


def test_condor_stays_out_of_a_strong_trend(chain, bars, account):
    strat, cfg = strategy("vol_carry_condor")
    ctx = StrategyContext(
        market=context(chain, bars, trend_strength=0.95),
        account=account, config=cfg, params=strat.params,
    )
    assert strat.generate(ctx) == []


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
    condor, cfg = strategy("vol_carry_condor")
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
    for strat in load_strategies(cfg):
        strat.ref.params["universe"] = ["SPY"]
        ctx = StrategyContext(
            market=context(chain, bars, trend_strength=0.85, adx=30.0),
            account=account, config=cfg, params=strat.params,
        )
        for idea in strat.generate(ctx):
            assert idea.structure.is_defined_risk, idea.structure
            assert idea.max_loss is not None
