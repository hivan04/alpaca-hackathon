from __future__ import annotations

import datetime as dt

import pytest

from oaa.core.errors import DataError
from oaa.core.types import Right
from oaa.options.chain import ChainFilter, ChainView
from tests.conftest import make_quote


def test_filter_drops_wide_spreads(today):
    tight = make_quote(bid=1.00, ask=1.05, expiry=today + dt.timedelta(days=20))
    wide = make_quote(strike=505, bid=1.00, ask=3.00, expiry=today + dt.timedelta(days=20))
    view = ChainView.from_quotes("SPY", 500, [tight, wide], ChainFilter(max_spread_pct=0.10), today)
    assert [q.symbol for q in view.quotes] == [tight.symbol]


def test_filter_respects_dte_window(today):
    near = make_quote(expiry=today + dt.timedelta(days=2))
    good = make_quote(strike=505, expiry=today + dt.timedelta(days=20))
    far = make_quote(strike=510, expiry=today + dt.timedelta(days=200))
    view = ChainView.from_quotes("SPY", 500, [near, good, far],
                                 ChainFilter(min_dte=5, max_dte=60), today)
    assert [q.symbol for q in view.quotes] == [good.symbol]


def test_by_delta_picks_the_closest(chain, today):
    view = ChainView.from_quotes("SPY", 500.0, chain, ChainFilter(min_dte=1, max_dte=60), today)
    expiry = view.expiries()[0]
    pick = view.by_delta(expiry, Right.CALL, 0.16)
    assert pick.greeks.delta == pytest.approx(0.16, abs=0.12)
    assert pick.strike > 500  # a 16-delta call is out of the money


def test_by_delta_falls_back_when_greeks_are_missing(today):
    expiry = today + dt.timedelta(days=20)
    quotes = [
        make_quote(strike=float(k), right=Right.CALL, delta=None, expiry=expiry)
        for k in range(480, 525, 5)
    ]
    view = ChainView.from_quotes("SPY", 500.0, quotes, ChainFilter(min_dte=1, max_dte=60), today)
    pick = view.by_delta(expiry, Right.CALL, 0.16)
    assert pick.strike > 500  # degraded to a moneyness proxy, still OTM


def test_expiry_in_range_raises_when_nothing_fits(chain, today):
    view = ChainView.from_quotes("SPY", 500.0, chain, ChainFilter(min_dte=1, max_dte=60), today)
    with pytest.raises(DataError):
        view.expiry_in_range((200, 300))


def test_strike_offset_snaps_to_the_listed_grid(chain, today):
    view = ChainView.from_quotes("SPY", 500.0, chain, ChainFilter(min_dte=1, max_dte=60), today)
    expiry = view.expiries()[0]
    wing = view.strike_offset(expiry, Right.PUT, 480.0, -5)
    assert wing.strike == 475.0
