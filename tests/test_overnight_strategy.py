"""The overnight pairs strategy: gates, overlay, sizing and defined risk."""

from __future__ import annotations

import pytest

from oaa.config.loader import load_config
from oaa.core.types import AssetKind, StructureType
from oaa.strategies.base import StrategyContext, strategy_registry


def build(overrides: dict | None = None):
    strategy_registry.autoload("oaa.strategies")
    cfg = load_config()
    ref = next(s for s in cfg.strategies if s.name == "overnight_pairs")
    ref.params["pairs"] = [{"left": "AAA", "right": "BBB", "hedge_ratio": 1.5,
                            "half_life_days": 6.0, "enabled": True}]
    # The synthetic fixture is a clean cointegrated pair, so relax the edge
    # floors that exist to reject marginal real-world nights.
    ref.params.setdefault("entry", {}).update({
        "min_abs_zscore": 0.0, "min_expected_return": 0.0,
        "min_edge_to_risk": 0.0, "min_confidence": 0.0, "max_tail_width": 1.0,
    })
    ref.params.setdefault("model", {})["min_train_rows"] = 100
    if overrides:
        for key, value in overrides.items():
            ref.params.setdefault(key, {}).update(value)
    return strategy_registry.get("overnight_pairs")(ref, cfg), cfg


def context(cfg, pair_contexts, account, budget: float = 200_000.0) -> StrategyContext:
    return StrategyContext(
        account=account, config=cfg, contexts=pair_contexts,
        params={}, budget=budget,
    )


# --------------------------------------------------------------------------- #
# registration and wiring
# --------------------------------------------------------------------------- #
def test_it_is_a_portfolio_strategy_on_the_overnight_book():
    strategy, _ = build()
    assert strategy.mode == "portfolio"      # needs both legs at once
    assert strategy.capital_book == "overnight"
    assert strategy.universe() == ["AAA", "BBB"]


def test_config_ships_it_enabled_with_a_pair_file():
    cfg = load_config()
    ref = next(s for s in cfg.strategies if s.name == "overnight_pairs")
    assert ref.enabled and ref.book == "overnight"
    assert ref.params["pairs"]          # loaded from config/pairs.yaml


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
def test_it_builds_a_collared_pair(pair_contexts, account):
    strategy, cfg = build()
    strategy.params = strategy.ref.params
    ideas = strategy.generate(context(cfg, pair_contexts, account))
    assert ideas, "expected a trade on a clean cointegrated pair"

    idea = ideas[0]
    assert idea.structure is StructureType.PAIRS_COLLAR
    assert idea.book == "overnight"
    assert len(idea.legs) == 4

    kinds = [leg.kind for leg in idea.legs]
    assert kinds.count(AssetKind.OPTION) == 2
    assert kinds.count(AssetKind.EQUITY) == 2


def test_the_structure_has_a_contractual_maximum_loss(pair_contexts, account):
    """The property that makes this approvable at all.

    A long/short equity pair has unbounded risk on the short leg. The collar
    is what converts that into a number.
    """
    strategy, cfg = build()
    idea = strategy.generate(context(cfg, pair_contexts, account))[0]
    assert idea.structure.is_defined_risk
    assert idea.max_loss is not None
    assert idea.max_loss > 0


def test_share_counts_are_whole_round_lots(pair_contexts, account):
    """Partially covered shares are an unhedged position with paperwork."""
    strategy, cfg = build()
    idea = strategy.generate(context(cfg, pair_contexts, account))[0]
    assert idea.meta["shares_long"] % 100 == 0
    assert idea.meta["shares_short"] % 100 == 0

    contracts = {leg.symbol: leg.qty for leg in idea.legs if leg.kind is AssetKind.OPTION}
    assert all(float(q).is_integer() and q >= 1 for q in contracts.values())


def test_the_pair_is_dollar_neutral_within_tolerance(pair_contexts, account):
    """The sizer searches the lot grid rather than taking the first fit.

    Naively sizing the long to the budget and rounding the short leaves a 10%
    residual on this price ratio (177.93 vs 106.79). Searching finds a
    combination that is essentially exact.
    """
    strategy, cfg = build()
    idea = strategy.generate(context(cfg, pair_contexts, account))[0]
    assert idea.meta["hedge_error_pct"] < 5.0
    assert idea.meta["hedge_ratio_realised"] == pytest.approx(1.0, abs=0.05)


def test_a_pair_that_cannot_be_hedged_neutrally_is_refused(pair_contexts, account):
    """Round lots plus an awkward price ratio can make neutrality unreachable.

    At a small budget the best available combination here is 10% off neutral.
    That is a directional bet with a hedge attached, not a pairs trade, and the
    right answer is to decline it rather than dress it up.
    """
    strategy, cfg = build()
    # 100k gross only reaches 2/3 lots; the exact 3/5 fit needs ~107k.
    assert strategy.generate(context(cfg, pair_contexts, account, budget=100_000.0)) == []


def test_raising_the_budget_unlocks_the_neutral_combination(pair_contexts, account):
    strategy, cfg = build()
    idea = strategy.generate(context(cfg, pair_contexts, account, budget=200_000.0))[0]
    assert idea.meta["hedge_error_pct"] < 1.0


def test_the_hedge_error_cap_is_configurable(pair_contexts, account):
    loose, cfg = build({"risk": {"max_hedge_error_pct": 0.20}})
    ideas = loose.generate(context(cfg, pair_contexts, account, budget=100_000.0))
    # Same budget that was refused above now trades, at a worse hedge.
    assert ideas and ideas[0].meta["hedge_error_pct"] > 5.0


def test_the_thesis_names_the_mechanism(pair_contexts, account):
    strategy, cfg = build()
    idea = strategy.generate(context(cfg, pair_contexts, account))[0]
    assert "Kalman" in idea.thesis
    assert "collared" in idea.thesis or "collar" in idea.thesis
    assert "09:35" in idea.thesis


def test_strikes_come_from_the_model_tails(pair_contexts, account):
    strategy, cfg = build()
    idea = strategy.generate(context(cfg, pair_contexts, account))[0]
    forecast = idea.meta["forecast"]
    assert idea.meta["put_strike"] is not None
    assert idea.meta["call_strike"] is not None
    # The put must sit below the long leg's spot, the call above the short's.
    long_spot = pair_contexts[idea.meta["long_leg"]].spot
    short_spot = pair_contexts[idea.meta["short_leg"]].spot
    assert idea.meta["put_strike"] <= long_spot
    assert idea.meta["call_strike"] >= short_spot
    assert forecast["lower_q05"] <= forecast["expected"] <= forecast["upper_q95"]


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def test_a_flat_spread_is_skipped(pair_contexts, account):
    strategy, cfg = build({"entry": {"min_abs_zscore": 99.0}})
    assert strategy.generate(context(cfg, pair_contexts, account)) == []


def test_an_extreme_zscore_is_treated_as_a_regime_break(pair_contexts, account):
    """A spread five sd from its mean usually means the relationship broke."""
    strategy, cfg = build({"entry": {"max_abs_zscore": 0.01}})
    assert strategy.generate(context(cfg, pair_contexts, account)) == []


def test_a_thin_edge_against_a_wide_tail_is_skipped(pair_contexts, account):
    strategy, cfg = build({"entry": {"min_edge_to_risk": 99.0}})
    assert strategy.generate(context(cfg, pair_contexts, account)) == []


def test_an_expensive_overlay_kills_the_trade(pair_contexts, account):
    """Protection that eats the edge is not protection."""
    strategy, cfg = build({"overlay": {"max_cost_pct_of_notional": 0.0}})
    assert strategy.generate(context(cfg, pair_contexts, account)) == []


def test_no_capital_budget_means_no_trade(pair_contexts, account):
    strategy, cfg = build()
    ctx = StrategyContext(
        account=account, config=cfg, contexts=pair_contexts, params={}, budget=0.0
    )
    ctx.config.strategies[0].params.setdefault("risk", {})["fallback_equity_pct"] = 0.0
    strategy.params.setdefault("risk", {})["fallback_equity_pct"] = 0.0
    assert strategy.generate(ctx) == []


def test_an_existing_position_in_a_leg_blocks_the_pair(pair_contexts, account):
    from oaa.core.types import PositionSnapshot

    strategy, cfg = build()
    loaded = account.model_copy(update={"positions": [
        PositionSnapshot(symbol="AAA", qty=100, avg_entry_price=100.0,
                         asset_class="us_equity", underlying="AAA")
    ]})
    assert strategy.generate(context(cfg, pair_contexts, loaded)) == []


# --------------------------------------------------------------------------- #
# the overlay contract
# --------------------------------------------------------------------------- #
def test_no_call_on_the_short_leg_means_no_trade(pair_contexts, account):
    """The one exposure this strategy will not carry."""
    strategy, cfg = build()
    ideas = strategy.generate(context(cfg, pair_contexts, account))
    short_leg = ideas[0].meta["short_leg"]

    # Strip every call from the short leg's chain and retry.
    stripped = dict(pair_contexts)
    market = stripped[short_leg]
    stripped[short_leg] = market.model_copy(update={
        "chain": [q for q in market.chain if q.right.value == "put"]
    })
    strategy2, cfg2 = build()
    assert strategy2.generate(context(cfg2, stripped, account)) == []


def test_put_only_mode_is_not_defined_risk(pair_contexts, account):
    strategy, cfg = build({"overlay": {"mode": "put_only"}})
    ideas = strategy.generate(context(cfg, pair_contexts, account))
    if ideas:
        idea = ideas[0]
        assert idea.structure is StructureType.PAIRS_PUT_HEDGE
        assert not idea.structure.is_defined_risk    # the risk engine will refuse it


def test_exits_are_on_the_clock_not_on_pnl(pair_contexts, account):
    strategy, cfg = build()
    ctx = context(cfg, pair_contexts, account)
    idea = strategy.generate(ctx)[0]
    # There is no taking profit at 03:00, and a stop would not fill anyway.
    assert strategy.should_exit(ctx, idea, pnl_pct=5.0) is None
    assert strategy.should_exit(ctx, idea, pnl_pct=-5.0) is None


# --------------------------------------------------------------------------- #
# risk engine integration
# --------------------------------------------------------------------------- #
def test_the_risk_engine_approves_a_collar_inside_the_entry_window(
    pair_contexts, account, frozen_clock
):
    from oaa.firewall.lock import Book, TemporalFirewall
    from oaa.risk.engine import RiskEngine

    strategy, cfg = build()
    idea = strategy.generate(context(cfg, pair_contexts, account))[0]
    cfg.risk.max_risk_per_trade_pct = 0.50   # the fixture pair is large vs 100k

    firewall = TemporalFirewall(cfg)
    firewall._acquire(Book.OVERNIGHT, frozen_clock("15:55"), 200_000)
    verdict = RiskEngine(cfg, firewall=firewall).evaluate(
        idea, account, now=frozen_clock("15:55")
    )
    assert verdict.approved, verdict.reasons


def test_the_risk_engine_refuses_the_same_idea_at_the_wrong_minute(
    pair_contexts, account, frozen_clock
):
    from oaa.firewall.lock import TemporalFirewall
    from oaa.risk.engine import RiskEngine

    strategy, cfg = build()
    idea = strategy.generate(context(cfg, pair_contexts, account))[0]
    cfg.risk.max_risk_per_trade_pct = 0.50

    verdict = RiskEngine(cfg, firewall=TemporalFirewall(cfg)).evaluate(
        idea, account, now=frozen_clock("11:00")
    )
    assert not verdict.approved
    assert "rule=firewall" in verdict.reasons


def test_the_risk_engine_refuses_an_unhedged_pair(pair_contexts, account, frozen_clock):
    from oaa.firewall.lock import Book, TemporalFirewall
    from oaa.risk.engine import RiskEngine

    strategy, cfg = build({"overlay": {"mode": "put_only", "require_full_hedge": False}})
    ideas = strategy.generate(context(cfg, pair_contexts, account))
    if not ideas:
        pytest.skip("put_only did not produce an idea on this fixture")

    firewall = TemporalFirewall(cfg)
    firewall._acquire(Book.OVERNIGHT, frozen_clock("15:55"), 200_000)
    verdict = RiskEngine(cfg, firewall=firewall).evaluate(
        ideas[0], account, now=frozen_clock("15:55")
    )
    assert not verdict.approved
    assert "rule=undefined_risk" in verdict.reasons


# --------------------------------------------------------------------------- #
# the macro overlay
# --------------------------------------------------------------------------- #
def test_the_macro_lens_can_stand_the_strategy_down(pair_contexts, account):
    from oaa.discovery.macro import MacroView

    strategy, cfg = build()
    ctx = context(cfg, pair_contexts, account)
    ctx.macro = MacroView(
        regime="risk_off",
        guidance={"overnight_pairs": "stand_down"},
        rationale="tape is unhedgeable tonight",
    )
    assert strategy.generate(ctx) == []


def test_a_flagged_leg_blocks_its_pair(pair_contexts, account):
    """Idiosyncratic news on one leg is exactly what the hedge does not cover."""
    from oaa.discovery.macro import MacroView

    strategy, cfg = build()
    ctx = context(cfg, pair_contexts, account)
    ctx.macro = MacroView(
        flagged_symbols={"AAA": "moving on a story BBB does not share"}
    )
    assert strategy.generate(ctx) == []


def test_a_shared_catalyst_does_not_block_the_pair(pair_contexts, account):
    """Both legs newsy is a sector move; the spread is intact."""
    from oaa.discovery.macro import MacroView

    strategy, cfg = build()
    ctx = context(cfg, pair_contexts, account)
    ctx.macro = MacroView(
        regime="high_dispersion",
        shared_themes=["AAA/BBB: both legs on unusual news - shared catalyst"],
    )
    assert strategy.generate(ctx)


def test_reduce_halves_the_size_rather_than_forfeiting_the_night(pair_contexts, account):
    from oaa.discovery.macro import MacroView

    full, cfg_full = build()
    reduced, cfg_reduced = build()

    # Headroom so the halved budget can still find a neutral lot combination -
    # otherwise the hedge-error gate correctly refuses it and we would be
    # testing the wrong thing.
    ctx_full = context(cfg_full, pair_contexts, account, budget=600_000.0)
    ctx_reduced = context(cfg_reduced, pair_contexts, account, budget=600_000.0)
    ctx_reduced.macro = MacroView(guidance={"overnight_pairs": "reduce"})

    big = full.generate(ctx_full)
    small = reduced.generate(ctx_reduced)
    assert big and small                      # still trades, just smaller
    assert small[0].meta["shares_long"] < big[0].meta["shares_long"]


def test_collar_widening_pushes_the_strikes_further_out(pair_contexts, account):
    from oaa.discovery.macro import MacroView

    base, cfg_base = build()
    wide, cfg_wide = build()

    ctx_base = context(cfg_base, pair_contexts, account)
    ctx_wide = context(cfg_wide, pair_contexts, account)
    ctx_wide.macro = MacroView(collar_widening=2.0)

    normal = base.generate(ctx_base)[0]
    widened = wide.generate(ctx_wide)[0]
    # A jumpy tape buys protection further from spot on both wings.
    assert widened.meta["put_strike"] <= normal.meta["put_strike"]
    assert widened.meta["call_strike"] >= normal.meta["call_strike"]
    assert widened.meta["collar_widening"] == 2.0


def test_the_macro_regime_is_recorded_on_the_idea(pair_contexts, account):
    from oaa.discovery.macro import MacroView

    strategy, cfg = build()
    ctx = context(cfg, pair_contexts, account)
    ctx.macro = MacroView(regime="high_dispersion")
    idea = strategy.generate(ctx)[0]
    assert idea.meta["macro_regime"] == "high_dispersion"
    assert idea.meta["macro_stance"] == "trade"


def test_no_macro_view_is_fully_permissive(pair_contexts, account):
    """Discovery is an overlay. With it absent, nothing changes."""
    strategy, cfg = build()
    ctx = context(cfg, pair_contexts, account)
    assert ctx.macro is None
    assert strategy.generate(ctx)
