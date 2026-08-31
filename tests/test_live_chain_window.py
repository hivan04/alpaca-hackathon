"""The live chain has to contain the contracts the live books trade.

Regression for `claude/live-chain-defect-confirmed.md`. Both live providers
built the context chain from `options.min_days_to_expiry` (3) while
`intraday_momentum` filters for 0-2 DTE, so its `ChainView` was empty on every
symbol of every cycle. `builder()` raises "no contracts survived the liquidity
filter" on an empty view, which reads as a liquidity problem and is actually an
empty shelf - the message that put a whole session's judged run at zero trades.

Replay never had the bug: `tradable_dte_range` unions what the enabled
strategies declare. These tests pin that the live path asks the same question.
"""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.backtest.runner import tradable_dte_range
from oaa.config.loader import load_config
from oaa.core.types import Greeks, OptionQuote, Right


def _quote(days: int) -> OptionQuote:
    """A quote with no IV on purpose - it keeps `context()` off the IV-history
    save path, so these tests touch no disk and no network."""
    return OptionQuote(
        symbol=f"SPY{days}",
        underlying="SPY",
        expiry=dt.date.today() + dt.timedelta(days=days),
        strike=500.0,
        right=Right.CALL,
        bid=1.0,
        ask=1.1,
        last=1.05,
        implied_volatility=None,
        greeks=Greeks(),
    )


class _Recorder:
    """Mixin that stubs every network call a provider's `context()` makes."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.asked: list[tuple[int | None, int | None]] = []

    def spot(self, symbol: str) -> float:
        return 500.0

    def bars(self, symbol, lookback_days=90, timeframe="1Day"):
        base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)
        return [
            {
                "timestamp": base + dt.timedelta(days=i),
                "open": 500.0, "high": 501.0, "low": 499.0,
                "close": 500.0 + (i % 3), "volume": 1_000_000.0,
            }
            for i in range(60)
        ]

    def option_chain(self, symbol, min_dte=None, max_dte=None,
                     strike_low=None, strike_high=None):
        self.asked.append((min_dte, max_dte))
        return [_quote(1), _quote(10)]


def _live_providers(cfg):
    from oaa.data.alpaca_data import AlpacaDataProvider
    from oaa.data.cli_data import AlpacaCliDataProvider

    out = []
    for base in (AlpacaDataProvider, AlpacaCliDataProvider):
        cls = type(f"Recording{base.__name__}", (_Recorder, base), {})
        out.append((base.__name__, cls(cfg)))
    return out


# --------------------------------------------------------------------------- #
def test_the_provider_window_is_the_tradable_band_not_the_envelope():
    """The helper both live providers use answers the same question replay does."""
    from oaa.data.base import MarketDataProvider

    cfg = load_config()
    window = MarketDataProvider.context_chain_window(
        type("Stub", (), {"cfg": cfg})()
    )
    assert window == tradable_dte_range(cfg)
    assert window[0] == 0, "the intraday book buys the front expiry"
    assert window[0] < cfg.options.min_days_to_expiry


@pytest.mark.parametrize("name", ["AlpacaDataProvider", "AlpacaCliDataProvider"])
def test_a_live_context_requests_a_chain_the_intraday_book_can_trade(name):
    """The defect, stated as a test. On the unfixed code the recorded window is
    (None, None) - i.e. the 3-45 envelope - and the assertion on 0 DTE fails."""
    cfg = load_config()
    provider = dict(_live_providers(cfg))[name]
    provider.context("SPY")

    assert provider.asked, f"{name}.context() never requested a chain"
    min_dte, max_dte = provider.asked[0]
    assert min_dte is not None and max_dte is not None, (
        "the chain was requested with the global envelope, which contains "
        "nothing the intraday book can trade"
    )
    assert min_dte == 0
    assert (min_dte, max_dte) == tradable_dte_range(cfg)


@pytest.mark.parametrize("name", ["AlpacaDataProvider", "AlpacaCliDataProvider"])
def test_the_live_window_covers_every_enabled_strategys_declared_window(name):
    """The general form: whatever a book declares it must SEE, the live chain
    request has to cover. A new strategy with a window outside the envelope
    fails here rather than silently taking zero trades for a week."""
    from oaa.strategies.base import load_strategies

    cfg = load_config()
    provider = dict(_live_providers(cfg))[name]
    provider.context("SPY")
    low, high = provider.asked[0]

    declared = [
        s.chain_dte_window() for s in load_strategies(cfg)
        if s.chain_dte_window() is not None
    ]
    assert declared, "no strategy declares a window - this test proves nothing"
    assert low is not None and high is not None, (
        f"{name} requested the global envelope, not a strategy-derived window"
    )
    for want_low, want_high in declared:
        assert low <= want_low, f"{name} chain starts at {low}, book needs {want_low}"
        assert high >= want_high, f"{name} chain ends at {high}, book needs {want_high}"
