"""The skin renders, escapes what it is given, and keeps colour meaning intact.

Every rule in `skin.css` is CSS against markup Streamlit generates, so the risk
this file exists to cover is not "does the page look nice" - it is that a
Streamlit upgrade, or a careless edit, silently turns the styling into a page
that renders wrong or, worse, one where colour lies about direction.
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from oaa.app import skin  # noqa: E402
from oaa.app.theme import DARK, LIGHT  # noqa: E402

# NOTE: `AppTest.from_function` execs the callable in a FRESH module namespace,
# so the imports above are not visible inside an `app()` body. Every one of them
# imports what it needs itself. Losing an hour to this is a rite of passage.


def _run(fn):
    return AppTest.from_function(fn).run(timeout=90)


# --------------------------------------------------------------------------- #
# the stylesheet
# --------------------------------------------------------------------------- #
def test_the_stylesheet_carries_the_palette_for_both_modes():
    for dark, pal in ((True, DARK), (False, LIGHT)):
        css = skin.css(dark=dark)
        for key in ("surface", "plane", "grid", "accent", "good", "critical"):
            assert pal[key] in css, f"{key} missing from the {dark=} stylesheet"


def test_the_stylesheet_is_a_single_raw_html_block():
    """The shape that stops it printing itself onto the page as text.

    Streamlit's markdown goes through a CommonMark parser. `<style>` opens a
    "type 1" raw-HTML block that runs to the closing tag whatever is inside; a
    leading `<link>` opens a "type 7" block instead, and type 7 **ends at the
    first blank line** - after which the rest of the sheet is parsed as
    markdown and rendered as paragraphs of CSS source. Both invariants below
    were learned the hard way.
    """
    css = skin.css(dark=True)
    assert css.startswith("<style>"), "must open the block, with no leading tag or newline"
    assert css.rstrip().endswith("</style>")
    assert not [ln for ln in css.splitlines() if not ln.strip()], "no blank lines"


def test_the_stylesheet_survives_a_commonmark_parser():
    """The invariants above, checked against a real parser rather than asserted.

    markdown-it-py implements the same block rules the frontend applies, so if
    any of the sheet comes back wrapped in a <p>, it would have been visible
    text on the page.
    """
    markdown_it = pytest.importorskip("markdown_it")
    rendered = markdown_it.MarkdownIt("commonmark", {"html": True}).render(
        skin.css(dark=True)
    )
    assert "<p>" not in rendered, "part of the stylesheet was parsed as markdown"
    assert "--accent" in rendered, "the sheet did not survive verbatim"


def test_the_stylesheet_targets_only_public_test_ids():
    """Streamlit's generated class names churn between releases; `data-testid`
    is the part it treats as public. A rule written against `.css-1v0mbdj` will
    break silently on the next upgrade, so none may be added."""
    css = skin.css(dark=True)
    assert ".css-" not in css
    assert ".st-emotion" not in css


def test_the_streamlit_header_bar_is_removed_not_just_emptied():
    """Hiding only the toolbar leaves the bar it lives in: sticky, opaque, and
    painted over the top of the page, which slices the masthead in half."""
    css = skin.css(dark=True)
    assert '[data-testid="stHeader"]' in css
    assert "display:none" in css.split('[data-testid="stHeader"]')[1][:40]


def test_the_accent_never_means_direction():
    """Amber is chrome. Green is profit, red is loss. If the accent is ever the
    same colour as a signal, a glance at the page can mislead - which on a P&L
    dashboard is the whole ballgame."""
    for pal in (DARK, LIGHT):
        assert pal["accent"] != pal["good"]
        assert pal["accent"] != pal["critical"]
        assert pal["good"] != pal["critical"]


# --------------------------------------------------------------------------- #
# components
# --------------------------------------------------------------------------- #
def test_components_escape_what_they_are_given():
    """These all render with unsafe_allow_html, so anything interpolated into
    them - a strategy name, a symbol, a config string - has to be escaped."""
    nasty = '<img src=x onerror="alert(1)">'
    assert "<img" not in skin.pill(nasty)
    assert "&lt;img" in skin.pill(nasty)


def test_a_pill_only_accepts_the_tones_the_stylesheet_defines():
    assert 'class="oaa-pill live"' in skin.pill("judged", "live")
    assert 'class="oaa-pill "' in skin.pill("dev", "chartreuse")


def test_the_masthead_is_a_nameplate_and_nothing_else_by_default():
    """Title only - no icon, no subtitle, no status. The account identity that
    used to sit up here is on every tab in `_identity_banner`, where it is read
    rather than glanced at."""
    def app():
        from oaa.app import skin

        skin.masthead("Eventus Algorithm")

    at = _run(app)
    assert not at.exception
    html = "".join(m.value for m in at.markdown)
    assert "Eventus Algorithm" in html
    assert "oaa-mark" not in html
    assert "oaa-sub" not in html
    assert "oaa-masthead-right" not in html


def test_the_masthead_still_carries_a_subtitle_and_aside_when_asked():
    def app():
        from oaa.app import skin

        skin.masthead("Eventus Algorithm", "alpaca paper",
                      right=skin.pill("judged", "live"))

    at = _run(app)
    assert not at.exception
    html = "".join(m.value for m in at.markdown)
    assert "oaa-sub" in html
    assert "oaa-pill live" in html


def test_the_masthead_escapes_its_title():
    """It renders with unsafe_allow_html and the title comes from config."""
    def app():
        from oaa.app import skin

        skin.masthead('<img src=x onerror="alert(1)">')

    at = _run(app)
    html = "".join(m.value for m in at.markdown)
    assert "<img" not in html
    assert "&lt;img" in html


# --------------------------------------------------------------------------- #
# injection
# --------------------------------------------------------------------------- #
def test_the_stylesheet_survives_a_rerun():
    """The regression that cost an evening.

    `inject` used to skip itself when a session-state flag said it had already
    run. Streamlit rebuilds the element tree from scratch on every rerun and
    renders only what the script emits *that* time, so the first paint was
    styled and every interaction after it dropped the stylesheet - the page
    reverted to stock Streamlit and looked like the theme had never applied.
    """
    def app():
        from oaa.app import skin

        skin.inject()

    at = AppTest.from_function(app).run(timeout=90)
    assert [m for m in at.markdown if "oaa-eyebrow" in m.value]

    at = at.run(timeout=90)  # same session, second script run
    assert not at.exception
    assert [m for m in at.markdown if "oaa-eyebrow" in m.value], \
        "the stylesheet vanished on rerun"
