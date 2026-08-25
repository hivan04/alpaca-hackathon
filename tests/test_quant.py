"""Kalman filter, cointegration screen, features and the gap model."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from oaa.quant.cointegration import find_pairs
from oaa.quant.cointegration import test_pair as screen_pair
from oaa.quant.features import FEATURES, build_features, overnight_gap_return
from oaa.quant.forecast import OvernightGapModel
from oaa.quant.kalman import KalmanPairFilter, half_life
from tests.conftest import cointegrated_series, daily_bars


# --------------------------------------------------------------------------- #
# Kalman
# --------------------------------------------------------------------------- #
def test_filter_recovers_the_true_hedge_ratio():
    y, x = cointegrated_series(n=400, beta=1.5)
    state = KalmanPairFilter().fit(y, x)
    assert state.beta == pytest.approx(1.5, abs=0.15)


def test_filter_tracks_a_drifting_beta():
    """A static OLS beta would sit in the middle; the filter should follow."""
    rng = np.random.default_rng(5)
    n = 500
    x = 100 + np.cumsum(rng.normal(0, 0.4, n))
    beta_path = np.linspace(1.0, 2.0, n)
    y = beta_path * x + rng.normal(0, 0.3, n)

    kf = KalmanPairFilter(delta=1e-3)
    kf.fit(y, x)
    betas = kf.betas()
    assert betas[-1] > betas[len(betas) // 2] > betas[50]
    assert kf.state.beta == pytest.approx(2.0, abs=0.35)


def test_zscore_is_standardised():
    y, x = cointegrated_series(n=400)
    kf = KalmanPairFilter(zscore_window=60)
    kf.fit(y, x)
    zs = np.asarray(kf.zscores()[100:])
    assert abs(float(np.mean(zs))) < 0.5
    assert 0.5 < float(np.std(zs)) < 2.0


def test_filter_is_not_ready_before_warmup():
    kf = KalmanPairFilter(warmup=30)
    for i in range(10):
        kf.update(150 + i, 100 + i)
    assert not kf.ready


def test_reset_clears_state():
    y, x = cointegrated_series(n=100)
    kf = KalmanPairFilter()
    kf.fit(y, x)
    kf.reset()
    assert kf.state.observations == 0
    assert kf.history == []


def test_half_life_detects_mean_reversion():
    rng = np.random.default_rng(2)
    series = np.zeros(500)
    for i in range(1, 500):
        series[i] = 0.9 * series[i - 1] + rng.normal(0, 1)
    hl = half_life(series)
    assert hl is not None and 1 < hl < 30


def test_half_life_flags_a_random_walk_as_untradable():
    """A random walk has no half-life worth trading.

    It may still fit a huge finite number rather than diverging outright, so
    the screen filters on the RANGE, not on None. Either answer keeps the pair
    out of the universe, which is what matters.
    """
    rng = np.random.default_rng(4)
    hl = half_life(np.cumsum(rng.normal(0, 1, 400)))
    assert hl is None or hl > 60


# --------------------------------------------------------------------------- #
# cointegration
# --------------------------------------------------------------------------- #
def test_cointegrated_pair_passes_the_screen():
    y, x = cointegrated_series(n=400, beta=2.0)
    result = screen_pair("AAA", "BBB", y, x)
    assert result.passed, result.reasons
    assert result.pvalue < 0.05
    assert result.hedge_ratio == pytest.approx(2.0, abs=0.2)


def test_independent_random_walks_are_rejected():
    rng = np.random.default_rng(9)
    y = 100 + np.cumsum(rng.normal(0, 1, 400))
    x = 100 + np.cumsum(rng.normal(0, 1, 400))
    assert not screen_pair("AAA", "BBB", y, x).passed


def test_short_history_is_rejected_with_a_reason():
    result = screen_pair("AAA", "BBB", [1.0] * 50, [1.0] * 50)
    assert not result.passed
    assert "observations" in result.reasons[0]


def test_find_pairs_dedupes_direction():
    y, x = cointegrated_series(n=400, beta=1.5)
    results = find_pairs({"AAA": y, "BBB": x})
    # AAA/BBB and BBB/AAA both test; only the stronger direction survives.
    assert len(results) <= 1


# --------------------------------------------------------------------------- #
# features
# --------------------------------------------------------------------------- #
def test_feature_row_is_complete_and_finite():
    y, x = cointegrated_series(n=200)
    kf = KalmanPairFilter()
    kf.fit(y, x)
    row = build_features(
        zscore=kf.state.zscore, prev_zscore=kf.history[-2].zscore,
        beta=kf.state.beta, prev_beta=kf.history[-2].beta,
        spread=kf.state.spread, spread_std=kf.state.spread_std,
        bars_y=daily_bars(y), bars_x=daily_bars(x),
        recent_gaps=[0.001, -0.002], asof=dt.date(2026, 9, 1),
    )
    assert set(row) == set(FEATURES)
    assert all(np.isfinite(v) for v in row.values())


def test_features_survive_missing_data():
    row = build_features(
        zscore=float("nan"), prev_zscore=None, beta=1.0, prev_beta=None,
        spread=0.0, spread_std=0.0, bars_y=[], bars_x=[],
    )
    assert all(np.isfinite(v) for v in row.values())


def test_gap_return_is_hedge_weighted():
    # y unchanged, x gaps up 1%. A long-y/short-x pair loses on the short leg.
    r = overnight_gap_return(close_y=100, open_y=100, close_x=50, open_x=50.5, beta=2.0)
    assert r == pytest.approx(-0.01, abs=1e-9)


def test_gap_return_is_zero_when_both_legs_move_together():
    r = overnight_gap_return(close_y=100, open_y=101, close_x=100, open_x=101, beta=1.0)
    assert r == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# the gap model
# --------------------------------------------------------------------------- #
def _training_set(n: int = 250):
    rng = np.random.default_rng(13)
    rows, targets = [], []
    for _ in range(n):
        z = float(rng.normal(0, 1))
        row = dict.fromkeys(FEATURES, 0.0)
        row["zscore"] = z
        row["zscore_abs"] = abs(z)
        row["vol_y"] = float(abs(rng.normal(0.2, 0.05)))
        # A genuine (if noisy) mean-reversion signal for the model to find.
        rows.append(row)
        targets.append(-0.002 * z + float(rng.normal(0, 0.003)))
    return rows, targets


def test_model_falls_back_to_empirical_quantiles_when_starved():
    model = OvernightGapModel(min_train_rows=500)
    rows, targets = _training_set(50)
    model.fit(rows, targets)
    assert model.backend == "empirical"
    forecast = model.predict(rows[0])
    assert forecast.model == "empirical"
    assert forecast.lower <= forecast.expected <= forecast.upper


def test_model_trains_and_orders_its_quantiles():
    model = OvernightGapModel(min_train_rows=100)
    rows, targets = _training_set(250)
    model.fit(rows, targets)
    assert model.trained
    for row in rows[:25]:
        forecast = model.predict(row)
        assert forecast.lower <= forecast.expected <= forecast.upper


def test_model_learns_the_mean_reversion_sign():
    model = OvernightGapModel(min_train_rows=100)
    rows, targets = _training_set(400)
    model.fit(rows, targets)

    stretched = dict.fromkeys(FEATURES, 0.0)
    stretched.update({"zscore": 2.0, "zscore_abs": 2.0, "vol_y": 0.2})
    compressed = dict(stretched)
    compressed.update({"zscore": -2.0, "zscore_abs": 2.0})

    # Spread stretched high should predict a fall, and vice versa.
    assert model.predict(stretched).expected < model.predict(compressed).expected


def test_edge_to_risk_penalises_wide_tails():
    model = OvernightGapModel(min_train_rows=100)
    rows, targets = _training_set(250)
    model.fit(rows, targets)
    forecast = model.predict(rows[0])
    assert forecast.edge_to_risk >= 0
    assert forecast.tail_width > 0


def test_forecast_direction_matches_its_sign():
    model = OvernightGapModel(min_train_rows=100)
    rows, targets = _training_set(200)
    model.fit(rows, targets)
    forecast = model.predict(rows[0])
    expected = "long_spread" if forecast.expected > 0 else "short_spread"
    assert forecast.direction in (expected, "flat")
