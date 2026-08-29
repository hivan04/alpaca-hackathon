"""Chart palette and Plotly chrome for the dashboard.

The colours are a validated categorical palette: hue order fixed, never cycled,
checked for colour-vision separation against both surfaces. Series identity is
always carried by a legend or a direct label as well as by colour, and no chart
in this dashboard uses two y-axes.
"""

from __future__ import annotations

from typing import Any

LIGHT: dict[str, Any] = {
    "surface": "#fcfcfb",
    "plane": "#f9f9f7",
    "text": "#0b0b0b",
    "text_secondary": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4",
               "#008300", "#4a3aa7", "#e34948"],
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
    "up_text": "#006300",
    "accent": "#1388a8",
}

DARK: dict[str, Any] = {
    "surface": "#1a1a19",
    "plane": "#0d0d0d",
    "text": "#ffffff",
    "text_secondary": "#c3c2b7",
    "muted": "#898781",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181",
               "#008300", "#9085e9", "#e66767"],
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
    "up_text": "#0ca30c",
    "accent": "#4cc4de",
}


# The one highlight colour, in the two modes. The dark value is the same hue
# lifted in lightness and chroma - a teal that is legible on #1a1a19 the way
# #1388a8 is on #fcfcfb. Both are also written into .streamlit/config.toml as
# theme.light/dark primaryColor, which is what Streamlit's own chrome (selected
# tabs, focus rings, sliders, links) uses; keep the two in step.
ACCENT_LIGHT = LIGHT["accent"]
ACCENT_DARK = DARK["accent"]

#: Session-state key holding the mode the user chose in the sidebar toggle.
MODE_KEY = "_theme_mode"


def palette(dark: bool = False) -> dict[str, Any]:
    return DARK if dark else LIGHT


def accent(dark: bool = False) -> str:
    """The highlight colour for the given mode."""
    return ACCENT_DARK if dark else ACCENT_LIGHT


def is_dark() -> bool:
    """The mode this render should draw in.

    The sidebar toggle wins; failing that the browser's reported theme; failing
    that the configured base. Never raises - a chart with the wrong background
    is better than a page that does not render.
    """
    try:
        import streamlit as st
    except Exception:  # noqa: BLE001
        return False
    chosen = st.session_state.get(MODE_KEY)
    if chosen in ("light", "dark"):
        return chosen == "dark"
    try:
        reported = getattr(getattr(st, "context", None), "theme", None)
        if reported is not None and getattr(reported, "type", None):
            return str(reported.type).lower() == "dark"
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(st.get_option("theme.base") or "light").lower() == "dark"
    except Exception:  # noqa: BLE001
        return False


def set_mode(dark: bool) -> bool:
    """Record the chosen mode and push it into Streamlit's own theme.

    Streamlit rebuilds the frontend theme from config on every script run, so
    setting `theme.base` here and rerunning repaints the chrome as well as the
    charts. Returns True when the base actually changed, i.e. when the caller
    needs to `st.rerun()`.
    """
    import streamlit as st

    want = "dark" if dark else "light"
    st.session_state[MODE_KEY] = want
    try:
        from streamlit import config as _config

        if str(st.get_option("theme.base") or "light").lower() == want:
            return False
        _config.set_option("theme.base", want, "<user defined>")
    except Exception:  # noqa: BLE001
        return False
    return True


def mode_toggle(container: Any = None, key: str = "_theme_dark_toggle") -> bool:
    """Render the light/dark switch. Reruns when the mode changes."""
    import streamlit as st

    target = container if container is not None else st
    dark = bool(target.toggle(
        "Dark mode",
        value=is_dark(),
        key=key,
        help="Repaints the page and every chart. The highlight colour stays "
             f"the same hue - {ACCENT_LIGHT} on light, {ACCENT_DARK} on dark.",
    ))
    if set_mode(dark):
        st.rerun()
    return dark


def style(fig: Any, colours: dict[str, Any], height: int = 320, ytitle: str = "") -> Any:
    """Recessive grid, hairline axes, tabular ticks, crosshair hover."""
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 28, "b": 8},
        paper_bgcolor=colours["surface"],
        plot_bgcolor=colours["surface"],
        font={"color": colours["text_secondary"], "size": 12,
              "family": "system-ui, -apple-system, Segoe UI, sans-serif"},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.12, "x": 0, "font": {"size": 11}},
    )
    fig.update_xaxes(
        showgrid=False, zeroline=False,
        linecolor=colours["axis"], tickcolor=colours["axis"],
        tickfont={"color": colours["muted"], "size": 11},
    )
    fig.update_yaxes(
        title=ytitle,
        gridcolor=colours["grid"], zeroline=True, zerolinecolor=colours["axis"],
        linecolor=colours["axis"],
        tickfont={"color": colours["muted"], "size": 11},
    )
    return fig
