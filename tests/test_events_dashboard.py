"""The Events tab renders, and the Control tab tells the truth about it.

Streamlit failures are runtime failures - an import test proves nothing, so
these drive the real script with `AppTest`. Skipped where streamlit is not
installed, since the dashboard is an optional extra.
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402


def _run(fn):
    return AppTest.from_function(fn).run(timeout=90)


def test_the_events_tab_renders_against_the_shipped_calendar():
    def app():
        from oaa.app.events_page import render_events
        from oaa.config.loader import load_settings

        render_events(load_settings())

    at = _run(app)
    assert not at.exception
    assert not at.error
    labels = {m.label for m in at.metric}
    assert {"Confirmed events", "Unconfirmed", "Arms today"} <= labels


def test_an_unconfirmed_print_is_called_out_on_the_page():
    """CPRT is in the calendar with confirmed=false. An operator looking at this
    page must be able to see that it will not be armed, not infer it."""
    def app():
        from oaa.app.events_page import render_events
        from oaa.config.loader import load_settings

        render_events(load_settings())

    at = _run(app)
    warnings = " ".join(str(w.value) for w in at.warning)
    assert "CPRT" in warnings
    assert "not armed" in warnings.lower()


def test_the_control_tab_offers_no_switch_for_the_events_book():
    """The events book runs in its own process and never leases firewall
    capital, so `oaa run` cannot open a position for it. A toggle here would
    imply control the page does not have - it must render as a read-only row.
    """
    def app():
        from oaa.app.control import render_control
        from oaa.config.loader import load_settings

        settings = load_settings()
        render_control({"dev": settings, "judged": settings})

    at = _run(app)
    assert not at.exception
    text = " ".join(str(m.value) for m in at.markdown)
    assert "Earnings events" in text, "the events book must still be listed"
    assert "separate process" in text
    assert "own process" in text
    # Three switchable books across two accounts: vol_carry, intraday_momentum,
    # event_premium. Was five until 29 Aug, when momentum_debit_spread and
    # earnings_calendar were deleted - both were multi-day strategies homed in
    # the intraday book, which the firewall liquidates at 15:15, so neither
    # could reach its own exit rules. If this number changes again, a book was
    # added or removed and this page needs to say so deliberately.
    assert len(at.toggle) == 6


def test_every_configured_strategy_appears_on_the_control_tab():
    """The Control tab answers "which books exist". A strategy in config that
    the page omits is a book an operator cannot see or switch."""
    from oaa.app.control import STRATEGIES
    from oaa.config.loader import load_config

    listed = {spec["name"] for spec in STRATEGIES}
    configured = {ref.name for ref in load_config().strategies}
    missing = configured - listed
    assert not missing, f"{missing} are configured but missing from the Control tab"


def test_a_stale_dashboard_process_is_named_as_such_not_as_a_broken_config():
    """The trap this page sets for itself.

    Streamlit keeps already-imported modules, so a server started before a new
    parameter was added parses the new YAML with the old dataclass and reports
    `unknown key(s) ...`. That reads as a broken config file when the config is
    fine and the process is stale - and the sidebar's "Reload config" does not
    fix it, because it clears the cache without re-importing Python. The page
    must say so.
    """
    def app():
        import oaa.app.events_page as page
        from oaa.app.events_page import render_events
        from oaa.core.errors import ConfigError

        def boom(_settings):
            raise ConfigError("direction: unknown key(s) derive_from_tape_when_no_call")

        page._params = boom          # simulate the stale-module failure
        render_events(object())

    at = _run(app)
    assert not at.exception
    errors = " ".join(str(e.value) for e in at.error)
    assert "older code" in errors
    body = " ".join(str(m.value) for m in at.markdown)
    assert "Restart the dashboard" in body
    assert "Reload config" in body
