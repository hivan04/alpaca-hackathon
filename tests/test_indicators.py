from __future__ import annotations

from oaa.data.indicators import (
    adx,
    atr,
    ema,
    iv_rank,
    max_drawdown,
    realised_vol,
    sharpe,
    trend_strength,
    volume_ratio,
)


def test_indicators_return_none_when_starved():
    assert ema([1, 2], 10) is None
    assert realised_vol([{"close": 1}], 20) is None
    assert adx([], 14) is None
    assert atr([], 14) is None


def test_realised_vol_is_positive_and_annualised(bars):
    vol = realised_vol(bars, 20)
    assert vol is not None and 0 < vol < 3


def test_trend_strength_is_signed_and_bounded(bars):
    value = trend_strength(bars)
    assert value is not None and -1.0 <= value <= 1.0
    assert value > 0  # the fixture trends up


def test_adx_is_in_range(bars):
    value = adx(bars)
    assert value is not None and 0 <= value <= 100


def test_iv_rank_endpoints():
    assert iv_rank(0.30, [0.10, 0.20, 0.30, 0.15, 0.25]) == 1.0
    assert iv_rank(0.10, [0.10, 0.20, 0.30, 0.15, 0.25]) == 0.0
    assert iv_rank(None, [0.1] * 10) is None


def test_max_drawdown_is_negative():
    assert max_drawdown([100, 120, 90, 110]) == -0.25


def test_sharpe_handles_flat_series():
    assert sharpe([0.0, 0.0, 0.0]) is None


def test_volume_ratio(bars):
    assert volume_ratio(bars) is not None
