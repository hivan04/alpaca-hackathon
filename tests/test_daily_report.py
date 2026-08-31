"""The end-of-day evaluator.

What these pin, in order of how expensive the bug would be:

  * the session window is an EXCHANGE day, not a UTC one - a 15:50 ET arm is
    the next UTC day, and a report that dropped it would be silently missing
    the events book every single session;
  * a risk-approved idea that never became an order is surfaced as such,
    because that is the one row worth chasing on a day that filled nothing;
  * the critique degrades to arithmetic rather than disappearing, and always
    says which of the two wrote it.
"""

from __future__ import annotations

import datetime as dt
import json

from oaa.core.types import (
    AccountSnapshot,
    Decision,
    DecisionAction,
    Fill,
    RiskVerdict,
)
from oaa.telemetry.daily import (
    _parse_bullets,
    collect_session,
    critique,
    generate_daily_report,
    session_bounds,
)
from oaa.telemetry.journal import Journal

ET = "America/New_York"
DAY = dt.date(2026, 8, 31)


def journal_at(tmp_path) -> Journal:
    return Journal(tmp_path / "j.jsonl", tmp_path / "j.sqlite", tmp_path / "e.csv")


def utc(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 31, hour, minute, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- #
# the window
# --------------------------------------------------------------------------- #
def test_session_window_is_an_exchange_day_not_a_utc_one():
    start, end = session_bounds(DAY, ET)
    # 31 Aug is EDT (UTC-4), so the session opens at 04:00 UTC the same day.
    assert start == "2026-08-31T04:00:00"
    assert end == "2026-09-01T04:00:00"


def test_a_late_afternoon_decision_lands_in_the_right_session(tmp_path):
    """15:50 ET is 19:50 UTC - same UTC day. 20:00 ET would not be, and the
    events book arms in the last ten minutes of the session."""
    journal = journal_at(tmp_path)
    journal.record(Decision(
        ts=utc(19, 50), cycle="events_arm", action=DecisionAction.SKIP,
        symbol="NIO", strategy="earnings_event_directional",
        verdict=RiskVerdict.reject("model abstained"),
    ))
    session = collect_session(journal, DAY, profile="judged", timezone=ET)
    assert [p.symbol for p in session.potential] == ["NIO"]
    # And it is reported in exchange-local time, not UTC.
    assert session.potential[0].ts.startswith("15:50")


def test_yesterdays_rows_are_not_in_todays_report(tmp_path):
    journal = journal_at(tmp_path)
    journal.record(Decision(
        ts=dt.datetime(2026, 8, 28, 18, tzinfo=dt.timezone.utc),
        action=DecisionAction.SKIP, symbol="SPY", verdict=RiskVerdict.reject("old"),
    ))
    assert collect_session(journal, DAY, timezone=ET).potential == []


# --------------------------------------------------------------------------- #
# what the session says
# --------------------------------------------------------------------------- #
def test_fills_pnl_and_declines_are_all_collected(tmp_path):
    journal = journal_at(tmp_path)
    journal.snapshot(AccountSnapshot(
        equity=100_000, last_equity=100_000, cash=100_000, asof=utc(14)))
    journal.snapshot(AccountSnapshot(
        equity=100_450, last_equity=100_000, cash=90_000, asof=utc(20)))
    journal.record(Decision(
        ts=utc(15), cycle="intraday_1100", action=DecisionAction.OPEN,
        symbol="SPY", strategy="intraday_momentum",
        verdict=RiskVerdict.approve(2),
        fill=Fill(order_id="o1", symbol="SPY", status="filled",
                  filled_qty=2, filled_avg_price=3.10),
    ))
    journal.record(Decision(
        ts=utc(16), action=DecisionAction.SKIP, symbol="TLT",
        strategy="intraday_momentum",
        verdict=RiskVerdict.reject("max loss $1,424 exceeds 1.0% of equity"),
    ))

    session = collect_session(journal, DAY, profile="judged", timezone=ET)

    assert session.orders_sent == 1
    assert [f.symbol for f in session.fills] == ["SPY"]
    assert [p.symbol for p in session.potential] == ["TLT"]
    assert session.open_equity == 100_000 and session.close_equity == 100_450
    assert session.day_pl == 450
    assert session.by_strategy["intraday_momentum"]["opened"] == 1
    assert session.by_strategy["intraday_momentum"]["declined"] == 1


def test_a_risk_approved_idea_that_never_traded_is_a_near_miss(tmp_path):
    """`approved=1` on a SKIP is the single most interesting row in the day:
    the risk engine signed the ticket and no order exists. The report must not
    file it alongside the ideas risk itself refused."""
    journal = journal_at(tmp_path)
    journal.record(Decision(
        ts=utc(18), action=DecisionAction.SKIP, symbol="QQQ",
        strategy="intraday_momentum", verdict=RiskVerdict.approve(3),
        rationale="critic scored it below the bar",
    ))
    journal.record(Decision(
        ts=utc(18, 5), action=DecisionAction.SKIP, symbol="TLT",
        strategy="intraday_momentum", verdict=RiskVerdict.reject("rule=sizing"),
    ))

    session = collect_session(journal, DAY, timezone=ET)
    assert len(session.potential) == 2
    assert [p.symbol for p in session.near_misses] == ["QQQ"]
    # An approved verdict carries no reasons, so the rationale is the only
    # record of why it did not trade - it must not be dropped.
    assert "critic scored it" in session.potential[0].reason


def test_the_gate_funnel_is_aggregated_by_gate_book_and_reason(tmp_path):
    journal = journal_at(tmp_path)
    for _ in range(3):
        journal.event("gate_rejection", book="intraday", strategy="intraday_momentum",
                      symbol="SPY", vetoed_by="time_of_day",
                      reason="11:53 is inside the 11:30-13:30 lunch window")
    journal.event("gate_rejection", book="carry", strategy="vol_carry", symbol="QQQ",
                  vetoed_by="premium", reason="no IV rank available")

    session = collect_session(journal, DAY, timezone=ET)
    assert session.gate_rejections == 4
    assert session.rejections_by_gate == {"time_of_day": 3, "premium": 1}
    assert session.rejections_by_book == {"intraday": 3, "carry": 1}
    assert session.symbols_examined == ["QQQ", "SPY"]
    assert max(session.rejections_by_reason.values()) == 3


def test_a_degraded_reasoning_layer_is_reported_not_hidden(tmp_path):
    journal = journal_at(tmp_path)
    journal.event("agent_degraded", cycle="startup", reason="FEATHERLESS_API_KEY is not set")
    session = collect_session(journal, DAY, timezone=ET)
    assert session.reasoning_available is False
    assert any("degraded" in e for e in session.errors)


# --------------------------------------------------------------------------- #
# the critic
# --------------------------------------------------------------------------- #
class _Fake:
    provider = "featherless"

    class cfg:
        model = "Qwen/Qwen3-32B"

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, tools=None) -> str:
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_bullets_survive_the_shapes_models_actually_return():
    assert _parse_bullets(
        "- lower the ceiling\n* re-order the gate\n3. measure the spread\n"
    ) == ["lower the ceiling", "re-order the gate", "measure the spread"]
    assert _parse_bullets("- **bold lead** rest\n-\n- x\n") == ["bold lead** rest"]
    assert _parse_bullets("Here is my analysis:\n\n- real point\n") == ["real point"]
    assert _parse_bullets("no bullets at all, just prose") == []


def test_the_critic_is_given_the_session_and_returns_its_bullets(tmp_path):
    journal = journal_at(tmp_path)
    journal.event("gate_rejection", book="intraday", vetoed_by="selection",
                  symbol="DIA", reason="IV rank 90% is above the ceiling")
    session = collect_session(journal, DAY, profile="judged", timezone=ET)

    llm = _Fake("- Lower the IV ceiling\n- Re-order the selection gate")
    bullets, author = critique(session, llm)

    assert bullets == ["Lower the IV ceiling", "Re-order the selection gate"]
    assert "featherless" in author and "Qwen" in author
    brief = json.loads(llm.calls[0][1])
    assert brief["gate_funnel"]["by_gate"] == {"selection": 1}


def test_a_dead_critic_costs_the_bullets_not_the_report(tmp_path):
    session = collect_session(journal_at(tmp_path), DAY, timezone=ET)
    bullets, author = critique(session, _Fake(RuntimeError("502 upstream")))
    assert bullets and "502 upstream" in author and author.startswith("deterministic")


def test_prose_instead_of_bullets_falls_back_rather_than_printing_an_essay(tmp_path):
    session = collect_session(journal_at(tmp_path), DAY, timezone=ET)
    bullets, author = critique(session, _Fake("On reflection the session was fine."))
    assert author.startswith("deterministic")
    assert all(len(b) < 400 for b in bullets)


def test_no_provider_still_produces_a_critique_and_says_so(tmp_path):
    session = collect_session(journal_at(tmp_path), DAY, timezone=ET)
    bullets, author = critique(session, None)
    assert bullets and "no reasoning provider" in author


# --------------------------------------------------------------------------- #
# the artefact
# --------------------------------------------------------------------------- #
def test_the_report_is_written_and_regenerating_a_day_replaces_it(tmp_path):
    journal = journal_at(tmp_path)
    journal.record(Decision(
        ts=utc(18), action=DecisionAction.SKIP, symbol="QQQ",
        strategy="intraday_momentum", verdict=RiskVerdict.approve(3),
        rationale="critic scored it below the bar",
    ))
    out = tmp_path / "reports" / "judged"

    session, paths = generate_daily_report(
        journal, DAY, out, profile="judged", timezone=ET, llm=_Fake("- do the thing"),
    )
    text = paths["markdown"].read_text()

    assert paths["markdown"].name == "2026-08-31.md"
    assert "## Potential executions" in text
    assert "## Where the algorithm can improve" in text
    assert "- do the thing" in text
    assert "QQQ" in text
    assert "risk engine and still did not reach the broker" in text

    payload = json.loads(paths["json"].read_text())
    assert payload["critique"]["bullets"] == ["do the thing"]
    assert payload["session"]["date"] == "2026-08-31"
    assert len(payload["session"]["near_misses"]) == 1

    # Second run of the same day corrects the file rather than adding another.
    generate_daily_report(journal, DAY, out, profile="judged", timezone=ET, llm=None)
    assert sorted(p.name for p in out.iterdir()) == ["2026-08-31.json", "2026-08-31.md"]
    assert "no reasoning provider" in paths["markdown"].read_text()


def test_an_empty_session_still_writes_a_readable_report(tmp_path):
    """A non-trading day must produce a file, not a traceback and not a blank."""
    out = tmp_path / "reports"
    session, paths = generate_daily_report(journal_at(tmp_path), DAY, out, timezone=ET)
    text = paths["markdown"].read_text()
    assert session.traded is False
    assert "No orders filled this session." in text
    assert "No idea reached pricing this session." in text
    assert "## Where the algorithm can improve" in text
