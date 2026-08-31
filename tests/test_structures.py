from __future__ import annotations

import datetime as dt

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


# --------------------------------------------------------------------------- #
# Wing width across a universe that spans an order of magnitude in price
# --------------------------------------------------------------------------- #
def _view(spot: float, step: float) -> ChainView:
    """Built directly, bypassing ChainFilter: these tests are about strike
    geometry, and the default liquidity filter thins the ladder to one strike."""
    import datetime as dt

    from tests.conftest import option_chain_for

    expiry = dt.date.today() + dt.timedelta(days=10)
    quotes = option_chain_for("TEST", spot, expiry, step=step, days=10.0, width=0.30)
    return ChainView(symbol="TEST", spot=spot, quotes=quotes, asof=dt.date.today())


def test_a_percent_wing_scales_with_the_underlying_price():
    """A flat 5-point wing is a few percent of a cheap ETF and under one percent
    of SPY. The same config line therefore produced structures with completely
    different max loss and delta depending only on share price - which is how
    GLD failed 24 times on 'wing width does not fit the strike grid' the moment
    the universe grew past mega-caps."""
    for spot, step in ((80.0, 1.0), (600.0, 5.0)):
        builder = StructureBuilder(view=_view(spot, step), strategy="t")
        idea = builder.iron_condor_by_delta(
            dte_range=(7, 14), short_put_delta=0.25, short_call_delta=0.25,
            wing_pct=0.015, wing_points=5,
        )
        strikes = sorted(leg.quote.strike for leg in idea.legs if leg.quote)
        width = strikes[1] - strikes[0]
        assert 0.005 * spot <= width <= 0.05 * spot, (
            f"spot {spot}: wing came out {width}, {width / spot:.1%} of spot"
        )


def test_a_wing_never_snaps_onto_the_short_strike():
    """`must_clear` is the point of the fix: on a coarse or filter-thinned
    ladder the nearest strike to `short - width` can BE the short strike, which
    is not a condor. One strike out is a narrower condor and strictly better
    than refusing to trade."""
    view = _view(300.0, 25.0)
    expiry = view.expiries()[0]
    short = view.by_delta(expiry, Right.PUT, 0.25)
    wing = view.strike_offset(expiry, Right.PUT, short.strike, -1.0, must_clear=True)
    assert wing.strike < short.strike


def test_the_wing_is_floored_at_one_rung_of_the_listed_ladder():
    """1.5% of a $50 ETF is $0.75, below a $1 strike increment - the wing then
    has nowhere to sit and the whole structure is declined. TLT and GLD were
    both losing trades to this. The floor is read off the actual chain, so an
    unusual or filter-thinned ladder is handled by observation."""
    spot = 50.0
    builder = StructureBuilder(view=_view(spot, 1.0), strategy="t")
    idea = builder.iron_condor_by_delta(
        dte_range=(7, 14), short_put_delta=0.25, short_call_delta=0.25,
        wing_pct=0.015, wing_points=5,          # 0.015 * 50 = 0.75 < one rung
    )
    strikes = sorted(leg.quote.strike for leg in idea.legs if leg.quote)
    assert strikes[1] - strikes[0] >= 1.0, "the wing collapsed inside one rung"
    assert strikes[3] - strikes[2] >= 1.0


# --------------------------------------------------------------------------- #
# The wing tests above build ChainView DIRECTLY, "bypassing ChainFilter ...
# the default liquidity filter thins the ladder to one strike". That is the
# condition production actually runs in, so the geometry was covered and the
# path that fails was not. These two go through the filter.
# --------------------------------------------------------------------------- #
def _filtered_view(spot: float, step: float = 1.0) -> ChainView:
    import datetime as dt

    from tests.conftest import option_chain_for

    expiry = dt.date.today() + dt.timedelta(days=10)
    quotes = option_chain_for("XLF", spot, expiry, step=step, days=10.0, width=0.30)
    return ChainView.from_quotes(
        "XLF", spot, quotes,
        # Production defaults for the two filters that bite on a cheap name.
        ChainFilter(min_dte=3, max_dte=45, min_price=0.10, max_spread_pct=0.12,
                    min_open_interest=0),
        asof=dt.date.today(),
    )


def test_a_cheap_underlying_can_still_build_a_condor_through_the_filter():
    """`min_price: 0.10` removes exactly the strikes a condor buys.

    A defined-risk wing is a cheap option BY CONSTRUCTION - that is what makes
    it a hedge rather than a second short. On a $52 underlying every strike
    beyond the 14-delta short prices under a dime, so the per-contract price
    floor deleted the entire outer ladder and `strike_offset(must_clear=True)`
    had nothing left to return. XLF declined 1,321 structures this way in the
    Jan-Aug run - every one of them reported as a wing WIDTH problem, which it
    never was.
    """
    view = _filtered_view(52.0)
    assert len(view.quotes) < len(view.all_quotes), (
        "fixture is not exercising the filter - nothing was stripped"
    )
    idea = StructureBuilder(view=view, strategy="t").iron_condor_by_delta(
        dte_range=(7, 14), short_put_delta=-0.14, short_call_delta=0.14,
        wing_pct=0.015, wing_points=5,
    )
    strikes = sorted(leg.quote.strike for leg in idea.legs if leg.quote)
    assert len(idea.legs) == 4
    assert strikes[0] < strikes[1] and strikes[2] < strikes[3], (
        "a wing landed on or inside its short strike"
    )
    assert idea.max_loss is not None and idea.max_loss > 0
    assert idea.structure.is_defined_risk


def test_the_unfiltered_pool_is_opt_in_only():
    """Short legs must never be chosen from the pre-filter pool - it exists so
    the PROTECTIVE leg can be bought, not so an illiquid strike can be sold."""
    view = _filtered_view(52.0)
    expiry = view.expiries()[0]
    outermost = max(q.strike for q in view.for_expiry(expiry, Right.CALL))
    # Without the opt-in there is nothing beyond the last filtered strike, so
    # the search falls back and cannot clear it.
    stays = view.strike_offset(expiry, Right.CALL, outermost, 1.0, must_clear=True)
    assert stays.strike <= outermost
    reaches = view.strike_offset(
        expiry, Right.CALL, outermost, 1.0, must_clear=True, allow_unfiltered=True
    )
    assert reaches.strike > outermost


# --------------------------------------------------------------------------- #
# the inverted condor body - claude/carry-chain-filter-guts-the-view.md
# --------------------------------------------------------------------------- #
def _thin_ladder_view(symbol="XLU", spot=43.0):
    """A chain whose CALL ladder ends below spot.

    This is what the per-contract tradeability filter leaves behind on a thin
    name: a condor sells the cheap, wide, thinly-held OTM strikes, which are
    exactly the ones `min_option_price` / `max_bid_ask_spread_pct` /
    `min_open_interest` remove. Greeks are absent, so `by_delta` falls through
    to `by_moneyness` - the unguarded nearest-strike search that returns a deep
    ITM call without complaint.
    """
    asof = dt.date(2026, 9, 1)
    expiry = dt.date(2026, 9, 18)
    quotes = [
        make_quote(root=symbol, expiry=expiry, strike=k, right=Right.PUT,
                   delta=None, bid=0.95, ask=1.05)
        for k in (42.5, 43.0, 43.5)
    ] + [
        # the OTM calls are gone; only these ITM ones survived the filter
        make_quote(root=symbol, expiry=expiry, strike=k, right=Right.CALL,
                   delta=None, bid=0.95, ask=1.05)
        for k in (34.0, 35.0)
    ] + [
        # cheap contracts that `min_price` removes from `quotes` but leaves in
        # the wing pool - which is why the WINGS resolve and the shorts do not.
        # That asymmetry is the bug; without these the builder would fail on
        # the wing lookup instead and never reach the inverted body.
        make_quote(root=symbol, expiry=expiry, strike=41.5, right=Right.CALL,
                   delta=None, bid=0.08, ask=0.12),
        make_quote(root=symbol, expiry=expiry, strike=42.0, right=Right.PUT,
                   delta=None, bid=0.08, ask=0.12),
    ]
    chain_filter = ChainFilter(
        min_dte=0, max_dte=400, min_open_interest=0, min_volume=0,
        max_spread_pct=float("inf"), min_price=0.50,
    )
    return ChainView.from_quotes(
        symbol=symbol, spot=spot, quotes=quotes,
        chain_filter=chain_filter, asof=asof,
    )


def test_an_inverted_condor_body_names_the_filter_not_the_strike_order():
    """XLU produced `iron condor strikes out of order: [43.0, 43.5, 35.0, 41.5]`
    live on 31 Aug. Both spreads were internally ordered; the call side simply
    sat below the put side because the filtered call ladder ended before spot.
    The old message described the symptom and sent you looking at the builder."""
    builder = StructureBuilder(view=_thin_ladder_view(), strategy="vol_carry")

    with pytest.raises(StrategyError) as excinfo:
        builder.iron_condor_by_delta(
            dte_range=(7, 30), short_put_delta=-0.16,
            short_call_delta=0.16, wing_points=1.0,
        )

    message = str(excinfo.value)
    assert "body is inverted" in message
    assert "filter result, not a listing gap" in message
    assert "strikes out of order" not in message, "that is the symptom, not the cause"
    assert "34-35 on calls" in message, "the message must show where the ladder ends"


def test_a_healthy_condor_body_is_still_built():
    """The guard must not cost a single valid structure. Same builder, a ladder
    that reaches both sides of spot."""
    asof, expiry = dt.date(2026, 9, 1), dt.date(2026, 9, 18)
    quotes = [
        # priced by distance from spot, so the shorts are worth more than the
        # wings and the structure is a genuine credit
        make_quote(root="SPY", expiry=expiry, strike=k, right=Right.PUT,
                   delta=None, bid=(k - 450) * 0.20 - 0.05,
                   ask=(k - 450) * 0.20 + 0.05)
        for k in (455, 460, 465, 470, 475, 480, 485)
    ] + [
        make_quote(root="SPY", expiry=expiry, strike=k, right=Right.CALL,
                   delta=None, bid=(550 - k) * 0.20 - 0.05,
                   ask=(550 - k) * 0.20 + 0.05)
        for k in (515, 520, 525, 530, 535, 540, 545)
    ]
    view = ChainView.from_quotes(
        symbol="SPY", spot=500.0, quotes=quotes,
        chain_filter=ChainFilter(min_dte=0, max_dte=400, min_open_interest=0,
                                 min_volume=0, max_spread_pct=float("inf"),
                                 min_price=0.0),
        asof=asof,
    )
    idea = StructureBuilder(view=view, strategy="vol_carry").iron_condor_by_delta(
        dte_range=(7, 30), short_put_delta=-0.16,
        short_call_delta=0.16, wing_points=5.0,
    )
    assert idea.structure is StructureType.IRON_CONDOR
    assert idea.is_credit
    strikes = [leg.quote.strike for leg in idea.legs]
    assert strikes == sorted(strikes), f"legs came back out of order: {strikes}"
