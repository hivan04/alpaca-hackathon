"""The cost model and the shared gates.

Paper trading charges none of this and fills at mid. The whole point of these
numbers is that the deck can show gross P&L, modelled cost and net side by
side rather than hoping nobody asks.
"""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.core.types import Intent, Leg, OptionQuote, Right, Side, StructureType, TradeIdea
from oaa.signals.gates import (
    entry_window_gate,
    gates_summary,
    relative_spread,
    round_trip_spread_cost,
    spread_gate,
    time_gate,
)
from oaa.telemetry.costs import CostModel


def quote(symbol: str, bid: float, ask: float) -> OptionQuote:
    return OptionQuote(
        symbol=symbol, underlying="SPY", expiry=dt.date(2026, 9, 11),
        strike=500.0, right=Right.CALL, bid=bid, ask=ask,
    )


def idea(bid: float = 1.98, ask: float = 2.02, legs: int = 1) -> TradeIdea:
    built = [
        Leg(
            symbol=f"SPY260911C0050{i}000",
            side=Side.BUY if i % 2 == 0 else Side.SELL,
            intent=Intent.BUY_TO_OPEN,
            quote=quote(f"SPY260911C0050{i}000", bid, ask),
        )
        for i in range(legs)
    ]
    return TradeIdea(
        symbol="SPY", strategy="test",
        structure=StructureType.SINGLE_LONG if legs == 1 else StructureType.IRON_CONDOR,
        legs=built, quantity=1, net_price=2.00, max_loss=200.0,
    )


# --------------------------------------------------------------------------- #
def test_the_spread_dwarfs_the_fee_load():
    """Crossing one 5c-wide quote once costs more than the regulatory fees on
    twenty iron condors. This is the fact the whole design turns on."""
    model = CostModel()
    wide = model.round_trip(idea(bid=1.975, ask=2.025))
    assert wide.spread > wide.regulatory * 20


def test_round_trip_fees_match_the_published_schedule():
    """OCC 2.5c + ORF 1.5c + CAT both ways, plus TAF on the sell."""
    model = CostModel()
    breakdown = model.round_trip(idea(bid=2.0, ask=2.0))
    per_contract = 2 * (0.025 + 0.015 + 0.0003) + 0.00329
    assert breakdown.regulatory == pytest.approx(per_contract + 0.0000206 * 200, abs=1e-4)
    assert breakdown.spread == 0.0


def test_index_exchange_fees_are_charged_on_top():
    model = CostModel(index_exchange_fees={"SPX": 0.66})
    plain = model.round_trip(idea())
    spx = idea()
    spx.symbol = "SPX"
    assert model.round_trip(spx).exchange > plain.exchange


def test_xsp_is_free_below_ten_contracts():
    """One-tenth SPX, cash settled, European exercise - so no early assignment
    risk, which removes an entire failure mode from short-premium structures."""
    model = CostModel(index_exchange_fees={"XSP": 0.0, "SPX": 0.66})
    xsp = idea()
    xsp.symbol = "XSP"
    assert model.round_trip(xsp).exchange == 0.0


def test_margin_interest_appears_in_the_attribution():
    model = CostModel()
    held = model.round_trip(idea(), held_days=7, margin_balance=10_000)
    assert held.margin_interest > 0
    assert held.total > model.round_trip(idea()).total


def test_net_of_costs_is_reported_alongside_gross():
    model = CostModel()
    breakdown = model.round_trip(idea(bid=1.95, ask=2.05))
    result = model.net_of_costs(120.0, breakdown)
    assert result["net_pnl"] < result["gross_pnl"]
    assert result["modelled_cost"] == breakdown.total


def test_breakeven_hit_rate_is_computable_not_assumed():
    assert CostModel().breakeven_hit_rate(0.10, 0.15) == pytest.approx(0.60, abs=0.01)
    assert CostModel().breakeven_hit_rate(0.15, 0.15) == pytest.approx(0.50)


# --------------------------------------------------------------------------- #
def test_the_spread_gate_rejects_a_wide_quote():
    result = spread_gate(idea(bid=1.80, ask=2.20), max_relative_spread=0.02)
    assert not result
    assert "ceiling" in result.reason


def test_the_spread_gate_rejects_when_cost_eats_the_target():
    tight = idea(bid=1.90, ask=2.10)
    assert relative_spread(tight) == pytest.approx(0.10, abs=0.01)
    result = spread_gate(tight, max_relative_spread=0.20, target_profit=20.0)
    assert not result
    assert round_trip_spread_cost(tight) == pytest.approx(20.0)


def test_time_gate_boundaries():
    def at(h, m):
        return dt.datetime(2026, 8, 28, h, m)

    assert not time_gate(at(9, 40))
    assert time_gate(at(10, 30))
    assert not time_gate(at(12, 0))
    assert time_gate(at(14, 0))
    assert not time_gate(at(14, 50))
    assert time_gate(at(12, 0), skip_lunch=False)


def test_entry_window_gate_stops_late_entries():
    late = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.timezone.utc)
    assert not entry_window_gate(late, "2026-09-02T20:00:00Z")
    assert entry_window_gate(late, None)


def test_gates_summary_names_the_first_veto():
    results = [
        spread_gate(idea(bid=1.99, ask=2.01), max_relative_spread=0.05),
        time_gate(dt.datetime(2026, 8, 28, 12, 0)),
    ]
    summary = gates_summary(results)
    assert summary["passed"] is False
    assert summary["vetoed_by"] == "time_of_day"
    assert "spread.relative_spread" in summary["metrics"]
