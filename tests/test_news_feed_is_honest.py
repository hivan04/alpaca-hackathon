"""The news feed must tell the truth about itself.

Two defects found on 30 Aug, both in the path the earnings watch depends on:

  1. `CliMarketData.news` swallowed `DataError` and returned `[]` at DEBUG. A
     dead news CLI therefore made every watched name report as *quiet* -
     indistinguishable from a stock with genuinely no news. The watch's own
     docstring names that failure as the one it exists to avoid.
  2. `news` took no `start`, so `alpaca_news_fetcher`'s keyword call raised
     TypeError, the fallback re-called it bare, and the events book's
     `news_lookback_days: 7` was silently replaced by the global
     `news_lookback_hours: 6`.
"""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.core.errors import DataError
from oaa.data.cli_data import AlpacaCliDataProvider
from oaa.strategies.events.params import load_params
from oaa.strategies.events.sentiment import alpaca_news_fetcher, gather


class _Feed(AlpacaCliDataProvider):
    """An AlpacaCliDataProvider whose only subprocess is a recorder."""

    def __init__(self, cfg, rows=None, fail=False):
        super().__init__(cfg)
        self.calls: list[list[str]] = []
        self._rows = rows if rows is not None else []
        self._fail = fail

    def run(self, args):
        self.calls.append(list(args))
        if self._fail:
            raise DataError("`alpaca data news` exited 1")
        return {"news": self._rows}

    def arg(self, flag: str) -> str:
        return self.calls[-1][self.calls[-1].index(flag) + 1]


def test_a_dead_feed_raises_instead_of_reading_as_quiet(cfg):
    feed = _Feed(cfg, fail=True)
    with pytest.raises(DataError):
        feed.news("GTLB")


def test_the_watch_records_a_dead_feed_rather_than_calling_the_name_quiet(cfg):
    """The whole point: an unreadable name must not look like a silent one."""
    params = load_params("config/strategies/earnings_event.yaml")
    pack = gather("GTLB", params.sentiment, alpaca_news_fetcher(_Feed(cfg, fail=True)))
    assert pack.headlines == []
    assert any(e.startswith("news:") for e in pack.errors), (
        "a feed that failed must appear on the pack's errors - the watch report "
        "reads that list to decide whether a name was quiet or unreadable"
    )


def test_the_events_window_reaches_the_command_line(cfg):
    """Seven days, because that is what earnings_event.yaml asks for."""
    params = load_params("config/strategies/earnings_event.yaml")
    feed = _Feed(cfg)
    fetch = alpaca_news_fetcher(feed)
    fetch("GTLB", params.sentiment.news_lookback_days, params.sentiment.max_headlines)

    started = dt.datetime.strptime(feed.arg("--start"), "%Y-%m-%dT%H:%M:%SZ").date()
    expected = dt.date.today() - dt.timedelta(days=params.sentiment.news_lookback_days)
    assert started == expected, "the events lookback was replaced by the global one"
    assert feed.arg("--limit") == str(params.sentiment.max_headlines)


def test_the_default_window_is_still_the_global_one(cfg):
    """Discovery must not inherit the events book's seven days."""
    feed = _Feed(cfg)
    feed.news("SPY")
    started = dt.datetime.strptime(feed.arg("--start"), "%Y-%m-%dT%H:%M:%SZ")
    hours = (dt.datetime.now(dt.timezone.utc) - started.replace(tzinfo=dt.timezone.utc))
    assert abs(hours.total_seconds() / 3600 - cfg.data.news_lookback_hours) < 0.1


def test_two_windows_do_not_share_one_cache_entry(cfg):
    """A six-hour fetch and a seven-day fetch for one symbol are not the same
    question, and whichever ran first must not answer the other."""
    feed = _Feed(cfg)
    feed.news("GTLB")
    feed.news("GTLB", start=dt.date.today() - dt.timedelta(days=7))
    assert len(feed.calls) == 2, "the window must be part of the cache key"


# --------------------------------------------------------------------------- #
# the MCP read surface
# --------------------------------------------------------------------------- #
def test_an_empty_mcp_server_is_not_a_silent_degradation(caplog):
    """30 Aug: `broker.mcp.toolsets` named toolsets the server did not know, so
    it registered zero tools and the agent booted with no read surface at all.
    The only trace was an INFO line reading '0 read tools exposed'."""
    import logging

    from oaa.agents.tools import MCP_READ_ALLOWLIST, mcp_read_tools

    class _Bridge:
        tools: dict = {}

    with caplog.at_level(logging.WARNING, logger="oaa"):
        assert mcp_read_tools(_Bridge()) == []
    assert any(
        "missing" in r.message and r.levelno >= logging.WARNING for r in caplog.records
    ), "an empty MCP surface must warn, not whisper"
    assert len(MCP_READ_ALLOWLIST) == 7


def test_mutating_tools_are_never_handed_to_the_model():
    """The safety property, and the reason dropping the server-side filter is
    safe: it lives here, in code, not in `broker.mcp.toolsets`."""
    from oaa.agents.tools import mcp_read_tools

    class _Tool:
        description = "x"
        inputSchema = {"type": "object", "properties": {}}

    dangerous = ["place_option_order", "close_all_positions", "cancel_all_orders"]

    class _Bridge:
        tools = {n: _Tool() for n in [*dangerous, "get_account_info", "get_clock"]}

    exposed = {s["name"] for s in mcp_read_tools(_Bridge())}
    assert not exposed & set(dangerous), (
        "an agent that can place a raw order bypasses the risk engine entirely"
    )
    assert "get_account_info" in exposed
