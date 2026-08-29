"""The weekend tab actually renders.

`oaa dashboard` starting is not evidence that a tab works: Streamlit renders
lazily per session, so a page that raises on its first draw still serves a
200 on the shell. AppTest executes the script the way a browser session does,
which is the cheapest way to catch the usual failures - a missing key in a
cached payload, a chart built from an empty frame, a helper renamed under it.
"""

from __future__ import annotations

import pytest

streamlit = pytest.importorskip("streamlit", reason="dashboard extra not installed")
pytest.importorskip("plotly")

from streamlit.testing.v1 import AppTest  # noqa: E402

SCRIPT = '''
import sys
sys.path.insert(0, "src")
from oaa.app.weekend_page import render_weekend
render_weekend(None)
'''


def _run() -> AppTest:
    app = AppTest.from_string(SCRIPT, default_timeout=120)
    app.run()
    return app


def test_the_weekend_tab_renders_without_raising() -> None:
    app = _run()
    assert not app.exception, [str(e) for e in app.exception]


def test_the_clock_is_always_on_screen() -> None:
    """An operator's first question is whether the book can trade right now.
    It must never be one click away."""
    app = _run()
    labels = {m.label for m in app.metric}
    assert "Window" in labels
    assert "Hours to flatten" in labels
    assert "Round trip cost" in labels


def test_the_sample_size_warning_is_not_optional() -> None:
    """Six trades presented without a health warning is the failure mode this
    whole workstream has been avoiding. If a replay renders, the caveat renders
    with it - and if history is missing, the page says so instead."""
    app = _run()
    text = " ".join(w.value for w in app.warning)
    assert text, "no warning rendered at all"
    assert ("not a distribution" in text) or ("Refusing to substitute" in text)
