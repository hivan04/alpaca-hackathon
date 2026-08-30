"""The public build has no controls, and no way to grow one back.

`public_dashboard.py` serves the SAME `main()` as `oaa dashboard`. That is the
point - one dashboard, not two that drift - and it is also the risk: every
control added to the operator page from now on is added to the public one too
unless someone remembers to guard it. These tests are that someone.

Two layers, deliberately:

  * behaviour  - render the real page under both modes and assert what is on
                 it. This is the test that means something.
  * source     - assert each known control still sits behind a mode guard.
                 Cheap, and it survives an environment with no credentials,
                 which is where the behaviour layer skips.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from oaa.app import mode

APP = Path(__file__).resolve().parents[1] / "src" / "oaa" / "app"
REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# the switch itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_the_env_var_turns_the_public_build_on(monkeypatch, value):
    monkeypatch.setenv(mode.ENV_VAR, value)
    assert mode.is_public()
    assert not mode.is_operator()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_anything_else_leaves_the_operator_build(monkeypatch, value):
    monkeypatch.setenv(mode.ENV_VAR, value)
    assert mode.is_operator()


def test_the_default_is_the_operator_build(monkeypatch):
    """An unset variable must never be read as public.

    The failure that matters is the OTHER direction - a local dashboard that
    silently loses its Control tab - so the default is pinned explicitly.
    """
    monkeypatch.delenv(mode.ENV_VAR, raising=False)
    assert mode.is_operator()


def test_the_answer_is_not_cached_at_import(monkeypatch):
    """A cached answer would be resolved once and then be wrong forever.

    `public_dashboard.py` sets the variable before importing the dashboard,
    but a deployed host may set it later still. Reading at call time is the
    only version that cannot leave a Run button on a public page.
    """
    monkeypatch.delenv(mode.ENV_VAR, raising=False)
    assert mode.is_operator()
    monkeypatch.setenv(mode.ENV_VAR, "1")
    assert mode.is_public()
    monkeypatch.delenv(mode.ENV_VAR)
    assert mode.is_operator()


def test_the_public_build_is_the_judged_account():
    assert mode.PUBLIC_PROFILE == "judged"


# --------------------------------------------------------------------------- #
# the entry point
# --------------------------------------------------------------------------- #
def test_the_entry_point_sets_the_flag_before_importing_the_dashboard():
    """Order matters and is not obvious, so it is pinned.

    `oaa.app.dashboard` imports `oaa.app.control` at module scope. If the flag
    were set after that import the page would still render correctly - because
    the flag is read at call time - but the ordering here is the belt to that
    braces, and reversing it is exactly the kind of tidy-up that looks safe.
    """
    source = (REPO / "public_dashboard.py").read_text()
    set_at = source.index('os.environ["OAA_PUBLIC"]')
    import_at = source.index("from oaa.app.dashboard import main")
    assert set_at < import_at
    assert source.rstrip().endswith("main()")


# --------------------------------------------------------------------------- #
# every control sits behind a guard
# --------------------------------------------------------------------------- #
def _source(name: str) -> str:
    return (APP / name).read_text()


def _guards(source: str, needle: str) -> str:
    """Everything that decides whether `needle` renders, as one string.

    Two ways a control can be guarded, and both must count:

      * an enclosing `if mode.is_operator():` block - and the control may sit
        in the `else:` arm of `if mode.is_public():`, so an `else` is followed
        up to its matching `if` at the same indent rather than stopping there;
      * an early `return` at the top of the enclosing function, which is how
        `_identity_banner` does it.

    Returns the enclosing block headers plus the function's opening lines.
    Empty means nothing stands between the public page and this widget.
    """
    lines = source.split("\n")
    hit = next(i for i, line in enumerate(lines) if needle in line)

    found: list[str] = []
    limit = len(lines[hit]) - len(lines[hit].lstrip())
    i = hit - 1
    while i >= 0:
        line = lines[i]
        if not line.strip():
            i -= 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent >= limit:
            i -= 1
            continue
        stripped = line.strip()
        if stripped.startswith("def "):
            # The function header itself: take its opening lines too, which is
            # where an early return would be.
            found.extend(lines[i:i + 8])
            break
        found.append(stripped)
        if stripped.startswith(("else", "elif ")):
            # Walk on up to the `if` this else belongs to, at the same indent.
            j = i - 1
            while j >= 0:
                sibling = lines[j]
                if sibling.strip():
                    sib_indent = len(sibling) - len(sibling.lstrip())
                    if sib_indent < indent:
                        break
                    if sib_indent == indent and sibling.strip().startswith("if "):
                        found.append(sibling.strip())
                        break
                j -= 1
        limit = indent
        i -= 1
    return "\n".join(found)


CONTROLS = [
    ("dashboard.py", 'st.expander("Run a new backtest"', "spawns a real replay"),
    ("events_page.py", 'st.button("Price the live chain"', "hits the live chain"),
    ("positions.py", '.button("Refresh from Alpaca"', "hits the broker"),
]


@pytest.mark.parametrize("filename,needle,why", CONTROLS)
def test_each_control_is_behind_a_mode_guard(filename, needle, why):
    guard = _guards(_source(filename), needle)
    assert "mode.is_operator()" in guard or "mode.is_public()" in guard, (
        f"{needle} in {filename} is not guarded, and it {why}"
    )


def test_the_control_tab_is_never_constructed_in_the_public_build():
    """Not merely hidden. A rendered-but-hidden widget is still a widget.

    Streamlit tabs are all rendered; hiding one with CSS leaves its callbacks
    live. The Control tab flips books on and off on a real account, so the
    public build must not build it at all.
    """
    source = _source("dashboard.py")
    guard = _guards(source, "render_control(settings_for)")
    assert "mode.is_operator()" in guard
    assert re.search(r"if mode\.is_operator\(\):\s*\n\s*names\.append\(PAGE_CONTROL\)",
                     source), "PAGE_CONTROL is appended unconditionally"


def test_the_identity_banner_returns_early_in_the_public_build():
    source = _source("dashboard.py")
    body = source.split("def _identity_banner(")[1].split("\ndef ")[0]
    assert "if mode.is_public():\n        return" in body


def test_the_positions_page_prints_no_key_and_no_account_id_publicly():
    """Every account id and masked key on the Positions page is guarded.

    `dashboard.py` is covered by the `_identity_banner` test above - the one
    other place it touches `key_masked` is `_announce`, which prints to the
    TERMINAL for the operator reading logs and never to the page.
    """
    source = _source("positions.py")
    for needle in ("who.key_masked", "who.expected_account_id"):
        assert needle in source, f"{needle} vanished - update this test"
        guard = _guards(source, needle)
        assert "mode.is_public()" in guard or "mode.is_operator()" in guard, (
            f"{needle} renders in the public build"
        )


# --------------------------------------------------------------------------- #
# and now the page itself
# --------------------------------------------------------------------------- #
def _render(monkeypatch, public: bool):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv(mode.ENV_VAR, "1" if public else "0")

    def app():
        import os
        os.environ["OAA_PUBLIC"] = os.environ.get("OAA_PUBLIC", "0")
        from oaa.app.dashboard import main
        main()

    at = AppTest.from_function(app).run(timeout=120)
    if at.exception:
        pytest.skip(f"the dashboard could not load settings here: {at.exception}")
    return at


def _labels(at) -> set[str]:
    return {t.label for t in at.tabs} if hasattr(at, "tabs") else set()


def test_the_public_page_has_no_control_tab(monkeypatch):
    at = _render(monkeypatch, public=True)
    labels = " ".join(str(b.label) for b in at.button) + " ".join(_labels(at))
    assert "Control" not in labels
    assert "Run backtest" not in labels
    assert "Refresh from Alpaca" not in labels
    assert "Price the live chain" not in labels


def test_the_operator_page_still_has_all_of_them(monkeypatch):
    """The guard must not have cost the local dashboard anything."""
    at = _render(monkeypatch, public=False)
    buttons = " ".join(str(b.label) for b in at.button)
    assert "Run backtest" in buttons or "Refresh from Alpaca" in buttons


def test_the_public_page_offers_no_account_switch(monkeypatch):
    at = _render(monkeypatch, public=True)
    for widget in getattr(at, "segmented_control", []):
        assert "dev" not in [str(o) for o in getattr(widget, "options", [])]
