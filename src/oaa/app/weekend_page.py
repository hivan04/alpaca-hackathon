"""The weekend book's tab.

Kept in its own module for the same reason the strategy is kept in its own
package: the weekend book has a different clock, a different instrument and a
different cost structure from the options books, and mixing its rendering into
`dashboard.py` would mean threading "is this crypto?" through every panel there.

The page answers three questions, in the order an operator asks them:

    where is the clock      can it trade right now, and what is open
    what is it seeing       the live gate stack, on demand (it costs a request)
    should it be trusted    the 58-weekend evidence, including the table that
                            shows the ADX gate is load-bearing rather than
                            decorative

The third section is the important one and is deliberately not a single equity
curve. Six trades in thirteen months is not a distribution, and a lone curve
would invite exactly the reading the numbers do not support.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from oaa.app.theme import is_dark, palette, style
from oaa.strategies.weekend.clock import WindowPhase

UTC = dt.timezone.utc
PARAMS_PATH = "config/strategies/weekend_crypto.yaml"

_PHASE_COPY: dict[WindowPhase, tuple[str, str]] = {
    WindowPhase.OPEN: ("OPEN", "entries and exits both allowed"),
    WindowPhase.MANAGE_ONLY: ("MANAGE ONLY", "past the last entry - exits only"),
    WindowPhase.FLATTEN: ("FLATTEN", "past the cutoff - liquidate and stay out"),
    WindowPhase.CLOSED: ("CLOSED", "an equity session is live or about to be"),
}


# --------------------------------------------------------------------------- #
# cached compute - a replay is four seconds, but not on every widget click
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def _load_params(path: str, _mtime: float) -> Any:
    from oaa.strategies.weekend.params import load_params

    return load_params(path)


def _params(path: str = PARAMS_PATH) -> Any:
    """Keyed on the file's mtime, so editing the YAML refreshes the page."""
    from pathlib import Path

    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        mtime = 0.0
    return _load_params(path, mtime)


@st.cache_data(show_spinner="Replaying weekends…")
def _backtest(path: str, _mtime: float, days: int, equity: float) -> dict[str, Any]:
    from oaa.strategies.weekend.backtest import run_backtest, sharpe_of_weekends

    params = _params(path)
    result = run_backtest(params, days=days, equity=equity)
    summary = result.summary()
    summary["weekend_sharpe"] = sharpe_of_weekends(result, equity)
    return {
        "summary": summary,
        "trades": [t.to_row() for t in result.trades],
        "by_weekend": result.by_weekend(),
        "rejections": dict(result.gate_rejections),
    }


@st.cache_data(show_spinner="Measuring forward returns…")
def _edge(path: str, _mtime: float, days: int) -> dict[str, Any]:
    from oaa.strategies.weekend.data import cached_bars
    from oaa.strategies.weekend.edgestudy import baseline, collect, tabulate, verdict

    params = _params(path)
    end = dt.datetime.now(UTC)
    bars = cached_bars(params.symbols[0], params.signal.timeframe, end - dt.timedelta(days=days), end)
    samples = collect(bars, params)
    rows = tabulate(samples, params, adx_split=True)
    return {
        "rows": rows,
        "baseline": baseline(samples),
        "verdict": verdict(rows, params),
        "n": len(samples),
    }


# --------------------------------------------------------------------------- #
# charts
# --------------------------------------------------------------------------- #
def _weekend_pnl_chart(by_weekend: dict[str, Any], colours: dict[str, Any]) -> go.Figure:
    keys = list(by_weekend)
    values = [by_weekend[k]["pnl"] for k in keys]
    fig = go.Figure(
        go.Bar(
            x=keys, y=values,
            marker={
                "color": [colours["good"] if v >= 0 else colours["critical"] for v in values],
                "line": {"width": 0},
            },
            hovertemplate="weekend of %{x}<br>$%{y:,.2f}<extra></extra>",
        )
    )
    fig.update_traces(marker_cornerradius=4)
    return style(fig, colours, height=260, ytitle="Net P&L ($)")


def _funnel_chart(rejections: dict[str, int], colours: dict[str, Any]) -> go.Figure:
    items = sorted(rejections.items(), key=lambda kv: kv[1])
    fig = go.Figure(
        go.Bar(
            x=[v for _, v in items], y=[k for k, _ in items], orientation="h",
            marker={"color": colours["series"][0], "line": {"width": 0}},
            hovertemplate="%{y}: %{x} bars declined<extra></extra>",
        )
    )
    fig.update_traces(marker_cornerradius=4)
    fig.update_yaxes(showgrid=False)
    return style(fig, colours, height=max(200, 36 * len(items) + 60), ytitle="")


def _forward_curve(
    rows: list[dict[str, Any]], colours: dict[str, Any], cost_bp: float = 54.0
) -> go.Figure:
    """Mean forward return by horizon, ranging vs trending, for the traded
    buckets. This is the whole thesis in one picture: the ranging line climbs
    away from the cost line, the trending line does not."""
    fig = go.Figure()
    # The cost line is the point of the chart: a curve below it is a signal
    # that is real and still unprofitable.
    fig.add_hline(
        y=cost_bp, line_width=1, line_dash="dot", line_color=colours["muted"],
        annotation_text=f"{cost_bp:.0f}bp round trip",
        annotation_position="top left",
        annotation_font_color=colours["muted"], annotation_font_size=11,
    )
    traded = [r for r in rows if r["bucket"].startswith(("z <= -2.5", "-2.5 < z"))]
    for index, regime in enumerate(("ranging", "trending")):
        for bucket in ("z <= -2.5", "-2.5 < z <= -2"):
            points = sorted(
                (r for r in traded if r["regime"] == regime and r["bucket"] == bucket),
                key=lambda r: r["horizon_h"],
            )
            if not points:
                continue
            fig.add_trace(
                go.Scatter(
                    x=[p["horizon_h"] for p in points],
                    y=[p["mean_bp"] for p in points],
                    name=f"{bucket}, {regime}",
                    mode="lines+markers",
                    line={
                        "width": 2,
                        "color": colours["series"][index],
                        "dash": "solid" if bucket == "z <= -2.5" else "dot",
                    },
                    hovertemplate="%{x}h: %{y:+.0f}bp<extra></extra>",
                )
            )
    return style(fig, colours, height=320, ytitle="Mean forward return (bp)")


# --------------------------------------------------------------------------- #
def render_weekend(settings: Any) -> None:
    colours = palette(is_dark())
    try:
        params = _params()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read {PARAMS_PATH}: {exc}")
        return

    now = dt.datetime.now(UTC)
    phase = params.window.phase(now)
    label, meaning = _PHASE_COPY[phase]

    st.subheader("The weekend book")
    st.caption(
        "BTC/USD mean reversion, long only, live only between the Friday equity "
        "close and the Sunday flatten. It cannot hold a position while an equity "
        "session is open, so it never competes with the carry book for Reg T."
    )

    # -- the clock ---------------------------------------------------------- #
    cols = st.columns(4)
    cols[0].metric("Window", label, help=meaning)
    cols[1].metric(
        "Hours to flatten", f"{params.window.hours_to_flatten(now):+.1f}",
        help="Hard liquidation, Sunday 20:00 UTC. No exceptions.",
    )
    cols[2].metric(
        "Last entry", f"{params.window.last_entry_at(now):%a %H:%M}Z",
        help="Eight hours of runway before the flatten - an entry with no room "
             "to work is a coin flip on the close.",
    )
    cols[3].metric(
        "Round trip cost", f"{params.costs.round_trip_bp:.0f}bp",
        help="Crypto fees are a percentage of notional. This is the number "
             "every signal has to beat.",
    )

    if phase is WindowPhase.CLOSED:
        st.info(
            f"Closed until {params.window.opens_at(now + dt.timedelta(days=7)):%a %d %b %H:%M}Z. "
            "Nothing in this book can hold a position while equities trade."
        )

    live_dry = "dry run" if params.execution.dry_run else "LIVE"
    st.caption(
        f"`{params.describe()}`  ·  enabled: **{params.enabled}**  ·  execution: **{live_dry}**"
    )

    # -- what is open ------------------------------------------------------- #
    _render_position(colours)

    st.divider()
    _render_evidence(colours, params)


def _render_position(colours: dict[str, Any]) -> None:
    from oaa.strategies.weekend.engine import WeekendState

    state = WeekendState.load()
    if state.position is None:
        st.success("Flat." if not state.cooldown_until else
                   f"Flat. Cooling down until {state.cooldown_until} (a stop means "
                   f"the regime read was wrong; straight back in is how one bad "
                   f"read becomes four).")
        return
    pos = state.position
    cols = st.columns(5)
    cols[0].metric("Symbol", pos.symbol)
    cols[1].metric("Quantity", f"{pos.qty:.6f}")
    cols[2].metric("Entry", f"${pos.entry:,.0f}")
    cols[3].metric("Stop", f"${pos.stop:,.0f}",
                   help="Enforced by the engine's poll, not resting at the broker - "
                        "a resting stop on a 24/7 venue gets swept by a wick that "
                        "trades no size.")
    cols[4].metric("Target", f"${pos.target:,.0f}")
    st.caption(
        f"entered {pos.entered_at} · z {pos.z:+.2f} · ADX {pos.adx:.0f} · "
        f"max loss ${(pos.entry - pos.stop) * pos.qty:,.2f}"
    )


def _render_evidence(colours: dict[str, Any], params: Any) -> None:
    from pathlib import Path

    from oaa.strategies.weekend.data import InsufficientHistory

    st.subheader("Does it work?")
    days = st.slider("History to study (days)", 60, 400, 400, step=20)
    equity = st.number_input("Replay equity ($)", 10_000, 1_000_000, 100_000, step=10_000)

    try:
        mtime = Path(PARAMS_PATH).stat().st_mtime
    except OSError:
        mtime = 0.0

    try:
        replay = _backtest(PARAMS_PATH, mtime, days, float(equity))
        study = _edge(PARAMS_PATH, mtime, days)
    except InsufficientHistory as exc:
        st.warning(str(exc))
        st.code("python3 scripts/fetch_weekend_bars.py --days 410", language="bash")
        return
    except Exception as exc:  # noqa: BLE001
        st.error(f"Study failed: {exc}")
        return

    summary = replay["summary"]
    cols = st.columns(5)
    cols[0].metric("Weekends", summary["weekends"])
    cols[1].metric("Trades", summary["trades"],
                   help=f"{summary['trades_per_weekend']} per weekend")
    cols[2].metric("Hit rate", f"{summary['hit_rate'] * 100:.0f}%")
    cols[3].metric("Net P&L", f"${summary['net_pnl']:,.0f}",
                   delta=f"{summary['return_pct'] * 100:.2f}% of equity")
    cols[4].metric("Cost drag", f"${summary['cost_drag']:,.0f}",
                   help="Gross minus net: what the fee model took.")

    st.warning(
        f"**Read this before the numbers.** {summary['trades']} trades across "
        f"{summary['weekends']} weekends is not a distribution, and the parameters "
        f"were calibrated on the same history they are quoted against. Treat this "
        f"as evidence that the gates do something real, not as an expected return."
    )

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Forward return after a displaced weekend**")
        st.plotly_chart(
            _forward_curve(study["rows"], colours, params.costs.round_trip_bp),
            width="stretch",
        )
        st.caption(
            "Model-free: no entry rule, no stop, no sizing. The reversion builds "
            "with horizon in the ranging regime and does not in the trending one - "
            "which is what the ADX gate is for. Unconditional forward return over "
            "the same bars: "
            + ", ".join(f"{h:g}h {v:+.0f}bp" for h, v in study["baseline"].items())
            + f". The horizontal reference is the {params.costs.round_trip_bp:.0f}bp round trip."
        )
    with right:
        st.markdown("**Where the bars went**")
        st.plotly_chart(_funnel_chart(replay["rejections"], colours), width="stretch")
        st.caption(
            "Gates run cheapest-first and the first veto ends the evaluation, so "
            "each bar is attributed to exactly one number."
        )

    st.info(study["verdict"])

    if replay["by_weekend"]:
        st.markdown("**P&L by weekend**")
        st.plotly_chart(_weekend_pnl_chart(replay["by_weekend"], colours), width="stretch")

    with st.expander("Every trade the replay took"):
        if replay["trades"]:
            frame = pd.DataFrame(replay["trades"])
            st.dataframe(
                frame[["entered_at", "z", "adx", "entry", "exit_price", "exit_reason",
                       "pnl", "pnl_bp", "bars_held"]],
                width="stretch", hide_index=True,
            )
        else:
            st.write(
                "No trades. That is an answer, not a failure - check the funnel "
                "above for which gate refused, and whether it was right to."
            )

    with st.expander("The forward-return table in full"):
        rows = pd.DataFrame(study["rows"])
        if not rows.empty:
            st.dataframe(rows, width="stretch", hide_index=True)
        st.caption(
            "`n` counts overlapping 15-minute bars; `episodes` counts "
            "non-overlapping ones and is the sample size that carries "
            "information. The t-statistic uses episodes alone - pooling "
            "overlapping bars inside one dislocation counts a single event many "
            "times and manufactures significance."
        )
