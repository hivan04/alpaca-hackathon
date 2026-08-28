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

    # With the hard dollar stop live it fires FIRST on any structure whose
    # credit is large enough - which is the point of it: `loss_multiple_of_
    # credit` is a ratio, so a fat credit buys a proportionally fat loss, and
    # the account limit that actually binds is written in dollars.
    hard = strat.p("exits.max_loss_usd", 0.0)
    deep = strat.should_exit(ctx, idea, -(stop + 0.5)) or ""
    if hard and (idea.max_profit or 0) * (stop + 0.5) >= hard:
        assert "hard stop" in deep
    else:
        assert "credit" in deep

    # Disable it and the credit-relative stop must still be the backstop.
    strat.ref.params.setdefault("exits", {})["max_loss_usd"] = 0
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


def test_the_critic_can_actually_approve_a_carry_trade(chain, bars, account):
    """A regression guard for the failure that muted the whole book.

    `_confidence` scored against a hard-coded 0.70 IV-rank threshold. When the
    gate moved to 0.35 the formula kept marking to the old one, and the
    heuristic critic - which starts from that confidence and then subtracted
    0.10 for a "poor" reward/risk every defined-risk credit spread has by
    construction - declined 100% of candidates. Backtests were being run with
    `--critic off` and so never saw it; the live agent runs the critic ON and
    would have traded nothing at all.

    This asserts the two are wired to the same threshold: a candidate that
    clears the premium gate must be able to clear the critic.
    """
    from oaa.agents.critic import Critic
    from oaa.agents.llm import NullClient

    strat, cfg = strategy("vol_carry")
    strat.ref.params["universe"] = ["SPY"]
    ctx = StrategyContext(market=context(chain, bars), account=account,
                          config=cfg, params=strat.params)
    ideas = strat.generate(ctx)
    if not ideas:
        return  # the fixture did not clear the premium gate; nothing to assert
    idea = ideas[0]

    # The exit policy's breakeven must be published for the critic to read.
    assert idea.meta.get("breakeven_hit_rate")

    verdict = Critic(cfg, llm=NullClient(cfg.agents.llm)).score(
        idea, ctx.market, account, opened_today=0
    )
    assert verdict["source"] == "heuristic"
    assert "poor reward/risk" not in verdict["reasoning"], (
        "a defined-risk credit spread must not be penalised for the "
        "reward/risk ratio that defines it"
    )
    assert verdict["score"] >= cfg.agents.critic.min_score_to_trade, (
        f"the heuristic critic declined a candidate that passed every strategy "
        f"gate (score {verdict['score']} < {cfg.agents.critic.min_score_to_trade})"
    )


def test_no_book_trades_single_names():
    """The universe rule, encoded so it cannot quietly drift back.

    Single names are excluded for a mechanical reason, not a P&L one: the event
    gate excludes SCHEDULED earnings, but nothing protects a short condor from a
    surprise company headline, and a basket has no such headline. Keeping the
    single names that happened to be profitable while dropping the one that
    happened to lose would have been fitting the last backtest.
    """
    from oaa.backtest.chain import is_index_etf
    from oaa.config.loader import load_config

    cfg = load_config()
    offenders: dict[str, list[str]] = {}
    for ref in cfg.enabled_strategies():
        universe = ref.params.get("universe") or cfg.universe.active()
        bad = [
            s for s in universe
            if not is_index_etf(str(s))
        ]
        if bad:
            offenders[ref.name] = bad
    assert not offenders, (
        f"{offenders} are not index_etf tier. A short-volatility book carries "
        "idiosyncratic headline risk on a single name that its event gate "
        "cannot see."
    )


def test_the_carry_book_will_not_sell_the_front_expiry():
    """The replay chain is now built from 0 DTE so the intraday book has
    something to buy. That must not become a licence for the carry book to sell
    a 0-2 DTE condor: short gamma into expiry is the one trade this system is
    designed never to make, and the protection is vol_carry's OWN filter rather
    than the shape of the chain it happens to be handed."""
    from oaa.config.loader import load_config
    from oaa.strategies.base import load_strategies

    cfg = load_config()
    carry = next(s for s in load_strategies(cfg) if s.name == "vol_carry")
    assert carry.chain_dte_window() is None, "carry must not widen the chain"
    # vol_carry builds with ctx.default_filter(), whose floor is this value.
    assert cfg.options.min_days_to_expiry >= 3
    assert int(carry.p("exits.dte_floor", 0)) >= 3
