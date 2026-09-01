"""The published watch store: the deployed Events tab shows what the model found.

`runs/` is gitignored at any depth, so a deploy host has no dossiers and
section 2b of the Events tab renders "Nothing logged yet" - on a page where a
week of notes was in fact logged. `scripts/publish_events.py` copies them into
`public/events/watch/`, and the page reads that when the local store has
nothing for a name.

The property that matters is the ORDER: local wins, because polling again is a
correction and a correction must not be shadowed by the copy published before
it. These tests pin the order and the fallback, not the file format.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from oaa.app import events_page


def _settings(root: Path, store: str):
    return SimpleNamespace(root=root, path=lambda p: root / p)


def _params(store: str):
    return SimpleNamespace(watch=SimpleNamespace(store_dir=store),
                           sentiment=SimpleNamespace())


def _write(directory: Path, symbol: str, summary: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{symbol}.json").write_text(json.dumps({
        "symbol": symbol,
        "seen": ["abc"],
        "notes": [{
            "asof": "2026-08-31", "salience": 0.8, "lean": "up",
            "headlines": 1, "messages": 0, "summary": summary,
            "injection_noticed": False,
        }],
    }))


def test_the_published_store_is_read_when_the_local_one_is_missing(tmp_path,
                                                                   monkeypatch):
    _write(tmp_path / events_page.PUBLISHED, "AVGO", "published copy")
    monkeypatch.setattr(events_page, "_repo_root", lambda: tmp_path)

    settings, params = _settings(tmp_path, "runs/events/watch"), _params("runs/events/watch")
    notes = events_page._dossier_notes(settings, params, "AVGO")
    assert [n.summary for n in notes] == ["published copy"]


def test_the_local_store_wins_over_a_published_copy(tmp_path, monkeypatch):
    _write(tmp_path / events_page.PUBLISHED, "AVGO", "published copy")
    _write(tmp_path / "runs" / "events" / "watch", "AVGO", "fresh local poll")
    monkeypatch.setattr(events_page, "_repo_root", lambda: tmp_path)

    settings, params = _settings(tmp_path, "runs/events/watch"), _params("runs/events/watch")
    notes = events_page._dossier_notes(settings, params, "AVGO")
    assert [n.summary for n in notes] == ["fresh local poll"]


def test_a_name_in_neither_store_is_an_empty_dossier_not_an_error(tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(events_page, "_repo_root", lambda: tmp_path)
    settings, params = _settings(tmp_path, "runs/events/watch"), _params("runs/events/watch")
    assert events_page._dossier_notes(settings, params, "NOPE") == []


def test_the_repo_ships_a_published_store_for_the_deployed_page():
    """A regression guard on the deploy: the committed directory exists and has
    dossiers in it. Without this the tab boots empty on the host with no error
    anywhere - which is exactly how it shipped the first time."""
    published = Path(__file__).resolve().parents[1] / events_page.PUBLISHED
    assert published.is_dir()
    assert list(published.glob("*.json"))
