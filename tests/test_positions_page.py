"""The Positions tab: the order-history mapper, and the failure isolation.

Both halves of the 1 Sep defect. `orders()` built its `underlying` column with
`parse_occ(symbol).underlying`, and `OccSymbol`'s field is `root` - so the
first option order raised AttributeError, `_fetch` caught it in the same try
block that had already read the account, and the judged account's positions
vanished behind one error line.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from oaa.app import positions as pos
from oaa.brokers.alpaca_rest import AlpacaRestBroker

OPTION = "SPY260904C00650000"


def _order(symbol: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        submitted_at=dt.datetime(2026, 9, 1, 14, 30),
        filled_at=None,
        side=SimpleNamespace(value="buy"),
        qty="1",
        filled_qty=kw.get("filled_qty", "1"),
        filled_avg_price=kw.get("filled_avg_price", "2.50"),
        status=SimpleNamespace(value="filled"),
        order_type=SimpleNamespace(value="limit"),
        id="oid",
        client_order_id="coid",
        legs=kw.get("legs"),
    )


# -- the mapper ---------------------------------------------------------- #

def test_an_option_order_maps_to_its_root_symbol() -> None:
    row = AlpacaRestBroker._order_row(_order(OPTION), False)
    assert row["underlying"] == "SPY"
    assert row["symbol"] == OPTION


def test_an_equity_or_crypto_order_keeps_its_own_symbol() -> None:
    assert AlpacaRestBroker._order_row(_order("AAPL"), False)["underlying"] == "AAPL"
    assert AlpacaRestBroker._order_row(_order("BTC/USD"), False)["underlying"] == "BTC/USD"


def test_only_option_notional_carries_the_hundred_multiplier() -> None:
    opt = AlpacaRestBroker._order_row(_order(OPTION), False)
    eq = AlpacaRestBroker._order_row(_order("AAPL"), False)
    assert opt["notional"] == 250.0
    assert eq["notional"] == 2.5


def test_one_unmappable_row_does_not_cost_the_rest_of_the_history() -> None:
    bad = _order(OPTION)
    del bad.symbol  # forces the row builder to raise

    class Stub(AlpacaRestBroker):
        def __init__(self) -> None:  # no network, no credentials
            pass

        # `client` is a property on the real broker, so it is overridden here
        # rather than assigned.
        @property
        def client(self) -> Any:  # type: ignore[override]
            return SimpleNamespace(get_orders=lambda _req: [bad, _order(OPTION)])

        @staticmethod
        def _order_row(item: Any, is_leg: bool) -> dict[str, Any]:
            if not hasattr(item, "symbol"):
                raise RuntimeError("unmappable")
            return AlpacaRestBroker._order_row(item, is_leg)

    rows = Stub().orders()
    assert len(rows) == 1
    assert rows[0]["underlying"] == "SPY"


# -- failure isolation in the page --------------------------------------- #

class _Broker:
    """An account that reads fine and an order history that does not."""

    def account(self) -> Any:
        return SimpleNamespace(positions=[
            SimpleNamespace(model_dump=lambda: {
                "symbol": OPTION, "underlying": "SPY", "qty": 1.0,
                "market_value": 250.0, "unrealized_pl": -12.0,
            })
        ])

    def orders(self, limit: int = 500) -> list[dict[str, Any]]:
        raise AttributeError("'OccSymbol' object has no attribute 'underlying'")


def test_a_broken_order_history_does_not_hide_the_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    import oaa.brokers.alpaca_rest as rest

    monkeypatch.setattr(rest, "AlpacaRestBroker", lambda *a, **k: _Broker())
    data = pos._fetch(SimpleNamespace(config=None, credentials=None))

    assert data["error"] is None, "the account read succeeded and must not be reported as failed"
    assert len(data["positions"]) == 1
    assert "underlying" in data["orders_error"]


def test_positions_render_without_a_pl_column_to_sort_on() -> None:
    frame = pos._positions_frame([{"symbol": OPTION, "qty": 1.0}])
    assert not frame.empty
    assert list(frame["symbol"]) == [OPTION]


def test_positions_are_sorted_worst_first_when_pl_is_present() -> None:
    frame = pos._positions_frame([
        {"symbol": "A", "unrealized_pl": 5.0},
        {"symbol": "B", "unrealized_pl": -9.0},
    ])
    assert list(frame["symbol"]) == ["B", "A"]


def test_an_empty_account_gives_an_empty_frame_not_an_error() -> None:
    assert pos._positions_frame([]).equals(pd.DataFrame())
