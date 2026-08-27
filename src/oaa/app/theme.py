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
}


def palette(dark: bool = False) -> dict[str, Any]:
    return DARK if dark else LIGHT


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
