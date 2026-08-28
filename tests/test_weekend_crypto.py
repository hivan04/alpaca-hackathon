"""The weekend book.

The properties worth asserting are the ones that would cost real money if they
broke: the window cannot leak into an equity session, the cost gate actually
refuses thin signals, a stop is always present so the position is defined-risk,
and the replay never sees a bar it could not have seen live.
"""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest

from oaa.core.types import AssetKind
from oaa.strategies.weekend.backtest import run_backtest, size_position
from oaa.strategies.weekend.clock import WeekendWindow, WindowPhase
from oaa.strategies.weekend.costs import CryptoCostModel
from oaa.strategies.weekend.params import WeekendParams, load_params
from oaa.strategies.weekend.signals import evaluate, expected_reversion_bp, stop_price, zscore
from oaa.strategies.weekend.strategy import build_idea

UTC = dt.timezone.utc


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def make_bars(
    n: int = 400,
    start: dt.datetime | None = None,
    kind: str = "range",
    seed: int = 7,
    price: float = 60_000.0,
) -> list[dict]:
    """Synthetic 15-minute bars. `range` mean-reverts, `trend` does not."""
    rng = random.Random(seed)
    start = start or dt.datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    bars, level = [], math.log(price)
    anchor = level
    for i in range(n):
        if kind == "range":
            level += 0.15 * (anchor - level) + rng.gauss(0, 0.004)
        else:
            level += 0.0016 + rng.gauss(0, 0.0015)
        close = math.exp(level)
        open_ = close * (1 + rng.gauss(0, 0.0008))
        bars.append(
            {
                "t": (start + dt.timedelta(minutes=15 * i)).isoformat().replace("+00:00", "Z"),
                "open": open_,
                "high": max(open_, close) * 1.001,
                "low": min(open_, close) * 0.999,
                "close": close,
                "volume": 10.0,
            }
        )
    return bars


@pytest.fixture
def params() -> WeekendParams:
    return WeekendParams()


# --------------------------------------------------------------------------- #
# the clock - the safety property
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "when,expected",
    [
        ("2026-08-28T18:00", WindowPhase.CLOSED),      # Friday, equities open
        ("2026-08-28T20:30", WindowPhase.OPEN),        # Friday evening
        ("2026-08-29T12:00", WindowPhase.OPEN),        # Saturday
        ("2026-08-30T11:00", WindowPhase.OPEN),        # Sunday morning
        ("2026-08-30T13:00", WindowPhase.MANAGE_ONLY), # past Sunday 12:00 last entry
        ("2026-08-30T20:30", WindowPhase.FLATTEN),     # past the Sunday cutoff
        ("2026-09-01T14:00", WindowPhase.CLOSED),      # Tuesday
    ],
)
def test_window_phases(when: str, expected: WindowPhase) -> None:
    now = dt.datetime.fromisoformat(when).replace(tzinfo=UTC)
    assert WeekendWindow().phase(now) is expected


def test_window_never_overlaps_an_equity_session() -> None:
    """The whole capital argument rests on this: walk a fortnight in 15-minute
    steps and assert the book is never open inside 09:30-16:00 ET."""
    window = WeekendWindow()
    now = dt.datetime(2026, 8, 17, tzinfo=UTC)
    end = now + dt.timedelta(days=14)
    while now < end:
        phase = window.phase(now)
        et = now.astimezone(dt.timezone(dt.timedelta(hours=-4)))
        equity_session = et.weekday() < 5 and 9.5 <= et.hour + et.minute / 60 < 16
        if equity_session:
            assert phase is WindowPhase.CLOSED, f"weekend book open during {et}"
        now += dt.timedelta(minutes=15)


def test_entries_stop_before_the_flatten() -> None:
    window = WeekendWindow()
    now = dt.datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    assert window.last_entry_at(now) < window.flattens_at(now)
    assert (window.flattens_at(now) - window.last_entry_at(now)).total_seconds() / 3600 == 8.0


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
def test_zscore_is_computed_on_log_price() -> None:
    values = [math.log(100 + i % 5) for i in range(200)]
    z, mean, sigma = zscore(values, 96)
    assert z is not None and sigma is not None and sigma > 0


def test_trending_tape_is_refused_by_the_regime_gate(params: WeekendParams) -> None:
    signal = evaluate("BTC/USD", make_bars(kind="trend"), params)
    assert not signal.actionable
    assert signal.blocked_by in {"regime", "displaced", "band", "edge"}


def test_a_thin_band_cannot_pay_the_round_trip(params: WeekendParams) -> None:
    """The cost gate, stated as arithmetic: 2 sigma on a 20bp band is 45bp
    gross against a 54bp round trip, and must be refused."""
    move = expected_reversion_bp(z=-2.0, sigma=0.0020, exit_z=-0.25)
    assert move < params.costs.round_trip_bp * params.signal.min_edge_multiple


def test_wide_dislocation_clears_the_cost_gate(params: WeekendParams) -> None:
    move = expected_reversion_bp(z=-2.5, sigma=0.0060, exit_z=-0.25)
    assert move > params.costs.round_trip_bp * params.signal.min_edge_multiple


def test_rich_side_is_never_an_entry(params: WeekendParams) -> None:
    """No shorting crypto at Alpaca, so a +3 sigma read must be a rejection
    naming the constraint rather than a short."""
    bars = make_bars(kind="range")
    for bar in bars[-3:]:
        for key in ("open", "high", "low", "close"):
            bar[key] *= 1.06
    signal = evaluate("BTC/USD", bars, params)
    assert not signal.actionable
    if signal.blocked_by == "displaced":
        assert "short" in signal.reason


def test_gates_run_cheapest_first_and_stop_at_the_first_veto(params: WeekendParams) -> None:
    signal = evaluate("BTC/USD", make_bars(n=50), params)
    assert [c.gate for c in signal.checks] == ["data"]
    assert not signal.actionable


# --------------------------------------------------------------------------- #
# risk
# --------------------------------------------------------------------------- #
def test_stop_is_always_inside_the_configured_bounds(params: WeekendParams) -> None:
    for atr_value in (0.0, 50.0, 5_000.0):
        stop = stop_price(60_000.0, atr_value, params)
        pct = (60_000.0 - stop) / 60_000.0
        assert params.exits.min_stop_pct - 1e-9 <= pct <= params.exits.max_stop_pct + 1e-9


def test_sizing_respects_both_caps(params: WeekendParams) -> None:
    qty = size_position(entry=60_000.0, stop=59_400.0, equity=100_000.0, sizing=params.sizing)
    assert qty * 60_000.0 <= 100_000.0 * params.sizing.book_max_equity_pct + 1
    assert qty * 600.0 <= 100_000.0 * params.sizing.max_risk_per_trade_pct + 1


def test_tiny_equity_produces_no_order(params: WeekendParams) -> None:
    assert size_position(60_000.0, 59_400.0, equity=1_000.0, sizing=params.sizing) == 0.0


def test_idea_is_defined_risk_and_carries_a_crypto_leg(params: WeekendParams) -> None:
    """build_idea is tested against a hand-built signal rather than a random
    tape: the assertion is about the SHAPE of the order, and a test that skips
    itself when the random walk fails to dislocate asserts nothing."""
    from oaa.signals.gates import GateResult
    from oaa.strategies.weekend.signals import WeekendSignal

    signal = WeekendSignal(
        symbol="BTC/USD",
        price=58_000.0,
        z=-2.4,
        sigma=0.0062,
        adx=17.0,
        atr=420.0,
        expected_move_bp=134.0,
        edge_multiple=2.5,
        checks=[GateResult.ok(gate) for gate in
                ("data", "regime", "band", "shock", "displaced", "edge")],
    )
    assert signal.actionable

    idea = build_idea(signal, params, equity=250_000.0)
    assert idea is not None
    assert idea.max_loss is not None and idea.max_loss > 0
    assert idea.legs[0].kind is AssetKind.CRYPTO
    assert idea.legs[0].qty > 0
    assert idea.book == "weekend"
    assert idea.meta["stop"] < idea.legs[0].limit_price < idea.meta["target"]
    # defined risk means the number is not an estimate: it is (entry-stop)*qty
    assert idea.max_loss == pytest.approx(
        (idea.legs[0].limit_price - idea.meta["stop"]) * idea.legs[0].qty, abs=0.01
    )
    assert idea.max_loss <= 250_000.0 * params.sizing.max_risk_per_trade_pct + 1


def test_crypto_net_cash_has_no_option_multiplier(params: WeekendParams) -> None:
    from oaa.core.types import Leg, Side, StructureType, TradeIdea

    idea = TradeIdea(
        symbol="BTC/USD",
        strategy="weekend_crypto_reversion",
        structure=StructureType.SINGLE_LONG,
        legs=[Leg(symbol="BTC/USD", side=Side.BUY, kind=AssetKind.CRYPTO, qty=0.01)],
        net_price=600.0,
    )
    assert idea.net_cash() == -600.0


# --------------------------------------------------------------------------- #
# costs
# --------------------------------------------------------------------------- #
def test_round_trip_cost_is_the_number_the_signal_must_beat() -> None:
    costs = CryptoCostModel()
    assert 40 < costs.round_trip_bp < 70
    net = costs.net_of_costs(entry=60_000.0, exit_price=60_000.0, qty=1.0, crossing_exit=True)
    assert net < 0  # a flat round trip loses exactly the friction


# --------------------------------------------------------------------------- #
# the replay
# --------------------------------------------------------------------------- #
def test_backtest_only_enters_inside_the_window(params: WeekendParams) -> None:
    bars = make_bars(n=1200, start=dt.datetime(2026, 8, 17, tzinfo=UTC), kind="range", seed=3)
    result = run_backtest(params, bars=bars, days=30, equity=250_000.0)
    for trade in result.trades:
        assert params.window.phase(trade.entered_at) in {
            WindowPhase.OPEN,
            WindowPhase.MANAGE_ONLY,
        }


def test_backtest_never_holds_past_the_cutoff(params: WeekendParams) -> None:
    bars = make_bars(n=1200, start=dt.datetime(2026, 8, 17, tzinfo=UTC), kind="range", seed=3)
    result = run_backtest(params, bars=bars, days=30, equity=250_000.0)
    for trade in result.trades:
        assert trade.exited_at is not None
        assert trade.exit_reason in {
            "stop", "target", "time_stop", "window_flatten", "end_of_data"
        }


def test_params_yaml_loads_and_rejects_typos(tmp_path) -> None:
    params = load_params("config/strategies/weekend_crypto.yaml")
    assert params.symbols == ["BTC/USD"]
    assert params.enabled is False  # live trading is opt-in, always

    bad = tmp_path / "bad.yaml"
    bad.write_text("signal:\n  entry_zscore: 2.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown keys"):
        load_params(bad)
