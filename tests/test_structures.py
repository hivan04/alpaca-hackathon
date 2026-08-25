from __future__ import annotations

import pytest

from oaa.core.errors import StrategyError
from oaa.core.types import Right, Side, StructureType
from oaa.options.chain import ChainFilter, ChainView
from oaa.options.structures import StructureBuilder, build_iron_condor, build_vertical
from tests.conftest import make_quote


def test_vertical_debit_maths():
    long_leg = make_quote(strike=500, right=Right.CALL, bid=9.90, ask=10.10)   # mid 10
    short_leg = make_quote(strike=510, right=Right.CALL, bid=5.90, ask=6.10)   # mid 6
    idea = build_vertical(symbol="SPY", strategy="t", long_leg=long_leg, short_leg=short_leg)

    assert idea.structure is StructureType.VERTICAL_DEBIT
    assert idea.net_price == pytest.approx(4.0)
    assert idea.max_loss == pytest.approx(400.0)     # debit paid
    assert idea.max_profit == pytest.approx(600.0)   # width 10 - debit 4
    assert idea.is_credit is False
    assert idea.total_risk == pytest.approx(400.0)


def test_vertical_credit_maths():
    long_leg = make_quote(strike=520, right=Right.CALL, bid=1.90, ask=2.10)   # mid 2
    short_leg = make_quote(strike=510, right=Right.CALL, bid=4.90, ask=5.10)  # mid 5
    idea = build_vertical(symbol="SPY", strategy="t", long_leg=long_leg, short_leg=short_leg)

    assert idea.structure is StructureType.VERTICAL_CREDIT
    assert idea.net_price == pytest.approx(-3.0)
    assert idea.is_credit is True
    assert idea.max_profit == pytest.approx(300.0)
    assert idea.max_loss == pytest.approx(700.0)     # width 10 - credit 3


def test_iron_condor_is_credit_and_capped():
    idea = build_iron_condor(
        symbol="SPY", strategy="t",
        long_put=make_quote(strike=470, right=Right.PUT, bid=0.95, ask=1.05),
        short_put=make_quote(strike=475, right=Right.PUT, bid=1.95, ask=2.05),
        short_call=make_quote(strike=525, right=Right.CALL, bid=1.95, ask=2.05),
        long_call=make_quote(strike=530, right=Right.CALL, bid=0.95, ask=1.05),
    )
    assert idea.structure is StructureType.IRON_CONDOR
    assert idea.is_credit
    assert idea.net_price == pytest.approx(-2.0)      # credit 2.00
    assert idea.max_profit == pytest.approx(200.0)
    assert idea.max_loss == pytest.approx(300.0)      # width 5 - credit 2
    assert idea.meta["breakeven_low"] == pytest.approx(473.0)
    assert idea.meta["breakeven_high"] == pytest.approx(527.0)
    assert len(idea.legs) == 4


def test_iron_condor_rejects_out_of_order_strikes():
    with pytest.raises(StrategyError):
        build_iron_condor(
            symbol="SPY", strategy="t",
            long_put=make_quote(strike=490, right=Right.PUT),
            short_put=make_quote(strike=475, right=Right.PUT),
            short_call=make_quote(strike=525, right=Right.CALL),
            long_call=make_quote(strike=530, right=Right.CALL),
        )


def test_every_defined_risk_structure_reports_a_max_loss(chain, today):
    view = ChainView.from_quotes("SPY", 500.0, chain, ChainFilter(min_dte=1, max_dte=60), today)
    builder = StructureBuilder(view=view, strategy="t")
    idea = builder.iron_condor_by_delta(
        dte_range=(10, 40), short_put_delta=-0.16, short_call_delta=0.16, wing_points=5
    )
    assert idea.max_loss is not None and idea.max_loss > 0
    assert idea.structure.is_defined_risk
    # Legs are ordered long put, short put, short call, long call.
    assert [leg.side for leg in idea.legs] == [Side.BUY, Side.SELL, Side.SELL, Side.BUY]


def test_multileg_never_exceeds_alpacas_four_leg_cap(chain, today):
    view = ChainView.from_quotes("SPY", 500.0, chain, ChainFilter(min_dte=1, max_dte=60), today)
    idea = StructureBuilder(view=view, strategy="t").iron_condor_by_delta(
        dte_range=(10, 40), short_put_delta=-0.16, short_call_delta=0.16, wing_points=5
    )
    assert len(idea.legs) <= 4
