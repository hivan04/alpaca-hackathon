"""The intraday book: the signal stack, the gates, and what they refuse."""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.config.loader import load_config
from oaa.core.types import MarketContext
from oaa.data.indicators import (
    bollinger_width,
    crossed,
    rsi,
    volume_zscore_by_bucket,
    vwap,
    vwap_series,
    width_is_rising,
)
from oaa.signals.catalyst import CatalystEngine, MacroCalendar
from oaa.strategies.base import StrategyContext, strategy_registry


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #
def test_session_vwap_does_not_bleed_across_days(intraday_bars):
    """A VWAP that spans days anchors to yesterday's value area rather than the
    level anyone is actually trading against."""
    session = vwap(intraday_bars, session_only=True)
    everything = vwap(intraday_bars, session_only=False)
    assert session is not None and everything is not None
    assert session != everything

    last_day = intraday_bars[-1]["timestamp"].date()
    today_closes = [b["close"] for b in intraday_bars if b["timestamp"].date() == last_day]
    mean_today = sum(today_closes) / len(today_closes)
    assert abs(session - mean_today) < abs(everything - mean_today)


def test_vwap_series_restarts_each_session(intraday_bars):
    series = vwap_series(intraday_bars)
    assert len(series) == len(intraday_bars)
    days = {b["timestamp"].date() for b in intraday_bars}
    assert len(days) >= 2


def test_a_cross_is_detected_not_merely_a_level(intraday_bars):
    closes = [b["close"] for b in intraday_bars]
    anchor = vwap_series(intraday_bars)
    # Somewhere in the final session the price crosses its own session VWAP.
    crosses = [
        crossed(closes[: i + 1], anchor[: i + 1]) for i in range(len(closes) - 20, len(closes))
    ]
    assert any(c > 0 for c in crosses)


def test_band_width_is_direction_agnostic():
    """Width is a volatility-regime measurement. Position is a mean-reversion
    signal and would fight the VWAP trigger on every candidate."""
    from oaa.data.indicators import bollinger

    window = [100 + i * 0.5 for i in range(20)]
    _, up_hi, up_lo = bollinger(window)
    _, dn_hi, dn_lo = bollinger(list(reversed(window)))
    # Same dispersion, opposite direction: the bands cannot tell them apart,
    # and that is exactly why width does not fight the VWAP trigger.
    assert (up_hi - up_lo) == pytest.approx(dn_hi - dn_lo, rel=1e-6)
    assert bollinger_width(window) > 0


def test_width_rising_separates_expansion_from_chop():
    expanding = [100 + (i % 2) * (0.2 + i * 0.05) for i in range(60)]
    flat = [100 + (i % 2) * 0.2 for i in range(60)]
    widths_e = [bollinger_width(expanding[: i + 1]) for i in range(20, len(expanding))]
    widths_f = [bollinger_width(flat[: i + 1]) for i in range(20, len(flat))]
    assert width_is_rising([w for w in widths_e if w is not None])
    assert not width_is_rising([w for w in widths_f if w is not None])


def test_rsi_is_bounded_and_directional():
    up = list(range(100, 140))
    down = list(range(140, 100, -1))
    assert rsi(up) > 85
    assert rsi(down) < 15


def test_volume_is_compared_within_its_own_time_of_day_bucket(intraday_bars):
    """09:45 volume is not comparable to 12:30 volume."""
    z = volume_zscore_by_bucket(intraday_bars, bucket_minutes=30)
    assert z is not None and z > 1.0


# --------------------------------------------------------------------------- #
# the strategy
# --------------------------------------------------------------------------- #
def build(cfg=None):
    strategy_registry.autoload("oaa.strategies")
    cfg = cfg or load_config()
    ref = next(s for s in cfg.strategies if s.name == "intraday_momentum")
    ref.enabled = True
    return strategy_registry.get("intraday_momentum")(ref, cfg), cfg


def market(chain, intraday, **overrides) -> MarketContext:
    base: dict = {
        "symbol": "SPY",
        "asof": dt.datetime(2026, 8, 28, 14, 40, tzinfo=dt.timezone.utc),
        "spot": 502.3,
        "bars": [],
        "intraday_bars": intraday,
        "chain": chain,
        "realised_vol": 0.14,
        "implied_vol": 0.18,
        "iv_rank": 0.40,
        "news": [{
            "headline": "Fed officials signal a pause; index futures rally",
            "symbols": ["SPY"],
            "created_at": (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=8)
            ).isoformat(),
        }],
    }
    base.update(overrides)
    return MarketContext(**base)


def context(strat, cfg, market_ctx, attention, catalyst=None):
    return StrategyContext(
        market=market_ctx, account=None, config=cfg, params=strat.params,
        catalyst=catalyst or CatalystEngine(calendar=MacroCalendar()),
        attention=attention,
    )


class FrozenFirewall:
    """Just enough firewall for the strategy's ET clock lookup."""

    def __init__(self, at="11:20"):
        from zoneinfo import ZoneInfo

        from oaa.firewall.clock import SessionClock

        self.clock = SessionClock(
            frozen_now=dt.datetime(2026, 8, 28, 0, 0, tzinfo=ZoneInfo("America/New_York")).replace(
                hour=int(at.split(":")[0]), minute=int(at.split(":")[1])
            )
        )
        self.journal = None


def scenario(chain, intraday, attention, account, at="11:20", **market_overrides):
    strat, cfg = build()
    ctx = context(strat, cfg, market(chain, intraday, **market_overrides), attention)
    ctx.account = account
    ctx.firewall = FrozenFirewall(at)
    return strat, ctx


def test_the_full_stack_fires_on_a_catalyst_confirmed_breakout(
    intraday_chain, intraday_bars, attention, account
):
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    ideas = strat.generate(ctx)
    assert ideas, "a confirmed VWAP breakout with a headline and breadth should trade"
    idea = ideas[0]
    assert not idea.is_credit                     # long premium by construction
    assert idea.max_loss and idea.max_loss > 0    # max loss = premium paid
    assert "bullish" in idea.tags
    assert idea.meta["gates"]["passed"] is True
    assert idea.meta["selection"]["mode"] in {"single", "vertical"}


def test_chop_is_rejected_by_the_band_width_filter(
    intraday_chain, choppy_intraday_bars, attention, account
):
    strat, ctx = scenario(intraday_chain, choppy_intraday_bars, attention, account)
    assert strat.generate(ctx) == []


def test_a_missing_catalyst_costs_a_vote_rather_than_vetoing(
    intraday_chain, intraday_bars, attention, account
):
    """A VWAP cross with no mechanism behind it is weaker evidence - but it is
    evidence, not a disqualification.

    The catalyst used to be a hard veto sitting behind the trigger and in front
    of four more hard vetoes. Eight conjunctive gates at ~70% each is 0.7^8 = 6%:
    the book was arithmetically designed never to fire, and over 864 measured
    candidates none survived the chain. The catalyst is now one confirmation of
    five, so what is asserted here is that the VOTE is lost, not the trade.
    """
    with_news, ctx_news = scenario(intraday_chain, intraday_bars, attention, account)
    without, ctx_none = scenario(intraday_chain, intraday_bars, attention, account, news=[])

    confirmed = with_news.generate(ctx_news)
    drifting = without.generate(ctx_none)
    assert confirmed, "the baseline scenario should trade"
    assert drifting, "one missing confirmation is not a veto"

    scored = confirmed[0].meta["gates"]["metrics"]["confirmation.confirmations"]
    unscored = drifting[0].meta["gates"]["metrics"]["confirmation.confirmations"]
    assert unscored == scored - 1, "the missing catalyst must cost exactly one vote"


def test_a_demoted_catalyst_is_still_measured(
    intraday_chain, intraday_bars, attention, account
):
    """Demoted, not deleted. The gate returns passed=True when it is not
    mandatory, so the tally has to read what it MEASURED - reading the pass bit
    handed the book a free fifth vote on every single candidate and removed the
    catalyst from the decision entirely."""
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, news=[])
    ideas = strat.generate(ctx)
    assert ideas
    metrics = ideas[0].meta["gates"]["metrics"]
    assert metrics["catalyst.confirmed"] == 0.0


def test_the_catalyst_can_still_be_made_mandatory(
    intraday_chain, intraday_bars, attention, account
):
    """The veto behaviour is a config flag, not a deleted idea - if live
    sessions show drift entries paying the spread for nothing, flip it back."""
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, news=[])
    strat.params["catalyst_gate"]["required"] = True
    assert strat.generate(ctx) == []


def test_mixed_breadth_costs_the_catalyst_vote(
    intraday_chain, intraday_bars, attention, account
):
    """An index rising on mixed breadth is one mega-cap dragging the tape, so
    the catalyst does not confirm - one vote lost, and a hard block only when
    the catalyst is configured as mandatory."""
    attention.breadth = {"gainers": 10, "losers": 10}
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    ideas = strat.generate(ctx)
    assert ideas
    assert ideas[0].meta["gates"]["metrics"]["catalyst.confirmed"] == 0.0

    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    strat.params["catalyst_gate"]["required"] = True
    assert strat.generate(ctx) == []


def test_enough_failed_confirmations_do_block_the_trade(
    intraday_chain, intraday_bars, attention, account
):
    """The counter is a real bar, not a formality: demand more agreement than
    the evidence supports and the book stands down."""
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, news=[])
    # Unanimity. `needed` is capped at the number of votes actually cast, so
    # this asks for every one of them - and the catalyst vote is lost here
    # because the scenario has no headline. Pinned as "more than the evidence
    # supports" rather than a literal count, so adding a confirmation (the
    # hourly trend filter did exactly this) does not silently defuse the test.
    strat.params["momentum"]["confirmations_required"] = 999
    assert strat.generate(ctx) == []


def test_no_entries_before_the_open_settles(intraday_chain, intraday_bars, attention, account):
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, at="09:35")
    assert strat.generate(ctx) == []


def test_no_entries_without_runway_before_the_cutoff(
    intraday_chain, intraday_bars, attention, account
):
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, at="15:05")
    assert strat.generate(ctx) == []


def test_the_lunch_window_is_skipped(intraday_chain, intraday_bars, attention, account):
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, at="12:30")
    assert strat.generate(ctx) == []


def test_extremely_rich_vol_declines_the_trade(intraday_chain, intraday_bars, attention, account):
    """Paying for a move that is already priced in is a negative-expectancy way
    to be right. This row of the selection table is worth a slide."""
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, iv_rank=0.95)
    assert strat.generate(ctx) == []


def test_the_universe_is_index_only(intraday_chain, intraday_bars, attention, account):
    """Not a preference - arithmetic. A $0.10-wide single-name quote costs $20
    round trip against a $10-30 target.

    Asserted against the liquidity model's OWN classification rather than a
    hard-coded list, so growing the universe is allowed but sneaking a single
    name into it is not. The previous version pinned {SPY, QQQ, IWM} literally
    and failed the moment the universe legitimately grew to ten ETFs - a test
    that encodes today's list instead of the rule behind it.
    """
    from oaa.backtest.chain import is_index_etf

    strat, _ = build()
    universe = strat.universe()
    assert universe, "the intraday book cannot trade an empty universe"
    misclassified = [
        symbol for symbol in universe
        if not is_index_etf(symbol)
    ]
    assert not misclassified, (
        f"{misclassified} are not index_etf tier - a single-name quote does not "
        "survive this book's spread arithmetic. Add the symbol to "
        "DEFAULT_TIER_MAP only if its real quote width justifies it."
    )


def test_the_spread_gate_is_mandatory_and_bites(intraday_chain, intraday_bars, attention, account):
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    strat.ref.params["spread_gate"]["max_relative_spread"] = 0.0001
    assert strat.generate(ctx) == []


def test_exits_are_mechanical(intraday_chain, intraday_bars, attention, account):
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    idea = strat.generate(ctx)[0]
    ctx.contexts = {}
    assert strat.should_exit(ctx, idea, 0.05) is None
    assert "target" in (strat.should_exit(ctx, idea, 0.12) or "")
    assert "stop" in (strat.should_exit(ctx, idea, -0.20) or "")


def test_the_stop_is_wider_than_the_target_on_purpose():
    """Option premium is noisy and a tight stop is hit by spread flicker alone.
    The cost is a demanding breakeven hit rate, which is stated, not hidden."""
    from oaa.telemetry.costs import CostModel

    strat, _ = build()
    target = strat.p("exits.target_pct_of_premium")
    stop = strat.p("exits.stop_pct_of_premium")
    assert stop > target
    assert CostModel().breakeven_hit_rate(target, stop) == pytest.approx(0.60, abs=0.01)


def test_the_thesis_does_not_claim_confirmations_that_did_not_happen(
    intraday_chain, intraday_bars, attention, account
):
    """The rationale a judge reads has to describe THIS trade.

    Written for the veto design, it asserted "Bollinger width expanding" and
    "breadth confirming" because reaching that line used to mean every gate had
    passed. Under a score it does not: measured over 624 candidates the
    catalyst confirmed in three, so that sentence would have been false on
    essentially every trade in the judged journal.

    Asserted against the GENERATED text, not the source - a rationale is a
    claim about a trade, and the only way to test a claim is to make one.
    """
    attention.breadth = {"gainers": 10, "losers": 10}
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, news=[])
    ideas = strat.generate(ctx)
    assert ideas, "this scenario should still trade on its other confirmations"
    thesis = ideas[0].thesis
    # neither the catalyst nor breadth confirmed in this scenario
    assert "breadth confirming" not in thesis
    assert "confirmations agree" in thesis, "it must report the tally it got"


def test_the_thesis_reports_the_confirmations_it_missed(
    intraday_chain, intraday_bars, attention, account
):
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, news=[])
    ideas = strat.generate(ctx)
    assert ideas
    thesis = ideas[0].thesis
    assert "confirmations agree" in thesis
    # the catalyst vote was lost here, so the rationale must say so
    assert "Not confirming" in thesis or "0 headline" in thesis


# --------------------------------------------------------------------------- #
# term structure - the seventh confirmation
#
# The property that has to hold is not "it improves the P&L" - one sample
# cannot show that. It is that the vote is STRICTLY ADDITIVE: `needed` is
# min(confirmations_required, possible) and `confirmations` is a sum over votes
# that passed, so raising `possible` from 6 to 7 cannot take a candidate that
# used to trade and stop it trading. If that ever stops being true, this signal
# has started holding the book back instead of helping it, and these tests are
# what say so.
# --------------------------------------------------------------------------- #
def _term_chain(front_iv: float, back_iv: float, base_chain: list) -> list:
    """The 0-2 DTE fixture plus a back expiry, so a slope exists at all."""
    from tests.conftest import _expiry_slice

    front = [q.model_copy(update={"implied_volatility": front_iv}) for q in base_chain]
    back = [
        q.model_copy(update={"implied_volatility": back_iv})
        for q in _expiry_slice(dt.date(2026, 8, 28), 30, vol=back_iv, half_spread=0.02)
    ]
    return front + back


def _with_term(strat, ctx, front_iv, back_iv, base_chain):
    from oaa.data.term_structure import term_structure

    chain = _term_chain(front_iv, back_iv, base_chain)
    ctx.market.chain = chain
    ctx.market.term_structure = term_structure(
        chain, ctx.market.spot, ctx.market.asof.date()
    )
    return ctx


def test_a_slope_inside_the_band_adds_a_confirmation(
    intraday_chain, intraday_bars, attention, account
):
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    # +10% relative slope: mild backwardation, inside [-10%, +25%].
    ctx = _with_term(strat, ctx, 0.22, 0.20, intraday_chain)
    gate = strat._momentum_gate(ctx.market, intraday_bars)
    assert gate.metrics["term_slope_pct"] == 0.1
    assert "term slope" not in (gate.metrics.get("confirmations_failed") or "")


def test_a_slope_outside_the_band_costs_a_vote_and_nothing_else(
    intraday_chain, intraday_bars, attention, account
):
    """The failing case must not remove a confirmation another gate earned."""
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    baseline = strat._momentum_gate(ctx.market, intraday_bars)

    ctx = _with_term(strat, ctx, 0.40, 0.20, intraday_chain)   # +100%, way outside
    gate = strat._momentum_gate(ctx.market, intraday_bars)

    assert gate.metrics["confirmations"] == baseline.metrics["confirmations"]
    assert gate.metrics["confirmations_possible"] == (
        baseline.metrics["confirmations_possible"] + 1
    )
    assert "backwardation" in gate.metrics["confirmations_failed"]


def test_the_vote_can_never_turn_a_trade_into_a_rejection(
    intraday_chain, intraday_bars, attention, account
):
    """The whole point, asserted end to end rather than argued in a comment."""
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    without = strat.generate(ctx)
    assert without, "the fixture must trade, or this test proves nothing"

    for front_iv, back_iv in ((0.40, 0.20), (0.10, 0.20), (0.22, 0.20)):
        strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
        ctx = _with_term(strat, ctx, front_iv, back_iv, intraday_chain)
        assert strat.generate(ctx), (
            f"a {front_iv:.0%}/{back_iv:.0%} term structure stopped a trade that "
            "fires without the signal - the vote is no longer additive"
        )


def test_a_modelled_slope_does_not_vote(
    intraday_chain, intraday_bars, attention, account
):
    """The modelled surface's term slope is `backtest.chain.term_slope`, a
    constant. A vote read off it would fire identically forever."""
    from oaa.data.term_structure import term_structure

    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    baseline = strat._momentum_gate(ctx.market, intraday_bars)

    chain = [
        q.model_copy(update={"iv_source": "modelled (no bar)"})
        for q in _term_chain(0.22, 0.20, intraday_chain)
    ]
    ctx.market.chain = chain
    ctx.market.term_structure = term_structure(
        chain, ctx.market.spot, ctx.market.asof.date(), require_measured=False
    )
    gate = strat._momentum_gate(ctx.market, intraday_bars)

    assert "term_slope_pct" not in gate.metrics
    assert gate.metrics["confirmations_possible"] == baseline.metrics["confirmations_possible"]
    assert "modelled" in gate.metrics["confirmations_failed"]


def test_a_chain_with_no_back_expiry_does_not_vote(
    intraday_chain, intraday_bars, attention, account
):
    """The 0-2 DTE fixture on its own. None must mean unanswerable, not flat -
    a caller reading None as 0.0 would place it inside the band and
    manufacture a confirmation out of missing data."""
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
    assert ctx.market.term_structure is None
    gate = strat._momentum_gate(ctx.market, intraday_bars)
    assert "term_slope_pct" not in gate.metrics
    assert "no two expiries" in gate.metrics["confirmations_failed"]
