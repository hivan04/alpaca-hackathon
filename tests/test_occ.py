from __future__ import annotations

import datetime as dt

import pytest

from oaa.core.types import Right
from oaa.options.occ import build_occ, is_occ, parse_occ, underlying_of


def test_build_matches_alpacas_unpadded_format():
    assert build_occ("AAPL", dt.date(2026, 9, 18), Right.CALL, 250.0) == "AAPL260918C00250000"
    assert build_occ("SPY", dt.date(2026, 12, 18), Right.PUT, 600.0) == "SPY261218P00600000"


def test_roundtrip():
    symbol = build_occ("NVDA", dt.date(2026, 10, 16), Right.PUT, 137.5)
    parsed = parse_occ(symbol)
    assert parsed.root == "NVDA"
    assert parsed.expiry == dt.date(2026, 10, 16)
    assert parsed.right is Right.PUT
    assert parsed.strike == 137.5


def test_fractional_strikes_survive():
    assert build_occ("SPY", dt.date(2026, 9, 4), Right.CALL, 552.5).endswith("00552500")


def test_rejects_garbage():
    assert not is_occ("AAPL")
    with pytest.raises(ValueError):
        parse_occ("not-an-option")


def test_underlying_of_handles_both_shapes():
    assert underlying_of("AAPL260918C00250000") == "AAPL"
    assert underlying_of("aapl") == "AAPL"
