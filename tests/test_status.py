"""`oaa status` - the one command you run in a fresh terminal.

What it must never do is look healthy when it is not. Each test below pins one
way last night's state could be misread: a process that is up but wedged, a
reasoning layer that fell back to rules, and a screening pass that rejected
everything for a reason the operator needs to see.
"""

from __future__ import annotations

import datetime as dt
import json

from oaa.app.status import STALE_AFTER, collect, human_age

UTC = dt.timezone.utc


class _Journal:
    """Stands in for telemetry.Journal - the reads `collect` actually uses."""

    def __init__(self, events: list[dict], decisions: list[dict] | None = None) -> None:
        self._events = list(reversed(events))  # collect expects newest-first
        self._decisions = decisions or []
        self.journal_path = "/tmp/journal.jsonl"

    def events(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        rows = [e for e in self._events if kind is None or e.get("kind") == kind]
        return rows[:limit]

    def decisions(self, limit: int = 200) -> list[dict]:
        return self._decisions[:limit]

    def counts(self) -> dict[str, int]:
        return {"decisions": len(self._decisions)}


def _ts(minutes_ago: float) -> str:
    return (dt.datetime.now(UTC) - dt.timedelta(minutes=minutes_ago)).isoformat()


OPEN = {"phase": "intraday", "open": True, "now_et": "Mon 31 Aug 10:30 EDT",
        "next_open": "Tue 01 Sep 09:30 EDT"}
SHUT = {"phase": "closed", "open": False, "now_et": "Sat 29 Aug 14:00 EDT",
        "next_open": "Mon 31 Aug 09:30 EDT"}


def _snapshot(events, monkeypatch, procs=(), market=None):
    monkeypatch.setattr("oaa.app.status.processes", lambda profile: list(procs))
    monkeypatch.setattr("oaa.app.status.session", lambda settings: dict(market or OPEN))
    return collect(settings=None, journal=_Journal(events), profile="judged")


ONLINE = [{"name": "oaa-judged", "pid": 1, "status": "online",
           "restarts": 0, "uptime": "2h"}]


# --------------------------------------------------------------------------- #
def test_no_process_reads_as_not_running(monkeypatch):
    snap = _snapshot([{"kind": "report", "ts": _ts(600)}], monkeypatch)
    assert snap["state"] == "offline"


def test_a_running_process_with_a_fresh_journal_is_live(monkeypatch):
    snap = _snapshot([{"kind": "report", "ts": _ts(2)}], monkeypatch, ONLINE)
    assert snap["state"] == "live"


def test_a_running_process_with_a_silent_journal_is_stale_not_live(monkeypatch):
    """Up is not the same as working - this is the wedged-loop case."""
    quiet = STALE_AFTER.total_seconds() / 60 + 10
    snap = _snapshot([{"kind": "report", "ts": _ts(quiet)}], monkeypatch, ONLINE)
    assert snap["state"] == "stale"


def test_silence_outside_a_session_is_not_a_fault(monkeypatch):
    """The runner is schedule-driven: a quiet weekend is correct behaviour.

    Reported as stale, this is the alarm that trains an operator to ignore the
    command - and it would have fired all weekend, every weekend.
    """
    snap = _snapshot(
        [{"kind": "report", "ts": _ts(60 * 12)}], monkeypatch, ONLINE, market=SHUT
    )
    assert snap["state"] == "idle"
    assert snap["session"]["next_open"] == "Mon 31 Aug 09:30 EDT"


def test_a_dead_process_is_still_dead_when_the_market_is_shut(monkeypatch):
    """Closed excuses silence, never absence."""
    snap = _snapshot([{"kind": "report", "ts": _ts(60)}], monkeypatch, market=SHUT)
    assert snap["state"] == "offline"


def test_a_degraded_ai_layer_is_surfaced_not_buried(monkeypatch):
    """The 28 Aug failure was invisible outside one log line."""
    snap = _snapshot([{
        "kind": "agent_run", "ts": _ts(1), "cycle": "carry_scan", "turns": 1,
        "tool_calls": [], "error": "'Client' object has no attribute 'messages'",
    }], monkeypatch, ONLINE)

    assert snap["agent"]["degraded"] is True
    assert "messages" in snap["agent"]["error"]


def test_a_healthy_ai_layer_counts_its_mutating_calls(monkeypatch):
    snap = _snapshot([{
        "kind": "agent_run", "ts": _ts(1), "cycle": "intraday_scan", "turns": 3,
        "tool_calls": [{"tool": "read_account", "mutating": False},
                       {"tool": "place_order", "mutating": True}],
        "error": None,
    }], monkeypatch, ONLINE)

    assert snap["agent"]["degraded"] is False
    assert snap["agent"]["mutating"] == 1


def test_screening_reports_why_candidates_were_dropped(monkeypatch):
    """'The pool is empty' is not an answer; 'all 25 were under $10' is."""
    snap = _snapshot([{
        "kind": "discovery", "ts": _ts(3), "tradable": [], "new_symbols": [],
        "pool": {"symbols": 0},
        "snapshot": {
            "symbols": [{"symbol": "FNGR", "score": 0.96}, {"symbol": "CHAI", "score": 0.83}],
            "source_errors": {"news": "invalid limit: larger than the allowed maximum of 50"},
        },
        "rejected": [
            {"symbol": "FNGR", "reasons": ["price 0.40 below 10.00"]},
            {"symbol": "CHAI", "reasons": ["price 0.40 below 10.00"]},
            {"symbol": "XYZ", "reasons": ["no asset record"]},
        ],
    }], monkeypatch, ONLINE)

    disc = snap["discovery"]
    assert disc["scanned"] == 2 and disc["rejected"] == 3
    assert dict(disc["reasons"])["price below $10.00"] == 2
    assert "news" in disc["source_errors"], "a dead source must be visible"
    assert disc["top"][0] == ("FNGR", 0.96)


def test_stand_downs_are_named(monkeypatch):
    snap = _snapshot([{
        "kind": "macro_view", "ts": _ts(3), "regime": "high_dispersion",
        "vol_expectation": "expanding", "overnight_risk": 0.75,
        "guidance": {"vol_carry": "reduce", "event_premium": "stand_down"},
    }], monkeypatch, ONLINE)

    assert snap["macro"]["stood_down"] == ["event_premium"]


def test_an_empty_journal_does_not_blow_up(monkeypatch):
    snap = _snapshot([], monkeypatch)
    assert snap["state"] == "offline"
    assert snap["discovery"] == {} and snap["agent"] == {}
    json.dumps(snap, default=str)  # the --json path must stay serialisable


def test_ages_read_like_a_person_wrote_them():
    assert human_age(None) == "never"
    assert human_age(dt.timedelta(seconds=30)) == "30s ago"
    assert human_age(dt.timedelta(minutes=9)) == "9m ago"
    assert human_age(dt.timedelta(hours=3, minutes=5)) == "3h 5m ago"


def test_only_the_trading_loop_counts_as_the_agent_process():
    """The dashboard, this command, and a stray grep are not the agent."""
    from oaa.app.status import _looks_like_agent

    assert _looks_like_agent("/repo/.venv/bin/python -m oaa.cli run --profile judged")
    assert _looks_like_agent("node /usr/lib/pm2 oaa-judged")
    assert not _looks_like_agent("python -m streamlit run src/oaa/app/dashboard.py")
    assert not _looks_like_agent("/repo/.venv/bin/oaa status --profile judged")
    assert not _looks_like_agent("grep oaa run")


def test_the_next_open_skips_the_weekend():
    """Saturday's 'next session' is Monday, not Sunday."""
    from oaa.app.status import _next_open

    saturday = dt.datetime(2026, 8, 29, 14, 0)
    assert _next_open(saturday, dt.time(9, 30)).startswith("Mon 31 Aug 09:30")

    tuesday_afternoon = dt.datetime(2026, 9, 1, 16, 30)
    assert _next_open(tuesday_afternoon, dt.time(9, 30)).startswith("Wed 02 Sep 09:30")


def test_a_process_on_another_account_is_called_out(monkeypatch):
    """A bare `oaa status` resolves to dev; a process on the OTHER account
    running beside it must not be read as evidence about what is on screen."""
    procs = [{"name": "oaa-dev", "pid": 1, "status": "online",
              "restarts": 0, "uptime": "12h"}]
    snap = _snapshot([{"kind": "report", "ts": _ts(5)}], monkeypatch, procs)
    assert snap["other_profiles"] == ["dev"], "viewing judged, a dev loop is running"


def test_processes_are_labelled_with_the_account_they_trade():
    from oaa.app.status import _profile_of

    assert _profile_of("oaa-judged") == "judged"
    assert _profile_of("oaa-dev") == "dev"
    assert _profile_of("oaa-dashboard") == "", "the dashboard trades nothing"


def test_a_recorded_decision_counts_as_activity(monkeypatch):
    """`Journal.record` writes `...Z` and no `kind`; `Journal.event` writes
    `+00:00` and one. Reading only the second reported "last journal entry
    never" while the loop was writing a decision every 60 seconds."""
    stamp = (dt.datetime.now(UTC) - dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    snap = _snapshot(
        [{"ts": stamp, "action": "skip", "symbol": "BTC/USD", "cycle": "weekend"}],
        monkeypatch, ONLINE,
    )
    assert snap["journal_age"] is not None, "a Z-suffixed timestamp must parse"
    assert snap["journal_age"] < dt.timedelta(minutes=5)
    assert snap["state"] == "live"
