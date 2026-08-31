"""The Daily Reports tab reads files, and only files.

Two things are worth pinning. The first is the search order: a date that
exists both locally and in `public/reports/` must render the LOCAL one,
because re-running `oaa daily-report` for a date is a correction and a
correction must not be shadowed by the copy published before it.

The second is that the page survives what a generated file actually does -
a missing sidecar, a malformed one, a session with no fills, no ideas and no
gates. Those are the normal cases for this book, not the edge ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from oaa.app import reports_page as rp

APP = Path(__file__).resolve().parents[1] / "src" / "oaa" / "app"

SESSION = {
    "date": "2026-08-31",
    "profile": "judged",
    "day_pl": 0.0,
    "day_pl_pct": 0.0,
    "snapshots": 274,
    "open_positions_at_close": 0,
    "cycles_run": {"intraday_1330": 1},
    "fills": [],
    "closes": [],
    "potential": [{
        "ts": "13:30:13", "symbol": "TLT", "strategy": "intraday_momentum",
        "structure": "single_long", "quantity": 1, "net_price": 14.235,
        "max_loss": 1423.5, "reason": "max loss exceeds 1.0%; rule=sizing",
        "thesis": "TLT crossed above session VWAP.", "risk_approved": False,
    }],
    "gate_rejections": 200,
    "rejections_by_gate": {"time_of_day": 56, "structure": 40},
    "rejections_by_reason": {"inside the lunch window": 40},
    "rejections_by_book": {"intraday": 136},
    "symbols_examined": ["DIA", "SPY"],
    "by_strategy": {"intraday_momentum": {
        "ideas": 8, "opened": 0, "closed": 0, "declined": 8,
        "near_misses": 1, "realised_pl": 0.0,
    }},
    "errors": [],
    "notes": ["watch noted ZS", "watch noted ZS"],
    "near_misses": [{
        "ts": "14:00:33", "symbol": "QQQ", "strategy": "intraday_momentum",
        "structure": "single_long", "quantity": 1, "net_price": 2.935,
        "max_loss": 293.5, "reason": "partial confirmations",
        "thesis": "QQQ crossed above VWAP.", "risk_approved": True,
    }],
}

PAYLOAD = {
    "session": SESSION,
    "critique": {"author": "featherless / Qwen/Qwen3-32B",
                 "bullets": ["Raise the sizing cap.", "Move the lunch gate earlier."]},
    "generated_at": "2026-08-31T22:15:26.368154+00:00",
}


@pytest.fixture(autouse=True)
def _root_is_the_tmp_repo(monkeypatch, tmp_path):
    """Pin the package-derived repo root to the test's own tree.

    `report_dirs` searches `settings.root` AND the root derived from the
    package's own location, because on a deploy host the first can be `cwd`
    and wrong. In a test the second is the REAL repo, whose `public/reports/`
    is committed - so without this every "no reports here" assertion quietly
    reads the developer's published files instead. `test_the_package_root_is_
    searched_too` covers the behaviour this hides.
    """
    monkeypatch.setattr(rp, "_repo_root", lambda: tmp_path)


def _settings(root: Path, profile: str = "judged"):
    """A stand-in for `Settings` with the three attributes the page touches."""
    return SimpleNamespace(
        root=root,
        config=SimpleNamespace(
            profile=profile,
            telemetry=SimpleNamespace(report_dir="reports"),
        ),
        path=lambda rel, root=root: (
            Path(rel) if Path(rel).is_absolute() else root / rel
        ),
    )


def _write(directory: Path, date: str, payload=PAYLOAD, markdown="# report\n"):
    directory.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (directory / f"{date}.json").write_text(json.dumps(payload))
    if markdown is not None:
        (directory / f"{date}.md").write_text(markdown)


# --------------------------------------------------------------------------- #
# where the files come from
# --------------------------------------------------------------------------- #
def test_local_reports_are_found(tmp_path):
    _write(tmp_path / "reports" / "judged", "2026-08-31")
    settings = _settings(tmp_path)
    assert rp.available(settings) == ["2026-08-31"]
    assert rp.load_report(settings, "2026-08-31")["data"] == PAYLOAD


def test_published_reports_are_found_when_the_local_store_is_absent(tmp_path):
    """The deployed case: reports/ was never cloned, public/reports/ was."""
    _write(tmp_path / "public" / "reports" / "judged", "2026-08-30")
    assert rp.available(_settings(tmp_path)) == ["2026-08-30"]


def test_a_local_report_beats_a_published_copy_of_the_same_date(tmp_path):
    """Re-running a date is a correction, not a second artefact.

    Both stores hold 2026-08-31. The local one must win, or regenerating a
    report on the Mac would leave the page showing the stale published copy
    with nothing to say it had.
    """
    corrected = json.loads(json.dumps(PAYLOAD))
    corrected["critique"]["bullets"] = ["the corrected bullet"]
    _write(tmp_path / "reports" / "judged", "2026-08-31", corrected, "# local\n")
    _write(tmp_path / "public" / "reports" / "judged", "2026-08-31",
           PAYLOAD, "# published\n")

    settings = _settings(tmp_path)
    assert rp.available(settings) == ["2026-08-31"]      # counted once
    report = rp.load_report(settings, "2026-08-31")
    assert report["data"]["critique"]["bullets"] == ["the corrected bullet"]
    assert report["markdown"] == "# local\n"


def test_dates_come_back_newest_first(tmp_path):
    for date in ("2026-08-28", "2026-08-31", "2026-08-29"):
        _write(tmp_path / "reports" / "judged", date)
    assert rp.available(_settings(tmp_path))[0] == "2026-08-31"


def test_a_different_profile_sees_a_different_folder(tmp_path):
    _write(tmp_path / "reports" / "judged", "2026-08-31")
    assert rp.available(_settings(tmp_path, "dev")) == []


def test_no_reports_anywhere_is_an_empty_list_not_an_error(tmp_path):
    assert rp.available(_settings(tmp_path)) == []
    assert rp.report_dirs(_settings(tmp_path)) == []


def test_the_package_root_is_searched_too(tmp_path, monkeypatch):
    """The deploy case `settings.root` cannot cover.

    Installed into site-packages rather than run from the tree,
    `project_root()` falls back to `Path.cwd()` - which on a hosted process is
    not the repo. The published directory is found from the package's own
    location instead, so the tab is not empty because of where the server was
    started from.
    """
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "public" / "reports" / "judged", "2026-08-27")
    monkeypatch.setattr(rp, "_repo_root", lambda: elsewhere)
    assert rp.available(_settings(tmp_path / "cwd-is-not-the-repo")) == ["2026-08-27"]


# --------------------------------------------------------------------------- #
# what generated files actually do
# --------------------------------------------------------------------------- #
def test_a_malformed_sidecar_falls_back_to_a_published_copy(tmp_path):
    """Local wins, but only if it parses.

    A truncated write - a report generated while the process was killed - must
    not take the date down when a published copy of it is sitting right there.
    """
    directory = tmp_path / "reports" / "judged"
    directory.mkdir(parents=True)
    (directory / "2026-08-31.json").write_text("{ not json")
    _write(tmp_path / "public" / "reports" / "judged", "2026-08-31")

    report = rp.load_report(_settings(tmp_path), "2026-08-31")
    assert report["data"] == PAYLOAD
    assert report["error"]


def test_a_malformed_sidecar_reports_the_error_and_keeps_the_markdown(tmp_path):
    directory = tmp_path / "reports" / "judged"
    directory.mkdir(parents=True)
    (directory / "2026-08-31.json").write_text("{ not json")
    (directory / "2026-08-31.md").write_text("# still readable\n")

    report = rp.load_report(_settings(tmp_path), "2026-08-31")
    assert report["data"] is None
    assert report["error"]
    assert report["markdown"] == "# still readable\n"


def test_markdown_alone_is_still_a_report(tmp_path):
    _write(tmp_path / "reports" / "judged", "2026-08-31", payload=None)
    settings = _settings(tmp_path)
    assert rp.available(settings) == ["2026-08-31"]
    assert rp.load_report(settings, "2026-08-31")["markdown"] == "# report\n"


def test_the_ideas_table_names_why_each_one_did_not_trade(tmp_path):
    frame = rp._ideas_frame(SESSION["potential"])
    assert "Why it did not trade" in frame.columns
    assert frame.iloc[0]["Risk approved"] == "no"
    assert "sizing" in frame.iloc[0]["Why it did not trade"]


def test_a_reason_with_no_rows_does_not_divide_by_zero():
    assert rp._counts_frame({}, "Reason").empty


def test_the_gate_chart_is_ordered_biggest_last_so_it_reads_top_down():
    """Plotly draws a horizontal bar chart bottom-up, so ascending input puts
    the largest gate at the top - which is where the eye starts."""
    fig = rp._gate_chart({"time_of_day": 56, "structure": 40, "premium": 13})
    assert list(fig.data[0].y) == ["premium", "structure", "time_of_day"]


# --------------------------------------------------------------------------- #
# and it is a reader, not a writer
# --------------------------------------------------------------------------- #
def test_the_page_reaches_no_network_and_submits_nothing():
    """The reason this tab is on the public build with no mode guard.

    Every other public page either had a control removed or reads a live
    endpoint behind a button. This one opens files, so the guarantee is that
    nothing in it can reach anything - asserted against the source rather than
    trusted, because the class of change that breaks it (a Refresh button, a
    'regenerate today' action) looks entirely reasonable in review.
    """
    source = (APP / "reports_page.py").read_text()
    for forbidden in (
        "st.button", "st.form_submit_button", "get_data_provider",
        "get_broker", "requests.", "httpx", "generate_daily_report",
        "subprocess",
    ):
        assert forbidden not in source, (
            f"{forbidden} appeared on the Daily Reports page - it is public, "
            "and it is public BECAUSE it only reads files"
        )
    # The download button is the one exception, and it hands over a string
    # already in memory.
    assert "st.download_button" in source


def test_the_tab_is_wired_into_both_builds():
    source = (APP / "dashboard.py").read_text()
    public, operator = source.split("if mode.is_public():")[-1].split("else:")[:2]
    assert "PAGE_REPORTS" in public
    assert "PAGE_REPORTS" in operator


@pytest.mark.parametrize("author,expected", [
    ("featherless / Qwen/Qwen3-32B", False),
    ("deterministic (fallback - featherless failed: 403)", True),
    ("deterministic (no reasoning provider)", True),
])
def test_a_fallback_critique_is_recognisable_from_its_author(author, expected):
    """The page says who wrote the critique, because 'here is what to improve'
    means something different from a model than from arithmetic."""
    assert (rp.FALLBACK_MARKER in author.lower()) is expected
