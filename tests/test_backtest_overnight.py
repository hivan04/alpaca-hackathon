"""Walk-forward overnight backtest: mechanics, costs, and no lookahead."""

from __future__ import annotations

import pytest

from oaa.backtest.overnight import OvernightBacktest
from oaa.backtest.pricing import bs_price, intrinsic_at_open, overnight_option_cost
from oaa.config.loader import load_settings
from tests.conftest import cointegrated_series, daily_bars

PARAMS = {
    "kalman": {"warmup": 30},
    "model": {"min_train_rows": 120},
    "backtest": {"refit_every": 40},
    "entry": {
        "min_abs_zscore": 0.5, "min_expected_return": 0.0005,
        "min_edge_to_risk": 0.05, "min_confidence": 0.03, "max_tail_width": 0.10,
    },
}


class FakeProvider:
    name = "fake"

    def __init__(self, left_closes, right_closes):
        self._left = daily_bars(left_closes)
        self._right = daily_bars(right_closes, seed=23)

    def bars(self, symbol, **kwargs):
        return self._left if symbol == "AAA" else self._right


@pytest.fixture(scope="module")
def result():
    y, x = cointegrated_series(n=520, beta=1.5)
    settings = load_settings(profile="dev")
    engine = OvernightBacktest(settings, FakeProvider(y, x), PARAMS)
    return engine.run("AAA", "BBB", initial_equity=100_000)


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #
def test_option_price_is_at_least_intrinsic():
    assert bs_price(100, 90, 1 / 365, 0.25, is_call=True) >= 10.0
    assert bs_price(100, 110, 1 / 365, 0.25, is_call=False) >= 10.0


def test_a_1dte_otm_option_is_cheap_but_not_free():
    cost = overnight_option_cost(100, 96, realised_vol=0.22, is_call=False)
    assert 0 < cost < 0.30


def test_overlay_cost_rises_with_volatility():
    calm = overnight_option_cost(100, 96, realised_vol=0.15, is_call=False)
    wild = overnight_option_cost(100, 96, realised_vol=0.60, is_call=False)
    assert wild > calm


def test_the_cost_model_marks_vol_up_over_realised():
    """Short-dated options carry a variance premium; pretending otherwise
    flatters every backtest that hedges."""
    fair = bs_price(100, 96, 1 / 365, 0.22, is_call=False)
    charged = overnight_option_cost(100, 96, realised_vol=0.22, is_call=False)
    assert charged > fair


def test_exit_assumes_intrinsic_only():
    assert intrinsic_at_open(95, 100, is_call=False) == 5.0
    assert intrinsic_at_open(105, 100, is_call=False) == 0.0
    assert intrinsic_at_open(105, 100, is_call=True) == 5.0


# --------------------------------------------------------------------------- #
# the walk-forward loop
# --------------------------------------------------------------------------- #
def test_it_runs_and_produces_a_curve(result):
    assert result.nights
    assert len(result.equity_curve) == len(result.nights)
    assert result.start < result.end


def test_it_skips_far_more_nights_than_it_trades(result):
    """A strategy that trades every night is not selecting, it is gambling."""
    metrics = result.metrics()
    assert 0 < metrics["participation_rate"] < 0.75
    assert metrics["skip_reasons"]


def test_nothing_trades_before_the_model_has_trained(result):
    warming = [n for n in result.nights if n.skip_reason == "model warming up"]
    assert warming
    assert all(not n.traded for n in warming)
    # And the warm-up nights come first.
    first_trade = next(i for i, n in enumerate(result.nights) if n.traded)
    assert all(not n.traded for n in result.nights[:first_trade])


def test_costs_are_actually_charged(result):
    metrics = result.metrics()
    assert metrics["total_overlay_cost"] > 0
    assert metrics["total_slippage"] > 0
    # Net P&L must be the gross less the frictions, not the gross.
    assert metrics["total_pnl"] != metrics["gross_pnl_before_costs"]


def test_net_pnl_reconciles_per_night(result):
    for night in result.traded[:40]:
        expected = (
            night.equity_pnl + night.overlay_payoff - night.overlay_cost - night.slippage
        )
        assert night.net_pnl == pytest.approx(expected, abs=1e-6)


def test_equity_compounds_from_the_night_pnl(result):
    equity = result.initial_equity
    for night in result.nights[:80]:
        equity += night.net_pnl
        assert night.equity_after == pytest.approx(equity, abs=1e-6)


def test_round_lots_only(result):
    for night in result.traded:
        assert night.shares_long % 100 == 0
        assert night.shares_short % 100 == 0


def test_the_forecast_is_not_the_realised_value(result):
    """The obvious lookahead bug: predicting the target by reading it."""
    traded = result.traded
    assert traded
    matches = sum(
        1 for n in traded if abs(n.expected - n.realised) < 1e-9
    )
    assert matches == 0


def test_quantiles_bracket_the_forecast(result):
    for night in result.traded:
        assert night.q05 <= night.expected <= night.q95


def test_metrics_are_self_consistent(result):
    metrics = result.metrics()
    assert metrics["nights_traded"] == len(result.traded)
    assert metrics["final_equity"] == pytest.approx(result.equity_curve[-1], abs=0.01)
    total = sum(n.net_pnl for n in result.traded)
    assert metrics["total_pnl"] == pytest.approx(total, abs=0.01)
    assert 0.0 <= metrics["win_rate"] <= 1.0
    assert metrics["max_drawdown"] <= 0.0


def test_summary_lines_render(result):
    lines = result.summary_lines()
    assert any("Pair" in line for line in lines)
    assert any("Overlay net" in line for line in lines)


def test_short_history_is_refused():
    settings = load_settings(profile="dev")
    y, x = cointegrated_series(n=100)
    engine = OvernightBacktest(settings, FakeProvider(y, x), PARAMS)
    with pytest.raises(Exception, match="aligned sessions"):
        engine.run("AAA", "BBB")


def test_rows_export_cleanly(result):
    row = result.nights[-1].as_row()
    assert set(row) >= {"date", "pair", "traded", "net_pnl", "equity_after"}
    assert isinstance(row["date"], str)
