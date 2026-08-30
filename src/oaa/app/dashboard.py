"""The operator dashboard: backtesting, live trading and positions.

Run it with `oaa dashboard` (or `streamlit run src/oaa/app/dashboard.py`).

Whichever tab is on screen, the resolved account identity is printed to the
terminal running the process AND shown at the top of the page: profile, masked
API key, which environment variable it came from, and whether these are the
judged keys. Pointing a page at the wrong account is the most expensive
available mistake this week, so it is never something you have to go and check.

Backtesting tab
---------------
Everything the replay produces, in the order a reader needs it:

    what was traded      universe, horizon, trade count, data provenance
    how it did           P&L, Sharpe, drawdown, volatility, worst trade
    what it looked like  equity path, drawdown, cumulative net vs gross
    why each trade       every fill with the gates it passed and the state of
                         the market when it fired
    why NOT              the gate funnel: candidates the agent declined

The provenance banner is not decoration. The underlying prices are real Alpaca
bars and the headlines are real Alpaca news, but the option chain is modelled -
and a reader who does not know that will read the equity curve as something it
is not.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any


def _bootstrap_path() -> None:
    """Make `streamlit run src/oaa/app/dashboard.py` work uninstalled.

    `streamlit run` puts the SCRIPT's directory on sys.path, not the repo root,
    so `import oaa` fails unless the package is installed in the interpreter
    Streamlit itself is running under. That is a real trap: `.venv` can have
    `oaa` installed perfectly while a Homebrew or pipx `streamlit` earlier on
    PATH is the one that actually starts, and the only symptom is
    `ModuleNotFoundError: No module named 'oaa'` pointing at this file.

    Adding `<repo>/src` here removes that failure mode. It is a no-op when the
    package is installed properly, which is still the supported setup -
    `oaa dashboard` launches Streamlit under `sys.executable` and never needs
    this.
    """
    try:
        import oaa  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "src"
        if (candidate / "oaa" / "__init__.py").exists():
            sys.path.insert(0, str(candidate))
            return


_bootstrap_path()

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from oaa.app import correlation as corr  # noqa: E402
from oaa.app import identity as ident  # noqa: E402
from oaa.app.control import render_control  # noqa: E402
from oaa.app.events_page import render_events  # noqa: E402
from oaa.app.positions import render_positions  # noqa: E402
from oaa.app.theme import is_dark, mode_toggle, palette, style  # noqa: E402
from oaa.core.errors import DataError  # noqa: E402

PAGE_BACKTEST = "Backtesting"
PAGE_LIVE = "Live Trading"
PAGE_POSITIONS = "Positions"
PAGE_EVENTS = "Events"
PAGE_CONTROL = "Control"


# --------------------------------------------------------------------------- #
# settings & identity
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def _settings(profile: str, config_path: str | None):
    from oaa.config.loader import load_settings
    from oaa.core.logging import setup_logging

    settings = load_settings(config_path=config_path, profile=profile)
    # WARNING, not the config's level. A replay logs one line per candidate per
    # gate per session - thousands of lines for a six-symbol window - and every
    # one of them is already captured in the run's rejection log and rendered
    # under "What the agent declined". Repeating it in the terminal buries the
    # lines that actually need a human: failed fetches, degraded providers,
    # dropped contracts.
    setup_logging("WARNING", "console")
    return settings


def _announce(settings: Any, page: str) -> ident.Identity:
    """Resolve the identity, and print it to the terminal when it changes."""
    who = ident.resolve(settings, page)
    key = (page, who.profile, who.key_masked)
    if st.session_state.get("_announced") != key:
        ident.print_banner(who)
        st.session_state["_announced"] = key
    return who


def _identity_banner(who: ident.Identity) -> None:
    body = (
        f"**{who.page}** &nbsp; | &nbsp; profile `{who.profile}` &nbsp; | &nbsp; "
        f"API key `{who.key_masked}` from `{who.key_source}` &nbsp; | &nbsp; "
        f"paper `{who.paper}` &nbsp; | &nbsp; judged account "
        f"`{who.judged_account_id or 'unset'}`"
    )
    if not who.configured:
        st.error(f"{body}\n\nNo credentials resolved - check `.env`.")
    elif who.is_judged:
        st.warning(f"JUDGED KEYS ACTIVE. {body}")
    else:
        st.info(body)


def _stale_server(exc: Exception, profile: str) -> None:
    """The settings load itself failed - almost always a stale server.

    `_check_stale` below catches a config that is missing a field the code
    expects. This catches the mirror image, which it structurally cannot: a
    config carrying a field the IN-MEMORY schema has never heard of, because
    the server imported that schema before the field existed. It surfaces as a
    pydantic ValidationError naming settings that are plainly present in the
    YAML, which reads like a config bug and is not one.
    """
    st.error(
        f"**Could not load the `{profile}` profile.**\n\n"
        "If the settings it is complaining about are present in your YAML, this "
        "server is running older Python than the files on disk. Streamlit reruns "
        "the script on every interaction but keeps already-imported modules, so "
        "**Reload config** cannot fix it - it re-reads YAML and `.env`, not code.\n\n"
        "Restart the process:\n\n```\npkill -f streamlit && make serve\n```",
        icon=":material/warning:",
    )
    with st.expander("The error"):
        st.code(str(exc))
    st.stop()


def _check_stale(settings: Any) -> None:
    """Catch a server running older Python than the files on disk.

    Streamlit reruns the script on every interaction but keeps already-imported
    modules, so editing the package under a live server leaves half the code
    new and half of it stale. That surfaces as an AttributeError deep inside a
    run - which reads like a bug in the harness rather than what it is. Naming
    it here costs one config lookup and saves a confusing hunt.
    """
    required = {
        "backtest.chain.source": lambda c: c.backtest.chain.source,
        "backtest.critic.mode": lambda c: c.backtest.critic.mode,
    }
    missing = []
    for name, getter in required.items():
        try:
            getter(settings.config)
        except AttributeError:
            missing.append(name)
    if missing:
        st.error(
            "**This server is running stale code.** The config object in memory "
            f"is missing `{'`, `'.join(missing)}`, which exists in the files on "
            "disk. Streamlit keeps already-imported modules across reruns, so a "
            "code change needs a full restart: stop the process and run "
            "`oaa dashboard` again."
        )
        st.stop()


def _dark() -> bool:
    """Whether to draw dark. Set by the sidebar toggle - see oaa.app.theme."""
    return is_dark()


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def _money(value: Any, dp: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.{dp}f}"


def _pct(value: Any, dp: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{dp}f}%"


def _num(value: Any, dp: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{dp}f}"


# --------------------------------------------------------------------------- #
# charts
# --------------------------------------------------------------------------- #
def _equity_chart(curve: pd.DataFrame, colours: dict[str, Any], start_equity: float) -> go.Figure:
    fig = go.Figure()
    fig.add_hline(
        y=start_equity, line_width=1, line_dash="dot", line_color=colours["muted"],
        annotation_text="starting capital", annotation_position="top left",
        annotation_font_color=colours["muted"], annotation_font_size=11,
    )
    fig.add_trace(
        go.Scatter(
            x=curve["timestamp"], y=curve["equity"], name="Account equity",
            mode="lines", line={"width": 2, "color": colours["series"][0]},
            hovertemplate="%{x|%d %b %Y}<br>$%{y:,.0f}<extra></extra>",
        )
    )
    return style(fig, colours, height=330, ytitle="Equity ($)")


def _drawdown_chart(curve: pd.DataFrame, colours: dict[str, Any]) -> go.Figure:
    peak = curve["equity"].cummax()
    drawdown = (curve["equity"] - peak) / peak * 100
    fig = go.Figure(
        go.Scatter(
            x=curve["timestamp"], y=drawdown, name="Drawdown from peak",
            mode="lines", fill="tozeroy", line={"width": 2, "color": colours["critical"]},
            fillcolor="rgba(208,59,59,0.16)",
            hovertemplate="%{x|%d %b %Y}<br>%{y:.2f}%<extra></extra>",
        )
    )
    fig.update_yaxes(ticksuffix="%")
    return style(fig, colours, height=200, ytitle="Drawdown (%)")


def _pnl_chart(trades: pd.DataFrame, colours: dict[str, Any]) -> go.Figure:
    """Cumulative realised P&L, gross and net of every modelled cost."""
    frame = trades.dropna(subset=["closed_at"]).sort_values("closed_at").copy()
    frame["cum_gross"] = frame["gross_pnl"].cumsum()
    frame["cum_net"] = frame["net_pnl"].cumsum()
    when = pd.to_datetime(frame["closed_at"], format="mixed", utc=True)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=when, y=frame["cum_gross"], name="Gross of costs", mode="lines+markers",
            line={"width": 2, "color": colours["series"][1], "dash": "dot"},
            marker={"size": 8},
            hovertemplate="%{x|%d %b}<br>gross $%{y:,.0f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=when, y=frame["cum_net"], name="Net of fees, interest and spread",
            mode="lines+markers", line={"width": 2, "color": colours["series"][0]},
            marker={"size": 8},
            hovertemplate="%{x|%d %b}<br>net $%{y:,.0f}<extra></extra>",
        )
    )
    fig.update_xaxes(tickformat="%d %b")
    return style(fig, colours, height=300, ytitle="Cumulative realised P&L ($)")


def _trade_bars(trades: pd.DataFrame, colours: dict[str, Any]) -> go.Figure:
    frame = trades.dropna(subset=["closed_at"]).copy()
    labels = frame["trade_id"] + "  " + frame["symbol"]
    fig = go.Figure(
        go.Bar(
            x=labels, y=frame["net_pnl"], name="Net P&L per trade",
            marker={"color": colours["series"][0], "line": {"width": 0}},
            hovertemplate="%{x}<br>net $%{y:,.2f}<extra></extra>",
        )
    )
    fig.update_traces(marker_cornerradius=4)
    fig.update_layout(bargap=max(0.3, 1 - 0.09 * max(1, len(frame))))
    return style(fig, colours, height=260, ytitle="Net P&L ($)")


def _funnel_chart(funnel: dict[str, int], colours: dict[str, Any]) -> go.Figure:
    items = sorted(funnel.items(), key=lambda kv: kv[1])
    fig = go.Figure(
        go.Bar(
            x=[v for _, v in items], y=[k for k, _ in items], orientation="h",
            marker={"color": colours["series"][0], "line": {"width": 0}},
            hovertemplate="%{y}: %{x} candidates declined<extra></extra>",
        )
    )
    fig.update_traces(marker_cornerradius=4)
    fig.update_yaxes(showgrid=False)
    return style(fig, colours, height=max(200, 34 * len(items) + 60), ytitle="")


def _cost_chart(metrics: dict[str, Any], colours: dict[str, Any]) -> go.Figure:
    rows = [
        ("Bid-ask spread crossed", metrics.get("spread_cost", 0.0)),
        ("Regulatory + exchange fees", metrics.get("fees_paid", 0.0)),
        ("Margin interest", metrics.get("margin_interest", 0.0)),
    ]
    fig = go.Figure(
        go.Bar(
            x=[v for _, v in rows], y=[k for k, _ in rows], orientation="h",
            marker={"color": [colours["series"][i] for i in range(3)],
                    "line": {"width": 0}},
            text=[f"${v:,.2f}" for _, v in rows], textposition="outside",
            textfont={"color": colours["text_secondary"], "size": 11},
            hovertemplate="%{y}: $%{x:,.2f}<extra></extra>",
        )
    )
    fig.update_traces(marker_cornerradius=4)
    fig.update_yaxes(showgrid=False)
    return style(fig, colours, height=220, ytitle="")


# --------------------------------------------------------------------------- #
# backtesting tab
# --------------------------------------------------------------------------- #
def render_backtest(settings: Any) -> None:
    from oaa.backtest.runner import (
        SOURCE_ALPACA,
        SOURCE_SYNTHETIC,
        BacktestRequest,
        list_runs,
        load_run,
        run_backtest,
        save_run,
    )

    who = _announce(settings, PAGE_BACKTEST)
    _identity_banner(who)
    cfg = settings.config
    colours = palette(_dark())

    # -- controls ------------------------------------------------------- #
    with st.sidebar:
        st.header("Backtest")
        available = sorted(set(cfg.universe.active()) | {"SPY", "QQQ", "IWM"})
        symbols = st.multiselect(
            "Investment universe", available,
            default=cfg.universe.active()[:6] or ["SPY", "QQQ"],
            help="Underlyings the replay offers to the strategies each session.",
        )
        extra = st.text_input("Add symbols (comma separated)", "")
        symbols = symbols + [s.strip().upper() for s in extra.split(",") if s.strip()]

        default_end = dt.date.fromisoformat(cfg.backtest.end)
        default_start = dt.date.fromisoformat(cfg.backtest.start)
        start = st.date_input("Start", default_start)
        end = st.date_input("End", default_end)

        enabled = [s.name for s in cfg.enabled_strategies()]
        chosen = st.multiselect("Strategies", enabled, default=enabled)

        capital = st.number_input(
            "Initial capital ($)", min_value=1_000.0, value=float(cfg.backtest.initial_cash),
            step=5_000.0,
        )
        slippage = st.slider(
            "Spread crossed on each fill", 0.0, 1.0,
            float(cfg.backtest.slippage_spread_fraction), 0.05,
            help=(
                "0.0 fills at mid, which is what paper trading does and what "
                "flatters every options backtest. 1.0 pays the full quoted side. "
                "Crossing is charged on entry AND exit."
            ),
        )
        # The option list must contain whatever the config defaults to, or
        # Streamlit refuses to render the whole page.
        session_choices = sorted(
            {"09:45", "10:00", "10:30", "11:00", "11:30", "13:45", "14:30", "14:45"}
            | set(cfg.backtest.session_times_et)
        )
        sessions = st.multiselect(
            "Session times (ET)", session_choices,
            default=list(cfg.backtest.session_times_et),
            help=(
                "The intraday book enters 09:45-14:45 and skips 11:30-13:30, so "
                "one scan a day can only catch one of its windows. Each extra "
                "scan costs replay time; the gates still apply at every one."
            ),
        )
        source_label = st.radio(
            "Data source",
            [f"Alpaca history ({SOURCE_ALPACA})", f"Synthetic demo ({SOURCE_SYNTHETIC})"],
            help=(
                "Alpaca history replays real daily bars and real headlines. "
                "Synthetic invents a price path - it proves the wiring works and "
                "nothing else."
            ),
        )
        source = SOURCE_ALPACA if source_label.startswith("Alpaca") else SOURCE_SYNTHETIC
        critic_mode = st.selectbox(
            "AI critic",
            ["heuristic", "off", "llm"],
            help=(
                "The live decision path is cost -> critic -> risk engine -> "
                "partner veto, so the replay runs the same Critic class. "
                "'heuristic' is the real critic with the documented null-LLM "
                "fallback: deterministic and free. 'llm' calls the model and "
                "caches every verdict - use it to read the reasoning, not to "
                "produce a P&L number. 'off' measures what the critic adds."
            ),
        )
        use_news = st.checkbox("Fetch Alpaca news for the catalyst read", value=True)
        offline = st.checkbox(
            "Offline (cache only)", value=False,
            help="Refuse to hit the network; use only bars already cached on disk.",
        )
        label = st.text_input("Run label", "")
        run_clicked = st.button("Run backtest", type="primary", width="stretch")

        st.divider()
        st.caption("Saved runs")
        saved = list_runs(settings)
        options = ["(current run)"] + [
            f"{r['id']}  |  {', '.join(r['symbols'][:4])}" for r in saved
        ]
        picked = st.selectbox("Load a previous run", options, label_visibility="collapsed")

    # -- resolve which result is on screen ------------------------------ #
    if run_clicked:
        if not symbols:
            st.error("Pick at least one symbol.")
            return
        request = BacktestRequest(
            symbols=symbols, start=start, end=end, strategies=chosen,
            initial_cash=capital, slippage_spread_fraction=slippage,
            session_times_et=sessions or None, source=source,
            use_news=use_news, offline=offline, critic_mode=critic_mode,
            label=label,
        )
        status = st.status("Running the replay...", expanded=True)
        try:
            def _tick(step: int, moment: dt.datetime, equity: float) -> None:
                if step % 5 == 0:
                    status.write(f"{moment:%d %b %Y}  equity ${equity:,.0f}")

            result = run_backtest(settings, request, progress=_tick)
            directory = save_run(settings, request, result)
            status.update(label=f"Done - saved to {directory.name}", state="complete")
            st.session_state["bt_result"] = result.as_dict()
        except DataError as exc:
            status.update(label="No historical data", state="error")
            st.error(
                f"{exc}\n\nThe replay needs Alpaca historical bars. Check the "
                f"keys for profile `{cfg.profile}` and that this machine can "
                "reach `data.alpaca.markets`. Once a window has been fetched it "
                f"is cached under `{cfg.backtest.cache_dir}` and the **Offline** "
                "box will replay it with no network at all."
            )
            return
        except Exception as exc:  # noqa: BLE001
            status.update(label="Backtest failed", state="error")
            st.exception(exc)
            return
    elif picked != "(current run)":
        chosen_run = saved[options.index(picked) - 1]
        st.session_state["bt_result"] = load_run(chosen_run["path"])
    elif "bt_result" not in st.session_state and saved:
        # Opening the dashboard cold should show the last run, not an empty
        # page - the run store exists precisely so nothing has to be re-run.
        st.session_state["bt_result"] = load_run(saved[0]["path"])
        st.caption(f"Showing the most recent saved run: `{saved[0]['id']}`")

    payload = st.session_state.get("bt_result")
    if not payload:
        st.markdown(
            "### No backtest loaded\n"
            "Set the universe and window in the sidebar, then **Run backtest**. "
            "Every run is saved under `runs/backtests/` and can be reopened here."
        )
        _methodology(cfg)
        return

    _render_result(payload, colours, cfg)


# --------------------------------------------------------------------------- #
def _render_result(payload: dict[str, Any], colours: dict[str, Any], cfg: Any) -> None:
    metrics = payload["metrics"]
    provenance = payload.get("provenance", {})
    trades = pd.DataFrame(payload["trades"])
    curve = pd.DataFrame(payload["equity_curve"], columns=["timestamp", "equity"])
    curve["timestamp"] = pd.to_datetime(curve["timestamp"], format="mixed", utc=True)

    # -- provenance ------------------------------------------------------ #
    if provenance.get("synthetic"):
        st.error(
            "**SYNTHETIC PRICE PATH.** These prices were invented by a random "
            "walk. This run proves the strategy, risk and execution wiring "
            "works end to end. It is not a backtest and no number below means "
            "anything about the strategy. Switch the data source to Alpaca."
        )
    else:
        source_meta = provenance.get("source") or {}
        coverage = source_meta.get("coverage") or {}
        marked = (
            coverage.get("marks_from_real_bars", 0) + coverage.get("marks_modelled", 0)
        )
        if coverage and not marked:
            requests = source_meta.get("chain_requests")
            st.error(
                "**No option contract could be given a price, so the strategy "
                "never saw a chain to trade.** "
                f"{coverage.get('contracts_listed', 0):,} contracts were listed"
                + (f" across {requests} sessions" if requests else "")
                + ", but none of them fell inside the strike and expiry range "
                "this window needs, so every session was skipped. The terminal "
                "log names which filter emptied it. Do not read any figure on "
                "this page - there is nothing behind them."
            )
        elif coverage:
            real = coverage.get("real_mark_fraction", 0.0)
            st.success(
                f"**{real:.0%} of option marks came from real Alpaca option "
                "bars.** Strikes and expiries are the contracts that were "
                "actually listed; implied volatility is recovered by inverting "
                "Black-Scholes on the traded price, not modelled. The "
                f"remaining {1 - real:.0%} are contract-days that never traded "
                "and fall back to the modelled surface. **The bid-ask spread "
                "is still modelled** - bars are OHLCV, not quotes - and it is "
                "the dominant cost here, so read Methodology before quoting a "
                "net figure."
            )
            row = st.columns(4)
            row[0].metric("Contracts listed", f"{coverage.get('contracts_listed', 0):,}")
            row[1].metric("Marks from real bars", f"{coverage.get('marks_from_real_bars', 0):,}")
            row[2].metric("Marks modelled", f"{coverage.get('marks_modelled', 0):,}")
            row[3].metric("IV recovered", f"{coverage.get('iv_recovered_from_price', 0):,}",
                          f"{coverage.get('iv_modelled', 0):,} modelled")
            iv_provenance = (provenance.get("source") or {}).get("iv_provenance") or {}
            if iv_provenance:
                st.caption(
                    "IV rank per symbol - "
                    + "; ".join(f"**{k}**: {v}" for k, v in sorted(iv_provenance.items()))
                )
        elif (
            provenance.get("chain_source_requested") == "real"
            and provenance.get("chain_source_used") == "modelled"
        ):
            st.warning(
                "**You asked for real option prices and this run did not get "
                "them.** `backtest.chain.source` is `real`, but the contract "
                "listing or the bar fetch failed, so every option mark here "
                "came from the model. Check the terminal for the reason - a "
                "rejected symbol, an empty listing, or a plan limit - and do "
                "not read this run as measured."
            )
        else:
            st.success(
                "**Underlying prices and headlines are real Alpaca history. "
                "The option chain is modelled.** Option prices come from "
                "Black-Scholes on a modelled surface, and every fill crosses "
                "the modelled spread on both sides. Set "
                "`backtest.chain.source: real` to mark from real option bars "
                "instead. See Methodology below before quoting any figure."
            )

    critic_stats = provenance.get("critic") or {}
    if critic_stats.get("lookahead_warning"):
        _p = (critic_stats.get("provider_config") or {})
        st.warning(
            f"**This run used the LLM critic** "
            f"(`{_p.get('provider')}` / `{_p.get('model')}`). The model was "
            "asked about a period that may sit inside its training data, so "
            "its verdicts are not necessarily reasoning from the prompt alone. "
            "Read the reasoning; do not quote the P&L."
        )

    # -- what was traded -------------------------------------------------- #
    st.subheader("What was traded")
    universe = provenance.get("universe") or sorted(trades["symbol"].unique()) if len(trades) else []
    per_symbol = (
        trades.groupby("symbol")["trade_id"].count().to_dict() if len(trades) else {}
    )
    first = curve["timestamp"].min() if len(curve) else None
    last = curve["timestamp"].max() if len(curve) else None

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Investment universe**")
        chips = " ".join(
            f"`{s}` {per_symbol.get(s, 0)} trade{'' if per_symbol.get(s, 0) == 1 else 's'}"
            for s in universe
        )
        st.markdown(chips or "_no symbols_")
        st.caption(
            "Strategies: "
            + ", ".join(provenance.get("strategies", []) or ["none"])
            + "  |  sessions evaluated at "
            + ", ".join((provenance.get("source") or {}).get("session_times_et", []) or ["10:00"])
            + " ET"
        )
        st.caption(
            "Decision path (the live one): "
            + " -> ".join(provenance.get("decision_pipeline") or ["strategy", "risk"])
        )
        risk = provenance.get("risk") or {}
        note = (
            f"Risk limits: **{risk.get('profile', '?')}** profile - "
            f"{risk.get('max_risk_per_trade_pct', 0):.2%} per trade, "
            f"{risk.get('max_new_positions_per_day', '?')} new positions/day, "
            f"{risk.get('max_positions', '?')} concurrent"
        )
        if risk.get("profile") != "judged":
            st.caption(f":orange[{note} - the judged account runs different "
                       "limits; switch the profile in the sidebar to match it.]")
        else:
            st.caption(note)
    with right:
        st.markdown("**Time horizon**")
        if first is not None:
            span = (last - first).days
            st.markdown(
                f"{first:%d %b %Y} to {last:%d %b %Y}  \n"
                f"{metrics['sessions']} sessions over {span} calendar days"
            )
        st.caption(
            f"{metrics['trades']} trades opened, {metrics['closed_trades']} closed, "
            f"{metrics['open_at_end']} still open at the end of the window "
            f"(closed at the last modelled mark)"
        )

    # -- headline metrics ------------------------------------------------- #
    st.subheader("Key performance metrics")
    row = st.columns(5)
    row[0].metric("Net P&L", _money(metrics["net_pnl"]), _pct(metrics["total_return"]))
    row[1].metric("Sharpe", _num(metrics["sharpe"]))
    row[2].metric("Max drawdown", _pct(metrics["max_drawdown"]))
    row[3].metric("Volatility (annual)", _pct(metrics["volatility_annual"]))
    row[4].metric("Win rate", _pct(metrics["win_rate"], 1))

    row = st.columns(5)
    row[0].metric("Worst trade", _money(metrics["worst_trade"]))
    row[1].metric("Best trade", _money(metrics["best_trade"]))
    row[2].metric("Profit factor", _num(metrics["profit_factor"]))
    row[3].metric("Expectancy / trade", _money(metrics["expectancy"]))
    row[4].metric("Avg hold", f"{metrics['avg_hold_days']:.1f} days")

    row = st.columns(5)
    row[0].metric("Sortino", _num(metrics["sortino"]))
    row[1].metric("Gross P&L", _money(metrics["gross_pnl"]))
    row[2].metric("Modelled cost", _money(metrics["total_modelled_cost"]))
    row[3].metric(
        "Approval rate", _pct(metrics["approval_rate"], 1),
        f"{metrics['ideas_approved']} of {metrics['ideas_generated']} ideas",
    )
    row[4].metric("Candidates declined", f"{metrics['rejections']:,}")

    # -- the P&L path ------------------------------------------------------ #
    st.subheader("P&L over time")
    st.caption(
        "Account equity marked to the model every session, so the path moves "
        "between trades rather than stepping only on closes."
    )
    st.plotly_chart(
        _equity_chart(curve, colours, metrics["start_equity"]), width="stretch"
    )
    st.plotly_chart(_drawdown_chart(curve, colours), width="stretch")

    if len(trades) and trades["closed_at"].notna().any():
        st.markdown("**Realised P&L, gross and net of modelled costs**")
        st.caption(
            "The gap between the two lines is what paper trading does not "
            "charge you: spread crossed on both sides, regulatory and exchange "
            "fees, and margin interest on the requirement while the structure "
            "was held."
        )
        st.plotly_chart(_pnl_chart(trades, colours), width="stretch")
        st.plotly_chart(_trade_bars(trades, colours), width="stretch")

    # -- how alike the underlyings were ------------------------------------ #
    _correlation_panel(payload, colours, cfg)

    # -- trades ------------------------------------------------------------ #
    st.subheader("Every trade, and what justified it")
    if not len(trades):
        st.info("No trade was approved in this window. The gate funnel below says why.")
    else:
        table = trades.assign(
            opened=pd.to_datetime(trades["opened_at"], format="mixed", utc=True).dt.strftime("%d %b %H:%M"),
            closed=pd.to_datetime(trades["closed_at"], format="mixed", utc=True).dt.strftime("%d %b %H:%M"),
        )[[
            "trade_id", "symbol", "strategy", "structure", "quantity", "opened",
            "closed", "held_days", "entry_price", "exit_price", "gross_pnl",
            "fees", "margin_interest", "spread_cost", "net_pnl",
            "return_on_risk", "exit_reason",
        ]]
        st.dataframe(
            table, width="stretch", hide_index=True,
            column_config={
                "trade_id": "ID",
                "held_days": st.column_config.NumberColumn("Held (d)", format="%.1f"),
                "entry_price": st.column_config.NumberColumn("Entry net", format="%.2f"),
                "exit_price": st.column_config.NumberColumn("Exit net", format="%.2f"),
                "gross_pnl": st.column_config.NumberColumn("Gross $", format="%.2f"),
                "fees": st.column_config.NumberColumn("Fees $", format="%.2f"),
                "margin_interest": st.column_config.NumberColumn("Interest $", format="%.2f"),
                "spread_cost": st.column_config.NumberColumn("Spread $", format="%.2f"),
                "net_pnl": st.column_config.NumberColumn("Net $", format="%.2f"),
                "return_on_risk": st.column_config.NumberColumn("Return on risk", format="%.1f%%"),
                "exit_reason": "Exit reason",
            },
        )
        st.markdown("**Trade justification**")
        labels = [
            f"{r.trade_id}  -  {r.symbol} {r.structure}  ({r.net_pnl:+,.0f})"
            for r in trades.itertuples()
        ]
        picked = st.selectbox("Trade", labels, label_visibility="collapsed")
        _trade_detail(trades.iloc[labels.index(picked)])

    # -- what did NOT trade ------------------------------------------------- #
    st.subheader("What the agent declined, and why")
    if critic_stats and critic_stats.get("mode") != "off":
        row = st.columns(4)
        row[0].metric("Critic mode", critic_stats.get("mode", "-"))
        row[1].metric("Candidates scored", critic_stats.get("scored", 0))
        row[2].metric("Passed on by the critic", critic_stats.get("declined", 0))
        row[3].metric(
            "Model calls", critic_stats.get("llm_calls", 0),
            f"{critic_stats.get('cache_hits', 0)} from cache",
        )
        provider = critic_stats.get("provider_config") or {}
        if critic_stats.get("mode") == "llm":
            st.caption(
                f"Verdicts from **{provider.get('provider')} / "
                f"{provider.get('model')}** at temperature "
                f"{provider.get('temperature')}"
                + (f", seed {provider['seed']}" if provider.get("seed") is not None else "")
                + ". The live agent uses its own provider - a replay scores "
                "every candidate in every session, so the two are split."
            )
        if critic_stats.get("degraded_to_heuristic"):
            st.caption(
                f"{critic_stats['degraded_to_heuristic']} verdict(s) fell back to the "
                "heuristic - the same degradation the live system has when the "
                "provider is unreachable or the budget is spent."
            )
    funnel = payload.get("rejection_funnel") or {}
    if funnel:
        st.caption(
            "Every candidate a gate refused, counted by the gate that refused "
            "it. A short-premium book that never declines anything is not "
            "selecting - it is just selling."
        )
        st.plotly_chart(_funnel_chart(funnel, colours), width="stretch")
        with st.expander("Rejection log"):
            rejections = pd.DataFrame(payload.get("rejections") or [])
            if len(rejections):
                st.dataframe(
                    rejections[["ts", "symbol", "strategy", "stage", "vetoed_by", "reason"]],
                    width="stretch", hide_index=True,
                )
    else:
        st.caption("No candidate was refused in this window.")

    # -- costs --------------------------------------------------------------- #
    st.subheader("Where the money went in costs")
    st.caption(
        "Alpaca charges no commission; the spread is 50-100x the fee load "
        "(COST_STRUCTURE.md). Paper trading models none of it."
    )
    st.plotly_chart(_cost_chart(metrics, colours), width="stretch")

    _methodology(cfg, provenance)


# --------------------------------------------------------------------------- #
# pairwise correlation
# --------------------------------------------------------------------------- #
def _correlation_caption() -> None:
    st.caption(
        "Correlation of **daily returns**, pair by pair. This is the diversification "
        "check: the risk in this book is not any single position, it is six "
        "positions turning out to be one. Two names at 0.9 are one bet held twice, "
        "and r-squared says how much of one name's daily move the other explains."
    )


def _render_correlation(
    prices: pd.DataFrame, colours: dict[str, Any], key: str, allow_levels: bool = False
) -> None:
    """Shared body: pick the series, show the grid, rank the pairs."""
    basis = "Daily returns"
    if allow_levels:
        basis = st.radio(
            "Correlate on", ["Daily returns", "Price levels"],
            horizontal=True, key=f"{key}_basis",
            help=(
                "Daily returns is the honest one. Two rising stocks correlate at "
                "0.99 on price levels whatever they do day to day - it measures "
                "the shared trend, not shared risk."
            ),
        )
    frame = prices.dropna(how="any") if basis == "Price levels" else corr.returns_frame(prices)
    matrix = corr.matrix(frame)
    if matrix.empty:
        st.info(
            "Not enough overlapping daily bars to correlate - at least two symbols "
            f"and {corr.MIN_OBSERVATIONS} shared sessions are needed."
        )
        return

    stats = corr.summary(matrix)
    if stats:
        cells = st.columns(3)
        cells[0].metric("Average pairwise", _num(stats["mean"], 2))
        cells[1].metric("Most alike", _num(stats["max"], 2), stats["max_pair"])
        cells[2].metric("Least alike", _num(stats["min"], 2), stats["min_pair"])

    st.plotly_chart(corr.heatmap(matrix, colours), width="stretch")
    table = corr.pairs_table(matrix, observations=len(frame))
    if not table.empty:
        st.dataframe(
            table, width="stretch", hide_index=True,
            column_config={
                "pair": st.column_config.TextColumn("Pair"),
                "correlation": st.column_config.NumberColumn("r", format="%.3f"),
                "r_squared": st.column_config.NumberColumn("r squared", format="%.3f"),
                "observations": st.column_config.NumberColumn("Sessions"),
            },
        )
    st.caption(
        f"{len(frame)} overlapping sessions, {matrix.shape[0]} symbols, "
        f"{len(table)} pairs. Basis: {basis.lower()}."
    )


def _correlation_panel(payload: dict[str, Any], colours: dict[str, Any], cfg: Any) -> None:
    """Backtest tab: correlation over the replayed window."""
    st.subheader("How alike the underlyings were")
    _correlation_caption()

    provenance = payload.get("provenance") or {}
    closes = provenance.get("underlying_closes") or {}
    if not closes:
        # Runs saved before this panel existed carry no closes. Rebuild them
        # from the disk-cached historical feed rather than showing nothing.
        request = provenance.get("request") or {}
        symbols = [s.upper() for s in (request.get("symbols") or provenance.get("universe") or [])]
        if not symbols or not request.get("start"):
            st.info(
                "This run was saved before correlations were recorded, and it does "
                "not carry enough request detail to rebuild them. Re-run it and the "
                "panel fills in."
            )
            return
        st.caption(
            "This run predates the panel, so the closes were refetched from the "
            "cached historical feed for the same window."
        )
        try:
            closes = corr.replay_closes(
                cfg.profile, st.session_state.get("_config_path"),
                tuple(symbols), str(request["start"]), str(request["end"]),
            )
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Could not rebuild the price history for this run: {exc}")
            return

    prices = corr.closes_frame(closes)
    if prices.empty:
        st.info("No usable daily closes for this run's universe.")
        return
    if provenance.get("synthetic"):
        st.warning(
            "These are SYNTHETIC price paths. Each symbol is an independent random "
            "walk, so the correlations below measure the generator, not the market."
        )
    _render_correlation(prices, colours, key="bt_corr")


def _live_correlation_panel(cfg: Any, colours: dict[str, Any]) -> None:
    """Live tab: correlation of the active universe, from live daily bars."""
    st.subheader("How alike the universe is right now")
    _correlation_caption()

    universe = [s.upper() for s in (cfg.universe.active() or ["SPY"])]
    controls = st.columns([3, 2, 1])
    symbols = controls[0].multiselect(
        "Symbols", universe, default=universe, key="live_corr_symbols"
    )
    lookback = controls[1].slider(
        "Lookback (calendar days)", 30, 365, 120, 15, key="live_corr_lookback",
        help="Daily bars. A shorter window reacts faster and is noisier.",
    )
    if controls[2].button("Refresh", width="stretch", key="live_corr_refresh"):
        corr.live_closes.clear()

    if len(symbols) < 2:
        st.info("Pick at least two symbols.")
        return
    try:
        closes = corr.live_closes(
            cfg.profile, st.session_state.get("_config_path"),
            tuple(sorted(symbols)), int(lookback),
        )
    except Exception as exc:  # noqa: BLE001
        st.error(
            f"Could not fetch daily bars: {exc}\n\nThe data provider is "
            f"`{cfg.data.provider}`; `oaa doctor` reports which backends are "
            "available."
        )
        return
    prices = corr.closes_frame(closes)
    missing = sorted(set(symbols) - set(prices.columns))
    if missing:
        st.caption(f"No usable bars for {', '.join(missing)} - left out of the grid.")
    if prices.empty:
        st.info("No daily bars came back for these symbols.")
        return
    _render_correlation(prices, colours, key="live_corr", allow_levels=True)


def _trade_detail(trade: pd.Series) -> None:
    """Why this fill happened: the thesis, the gates, the tape, the risk checks."""
    st.markdown(f"**Thesis at entry** &nbsp; `{trade['trade_id']}`")
    st.write(trade["thesis"] or "_none recorded_")

    left, right = st.columns(2)
    with left:
        st.markdown("**Why it was closed**")
        st.write(trade["exit_reason"])
        st.caption(
            f"held {trade['held_days']:.1f} days  |  entry net {trade['entry_price']:+.2f}  "
            f"-> exit net {trade['exit_price']:+.2f}  |  gross ${trade['gross_pnl']:,.2f}  "
            f"net ${trade['net_pnl']:,.2f}  |  return on risk "
            f"{trade['return_on_risk'] * 100:+.1f}%"
        )
    with right:
        st.markdown("**Headlines on the tape when it fired** (real Alpaca news)")
        headlines = trade.get("headlines") or []
        if len(headlines):
            for line in headlines:
                st.write(f"- {line}")
            st.caption(
                "Keyword polarity of those headlines: "
                f"{trade.get('news_sentiment', 0):+.2f} "
                "(a word count, not a sentiment model - nothing gates on it)"
            )
        else:
            st.caption("No headlines in the lookback window for this name.")

    critic = trade.get("critic") or {}
    if critic and critic.get("source") != "critic_off":
        st.markdown(
            f"**AI critic** &nbsp; score `{critic.get('score', 0):.2f}` &nbsp; "
            f"verdict `{critic.get('verdict', '-')}` &nbsp; source "
            f"`{critic.get('source', '-')}`"
        )
        # The heuristic critic appends its notes to the strategy's thesis, which
        # is printed directly above. Show only what the critic added.
        reasoning = str(critic.get("reasoning", "")).strip()
        thesis = (trade.get("thesis") or "").strip()
        if thesis and reasoning.startswith(thesis):
            reasoning = reasoning[len(thesis):].strip()
        if reasoning:
            st.write(reasoning)
        else:
            st.caption("The critic added nothing beyond the strategy's own thesis.")
        concerns = critic.get("concerns") or []
        if concerns:
            st.markdown("Concerns it raised:")
            for concern in concerns:
                st.write(f"- {concern}")
        st.caption(
            "The critic scores and writes reasoning. It can decline a "
            "candidate; it cannot approve one - only the deterministic risk "
            "engine stamps a trade."
        )

    gates = trade.get("gates") or {}
    checked = gates.get("checked") or []
    checks = trade.get("risk_checks") or {}
    st.markdown(
        "**Gates passed** &nbsp; " + (", ".join(f"`{g}`" for g in checked) or "_not recorded_")
    )
    st.markdown(
        "**Risk engine checks passed** &nbsp; "
        + (", ".join(f"`{k}`" for k, v in checks.items() if v) or "_not recorded_")
    )

    metrics_col, state_col, legs_col = st.columns(3)
    with metrics_col:
        st.markdown("**What each gate measured**")
        metrics = gates.get("metrics") or {}
        st.dataframe(
            pd.DataFrame(
                [{"metric": k, "value": round(v, 4) if isinstance(v, (int, float)) else v}
                 for k, v in metrics.items()]
            ) if metrics else pd.DataFrame({"metric": [], "value": []}),
            width="stretch", hide_index=True, height=280,
        )
    with state_col:
        st.markdown("**Market state at entry**")
        state = trade.get("market_state") or {}
        st.dataframe(
            pd.DataFrame([{"field": k, "value": v} for k, v in state.items()]),
            width="stretch", hide_index=True, height=280,
        )
    with legs_col:
        st.markdown("**Legs as filled**")
        st.dataframe(
            pd.DataFrame(trade.get("legs") or []),
            width="stretch", hide_index=True, height=280,
        )


def _methodology(cfg: Any, provenance: dict[str, Any] | None = None) -> None:
    with st.expander("Methodology and limitations - read before quoting any number"):
        st.markdown(
            """
**Real, from Alpaca**

- daily bars for every underlying (`StockHistoricalDataClient`)
- headlines with their real publication timestamps (`NewsClient`), which is
  what the catalyst read is built from
- the strategy, risk engine, sizing and execution routing are the same code
  that runs live - not a backtest re-implementation

**Modelled, because Alpaca's free tier serves no historical option chain**

- ATM implied volatility is anchored to a slow EWMA of realised vol and scaled
  by the market's own vol level, so IV is sticky and the IV-RV spread widens
  when realised vol collapses and goes negative when it spikes. A constant
  multiple of realised vol would make the premium gate meaningless.
- the surface adds a standard equity put skew and a mild upward term structure
- bid-ask is a floor plus a fraction of mid, widened away from the money, and
  every fill crosses it on **both** entry and exit
- open interest and volume decay with moneyness and maturity, so the config's
  liquidity filters actually bind
- expiries are Fridays only, weeklies restricted to tiers that really list them

**Costs charged**

- OCC clearing, ORF, FINRA CAT and TAF, SEC fee, per COST_STRUCTURE.md
- index exchange fees where they apply
- margin interest at the configured annual rate on the structure's requirement
  for as long as it is held
- the spread, inside the fills rather than beside them

**No lookahead**

A context stamped 10:00 contains complete prior sessions, that morning's open
as the spot, and headlines published before 10:00. It never contains the day's
close.

**What this is not**

It is not evidence of edge. There is no earnings crush, no event repricing that
realised vol never explains, and no way to validate the surface against
historical option prices - obtaining those is the problem the model exists to
work around. What a run demonstrates is that the logic fires when intended,
stays quiet otherwise, sizes correctly and survives its own risk limits. The
judged number is live paper P&L.
            """
        )
        if provenance:
            st.json(provenance, expanded=False)


# --------------------------------------------------------------------------- #
# live tab
# --------------------------------------------------------------------------- #
def render_live(settings: Any) -> None:
    """The judged account: what it can see, what it did, and why."""
    from oaa.app import live as livemod

    who = _announce(settings, PAGE_LIVE)
    _identity_banner(who)
    cfg = settings.config
    colours = palette(_dark())

    from oaa.telemetry.journal import Journal

    t = cfg.telemetry
    journal = Journal(
        settings.path(t.journal), settings.path(t.db), settings.path(t.equity_curve)
    )
    try:
        equity_rows = journal.equity_series()
    except Exception as exc:  # noqa: BLE001
        st.error(f"The journal at `{t.db}` is not readable: {exc}")
        return

    # -- account ---------------------------------------------------------- #
    st.subheader("Account")
    account = _live_account(settings)
    if account is None:
        st.warning(
            "Could not reach the broker, so the figures below come from the "
            "journal only - they are as fresh as the agent's last snapshot, "
            "not as fresh as now. Check `oaa doctor`."
        )
    else:
        row = st.columns(5)
        row[0].metric("Equity", _money(account.equity))
        row[1].metric("Cash", _money(account.cash))
        row[2].metric("Options buying power", _money(account.options_buying_power))
        row[3].metric("Open option legs", len(account.option_positions()))
        row[4].metric("Options level", account.options_trading_level or "-")

    # -- performance: today and since inception ---------------------------- #
    st.subheader("Performance")
    st.caption(
        "The judged window is one week, so **since inception** is the number "
        "that gets scored. Today is for watching it happen."
    )
    if not equity_rows:
        st.info(
            "No equity snapshots yet - nothing has run against this account. "
            "Start the agent with `oaa run` and this page fills in."
        )
    else:
        overall = livemod.window_metrics(equity_rows)
        today = livemod.window_metrics(equity_rows, since=dt.date.today())
        st.markdown("**Since inception** - the number the judged window scores")
        cells = st.columns(5)
        cells[0].metric("P&L", _money(overall["pl"]), _pct(overall["return_pct"]))
        cells[1].metric("Equity", _money(overall["end"]))
        cells[2].metric("Peak", _money(overall["peak"]))
        cells[3].metric("Max drawdown", _pct(overall["max_drawdown"]))
        cells[4].metric("Snapshots", overall["snapshots"])
        if overall.get("first_ts"):
            st.caption(f"from {str(overall['first_ts'])[:16].replace('T', ' ')}")

        st.markdown("**Today**")
        cells = st.columns(5)
        cells[0].metric("P&L", _money(today["pl"]), _pct(today["return_pct"]))
        cells[1].metric("Equity", _money(today["end"]))
        cells[2].metric("Peak", _money(today["peak"]))
        cells[3].metric("Max drawdown", _pct(today["max_drawdown"]))
        cells[4].metric("Snapshots", today["snapshots"])
        halt = cfg.risk.daily_loss_limit_pct
        if today["return_pct"] <= -abs(halt):
            st.error(
                f"Today is past the {halt:.0%} daily loss limit - the risk "
                "engine halts new entries at this point."
            )

        curve = pd.DataFrame(equity_rows).rename(columns={"ts": "timestamp"})
        curve["timestamp"] = pd.to_datetime(curve["timestamp"], format="mixed", utc=True)
        if st.checkbox("Today only", value=False):
            curve = curve[curve["timestamp"].dt.date == dt.date.today()]
        if len(curve) > 1:
            st.plotly_chart(
                _equity_chart(curve, colours, float(curve["equity"].iloc[0])),
                width="stretch",
            )

    # -- how alike the universe is ------------------------------------------ #
    _live_correlation_panel(cfg, colours)

    # -- open positions ----------------------------------------------------- #
    if account is not None and account.positions:
        st.subheader("Open positions")
        st.dataframe(
            pd.DataFrame([p.model_dump(mode="json") for p in account.positions]),
            width="stretch", hide_index=True,
        )

    # -- the live chain and its surface ------------------------------------- #
    st.subheader("Live option chain")
    st.caption(
        "The real bid and ask from Alpaca's chain snapshot - the surface the "
        "agent prices against. Nothing here is modelled."
    )
    universe = cfg.universe.active() or ["SPY"]
    controls = st.columns([2, 2, 1])
    symbol = controls[0].selectbox("Underlying", universe, key="live_symbol")
    only_liquid = controls[1].checkbox(
        "Hide contracts the liquidity filter would reject", value=True,
        help=(
            f"Applies the configured floors: open interest >= "
            f"{cfg.options.min_open_interest}, volume >= {cfg.options.min_volume}, "
            f"spread <= {cfg.options.max_bid_ask_spread_pct:.0%} of mid."
        ),
    )
    if controls[2].button("Refresh", width="stretch"):
        livemod.fetch_chain.clear()

    backends = ["(config default)", "rest", "cli", "mcp"]
    backend = st.radio(
        "Data backend", backends, horizontal=True,
        help=(
            f"The config says `{cfg.data.provider}`. `cli` shells out to the "
            "Alpaca binary and fails if it is not installed; `rest` uses "
            "alpaca-py, which ships with this package and always works."
        ),
    )
    try:
        rows = livemod.fetch_chain(
            cfg.profile, st.session_state.get("_config_path"), symbol,
            None if backend == "(config default)" else backend,
        )
    except Exception as exc:  # noqa: BLE001
        st.error(
            f"Could not fetch the chain for {symbol}: {exc}\n\n"
            f"The data provider is `{cfg.data.provider}`. `cli` shells out to "
            "the Alpaca binary; `rest` uses alpaca-py. `oaa doctor` reports "
            "which are available."
        )
        rows = []

    if rows:
        chain = pd.DataFrame(rows)
        spot = _spot_from_chain(chain)
        if only_liquid:
            before = len(chain)
            chain = chain[
                (chain["bid"].fillna(0) > 0)
                & (chain["spread_pct"].fillna(9) <= cfg.options.max_bid_ask_spread_pct)
                & (chain["open_interest"].fillna(0) >= cfg.options.min_open_interest)
                & (chain["volume"].fillna(0) >= cfg.options.min_volume)
            ]
            st.caption(
                f"{len(chain)} of {before} contracts pass the liquidity floors. "
                "The rejected ones are not tradable at a sensible price, which "
                "is why the spread gate exists."
            )

        if chain.empty:
            st.info("No contract passed the filters. Untick the box to see them all.")
        else:
            expiries = sorted(chain["expiry"].unique())
            chosen = st.multiselect(
                "Expiries", expiries, default=expiries[: min(3, len(expiries))]
            )
            shown = chain[chain["expiry"].isin(chosen)] if chosen else chain
            st.dataframe(
                shown.sort_values(["expiry", "strike", "right"]),
                width="stretch", hide_index=True,
                column_config={
                    "bid": st.column_config.NumberColumn("Bid", format="%.2f"),
                    "ask": st.column_config.NumberColumn("Ask", format="%.2f"),
                    "mid": st.column_config.NumberColumn("Mid", format="%.2f"),
                    "spread": st.column_config.NumberColumn("Spread", format="%.2f"),
                    "spread_pct": st.column_config.NumberColumn("Spread %", format="%.1f%%"),
                    "iv": st.column_config.NumberColumn("IV", format="%.1f%%"),
                    "delta": st.column_config.NumberColumn("Delta", format="%.3f"),
                },
            )

            st.subheader("Volatility surface")
            st.caption(
                "Built from the snapshot above. Alpaca computes the implied "
                "vol; this only arranges it. The left-to-right downward slope "
                "is the put skew the carry book sells into - and a near expiry "
                "sitting above a far one is the market pricing a dated event."
            )
            surface = shown if chosen else chain
            st.plotly_chart(livemod.skew_chart(surface, colours, spot), width="stretch")
            grid, term = st.columns([3, 2])
            with grid:
                st.markdown("**Strike x expiry**")
                st.plotly_chart(livemod.surface_grid(surface, colours), width="stretch")
            with term:
                st.markdown("**Term structure (ATM)**")
                st.plotly_chart(
                    livemod.term_chart(chain, colours, spot), width="stretch"
                )

    # -- decisions and what let them through --------------------------------- #
    st.subheader("Decisions, and what let each one through")
    decisions = journal.decisions(200)
    if not decisions:
        st.info("No decisions recorded yet.")
    else:
        frame = pd.DataFrame(decisions)
        approved_only = st.checkbox("Approved only", value=False)
        view = frame[frame["approved"] == 1] if approved_only else frame
        st.dataframe(
            view[[
                "ts", "cycle", "symbol", "strategy", "action", "structure",
                "quantity", "net_price", "max_loss", "approved", "status", "reason",
            ]],
            width="stretch", hide_index=True,
        )

        labels = [
            f"{str(r['ts'])[5:16].replace('T', ' ')}  {r.get('symbol') or '-'}  "
            f"{r.get('strategy') or '-'}  {r.get('action')}"
            f"{'  APPROVED' if r.get('approved') == 1 else ''}"
            for _, r in view.iterrows()
        ]
        if labels:
            picked = st.selectbox("Inspect a decision", labels)
            _live_decision_detail(view.iloc[labels.index(picked)].to_dict())

    # -- the rejection log ---------------------------------------------------- #
    with st.expander("Gate rejection log - the trades the agent declined"):
        events = journal.events("gate_rejection", 300)
        if events:
            st.dataframe(pd.DataFrame(events), width="stretch", hide_index=True)
        else:
            st.caption("No gate rejections recorded yet.")


def _live_account(settings: Any) -> Any:
    """The broker's account snapshot, or None if it cannot be reached."""
    try:
        from oaa.brokers.factory import get_broker

        broker = get_broker(settings.config, settings.credentials)
        return broker.account()
    except Exception:  # noqa: BLE001
        return None


def _spot_from_chain(chain: pd.DataFrame) -> float | None:
    """Infer spot from put-call parity at the strike where they cross.

    The chain snapshot carries no underlying price, and asking for one is
    another API call. Where the call and put mids are closest, the strike is
    the forward - close enough to place a reference line.
    """
    frame = chain.dropna(subset=["mid"])
    if frame.empty:
        return None
    nearest = frame["expiry"].min()
    slice_ = frame[frame["expiry"] == nearest]
    calls = slice_[slice_["right"] == "call"].set_index("strike")["mid"]
    puts = slice_[slice_["right"] == "put"].set_index("strike")["mid"]
    common = calls.index.intersection(puts.index)
    if common.empty:
        return None
    gaps = (calls[common] - puts[common]).abs()
    return float(gaps.idxmin())


def _live_decision_detail(row: dict[str, Any]) -> None:
    from oaa.app import live as livemod

    detail = livemod.decision_justification(row)
    if not detail:
        st.caption("No payload recorded for this decision.")
        return

    verdict = detail.get("approved")
    banner = (
        ":green[APPROVED by the risk engine]" if verdict is True
        else ":red[REFUSED]" if verdict is False else "no risk verdict recorded"
    )
    st.markdown(
        f"**{banner}** &nbsp; cycle `{detail.get('cycle')}` &nbsp; book "
        f"`{detail.get('book')}` &nbsp; confidence `{detail.get('confidence')}`"
        + (f" &nbsp; stamp `{detail['stamp']}`" if detail.get("stamp") else "")
    )
    if detail.get("error"):
        st.error(detail["error"])

    st.markdown("**Thesis**")
    st.write(detail.get("thesis") or "_none recorded_")

    critic = detail.get("critic") or {}
    if critic:
        st.markdown(
            f"**AI critic** score `{critic.get('score')}` verdict "
            f"`{critic.get('verdict')}` source `{critic.get('source')}`"
        )
        reasoning = str(critic.get("reasoning", "")).strip()
        thesis = (detail.get("thesis") or "").strip()
        if thesis and reasoning.startswith(thesis):
            reasoning = reasoning[len(thesis):].strip()
        if reasoning:
            st.write(reasoning)
        for concern in critic.get("concerns") or []:
            st.write(f"- {concern}")

    gates = detail.get("gates") or {}
    checked = gates.get("checked") or []
    checks = detail.get("risk_checks") or {}
    st.markdown("**Gates passed** &nbsp; " + (", ".join(f"`{g}`" for g in checked) or "_none_"))
    st.markdown(
        "**Risk engine checks** &nbsp; "
        + (", ".join(f"`{k}`" for k, v in checks.items() if v) or "_none_")
    )
    for reason in detail.get("risk_reasons") or []:
        st.write(f"- {reason}")

    metrics, cost, legs = st.columns(3)
    with metrics:
        st.markdown("**What each gate measured**")
        values = gates.get("metrics") or {}
        st.dataframe(
            pd.DataFrame([{"metric": k, "value": v} for k, v in values.items()]),
            width="stretch", hide_index=True, height=240,
        )
    with cost:
        st.markdown("**Modelled round-trip cost**")
        st.dataframe(
            pd.DataFrame(
                [{"item": k, "value": v} for k, v in (detail.get("modelled_cost") or {}).items()]
            ),
            width="stretch", hide_index=True, height=240,
        )
        st.caption("Paper trading charges none of this - see COST_STRUCTURE.md.")
    with legs:
        st.markdown("**Legs**")
        st.dataframe(
            pd.DataFrame([
                {k: v for k, v in leg.items() if k in ("symbol", "side", "ratio", "limit_price")}
                for leg in (detail.get("legs") or [])
            ]),
            width="stretch", hide_index=True, height=240,
        )


# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(
        page_title="Options Alpha Agents", page_icon="*", layout="wide",
    )
    with st.sidebar:
        st.title("Options Alpha Agents")
        mode_toggle()
        profile = st.selectbox(
            "Account profile", ["dev", "judged"],
            help=(
                "dev uses ALPACA_DEV_* keys. judged uses ALPACA_* - the account "
                "the submission points at. The active key is printed to the "
                "terminal every time a page renders."
            ),
        )
        config_path = st.text_input("Config", "config/default.yaml")
        if st.button("Reload config", width="stretch",
                     help="Re-read the YAML and .env without restarting. Note "
                          "this does NOT reload changed Python - Streamlit keeps "
                          "already-imported modules, so after a code change you "
                          "must restart the process."):
            st.cache_resource.clear()
            st.session_state.pop("_announced", None)
            st.rerun()

    st.session_state["_config_path"] = config_path or None
    try:
        settings = _settings(profile, config_path or None)
    except Exception as exc:  # noqa: BLE001
        _stale_server(exc, profile)
        return
    _check_stale(settings)

    # Both accounts are loaded here, not just the selected one: the Control tab
    # shows and switches BOTH, and seeing them side by side is the whole point.
    # A profile whose credentials are missing loads as None and renders as such
    # rather than taking the page down.
    settings_for = {}
    for candidate in ("dev", "judged"):
        try:
            settings_for[candidate] = (
                settings if candidate == profile
                else _settings(candidate, config_path or None)
            )
        except Exception as exc:  # noqa: BLE001
            settings_for[candidate] = None
            st.session_state[f"_load_error_{candidate}"] = str(exc)

    # Events sits between Positions and Control deliberately: it is a book you
    # READ before a print and act on from the terminal, not one you switch on.
    backtest_tab, live_tab, positions_tab, events_tab, control_tab = st.tabs(
        [PAGE_BACKTEST, PAGE_LIVE, PAGE_POSITIONS, PAGE_EVENTS, PAGE_CONTROL]
    )
    with backtest_tab:
        render_backtest(settings)
    with live_tab:
        render_live(settings)
    with positions_tab:
        render_positions(settings_for)
    with events_tab:
        render_events(settings)
    with control_tab:
        render_control(settings_for)


if __name__ == "__main__":
    main()
