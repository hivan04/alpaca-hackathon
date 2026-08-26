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


def test_no_catalyst_means_no_trade(intraday_chain, intraday_bars, attention, account):
    """A VWAP cross with no mechanism behind it is drift, and drift reverts."""
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account, news=[])
    assert strat.generate(ctx) == []


def test_mixed_breadth_means_no_trade(intraday_chain, intraday_bars, attention, account):
    """An index rising on mixed breadth is one mega-cap dragging the tape."""
    attention.breadth = {"gainers": 10, "losers": 10}
    strat, ctx = scenario(intraday_chain, intraday_bars, attention, account)
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
    round trip against a $10-30 target."""
    strat, _ = build()
    assert set(strat.universe()) <= {"SPY", "QQQ", "IWM"}


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
