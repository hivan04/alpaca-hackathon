"""The Live Trading tab.

What the backtest tab is for one replayed window, this is for the account that
is actually being judged: what the agent can see right now, what it has done,
and what let each trade through.

Four things it shows, and where each comes from:

    live option chain   the REAL bid and ask, from Alpaca's chain snapshot via
                        the configured data provider. No model anywhere in it -
                        this is the surface the live agent prices against, and
                        the one number the backtest could never obtain.
    volatility surface  built from that same snapshot: skew by strike, term
                        structure by expiry, and the two together as a grid.
                        Alpaca computes the IV; this only arranges it.
    performance         today and since-inception, from the journal's equity
                        snapshots. The judged window is one week, so
                        "since inception" is the number that matters and the
                        daily one is for watching it happen.
    justification       per decision: the gates it passed, the metrics each one
                        measured, the risk-engine checks, and the critic's
                        verdict - read out of the decision payload the live
                        agent already writes.

Read-only by design. Nothing on this page can place, size or cancel an order.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from oaa.app.theme import style


# --------------------------------------------------------------------------- #
# performance
# --------------------------------------------------------------------------- #
def window_metrics(rows: list[dict[str, Any]], since: dt.date | None = None) -> dict[str, Any]:
    """Equity-derived metrics over the whole series, or from a given date.

    Deliberately computed from the account's own equity snapshots rather than
    from the trade log: that is what a judge reading the account sees, and it
    includes anything the trade log missed.
    """
    series = [
        (str(r["ts"]), float(r["equity"]))
        for r in rows
        if r.get("equity") is not None
    ]
    series.sort()
    if since is not None:
        cut = since.isoformat()
        series = [(ts, eq) for ts, eq in series if ts[:10] >= cut]
    if len(series) < 2:
        return {"snapshots": len(series), "start": None, "end": None,
                "pl": 0.0, "return_pct": 0.0, "max_drawdown": 0.0, "peak": None}

    values = [eq for _, eq in series]
    start, end = values[0], values[-1]
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = min(drawdown, (value - peak) / peak)
    return {
        "snapshots": len(series),
        "start": round(start, 2),
        "end": round(end, 2),
        "pl": round(end - start, 2),
        "return_pct": round((end - start) / start, 5) if start else 0.0,
        "max_drawdown": round(drawdown, 5),
        "peak": round(max(values), 2),
        "first_ts": series[0][0],
        "last_ts": series[-1][0],
    }


# --------------------------------------------------------------------------- #
# the live chain
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=30, show_spinner=False)
def fetch_chain(
    profile: str,
    config_path: str | None,
    symbol: str,
    backend: str | None = None,
) -> list[dict[str, Any]]:
    """One live chain snapshot, cached briefly.

    A 30-second TTL is a quota decision, not a UI one: the free tier allows 200
    requests a minute and a chain request is not cheap. Re-rendering the page
    must not re-hit the API.
    """
    from oaa.config.loader import load_settings
    from oaa.data.factory import get_data_provider

    settings = load_settings(config_path=config_path, profile=profile)
    if backend:
        # A missing `alpaca` binary should not be a dead end on the one page
        # that needs live quotes - alpaca-py ships with the package.
        settings.config.data.provider = backend
    provider = get_data_provider(settings.config, settings.credentials)
    quotes = provider.option_chain(symbol)
    return [
        {
            "symbol": q.symbol,
            "expiry": q.expiry.isoformat(),
            "strike": q.strike,
            "right": q.right.value,
            "bid": q.bid,
            "ask": q.ask,
            "mid": q.mid,
            "spread": None if q.bid is None or q.ask is None else round(q.ask - q.bid, 4),
            "spread_pct": q.spread_pct,
            "iv": q.implied_volatility,
            "delta": q.greeks.delta,
            "gamma": q.greeks.gamma,
            "theta": q.greeks.theta,
            "vega": q.greeks.vega,
            "open_interest": q.open_interest,
            "volume": q.volume,
        }
        for q in quotes
    ]


def _atm_row(frame: pd.DataFrame, spot: float | None) -> pd.DataFrame:
    if spot is None or frame.empty:
        return frame
    return frame.assign(distance=(frame["strike"] - spot).abs()).sort_values("distance")


# --------------------------------------------------------------------------- #
# volatility surface
# --------------------------------------------------------------------------- #
def skew_chart(frame: pd.DataFrame, colours: dict[str, Any], spot: float | None) -> go.Figure:
    """Implied vol against strike, one line per expiry.

    A readable slice of the surface. Two things to look for: the downward
    left-to-right slope, which is the put skew a short-premium book sells into,
    and whether the near expiry sits above or below the far one - an inversion
    is the market pricing a dated event.
    """
    fig = go.Figure()
    expiries = sorted(frame["expiry"].unique())[:6]
    for index, expiry in enumerate(expiries):
        slice_ = frame[(frame["expiry"] == expiry) & frame["iv"].notna()].sort_values("strike")
        if slice_.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=slice_["strike"], y=slice_["iv"] * 100,
                name=str(expiry), mode="lines+markers",
                line={"width": 2, "color": colours["series"][index % 8]},
                marker={"size": 6},
                hovertemplate=(
                    f"{expiry}<br>strike %{{x}}<br>IV %{{y:.1f}}%<extra></extra>"
                ),
            )
        )
    if spot:
        fig.add_vline(
            x=spot, line_width=1, line_dash="dot", line_color=colours["muted"],
            annotation_text="spot", annotation_position="top",
            annotation_font_color=colours["muted"], annotation_font_size=11,
        )
    return style(fig, colours, height=340, ytitle="Implied volatility (%)")


def term_chart(frame: pd.DataFrame, colours: dict[str, Any], spot: float | None) -> go.Figure:
    """ATM implied vol against days to expiry - the term structure."""
    today = dt.date.today()
    points: list[tuple[int, float]] = []
    for expiry in sorted(frame["expiry"].unique()):
        slice_ = frame[(frame["expiry"] == expiry) & frame["iv"].notna()]
        if slice_.empty or spot is None:
            continue
        nearest = _atm_row(slice_, spot).head(2)
        if nearest.empty:
            continue
        dte = (dt.date.fromisoformat(str(expiry)) - today).days
        points.append((dte, float(nearest["iv"].mean()) * 100))
    fig = go.Figure(
        go.Scatter(
            x=[d for d, _ in points], y=[v for _, v in points],
            mode="lines+markers", line={"width": 2, "color": colours["series"][0]},
            marker={"size": 8},
            hovertemplate="%{x} DTE<br>ATM IV %{y:.1f}%<extra></extra>",
        )
    )
    return style(fig, colours, height=260, ytitle="ATM implied vol (%)")


def surface_grid(frame: pd.DataFrame, colours: dict[str, Any]) -> go.Figure:
    """Strike x expiry x IV as a grid. One hue, light to dark - magnitude."""
    pivot = (
        frame[frame["iv"].notna()]
        .pivot_table(index="expiry", columns="strike", values="iv", aggfunc="mean")
        .sort_index()
    )
    fig = go.Figure(
        go.Heatmap(
            z=pivot.values * 100,
            x=pivot.columns, y=[str(i) for i in pivot.index],
            colorscale=[[0.0, "#cde2fb"], [0.5, "#3987e5"], [1.0, "#0d366b"]],
            colorbar={"title": "IV %", "thickness": 12},
            hovertemplate="strike %{x}<br>%{y}<br>IV %{z:.1f}%<extra></extra>",
        )
    )
    fig.update_yaxes(showgrid=False)
    return style(fig, colours, height=max(240, 34 * len(pivot) + 90), ytitle="")


# --------------------------------------------------------------------------- #
# decisions and their justification
# --------------------------------------------------------------------------- #
def decision_justification(row: dict[str, Any]) -> dict[str, Any]:
    """Unpack what let a decision through, from the payload the agent wrote.

    The live agent already records everything needed - gates, gate metrics,
    risk checks, the critic's verdict - inside the decision payload. Nothing
    here is recomputed or inferred; it is read out and arranged.
    """
    try:
        payload = json.loads(row.get("payload") or "{}")
    except json.JSONDecodeError:
        return {}
    idea = payload.get("idea") or {}
    verdict = payload.get("verdict") or {}
    meta = idea.get("meta") or {}
    return {
        "gates": meta.get("gates") or {},
        "selection": meta.get("selection") or {},
        "modelled_cost": meta.get("modelled_cost") or {},
        "risk_checks": verdict.get("checks") or {},
        "risk_reasons": verdict.get("reasons") or [],
        "approved": verdict.get("approved"),
        "stamp": verdict.get("stamp"),
        "critic": payload.get("agent_notes") or {},
        "thesis": idea.get("thesis") or payload.get("rationale") or "",
        "legs": idea.get("legs") or [],
        "book": idea.get("book"),
        "confidence": idea.get("confidence"),
        "cycle": payload.get("cycle"),
        "action": payload.get("action"),
        "error": payload.get("error"),
    }
