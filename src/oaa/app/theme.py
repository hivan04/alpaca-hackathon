"""Chart palette and Plotly chrome for the dashboard.

The colours are a validated categorical palette: hue order fixed, never cycled,
checked for colour-vision separation against both surfaces. Series identity is
always carried by a legend or a direct label as well as by colour, and no chart
in this dashboard uses two y-axes.
"""

from __future__ import annotations

from typing import Any

LIGHT: dict[str, Any] = {
    "surface": "#f6f7f9",
    "plane": "#ffffff",
    "raised": "#f0f2f5",
    "text": "#12161f",
    "text_secondary": "#6b7280",
    "muted": "#9aa1ac",
    "grid": "#e3e6eb",
    "axis": "#c9ced6",
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4",
               "#008300", "#4a3aa7", "#e34948"],
    "good": "#178a5c",
    "warning": "#c97f0e",
    "serious": "#ec835a",
    "critical": "#d1373c",
    "up_text": "#178a5c",
    "accent": "#1388a8",
}

DARK: dict[str, Any] = {
    "surface": "#0a0e14",
    "plane": "#10151f",
    "raised": "#141b28",
    "text": "#e8edf4",
    "text_secondary": "#6b7688",
    "muted": "#414c5e",
    "grid": "#1c2430",
    "axis": "#2a3442",
    "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181",
               "#2f9e2f", "#9085e9", "#e66767"],
    "good": "#3ddc97",
    "warning": "#f5a623",
    "serious": "#ec835a",
    "critical": "#ff5c5c",
    "up_text": "#3ddc97",
    "accent": "#4cc4de",
}



# The one highlight colour, in the two modes. A teal - the dark value is the
# same hue lifted in lightness and chroma, so #4cc4de reads on #0a0e14 the way
# #1388a8 reads on #f6f7f9. It is chrome and "look here" ONLY. Direction is
# never carried by the accent: profit is `good`, loss is `critical`, caution is
# `warning` (amber), and those are the only colours allowed to mean anything.
# Both accents are also written into .streamlit/config.toml as theme.light/dark
# primaryColor, which is what Streamlit's own chrome (selected tabs, focus
# rings, sliders, links) uses; keep the two in step.
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


#: Streamlit's own chrome, per mode. Set on the *base* [theme] section rather
#: than [theme.light]/[theme.dark], because a viewer who has picked Light or
#: Dark in Streamlit's Appearance menu has that choice saved in their browser
#: and it beats `theme.base`. Values written to the base section apply whatever
#: that saved preference says, so the sidebar toggle always wins.
CHROME: dict[str, dict[str, str]] = {
    "light": {
        "theme.backgroundColor": LIGHT["surface"],
        "theme.secondaryBackgroundColor": LIGHT["plane"],
        "theme.textColor": LIGHT["text"],
        "theme.borderColor": LIGHT["grid"],
        "theme.primaryColor": ACCENT_LIGHT,
        "theme.linkColor": ACCENT_LIGHT,
    },
    "dark": {
        "theme.backgroundColor": DARK["surface"],
        "theme.secondaryBackgroundColor": DARK["plane"],
        "theme.textColor": DARK["text"],
        "theme.borderColor": DARK["grid"],
        "theme.primaryColor": ACCENT_DARK,
        "theme.linkColor": ACCENT_DARK,
    },
}


def apply_mode(dark: bool) -> bool:
    """Push the chosen mode into Streamlit's own theme config.

    Streamlit rebuilds the frontend theme from config on every script run, so
    writing the options here and rerunning repaints the chrome as well as the
    charts. Returns True when something actually changed - i.e. when the caller
    needs to `st.rerun()`.
    """
    import streamlit as st

    want = "dark" if dark else "light"
    st.session_state[MODE_KEY] = want
    changed = False
    try:
        from streamlit import config as _config

        for key, value in ({"theme.base": want} | CHROME[want]).items():
            if str(st.get_option(key) or "").lower() != value.lower():
                _config.set_option(key, value, "<user defined>")
                changed = True
    except Exception:  # noqa: BLE001
        return False
    return changed


#: Kept for callers that predate the rename.
set_mode = apply_mode


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
    if apply_mode(dark):
        st.rerun()
    return dark


def style(fig: Any, colours: dict[str, Any], height: int = 320, ytitle: str = "") -> Any:
    """Recessive grid, hairline axes, tabular ticks, crosshair hover."""
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 28, "b": 8},
        paper_bgcolor=colours["surface"],
        plot_bgcolor=colours["surface"],
        font={"color": colours["text_secondary"], "size": 11,
              "family": "'IBM Plex Mono', ui-monospace, SFMono-Regular, monospace"},
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
