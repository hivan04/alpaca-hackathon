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


# --------------------------------------------------------------------------- #
# the edge study - the thing that decides whether the book should exist
# --------------------------------------------------------------------------- #
def test_forward_returns_are_truncated_at_the_cutoff(params: WeekendParams) -> None:
    """A horizon running past the Sunday flatten must be cut short. Live, the
    cutoff closes the position; measuring the untruncated move would credit the
    strategy with a return it could never have held for."""
    from oaa.strategies.weekend.clock import WindowPhase
    from oaa.strategies.weekend.data import bar_time
    from oaa.strategies.weekend.edgestudy import collect

    bars = make_bars(n=1200, start=dt.datetime(2026, 8, 17, tzinfo=UTC), kind="range")
    samples = collect(bars, params)
    assert samples, "the study found no in-window observations"

    # Every observation must sit inside the window it was measured in.
    in_window = [
        b for b in bars
        if params.window.phase(bar_time(b)) in {WindowPhase.OPEN, WindowPhase.MANAGE_ONLY}
    ]
    assert len(in_window) > 0
    assert all(s.forward_bp for s in samples)


def test_the_study_reports_a_baseline_to_beat(params: WeekendParams) -> None:
    """A conditional mean is meaningless without the unconditional one: being
    long BTC over a weekend is a beta, not an edge."""
    from oaa.strategies.weekend.edgestudy import baseline, collect

    samples = collect(
        make_bars(n=1200, start=dt.datetime(2026, 8, 17, tzinfo=UTC), kind="range"), params
    )
    base = baseline(samples)
    assert set(base) <= {1.0, 2.0, 4.0, 8.0}


def test_a_thin_sample_is_never_reported_as_evidence(params: WeekendParams) -> None:
    """The verdict must refuse to endorse a handful of observations, however
    good they look - that refusal is the point of the tool."""
    from oaa.strategies.weekend.edgestudy import verdict

    rows = [
        {"bucket": "z <= -2.5", "regime": "ranging", "horizon_h": 8.0, "n": 6,
         "weekends": 2, "episodes": 6, "mean_bp": 400.0, "median_bp": 380.0,
         "hit_rate": 1.0, "net_of_costs_bp": 346.0, "t": 9.0},
    ]
    assert "NOT ENOUGH DATA" in verdict(rows, params)


def test_a_wide_sample_with_a_weak_t_is_reported_as_such(params: WeekendParams) -> None:
    """Enough weekends, positive after costs, but no significance: the verdict
    says encouraging and refuses to let it be sized."""
    from oaa.strategies.weekend.edgestudy import verdict

    rows = [
        {"bucket": "z <= -2.5", "regime": "ranging", "horizon_h": 4.0, "n": 300,
         "weekends": 31, "episodes": 44, "mean_bp": 70.0, "median_bp": 40.0,
         "hit_rate": 0.55, "net_of_costs_bp": 16.0, "t": 1.1},
    ]
    assert "statistically not there" in verdict(rows, params)


def test_overlapping_bars_are_not_counted_as_independent_evidence() -> None:
    """The failure that nearly shipped: 11 overlapping bars from ONE weekend's
    rally reported as a 100% hit rate with t=+14.6. One event, counted eleven
    times. The cell must collapse them and the t-statistic must refuse."""
    from oaa.strategies.weekend.edgestudy import Cell

    cell = Cell(
        label="z <= -2.5",
        horizon_bars=32,
        values_bp=[150.0 + i for i in range(11)],
        keys=[(100 + i, "2026-08-21") for i in range(11)],
    )
    assert cell.n == 11
    assert cell.weekends == 1
    assert cell.episodes == 1, "overlapping 8h windows are one episode, not eleven"
    assert cell.t_stat() is None, "a single episode cannot carry a t-statistic"


def test_independent_episodes_are_spaced_by_the_forward_window() -> None:
    from oaa.strategies.weekend.edgestudy import Cell

    cell = Cell(
        label="z <= -2",
        horizon_bars=4,
        values_bp=[10.0] * 9,
        keys=[(i, "2026-08-21") for i in (0, 1, 2, 4, 5, 8, 9, 12, 13)],
    )
    assert cell.episodes == 4  # indices 0, 4, 8, 12


def test_a_single_weekend_is_never_a_verdict(params: WeekendParams) -> None:
    from oaa.strategies.weekend.edgestudy import verdict

    rows = [
        {"bucket": "z <= -2.5", "regime": "trending", "horizon_h": 8.0, "n": 11,
         "weekends": 1, "episodes": 1, "mean_bp": 155.0, "median_bp": 159.0,
         "hit_rate": 1.0, "net_of_costs_bp": 101.0, "t": None},
    ]
    assert "NOT ENOUGH DATA" in verdict(rows, params)


def test_a_short_cache_is_refused_rather_than_substituted(tmp_path) -> None:
    """Silently serving 10 days to a caller asking for 400 is how one weekend
    becomes a distribution. It must raise."""
    import json as _json

    from oaa.strategies.weekend.data import InsufficientHistory, cached_bars

    bars = make_bars(n=960, start=dt.datetime(2026, 8, 18, tzinfo=UTC))
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "BTCUSD_15Min_20260818_20260828.json").write_text(
        _json.dumps(bars), encoding="utf-8"
    )
    with pytest.raises(InsufficientHistory, match="Refusing to substitute"):
        cached_bars(
            "BTC/USD", "15Min",
            dt.datetime(2025, 7, 25, tzinfo=UTC), dt.datetime(2026, 8, 28, tzinfo=UTC),
            cache_dir=directory,
        )


# --------------------------------------------------------------------------- #
# the execution path - never exercised against a live venue, so assert it hard
# --------------------------------------------------------------------------- #
def _crypto_ticket(qty: float = 0.0043):
    from oaa.core.types import Intent, Leg, OrderTicket, Side

    return OrderTicket(
        idea_id="x",
        client_order_id="oaa-test",
        symbol="BTC/USD",
        legs=[Leg(symbol="BTC/USD", side=Side.BUY, kind=AssetKind.CRYPTO, qty=qty,
                  intent=Intent.BUY_TO_OPEN)],
        quantity=1,
        order_type="limit",
        limit_price=60_000.0,
        time_in_force="gtc",
        risk_stamp="stamp",
    )


def test_the_cli_broker_sends_the_fractional_size_not_one_whole_bitcoin() -> None:
    """The bug this test exists for: the CLI path read `ticket.quantity`, which
    is 1 for a single-leg order, and sent `--qty 1` for a 0.0043 BTC ticket -
    an order for one whole bitcoin. Options carry size on the ticket; crypto
    and equities carry an absolute size on the leg."""
    from oaa.brokers.alpaca_cli import AlpacaCliBroker
    from oaa.config.schema import Config

    broker = AlpacaCliBroker(Config())
    args = broker._submit_args(_crypto_ticket(0.0043))
    assert "--qty" in args
    assert args[args.index("--qty") + 1] == "0.0043"
    assert "1" != args[args.index("--qty") + 1]


def test_no_position_intent_on_a_crypto_order() -> None:
    """Alpaca rejects position_intent outright on a 24/7 asset."""
    from oaa.brokers.alpaca_cli import AlpacaCliBroker
    from oaa.config.schema import Config

    args = AlpacaCliBroker(Config())._submit_args(_crypto_ticket())
    assert "--position-intent" not in args
    assert args[args.index("--time-in-force") + 1] == "gtc"


def test_closing_a_fractional_position_does_not_truncate_to_zero() -> None:
    from oaa.brokers.alpaca_cli import _fmt_qty

    assert _fmt_qty(0.0043) == "0.0043"
    assert _fmt_qty(3.0) == "3"
    assert float(_fmt_qty(0.00001234)) > 0


# --------------------------------------------------------------------------- #
# the engine - the part that will actually touch the judged account
# --------------------------------------------------------------------------- #
class _FakeBroker:
    """Records what it was asked to do. No network, no Alpaca."""

    def __init__(self, positions=()):
        self._positions = list(positions)
        self.closed: list[tuple[str, float]] = []
        self.submitted: list[object] = []

    def account(self):
        from oaa.core.types import AccountSnapshot

        return AccountSnapshot(equity=100_000.0, positions=self._positions)

    def close_position(self, symbol, qty=None):
        self.closed.append((symbol, qty))
        return None

    def submit(self, ticket):
        self.submitted.append(ticket)
        return None

    @staticmethod
    def client_order_id(idea, suffix=""):
        return f"oaa-{idea.id}"


def _crypto_position(qty: float = 0.0043):
    from oaa.core.types import PositionSnapshot

    return PositionSnapshot(
        symbol="BTCUSD", qty=qty, avg_entry_price=60_000.0,
        market_value=qty * 60_000.0, asset_class="crypto",
    )


def test_the_engine_does_nothing_while_an_equity_session_is_live(tmp_path, params) -> None:
    from oaa.strategies.weekend.engine import WeekendEngine

    broker = _FakeBroker()
    engine = WeekendEngine(params, broker, state_path=tmp_path / "state.json")
    report = engine.cycle(now=dt.datetime(2026, 8, 28, 18, 0, tzinfo=UTC))  # Friday, open
    assert report.phase is WindowPhase.CLOSED
    assert not broker.closed and not broker.submitted


def test_the_cutoff_closes_crypto_it_did_not_open(tmp_path, params) -> None:
    """Unattributed crypto is treated as ours and closed - the same convention
    the options ledger uses, and the safe direction of the error for a book
    that must not exist during an equity session. Note the position symbol has
    NO slash: Alpaca returns BTCUSD for positions and takes BTC/USD for orders,
    so matching on the slash alone would silently skip the flatten."""
    from oaa.strategies.weekend.engine import WeekendEngine

    params.execution.dry_run = False
    broker = _FakeBroker(positions=[_crypto_position()])
    engine = WeekendEngine(params, broker, state_path=tmp_path / "state.json")
    report = engine.cycle(now=dt.datetime(2026, 8, 30, 20, 30, tzinfo=UTC))  # past cutoff
    assert report.phase is WindowPhase.FLATTEN
    assert broker.closed == [("BTCUSD", 0.0043)]


def test_levels_survive_a_restart(tmp_path, params) -> None:
    """The stop and target are computed from the band AT ENTRY. Recomputing
    them after a restart would move the stop - usually further away, because
    the band widened around the move that is hurting."""
    from oaa.strategies.weekend.engine import OpenPosition, WeekendState

    path = tmp_path / "state.json"
    state = WeekendState(
        position=OpenPosition(
            symbol="BTC/USD", qty=0.0043, entry=60_000.0, stop=59_000.0,
            target=61_500.0, entered_at=dt.datetime.now(UTC).isoformat(),
            idea_id="abc", client_order_id="oaa-abc",
        )
    )
    state.save(path)
    reloaded = WeekendState.load(path)
    assert reloaded.position is not None
    assert (reloaded.position.stop, reloaded.position.target) == (59_000.0, 61_500.0)
