"""Aggregate Greek exposure, and the units it is measured in.

`risk.max_net_delta`, `risk.max_net_vega` and `risk.max_notional_per_trade_pct`
were configured, documented and enforced nowhere until 30 Aug. These tests pin
the arithmetic, and above all the UNITS - the first implementation divided vega
by 100 a second time, which reported a net vega of exactly $0.00 per point on
every evaluation in a 44-trade run. That reads as "no vol exposure", not as a
unit error, and the cap on top of it could never have fired.
"""

from __future__ import annotations

import datetime as dt

from oaa.core.types import (
    AccountSnapshot,
    Greeks,
    Intent,
    Leg,
    MarketContext,
    OptionQuote,
    PositionSnapshot,
    Right,
    Side,
    StructureType,
    TradeIdea,
)
from oaa.risk.exposure import book_exposure, idea_exposure, normalised

SPOT = 500.0
EQUITY = 100_000.0


def quote(symbol="SPY260904C00500000", delta=0.5, vega=0.6, strike=SPOT):
    return OptionQuote(
        symbol=symbol, underlying="SPY", expiry=dt.date(2026, 9, 4), strike=strike,
        right=Right.CALL, bid=5.0, ask=5.1, last=5.05, implied_volatility=0.18,
        greeks=Greeks(delta=delta, vega=vega, gamma=0.01, theta=-0.05),
    )


def idea(legs, structure=StructureType.SINGLE_LONG, quantity=1):
    return TradeIdea(
        symbol="SPY", strategy="intraday_momentum", structure=structure,
        legs=legs, quantity=quantity, net_price=5.05, max_loss=505.0,
    )


def leg(q, side=Side.BUY, ratio=1):
    return Leg(symbol=q.symbol, side=side, ratio=ratio, quote=q,
               intent=Intent.opening(side))


# --------------------------------------------------------------------------- #
# units
# --------------------------------------------------------------------------- #
def test_delta_is_dollars_per_one_dollar_move():
    """1 contract, 0.50 delta, $500 spot -> 50 shares -> $25,000 of exposure."""
    exp = idea_exposure(idea([leg(quote(delta=0.5))]), quantity=1)
    assert exp is not None
    assert exp.dollar_delta == 0.5 * 1 * 100 * SPOT == 25_000.0


def test_vega_is_dollars_per_vol_POINT_and_is_not_divided_twice():
    """`bs_greeks` already returns per-point vega - it divides by 100 itself.

    0.60 per share x 100 shares = $60 per point for one contract. The bug this
    pins reported $0.60, and across a whole book that rounded to 0.000.
    """
    exp = idea_exposure(idea([leg(quote(vega=0.6))]), quantity=1)
    assert exp is not None
    assert exp.vega == 60.0
    assert exp.vega != 0.6           # the double-division result
    per_10k = normalised(exp, EQUITY)["vega_ratio"]
    assert per_10k == 6.0            # $60 over ten $10k units of equity


def test_delta_ratio_is_a_fraction_of_equity():
    exp = idea_exposure(idea([leg(quote(delta=0.5))]), quantity=1)
    assert normalised(exp, EQUITY)["delta_ratio"] == 0.25


# --------------------------------------------------------------------------- #
# the point of the whole thing: opposing risk nets, correlated risk stacks
# --------------------------------------------------------------------------- #
def test_a_short_leg_subtracts():
    long_call = quote("SPY260904C00500000", delta=0.5, vega=0.6)
    short_call = quote("SPY260904C00510000", delta=0.3, vega=0.4, strike=510.0)
    exp = idea_exposure(
        idea([leg(long_call), leg(short_call, Side.SELL)],
             structure=StructureType.VERTICAL_DEBIT),
        quantity=1, spot=SPOT,
    )
    assert exp is not None
    # 0.5 - 0.3 = 0.2 net delta; vega 0.6 - 0.4 = 0.2 -> $20/pt
    assert round(exp.dollar_delta, 2) == round(0.2 * 100 * SPOT, 2)
    assert round(exp.vega, 2) == 20.0


def test_every_leg_is_priced_off_ONE_spot():
    """Each leg against its own strike-as-spot made a 500/510 vertical measure
    $9,700 of delta instead of $10,000 - an error that grows with the strike
    separation, and compounds on a condor with wings. Same defect in miniature
    as the mixed-surface marking fixed on 27 Aug."""
    legs = [
        leg(quote("SPY260904C00500000", delta=0.5, vega=0.6)),
        leg(quote("SPY260904C00510000", delta=0.3, vega=0.4, strike=510.0), Side.SELL),
    ]
    supplied = idea_exposure(idea(legs, StructureType.VERTICAL_DEBIT), 1, spot=SPOT)
    proxy = idea_exposure(idea(legs, StructureType.VERTICAL_DEBIT), 1)
    assert supplied is not None and proxy is not None
    # The proxy uses the MEDIAN strike (505) for BOTH legs, so it is a level
    # error only - the netting is identical and stays proportional.
    assert round(proxy.dollar_delta, 2) == round(0.2 * 100 * 505.0, 2)
    assert abs(proxy.dollar_delta - supplied.dollar_delta) / supplied.dollar_delta < 0.02


def test_correlated_longs_stack_where_a_position_count_sees_nothing():
    """Four separate structures, four different tickers, one bet.

    `max_positions`, `max_positions_per_underlying` and `duplicate_structure`
    all pass this. Delta is the only thing that sees it.
    """
    positions, contexts = [], {}
    for name in ("SPY", "QQQ", "IWM", "DIA"):
        sym = f"{name}260904C00500000"
        q = quote(sym, delta=0.5, vega=0.6)
        q = q.model_copy(update={"underlying": name})
        positions.append(PositionSnapshot(
            symbol=sym, qty=2, avg_entry_price=5.0, market_value=1000.0,
            underlying=name,
        ))
        contexts[name] = MarketContext(
            symbol=name, asof=dt.datetime(2026, 8, 28, 15, 0), spot=SPOT, chain=[q],
        )
    account = AccountSnapshot(equity=EQUITY, positions=positions)
    exp = book_exposure(account, contexts)
    assert exp.matched == 4 and exp.complete
    # 4 names x 2 contracts x 0.5 delta x 100 x $500 = $200,000 -> 2.0x equity
    assert normalised(exp, EQUITY)["delta_ratio"] == 2.0


def test_a_short_position_is_signed_by_the_broker_quantity():
    sym = "SPY260904C00500000"
    account = AccountSnapshot(equity=EQUITY, positions=[PositionSnapshot(
        symbol=sym, qty=-3, avg_entry_price=5.0, market_value=-1500.0, underlying="SPY",
    )])
    contexts = {"SPY": MarketContext(
        symbol="SPY", asof=dt.datetime(2026, 8, 28, 15, 0), spot=SPOT,
        chain=[quote(sym, delta=0.5, vega=0.6)],
    )}
    exp = book_exposure(account, contexts)
    assert exp.dollar_delta < 0 and exp.vega < 0      # short premium is short vega


# --------------------------------------------------------------------------- #
# coverage - a partial read must never present as a whole one
# --------------------------------------------------------------------------- #
def test_a_position_with_no_chain_is_unmatched_not_dropped():
    """Dropping it reports a smaller book than exists, and a cap on an
    understated book is a cap that does not bind."""
    account = AccountSnapshot(equity=EQUITY, positions=[
        PositionSnapshot(symbol="TLT260904C00090000", qty=5, avg_entry_price=1.0,
                         market_value=500.0, underlying="TLT"),
    ])
    exp = book_exposure(account, contexts={})
    assert exp.unmatched == 1
    assert exp.matched == 0
    assert exp.coverage == 0.0
    assert not exp.complete
    assert exp.missing == ["TLT"]


def test_an_idea_whose_legs_carry_no_greeks_is_unmeasurable_not_zero():
    q = quote()
    q = q.model_copy(update={"greeks": Greeks(delta=None, vega=None)})
    assert idea_exposure(idea([leg(q)]), quantity=1) is None


def test_equity_of_zero_does_not_divide_by_zero():
    exp = idea_exposure(idea([leg(quote())]), quantity=1)
    assert normalised(exp, 0.0) == {
        "delta_ratio": 0.0, "vega_ratio": 0.0, "notional_ratio": 0.0
    }
