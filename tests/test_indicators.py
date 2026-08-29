from __future__ import annotations

import datetime as _dt

from oaa.data.indicators import (
    IV_RANK_MIN_OBSERVATIONS,
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
from oaa.data.iv_history import IVHistoryStore


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
    # A percentile, not a min-max scaling, and undefined below the observation
    # floor - see oaa.data.indicators.iv_rank.
    history = [0.10 + i / 100 for i in range(25)]
    assert iv_rank(max(history), history) == 1.0
    assert iv_rank(min(history), history) == round(1 / len(history), 4)
    assert iv_rank(None, [0.1] * 30) is None
    assert iv_rank(0.30, [0.10, 0.20, 0.30, 0.15, 0.25]) is None


def test_max_drawdown_is_negative():
    assert max_drawdown([100, 120, 90, 110]) == -0.25


def test_sharpe_handles_flat_series():
    assert sharpe([0.0, 0.0, 0.0]) is None


def test_volume_ratio(bars):
    assert volume_ratio(bars) is not None


# --------------------------------------------------------------------------- #
# IV rank: ONE definition, live and replay
#
# These pin the fix for a divergence that made the carry book's entry gate mean
# different things in the two places it runs. Replay ranked one observation per
# session against a trailing year, as a percentile. Live min-max scaled an
# in-memory list of intraday polls that reset on every restart. Same gate, same
# name, two different numbers.
# --------------------------------------------------------------------------- #


def test_an_unmeasurable_iv_rank_is_none_not_a_guess():
    short = [0.20] * (IV_RANK_MIN_OBSERVATIONS - 1)
    assert iv_rank(0.25, short) is None
    assert iv_rank(0.25, short + [0.21]) is not None
    assert iv_rank(None, [0.2] * 50) is None


def test_iv_rank_is_a_percentile_so_one_spike_cannot_pin_it_to_zero():
    """Under min-max a single vol event parked every later reading near 0 and
    stood the book down for as long as it stayed in the window."""
    history = [0.20] * 40 + [2.00]          # one enormous print
    history.append(0.21)                    # today: above almost everything
    rank = iv_rank(0.21, history)
    assert rank is not None and rank > 0.9


def test_live_and_replay_compute_the_same_iv_rank():
    from oaa.backtest.ivmodel import IVModel
    from oaa.backtest.source import synthetic_bars

    bars = synthetic_bars("SPY", _dt.date(2025, 1, 1), _dt.date(2026, 6, 30), seed=7)
    model = IVModel()
    series = model.build(bars)
    iv = [v for v in series["iv"] if v is not None]
    replayed = series["iv_rank"][-1]
    window = [v for v in series["iv"][-(model.rank_lookback + 1):] if v is not None]
    assert replayed == iv_rank(iv[-1], window)


# --------------------------------------------------------------------------- #
# the live IV history store
# --------------------------------------------------------------------------- #
def test_twelve_scans_in_one_session_are_one_observation():
    store = IVHistoryStore()
    day = _dt.date(2026, 8, 28)
    for value in (0.20, 0.21, 0.22):
        store.observe("SPY", value, day=day)
    assert store.observations("SPY") == 1
    assert store.series("SPY") == [0.22]        # last read of the day wins


def test_the_history_survives_a_restart(tmp_path):
    store = IVHistoryStore.open(tmp_path)
    store.observe("SPY", 0.20, day=_dt.date(2026, 8, 27))
    store.observe("SPY", 0.22, day=_dt.date(2026, 8, 28))
    store.save()

    reopened = IVHistoryStore.open(tmp_path)
    assert reopened.series("SPY") == [0.20, 0.22]


def test_history_is_capped_at_a_trailing_year():
    store = IVHistoryStore(max_days=5)
    for i in range(20):
        store.observe("SPY", 0.20 + i / 100, day=_dt.date(2026, 1, 1) + _dt.timedelta(days=i))
    assert store.observations("SPY") == 5
    assert store.series("SPY")[-1] == 0.20 + 19 / 100


def test_seeding_gives_the_first_live_session_a_rankable_history():
    from oaa.backtest.source import synthetic_bars

    store = IVHistoryStore()
    bars = synthetic_bars("SPY", _dt.date(2025, 6, 1), _dt.date(2026, 6, 30), seed=3)
    added = store.seed_from_bars("SPY", bars)
    assert added >= IV_RANK_MIN_OBSERVATIONS
    assert not store.needs_seed("SPY")
    assert iv_rank(0.25, store.series("SPY")) is not None
    # today's real print is the observation for today - seeding must not claim it
    assert bars[-1]["timestamp"].date().isoformat() not in store._series["SPY"]


def test_a_real_observation_is_never_overwritten_by_a_modelled_one():
    from oaa.backtest.source import synthetic_bars

    bars = synthetic_bars("SPY", _dt.date(2025, 6, 1), _dt.date(2026, 6, 30), seed=3)
    day = bars[-5]["timestamp"].date()   # recent, so the year cap cannot trim it
    store = IVHistoryStore()
    store.observe("SPY", 9.99, day=day)
    store.seed_from_bars("SPY", bars)
    assert store._series["SPY"][day.isoformat()] == 9.99
