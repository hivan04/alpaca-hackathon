"""The pairwise-correlation panel's arithmetic.

The charts are not tested here - the numbers under them are. A correlation grid
is the kind of thing that looks plausible while being wrong (misaligned dates,
a symbol silently dropped, levels correlated where returns were meant), so each
of those failure modes gets a test.
"""

from __future__ import annotations

import math
import random

import pytest

pd = pytest.importorskip("pandas")

from oaa.app import correlation as corr  # noqa: E402


def _dates(n: int) -> list[str]:
    return [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]


def _path(n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    out = [100.0]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + rng.gauss(0, 0.01)))
    return out


def _closes(**series: list[float]) -> dict[str, list[tuple[str, float]]]:
    return {
        name: list(zip(_dates(len(values)), values, strict=True))
        for name, values in series.items()
    }


def test_identical_paths_correlate_at_one():
    base = _path(40, seed=3)
    frame = corr.returns_frame(corr.closes_frame(_closes(AAA=base, BBB=base)))
    matrix = corr.matrix(frame)
    assert math.isclose(matrix.loc["AAA", "BBB"], 1.0, abs_tol=1e-9)


def test_inverse_path_correlates_at_minus_one():
    base = _path(40, seed=5)
    mirrored = [200.0 - value for value in base]
    frame = corr.returns_frame(corr.closes_frame(_closes(AAA=base, BBB=mirrored)))
    assert corr.matrix(frame).loc["AAA", "BBB"] < -0.95


def test_symbols_are_aligned_on_shared_dates_only():
    """A symbol listed mid-window must not drag the join to nothing."""
    long_path = _path(40, seed=7)
    closes = _closes(AAA=long_path, BBB=long_path)
    closes["CCC"] = list(zip(_dates(40)[20:], long_path[20:], strict=True))
    prices = corr.closes_frame(closes)
    frame = corr.returns_frame(prices)
    assert len(frame) == 19          # 20 shared closes -> 19 returns
    assert set(corr.matrix(frame).columns) == {"AAA", "BBB", "CCC"}


def test_symbol_with_too_little_history_is_dropped_not_fatal():
    base = _path(40, seed=11)
    closes = _closes(AAA=base, BBB=base)
    closes["STUB"] = list(zip(_dates(3), base[:3], strict=True))
    prices = corr.closes_frame(closes)
    assert "STUB" not in prices.columns
    assert not corr.matrix(corr.returns_frame(prices)).empty


def test_returns_and_levels_disagree_on_two_trending_names():
    """The reason the panel defaults to returns rather than price levels."""
    up_a = [100 + i for i in range(60)]
    up_b = [50 + i * 0.5 + (1 if i % 2 else -1) for i in range(60)]
    prices = corr.closes_frame(_closes(AAA=up_a, BBB=up_b))
    levels = corr.matrix(prices).loc["AAA", "BBB"]
    returns = corr.matrix(corr.returns_frame(prices)).loc["AAA", "BBB"]
    assert levels > 0.9
    assert returns < levels


def test_pairs_table_lists_each_pair_once_ranked():
    closes = _closes(AAA=_path(40, 1), BBB=_path(40, 2), CCC=_path(40, 3))
    matrix = corr.matrix(corr.returns_frame(corr.closes_frame(closes)))
    table = corr.pairs_table(matrix, observations=39)
    assert len(table) == 3                                    # 3 choose 2
    assert table["pair"].is_unique
    assert list(table["correlation"]) == sorted(table["correlation"], reverse=True)
    assert (table["observations"] == 39).all()


def test_summary_ignores_the_diagonal():
    closes = _closes(AAA=_path(40, 1), BBB=_path(40, 2), CCC=_path(40, 3))
    matrix = corr.matrix(corr.returns_frame(corr.closes_frame(closes)))
    stats = corr.summary(matrix)
    assert stats["max"] < 1.0
    assert " / " in stats["max_pair"]
    assert stats["min"] <= stats["mean"] <= stats["max"]


@pytest.mark.parametrize("closes", [{}, {"AAA": []}, {"AAA": [("2026-01-01", 100.0)]}])
def test_empty_input_yields_empty_output_not_an_exception(closes):
    prices = corr.closes_frame(closes)
    matrix = corr.matrix(corr.returns_frame(prices))
    assert matrix.empty
    assert corr.pairs_table(matrix, 0).empty
    assert corr.summary(matrix) == {}


def test_one_symbol_alone_is_not_a_correlation():
    prices = corr.closes_frame(_closes(AAA=_path(40, 1)))
    assert corr.matrix(corr.returns_frame(prices)).empty


def test_underlying_closes_are_clipped_to_the_replayed_window():
    """Warmup bars must not leak into what the panel describes."""
    import datetime as dt
    from types import SimpleNamespace

    from oaa.backtest.runner import BacktestRequest, _underlying_closes

    bars = [
        {"timestamp": f"2026-03-{day:02d}T00:00:00Z", "close": 100.0 + day}
        for day in range(1, 11)
    ]
    source = SimpleNamespace(
        histories={"spy": SimpleNamespace(bars=bars), "EMPTY": SimpleNamespace(bars=[])}
    )
    request = BacktestRequest(
        symbols=["SPY"], start=dt.date(2026, 3, 4), end=dt.date(2026, 3, 8)
    )
    out = _underlying_closes(source, request)
    assert set(out) == {"SPY"}                                # upper-cased, empties dropped
    assert [day for day, _ in out["SPY"]] == [f"2026-03-{d:02d}" for d in range(4, 9)]
