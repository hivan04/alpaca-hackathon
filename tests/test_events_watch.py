"""The watch: days of reading before a print, and none after it.

The book's original shape asked the model one question, once, at 15:50 on arm
day. Everything that decided the print and arrived before that minute - an
estimate revision on the Tuesday, a supplier's read-across on the Wednesday,
three days of retail crowding one way - was information the book never had.

These tests pin the four properties that make the watch worth having, each of
which is also the way it would go wrong:

  1. it reads a name only inside the window before its print;
  2. it STOPS the day the print is behind us;
  3. it does not re-read what it has already read, and spends no model call on
     a poll where nothing arrived;
  4. what it retains reaches the arm-time direction call.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from oaa.strategies.events.calendar import EarningsEvent
from oaa.strategies.events.params import SentimentParams, WatchParams
from oaa.strategies.events.sentiment import EvidencePack
from oaa.strategies.events.watch import Dossier, EventWatcher, WatchNote

TODAY = dt.date(2026, 9, 1)      # a Tuesday


class FakeLLM:
    """Counts calls, so 'spends nothing on a quiet day' is testable."""

    provider = "fake"

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {
            "salience": 0.8,
            "summary": "Two brokers raised estimates into the print.",
            "lean": "bullish",
            "evidence": ["broker raised FY26 estimates"],
            "injection_noticed": False,
        }
        self.calls: list[tuple[str, str]] = []

    def json_complete(self, system, user, default=None):
        self.calls.append((system, user))
        return self.payload


def _event(symbol: str, report: dt.date, timing: str = "amc", confirmed: bool = True):
    return EarningsEvent(
        symbol=symbol, report_date=report, timing=timing,
        confirmed=confirmed, source="test", history=(3.0, -2.0, 4.0, -1.0),
    )


def _watcher(tmp_path, calendar, llm=None, **overrides):
    params = WatchParams(**overrides)
    return EventWatcher(
        llm=llm or FakeLLM(),
        params=params,
        sentiment=SentimentParams(stocktwits_enabled=False),
        calendar=calendar,
        news_fn=None,
        store_dir=tmp_path / "watch",
    )


# --------------------------------------------------------------------------- #
# the window
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "report_offset,expected",
    [
        (0, True),      # reports today - the last read before the print
        (1, True),
        (3, True),      # the far edge of a 3-day lookahead
        (4, False),     # too early: not yet worth the tokens
        (-1, False),    # ALREADY REPORTED - the whole point
    ],
)
def test_a_name_is_watched_only_between_the_window_opening_and_its_print(
    tmp_path, report_offset, expected
):
    symbol = "NVDA"
    calendar = {symbol: _event(symbol, TODAY + dt.timedelta(days=report_offset))}
    watcher = _watcher(tmp_path, calendar, lookahead_days=3)
    assert (symbol in [e.symbol for e in watcher.due(TODAY)]) is expected


def test_an_unconfirmed_name_is_never_watched(tmp_path):
    """The same rule the arm follows. A calendar row nobody confirmed is a date
    that may not exist, and reading around it is reading around nothing."""
    calendar = {"CPRT": _event("CPRT", TODAY + dt.timedelta(days=2), confirmed=False)}
    watcher = _watcher(tmp_path, calendar)
    assert watcher.due(TODAY) == []


def test_the_dossier_is_retired_the_day_after_the_print(tmp_path):
    """Not deleted - retired. It is the evidence trail behind a trade the
    journal already recorded, and it must stop being readable as pre-print
    evidence the moment the print is behind us."""
    calendar = {"NVDA": _event("NVDA", TODAY - dt.timedelta(days=1))}
    watcher = _watcher(tmp_path, calendar)
    watcher.save(Dossier(symbol="NVDA", notes=[WatchNote(asof="2026-08-30", salience=0.9)]))

    assert watcher._path("NVDA").exists()
    retired = watcher.retired(TODAY)
    assert retired == ["NVDA"]
    assert not watcher._path("NVDA").exists()
    archived = watcher.store / "reported" / "NVDA.json"
    assert archived.exists(), "a retired dossier is kept, not thrown away"
    assert json.loads(archived.read_text())["notes"]


# --------------------------------------------------------------------------- #
# the poll
# --------------------------------------------------------------------------- #
def _news(items):
    def fetch(symbol, days, limit):
        return items
    return fetch


def test_a_poll_writes_a_note_from_what_arrived(tmp_path):
    calendar = {"NVDA": _event("NVDA", TODAY + dt.timedelta(days=2))}
    llm = FakeLLM()
    watcher = _watcher(tmp_path, calendar, llm=llm)
    watcher.news_fn = _news([
        {"headline": "Broker raises NVDA estimates into the quarter",
         "created_at": "2026-08-31T12:00:00"},
    ])

    report = watcher.poll(TODAY)
    assert report.watching == ["NVDA"]
    assert report.new_items["NVDA"] == 1
    assert report.noted == ["NVDA"]
    assert len(llm.calls) == 1

    dossier = watcher.load("NVDA")
    assert len(dossier.notes) == 1
    assert dossier.notes[0].lean == "bullish"
    assert dossier.notes[0].salience == 0.8


def test_a_second_poll_over_the_same_items_costs_nothing(tmp_path):
    """The property that makes three polls a day affordable, and that makes the
    note count an honest measure of how much actually happened rather than of
    how often the loop ran."""
    calendar = {"NVDA": _event("NVDA", TODAY + dt.timedelta(days=2))}
    llm = FakeLLM()
    watcher = _watcher(tmp_path, calendar, llm=llm)
    watcher.news_fn = _news([
        {"headline": "Broker raises NVDA estimates", "created_at": "2026-08-31T12:00:00"},
    ])

    watcher.poll(TODAY)
    watcher.poll(TODAY)

    assert len(llm.calls) == 1, "the second poll re-read an item it had already read"
    assert len(watcher.load("NVDA").notes) == 1
    assert watcher.poll(TODAY).quiet == ["NVDA"]


def test_new_items_arriving_later_do_earn_a_second_note(tmp_path):
    calendar = {"NVDA": _event("NVDA", TODAY + dt.timedelta(days=2))}
    llm = FakeLLM()
    watcher = _watcher(tmp_path, calendar, llm=llm)
    watcher.news_fn = _news([{"headline": "First", "created_at": "2026-08-31T12:00:00"}])
    watcher.poll(TODAY)
    watcher.news_fn = _news([
        {"headline": "First", "created_at": "2026-08-31T12:00:00"},
        {"headline": "Second, and different", "created_at": "2026-09-01T09:00:00"},
    ])
    watcher.poll(TODAY)

    assert len(llm.calls) == 2
    assert len(watcher.load("NVDA").notes) == 2
    # Only the NEW item is sent - re-sending the batch would pay for it twice
    # and let an old headline be judged material a second time.
    assert "Second, and different" in llm.calls[1][1]
    assert "First" not in llm.calls[1][1]


def test_an_immaterial_batch_is_counted_but_not_retained(tmp_path):
    """Ten low-salience notes dilute the two that matter."""
    calendar = {"NVDA": _event("NVDA", TODAY + dt.timedelta(days=2))}
    llm = FakeLLM({"salience": 0.1, "summary": "price chatter", "lean": "neutral",
                   "evidence": [], "injection_noticed": False})
    watcher = _watcher(tmp_path, calendar, llm=llm, min_salience=0.35)
    watcher.news_fn = _news([{"headline": "NVDA up 2%", "created_at": "2026-09-01T10:00:00"}])

    report = watcher.poll(TODAY)
    assert report.new_items["NVDA"] == 1
    assert report.noted == []
    assert watcher.load("NVDA").notes == []
    # ...but it is remembered as READ, so it is not re-judged tomorrow.
    assert watcher.load("NVDA").seen


def test_a_dead_feed_does_not_stop_the_watch(tmp_path):
    calendar = {
        "NVDA": _event("NVDA", TODAY + dt.timedelta(days=2)),
        "CRM": _event("CRM", TODAY + dt.timedelta(days=2)),
    }
    watcher = _watcher(tmp_path, calendar)

    def explode(symbol, days, limit):
        if symbol == "NVDA":
            raise RuntimeError("news 400")
        return [{"headline": "CRM guides up", "created_at": "2026-09-01T10:00:00"}]

    watcher.news_fn = explode
    report = watcher.poll(TODAY)
    assert "CRM" in report.noted
    assert any("NVDA" in e for e in report.errors)


def test_with_no_model_the_batch_is_retained_unjudged(tmp_path):
    """A watch that silently stopped recording would look exactly like a quiet
    week, which is the failure this book keeps having in other forms."""
    class NoLLM:
        provider = "null"

        def json_complete(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("no provider should mean no call")

    calendar = {"NVDA": _event("NVDA", TODAY + dt.timedelta(days=2))}
    watcher = _watcher(tmp_path, calendar, llm=NoLLM())
    watcher.news_fn = _news([{"headline": "Something", "created_at": "2026-09-01T10:00:00"}])

    watcher.poll(TODAY)
    notes = watcher.load("NVDA").notes
    assert len(notes) == 1
    assert "no model was available" in notes[0].summary


# --------------------------------------------------------------------------- #
# what the arm reads
# --------------------------------------------------------------------------- #
def test_the_dossier_reaches_the_arm_time_evidence_pack(tmp_path):
    calendar = {"NVDA": _event("NVDA", TODAY)}
    watcher = _watcher(tmp_path, calendar)
    watcher.save(Dossier(symbol="NVDA", notes=[
        WatchNote(asof="2026-08-30", salience=0.8, summary="Brokers raised estimates",
                  lean="bullish"),
        WatchNote(asof="2026-08-31", salience=0.6, summary="Supplier guided up",
                  lean="bullish"),
    ]))

    pack = watcher.attach(EvidencePack(symbol="NVDA"), TODAY)
    assert len(pack.notes) == 2
    assert pack.watch_lean == "bullish"
    assert pack.counts()["watch_notes"] == 2

    block = pack.as_prompt_block(8000)
    assert "Brokers raised estimates" in block
    assert "Supplier guided up" in block
    # The log must be readable as dated history, not as today's headlines.
    assert "2026-08-30" in block and "salience" in block


def test_a_pack_with_only_watch_notes_is_not_empty(tmp_path):
    """Otherwise a name whose news feed dies on arm afternoon would be skipped
    for 'nothing to read' while a week of evidence sat on disk."""
    pack = EvidencePack(symbol="NVDA")
    assert pack.is_empty
    pack.notes = [{"asof": "2026-08-30", "salience": 0.7, "summary": "x", "lean": "bullish"}]
    assert not pack.is_empty


def test_notes_are_aged_out_and_capped(tmp_path):
    calendar = {"NVDA": _event("NVDA", TODAY)}
    watcher = _watcher(tmp_path, calendar, note_ttl_days=5, max_notes=3)
    watcher.save(Dossier(symbol="NVDA", notes=[
        WatchNote(asof="2026-07-01", salience=0.9, summary="last quarter", lean="bullish"),
        WatchNote(asof="2026-08-29", salience=0.5, summary="a", lean="bullish"),
        WatchNote(asof="2026-08-30", salience=0.5, summary="b", lean="bearish"),
        WatchNote(asof="2026-08-31", salience=0.5, summary="c", lean="bullish"),
        WatchNote(asof="2026-09-01", salience=0.5, summary="d", lean="bullish"),
    ]))
    pack = watcher.attach(EvidencePack(symbol="NVDA"), TODAY)
    summaries = [n["summary"] for n in pack.notes]
    assert "last quarter" not in summaries, "a note from the previous quarter is not evidence"
    assert len(summaries) == 3
    assert summaries == ["b", "c", "d"]


def test_the_dossier_lean_is_weighted_by_salience_not_by_count():
    dossier = Dossier(symbol="NVDA", notes=[
        WatchNote(asof="2026-08-29", salience=0.9, lean="bearish"),
        WatchNote(asof="2026-08-30", salience=0.2, lean="bullish"),
        WatchNote(asof="2026-08-31", salience=0.1, lean="bullish"),
    ])
    lean, score = dossier.lean()
    assert lean == "bearish", "two weak bullish notes outvoted one strong bearish one"
    assert score < 0


# --------------------------------------------------------------------------- #
# two roles, two models
#
# The watch triage and the direction call are different questions asked at
# different frequencies with different stakes, and until 30 Aug they shared one
# client - while `DirectionParams.model` sat in the YAML being read by nothing.
# --------------------------------------------------------------------------- #
def test_a_role_naming_no_model_and_no_key_reuses_the_shared_client():
    """The default path, and the one that must not build anything.

    A second identical client is a second connection and, on a provider that
    cold-starts, a second warm-up - for a client that is byte-for-byte the
    shared one. It would also silently replace a client the caller injected,
    which is every test and the CLI.
    """
    from oaa.config.schema import LLMConfig
    from oaa.strategies.events.llm_roles import role_llm

    shared = object()
    got = role_llm(
        LLMConfig(), role="watch", model=None, api_key_env=None,
        temperature=0.0, max_tokens=600, seed=11, fallback=shared,
    )
    assert got is shared, "temperature and token caps alone are not a new agent"


def test_a_role_naming_a_model_gets_its_own_client_without_touching_the_base():
    from oaa.config.schema import LLMConfig
    from oaa.strategies.events import llm_roles

    base = LLMConfig(model="Qwen/Qwen3-32B", temperature=0.2, max_tokens=4000)
    seen: dict[str, object] = {}

    def fake_get_llm(cfg):
        seen["cfg"] = cfg
        return "role-client"

    import oaa.agents.llm as llm_module
    original = llm_module.get_llm
    llm_module.get_llm = fake_get_llm
    try:
        got = llm_roles.role_llm(
            base, role="watch", model="Qwen/Qwen3-8B",
            temperature=0.0, max_tokens=600, seed=11, fallback="shared",
        )
    finally:
        llm_module.get_llm = original

    assert got == "role-client"
    assert seen["cfg"].model == "Qwen/Qwen3-8B"
    assert seen["cfg"].temperature == 0.0
    # The main loop's config must be untouched - the carry book runs on it.
    assert base.model == "Qwen/Qwen3-32B"
    assert base.temperature == 0.2


def test_a_named_key_that_resolves_to_nothing_falls_back_loudly():
    """Naming an unset variable must not leave the book mute.

    With no usable client the watch retains batches unjudged and the direction
    call abstains - which from the outside is indistinguishable from a careful
    model on a quiet week. Falling back to the shared client keeps the book
    working; the WARNING is what makes the misconfiguration findable.
    """
    from oaa.config.schema import LLMConfig
    from oaa.strategies.events import llm_roles

    class NullClient:
        provider = "null"

    import oaa.agents.llm as llm_module
    original = llm_module.get_llm
    llm_module.get_llm = lambda cfg: NullClient()
    try:
        got = llm_roles.role_llm(
            LLMConfig(), role="watch", model="Qwen/Qwen3-8B",
            api_key_env="WATCH_KEY_THAT_IS_NOT_SET", fallback="shared",
        )
    finally:
        llm_module.get_llm = original

    assert got == "shared"


def test_the_shipped_config_gives_the_watch_a_smaller_model_than_the_arm():
    """The whole point of the split. If these ever converge it should be a
    decision, not a merge artefact."""
    from oaa.config.loader import load_config
    from oaa.strategies.events.params import load_params

    params = load_params("config/strategies/earnings_event.yaml")
    base = load_config().agents.llm

    watch_model = params.watch.model or base.model
    arm_model = params.direction.model or base.model
    assert watch_model != arm_model, (
        "the watch runs dozens of times a day on a narrow question; it should "
        "not be paying for the model that sizes the position"
    )
    # Both stay on the event's inference partner unless someone deliberately
    # moves them, which is a decision worth making visible in a diff.
    assert params.watch.api_key_env is None
    assert params.direction.api_key_env is None
