"""Pairwise correlation between the underlyings, for the backtest and live tabs.

Why this is on the dashboard at all: every book here is short or long premium on
a handful of large-cap names, and the risk that actually shows up in the equity
curve is not the risk of any one position - it is the risk that six positions
are one position. Two names correlating at 0.9 on daily returns are one bet held
twice, and the drawdown chart is the first place that becomes visible and the
last place it should be discovered.

Two surfaces, one calculation:

    backtest   daily returns of the underlyings the replay actually offered the
               strategies, over the replayed window, from the same bars the
               engine saw. Stored on the run, so a saved run reopens with its
               own correlations rather than today's.
    live       daily bars for the active universe pulled from the configured
               data provider. Returns by default; price levels are offered
               because that is the question people ask, with the caveat that
               two trending series correlate at 0.99 while telling you nothing.

Everything here is pure pandas on a frame of closes. Nothing fetches; the
callers supply the closes so this module stays testable without a network or a
Streamlit session.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go

from oaa.app.theme import style

#: Blue - neutral - red, symmetric about zero. Correlation is diverging data and
#: a sequential ramp would hide the sign, which is the whole point of the chart.
DIVERGING = [
    [0.0, "#2a5fa8"], [0.25, "#8fb4dd"], [0.5, "#f2f1ec"],
    [0.75, "#e79a86"], [1.0, "#c0392b"],
]

MIN_OBSERVATIONS = 5


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #
def closes_frame(closes: dict[str, Any]) -> pd.DataFrame:
    """`{symbol: [(date, close), ...]}` -> a date-indexed frame of closes.

    Dates are truncated to the day and the columns are aligned on the dates all
    symbols share. A symbol that was listed mid-window, or whose bars failed to
    fetch, otherwise drags an inner join down to nothing.
    """
    series: dict[str, pd.Series] = {}
    for symbol, rows in (closes or {}).items():
        pairs = [
            (str(ts)[:10], float(value))
            for ts, value in (tuple(r) for r in rows)
            if value is not None
        ]
        if len(pairs) < MIN_OBSERVATIONS:
            continue
        frame = pd.DataFrame(pairs, columns=["date", "close"]).drop_duplicates("date")
        series[symbol.upper()] = frame.set_index("date")["close"].sort_index()
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index()


def returns_frame(prices: pd.DataFrame) -> pd.DataFrame:
    """Simple daily returns, on the dates every column has a price for."""
    if prices.empty:
        return prices
    return prices.dropna(how="any").pct_change().dropna(how="any")


def matrix(frame: pd.DataFrame) -> pd.DataFrame:
    """Correlation matrix, columns ordered so the tight cluster sits together.

    Ordering by each name's average correlation to the rest puts the block that
    moves as one in the top-left corner, which is where the concentration is.
    """
    if frame.empty or frame.shape[1] < 2 or len(frame) < MIN_OBSERVATIONS:
        return pd.DataFrame()
    corr = frame.corr()
    order = (corr.sum(axis=1) - 1.0).sort_values(ascending=False).index
    return corr.loc[order, order]


def pairs_table(corr: pd.DataFrame, observations: int) -> pd.DataFrame:
    """Every unordered pair once, most-correlated first."""
    if corr.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    names = list(corr.columns)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            value = corr.loc[left, right]
            if pd.isna(value):
                continue
            rows.append(
                {
                    "pair": f"{left} / {right}",
                    "correlation": round(float(value), 3),
                    "r_squared": round(float(value) ** 2, 3),
                    "observations": observations,
                }
            )
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .sort_values("correlation", ascending=False)
        .reset_index(drop=True)
    )


def summary(corr: pd.DataFrame) -> dict[str, Any]:
    """Mean, highest and lowest off-diagonal correlation."""
    if corr.empty or len(corr) < 2:
        return {}
    values = corr.copy()
    for name in values.columns:
        values.loc[name, name] = float("nan")
    flat = values.stack()
    if flat.empty:
        return {}
    top = flat.idxmax()
    bottom = flat.idxmin()
    return {
        "mean": float(flat.mean()),
        "max": float(flat.max()),
        "max_pair": f"{top[0]} / {top[1]}",
        "min": float(flat.min()),
        "min_pair": f"{bottom[0]} / {bottom[1]}",
    }


# --------------------------------------------------------------------------- #
# chart
# --------------------------------------------------------------------------- #
def heatmap(corr: pd.DataFrame, colours: dict[str, Any], title: str = "") -> go.Figure:
    """The matrix as a labelled grid, fixed to -1..1 so runs are comparable."""
    names = list(corr.columns)
    fig = go.Figure(
        go.Heatmap(
            z=corr.values,
            x=names,
            y=names,
            zmin=-1.0,
            zmax=1.0,
            colorscale=DIVERGING,
            colorbar={"title": "r", "thickness": 12},
            text=[[f"{v:.2f}" for v in row] for row in corr.values],
            texttemplate="%{text}",
            textfont={"size": 11},
            hovertemplate="%{y} vs %{x}<br>r = %{z:.3f}<extra></extra>",
        )
    )
    fig.update_xaxes(showgrid=False, side="bottom")
    fig.update_yaxes(showgrid=False, autorange="reversed")
    if title:
        fig.update_layout(title={"text": title, "font": {"size": 13}})
    return style(fig, colours, height=max(280, 42 * len(names) + 120), ytitle="")


# --------------------------------------------------------------------------- #
# fetchers
# --------------------------------------------------------------------------- #
# Both are cached on the Streamlit side. The live one has a five-minute TTL:
# these are DAILY bars, so a shorter one would spend quota re-fetching a number
# that changes once a session. The historical one has none - a closed window
# does not change at all.
try:  # pragma: no cover - exercised by the dashboard, not by the test suite
    import streamlit as _streamlit

    _cache_live = _streamlit.cache_data(ttl=300, show_spinner=False)
    _cache_history = _streamlit.cache_data(show_spinner=False)
except Exception:  # noqa: BLE001 - importable without Streamlit for tests
    def _cache_live(fn):  # type: ignore[misc]
        return fn

    def _cache_history(fn):  # type: ignore[misc]
        return fn


@_cache_live
def live_closes(
    profile: str,
    config_path: str | None,
    symbols: tuple[str, ...],
    lookback_days: int,
) -> dict[str, list[tuple[str, float]]]:
    """Daily closes for the live universe, from the configured data provider."""
    from oaa.config.loader import load_settings
    from oaa.data.factory import get_data_provider

    settings = load_settings(config_path=config_path, profile=profile)
    provider = get_data_provider(settings.config, settings.credentials)
    out: dict[str, list[tuple[str, float]]] = {}
    for symbol in symbols:
        try:
            bars = provider.bars(symbol, lookback_days=lookback_days, timeframe="1Day")
        except Exception:  # noqa: BLE001 - one bad symbol must not empty the grid
            continue
        rows = [
            (str(b.get("timestamp"))[:10], float(b["close"]))
            for b in bars or []
            if b.get("close") is not None
        ]
        if rows:
            out[symbol.upper()] = rows
    return out


@_cache_history
def replay_closes(
    profile: str,
    config_path: str | None,
    symbols: tuple[str, ...],
    start: str,
    end: str,
) -> dict[str, list[tuple[str, float]]]:
    """Daily closes over a past window, for runs saved before this panel existed.

    Goes through the backtest's own disk-cached historical feed, so a window
    that has already been replayed needs no network at all.
    """
    import datetime as dt

    from oaa.backtest.feed import HistoricalFeed
    from oaa.config.loader import load_settings

    settings = load_settings(config_path=config_path, profile=profile)
    cfg = settings.config
    feed = HistoricalFeed(
        api_key=settings.credentials.api_key,
        secret_key=settings.credentials.secret_key,
        cache_dir=settings.path(cfg.backtest.cache_dir),
        stock_feed=cfg.data.stock_feed,
        offline=False,
    )
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    out: dict[str, list[tuple[str, float]]] = {}
    for symbol in symbols:
        try:
            bars = feed.bars(symbol, first, last, "1Day")
        except Exception:  # noqa: BLE001
            continue
        rows = [
            (str(b.get("timestamp"))[:10], float(b["close"]))
            for b in bars or []
            if b.get("close") is not None
        ]
        if rows:
            out[symbol.upper()] = rows
    return out
