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


# --------------------------------------------------------------------------- #
# reject_reason: the filter has to be able to explain itself
# --------------------------------------------------------------------------- #
def test_the_filter_names_the_config_line_that_removed_a_contract():
    """Twice this week an empty chain was reported as a market condition when it
    was our own config. `accepts` returning a bare bool is what made that
    possible - there was nothing to print."""
    import datetime as _dt

    from oaa.options.chain import ChainFilter
    from tests.conftest import make_quote

    asof = _dt.date(2026, 9, 1)
    expiry = _dt.date(2026, 9, 18)          # 17 DTE
    cf = ChainFilter(min_dte=3, max_dte=45, min_open_interest=250,
                     min_price=0.10, max_spread_pct=0.12)

    too_soon = make_quote(expiry=_dt.date(2026, 9, 2), oi=5000)
    assert "DTE window" in (cf.reject_reason(too_soon, asof) or "")

    too_cheap = make_quote(expiry=expiry, bid=0.01, ask=0.03, oi=5000)
    assert "min_price" in (cf.reject_reason(too_cheap, asof) or "")

    too_wide = make_quote(expiry=expiry, bid=1.00, ask=2.00, oi=5000)
    assert "max_spread_pct" in (cf.reject_reason(too_wide, asof) or "")

    thin = make_quote(expiry=expiry, oi=10)
    assert "open interest" in (cf.reject_reason(thin, asof) or "")

    good = make_quote(expiry=expiry, oi=5000)
    assert cf.reject_reason(good, asof) is None


def test_a_missing_open_interest_does_not_reject():
    """The live feed serves open_interest as null on every contract - the OI
    column of `oaa chain SPY` is all dashes. `min_open_interest` is therefore a
    no-op live, and any theory that blames it for an empty chain is wrong."""
    import datetime as _dt

    from oaa.options.chain import ChainFilter
    from tests.conftest import make_quote

    asof, expiry = _dt.date(2026, 9, 1), _dt.date(2026, 9, 18)
    unknown_oi = make_quote(expiry=expiry, oi=None)
    cf = ChainFilter(min_dte=3, max_dte=45, min_open_interest=250)
    assert cf.reject_reason(unknown_oi, asof) is None
    assert cf.accepts(unknown_oi, asof)


def test_accepts_still_agrees_with_reject_reason():
    import datetime as _dt

    from oaa.options.chain import ChainFilter
    from tests.conftest import make_quote

    asof, expiry = _dt.date(2026, 9, 1), _dt.date(2026, 9, 18)
    cf = ChainFilter(min_dte=3, max_dte=45, min_open_interest=250)
    for q in (make_quote(expiry=expiry, oi=5000),
              make_quote(expiry=expiry, oi=1),
              make_quote(expiry=_dt.date(2026, 9, 2), oi=5000)):
        assert cf.accepts(q, asof) is (cf.reject_reason(q, asof) is None)
