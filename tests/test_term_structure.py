"""The ATM IV term structure, and the guard that keeps it honest.

The signal is cheap. What is not cheap is the failure mode: in replay a
contract with no traded print falls back to the modelled surface, whose term
structure is `backtest.chain.term_slope` - a CONSTANT. A slope measured across
two modelled anchors is that config value read back out, and it would vote
identically on every candidate in every session while looking like a signal
doing work. Most of what follows tests the refusal, not the arithmetic.
"""

from __future__ import annotations

import datetime as dt

from oaa.core.types import Greeks, MarketContext, OptionQuote, Right
from oaa.data.term_structure import term_structure

ASOF = dt.date(2026, 8, 28)
SPOT = 500.0

REAL = "recovered from the traded price"
FAKE = "modelled (no bar)"


def quote(dte: int, strike: float, right: Right, iv: float, source: str | None = REAL):
    expiry = ASOF + dt.timedelta(days=dte)
    return OptionQuote(
        symbol=f"SPY{expiry:%y%m%d}{'C' if right is Right.CALL else 'P'}{int(strike*1000):08d}",
        underlying="SPY", expiry=expiry, strike=strike, right=right,
        bid=1.0, ask=1.1, last=1.05, implied_volatility=iv, iv_source=source,
        greeks=Greeks(delta=0.5),
    )


def ladder(front_iv: float, back_iv: float, front_dte=1, back_dte=30, source=REAL):
    """Two expiries, both rights, strikes either side of spot."""
    out = []
    for dte, iv in ((front_dte, front_iv), (back_dte, back_iv)):
        for strike in (495.0, 500.0, 505.0):
            for right in (Right.CALL, Right.PUT):
                # Skew the wings so an ATM-selection bug shows up as a wrong number.
                offset = 0.02 * abs(strike - SPOT) / 5.0
                out.append(quote(dte, strike, right, iv + offset, source))
    return out


# --------------------------------------------------------------------------- #
# the arithmetic
# --------------------------------------------------------------------------- #
def test_backwardation_and_contango_carry_the_signs_the_docs_claim():
    back = term_structure(ladder(0.28, 0.20), SPOT, ASOF)
    assert back is not None
    assert back.slope > 0 and back.backwardated
    assert back.slope_pct == round((0.28 - 0.20) / 0.20, 4)

    contango = term_structure(ladder(0.18, 0.24), SPOT, ASOF)
    assert contango is not None
    assert contango.slope < 0 and not contango.backwardated


def test_the_anchors_are_at_the_money_not_merely_on_the_expiry():
    """The wings are skewed 2 vol points per 5 strikes. Reading one would show."""
    ts = term_structure(ladder(0.20, 0.20), SPOT, ASOF)
    assert ts is not None
    assert ts.front_iv == 0.20 and ts.back_iv == 0.20
    assert ts.slope == 0.0


def test_slope_is_relative_so_it_is_comparable_across_a_12_vol_and_a_45_vol_name():
    cheap = term_structure(ladder(0.132, 0.120), SPOT, ASOF)
    rich = term_structure(ladder(0.495, 0.450), SPOT, ASOF)
    assert cheap is not None and rich is not None
    # Same 10% relative slope, very different absolute one.
    assert cheap.slope_pct == rich.slope_pct == 0.1
    assert cheap.slope != rich.slope


# --------------------------------------------------------------------------- #
# the refusals - the part that matters
# --------------------------------------------------------------------------- #
def test_a_modelled_anchor_is_not_a_measurement():
    """Both anchors modelled: the 'slope' is `backtest.chain.term_slope`."""
    assert term_structure(ladder(0.28, 0.20, source=FAKE), SPOT, ASOF) is None


def test_one_modelled_anchor_is_still_not_a_measurement():
    chain = (
        ladder(0.28, 0.20)[:6]                      # front, real
        + ladder(0.28, 0.20, source=FAKE)[6:]       # back, modelled
    )
    assert term_structure(chain, SPOT, ASOF) is None


def test_the_unmeasured_slope_is_available_but_labelled_as_such():
    """`require_measured=False` is for diagnostics. It must say what it is."""
    ts = term_structure(ladder(0.28, 0.20, source=FAKE), SPOT, ASOF, require_measured=False)
    assert ts is not None
    assert ts.measured is False
    assert "modelled" in ts.source


def test_expiries_too_close_together_are_one_maturity_not_a_slope():
    chain = ladder(0.28, 0.20, front_dte=1, back_dte=4)
    assert term_structure(chain, SPOT, ASOF, min_separation_days=7) is None
    assert term_structure(chain, SPOT, ASOF, min_separation_days=3) is not None


def test_a_single_expiry_cannot_produce_a_slope():
    chain = [q for q in ladder(0.28, 0.20) if (q.expiry - ASOF).days == 1]
    assert term_structure(chain, SPOT, ASOF) is None


def test_none_means_unanswerable_not_flat():
    """The distinction the whole design rests on. A caller reading None as 0.0
    would place a flat surface inside the vote band and manufacture a vote."""
    assert term_structure([], SPOT, ASOF) is None
    flat = term_structure(ladder(0.20, 0.20), SPOT, ASOF)
    assert flat is not None and flat.slope_pct == 0.0


def test_the_back_anchor_is_chosen_from_expiries_that_clear_the_separation():
    """A 25-day expiry sits nearer the 30-day target than a 21-day one, but if
    only the 21 clears the gap the 21 is the answer - not a refusal."""
    chain = ladder(0.28, 0.20, front_dte=1, back_dte=21)
    ts = term_structure(chain, SPOT, ASOF, back_dte=30, min_separation_days=7)
    assert ts is not None and ts.back_dte == 21


def test_the_context_field_defaults_to_none_rather_than_to_a_number():
    ctx = MarketContext(symbol="SPY", asof=dt.datetime(2026, 8, 28, 14, 0), spot=SPOT)
    assert ctx.term_structure is None


# --------------------------------------------------------------------------- #
# the two guards the first real run put here
# --------------------------------------------------------------------------- #
def test_zero_dte_is_never_the_front_anchor():
    """`front_dte` is a floor, not a target.

    Measured 30 Aug on real Alpaca bars: where the only listed expiries were
    0 DTE and 32 DTE, the front anchor landed on the 0 and the slope came back
    +257.9% on TLT and +621.4% on XLF. That is the pin and half the bid-ask
    inverted through Black-Scholes, not a forecast of the session.
    """
    chain = ladder(0.90, 0.20, front_dte=0, back_dte=32)
    assert term_structure(chain, SPOT, ASOF, front_dte=1) is None

    chain = ladder(0.90, 0.20, front_dte=0, back_dte=32) + ladder(
        0.22, 0.20, front_dte=2, back_dte=32
    )
    ts = term_structure(chain, SPOT, ASOF, front_dte=1)
    assert ts is not None and ts.front_dte == 2


def test_an_implausible_slope_is_unmeasurable_not_extreme():
    """Failing the band and being unmeasurable are the same for the VOTE and
    opposite for anyone reading the log to find out why."""
    ts = term_structure(ladder(1.50, 0.20), SPOT, ASOF, max_abs_slope_pct=1.0)
    assert ts is not None
    assert ts.measured is False
    assert "plausibility ceiling" in ts.source

    # A hard but real inversion still reads as a measurement.
    ts = term_structure(ladder(0.30, 0.20), SPOT, ASOF, max_abs_slope_pct=1.0)
    assert ts is not None and ts.measured is True
