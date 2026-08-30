"""The Events tab: the earnings book, from calendar to filled spread.

This book is different from the other three and the page says so at the top.
Since 30 Aug it is driven by `oaa run` like everything else - events_flatten at
09:45, events_arm at 15:50 - but it is still not a firewall tenant: it arms
after the 15:15 transient cutoff and holds one night, so its cycles build their
own RiskEngine with firewall=None. It has a real toggle on the Control tab now;
what this page adds is the four things an operator actually needs before a
print:

  1. which events are CONFIRMED this week, and which are only proposed;
  2. what the market is charging for each, against what the stock has done;
  3. what the last arming cycle decided - including every name it declined;
  4. the parameters that produced those decisions.

Section 2 is a live chain read, so it sits behind a button and its result is
held in session state until asked again - the same rule the Positions tab
follows for the same reason.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pandas as pd
import streamlit as st

CYCLE = "events_arm"


# --------------------------------------------------------------------------- #
def _params(settings: Any):
    from oaa.strategies.events.params import load_params

    return load_params(settings.path("config/strategies/earnings_event.yaml"))


def _calendar(settings: Any, params: Any) -> dict[str, Any]:
    from oaa.strategies.events.calendar import load_calendar

    return load_calendar(settings.path(params.calendar_path))


def _week(asof: dt.date) -> tuple[dt.date, dt.date]:
    monday = asof - dt.timedelta(days=asof.weekday())
    if asof.weekday() >= 5:          # screening over a weekend looks ahead
        monday += dt.timedelta(days=7)
    return monday, monday + dt.timedelta(days=4)


def _calendar_frame(events: list[Any]) -> pd.DataFrame:
    rows = []
    for event in events:
        history = event.mean_abs_history
        rows.append({
            "Symbol": event.symbol,
            "Reports": event.report_date.strftime("%a %d %b"),
            "Session": "after close" if event.timing == "amc" else "before open",
            "Arms": event.entry_date.isoformat(),
            "Exits": event.exit_date.isoformat(),
            "Avg past move": f"{history:.2f}%" if history else "—",
            "Quarters": len(event.history),
            "Confirmed": "yes" if event.confirmed else "NO",
            "Source": event.source or "—",
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
def _screen(settings: Any, params: Any, events: list[Any]) -> dict[str, Any]:
    """Price the live chain for each confirmed event. One network round trip."""
    out: dict[str, Any] = {"rows": [], "errors": [], "at": dt.datetime.now()}
    try:
        from oaa.data.factory import get_data_provider
        from oaa.options.chain import ChainFilter, ChainView
        from oaa.strategies.events.volscreen import screen_one

        data = get_data_provider(settings.config, settings.credentials)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"data provider unavailable: {exc}")
        return out

    chain_filter = ChainFilter(
        min_dte=params.structure.dte_window[0],
        max_dte=params.structure.dte_window[1],
        min_price=params.screen.min_option_price,
        max_price=params.screen.max_option_price,
        min_open_interest=settings.config.options.min_open_interest,
        min_volume=0,
        max_spread_pct=settings.config.options.max_bid_ask_spread_pct,
    )
    for event in events:
        try:
            market = data.context(event.symbol)
            view = ChainView.from_quotes(
                symbol=event.symbol, spot=market.spot, quotes=market.chain,
                chain_filter=chain_filter, asof=market.asof.date(),
            )
            read = screen_one(event, market, view, params.screen)
            out["rows"].append({
                "Symbol": event.symbol,
                "Spot": round(market.spot, 2),
                "Expiry": read.expiry.isoformat() if read.expiry else "—",
                "Implied": read.implied_move_pct,
                "Realised": read.realised_mean_abs_pct,
                "Ratio": read.ratio,
                "Spread": read.relative_spread,
                "Verdict": read.rejected or "priced",
            })
        except Exception as exc:  # noqa: BLE001 - one dead symbol, not the page
            out["errors"].append(f"{event.symbol}: {exc}")
    return out


def _screen_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["Implied"] = df["Implied"].map(lambda v: f"{v:.2f}%" if v is not None else "—")
    df["Realised"] = df["Realised"].map(lambda v: f"{v:.2f}%" if v is not None else "—")
    df["Ratio"] = df["Ratio"].map(lambda v: f"{v:.2f}x" if v is not None else "—")
    df["Spread"] = df["Spread"].map(lambda v: f"{v:.1%}" if v is not None else "—")
    return df


# --------------------------------------------------------------------------- #
def _watcher(settings: Any, params: Any):
    """A read-only watcher: no LLM, so nothing this page does can spend a call."""
    from oaa.strategies.events.watch import EventWatcher

    return EventWatcher(
        llm=None,
        params=params.watch,
        sentiment=params.sentiment,
        calendar={},
        news_fn=None,
        store_dir=settings.path(params.watch.store_dir),
    )


def _dossiers(settings: Any, params: Any, events: list[Any]) -> list[dict[str, Any]]:
    """One row per name currently being watched, newest activity first."""
    watcher = _watcher(settings, params)
    rows: list[dict[str, Any]] = []
    for event in events:
        dossier = watcher.load(event.symbol)
        if not dossier.notes and not dossier.seen:
            continue
        lean, score = dossier.lean()
        last = dossier.notes[-1].asof if dossier.notes else "-"
        rows.append({
            "Symbol": event.symbol,
            "Reports": f"{event.report_date:%a %d %b}",
            "Notes": len(dossier.notes),
            "Items read": len(dossier.seen),
            "Dossier lean": lean,
            "Weight": round(score, 2),
            "Last note": last,
        })
    return rows


def _dossier_notes(settings: Any, params: Any, symbol: str) -> list[Any]:
    return _watcher(settings, params).load(symbol).notes


def _journal_rows(settings: Any, limit: int = 400) -> list[dict[str, Any]]:
    from oaa.telemetry.journal import Journal

    t = settings.config.telemetry
    journal = Journal(settings.path(t.journal), settings.path(t.db), settings.path(t.equity_curve))
    try:
        rows = journal.decisions(limit=limit)
    except Exception:  # noqa: BLE001 - an empty or missing db is not an error
        return []
    return [r for r in rows if str(r.get("cycle", "")) == CYCLE]


def _notes(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("agent_notes")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}



def _config_error(exc: Exception) -> None:
    """Explain a config failure, and name the one cause that is not a config bug.

    `unknown key(s) ...` from the params loader means the YAML has keys the
    dataclass does not define. Almost always that is a typo - but on THIS page
    it is usually something else entirely: Streamlit keeps already-imported
    modules, so a server started before a parameter was added is running the
    old dataclass against the new YAML. The message then reads as a broken
    config file when the config is fine and the process is stale, and the
    cache is no help either - clearing it re-reads YAML without re-importing
    Python.
    """
    message = str(exc)
    if "unknown key" in message:
        st.error(
            "This dashboard is running older code than the config on disk.",
            icon=":material/sync_problem:",
        )
        st.markdown(
            f"The params file has settings this process does not recognise:\n\n"
            f"> {message}\n\n"
            "Streamlit keeps modules it has already imported, so a server "
            "started before those settings were added is reading a new YAML "
            "with an old parser. **Restart the dashboard** — nothing short of "
            "that re-imports Python."
        )
        st.code("oaa dashboard", language="bash")
        return
    st.error(f"could not load the events config: {message}")

# --------------------------------------------------------------------------- #
def render_events(settings: Any) -> None:
    st.subheader("Earnings events")
    st.caption(
        "Armed on a DATE, not on a signal. `oaa run` arms it at 15:50 and "
        "flattens it at 09:45 the next morning; `oaa events arm` still works "
        "for a manual or dry-run arm. It leases no capital from the firewall - "
        "it holds overnight, which no firewall phase permits - so its risk is "
        "bounded by its own three sizing caps instead."
    )

    try:
        params = _params(settings)
        calendar = _calendar(settings, params)
    except Exception as exc:  # noqa: BLE001
        _config_error(exc)
        return

    today = dt.date.today()
    start, end = _week(today)
    in_window = sorted(
        (e for e in calendar.values() if start <= e.report_date <= end),
        key=lambda e: (e.report_date, e.symbol),
    )
    confirmed = [e for e in in_window if e.confirmed]
    unconfirmed = [e for e in in_window if not e.confirmed]
    arming_today = [e for e in confirmed if e.entry_date == today]

    top = st.columns(4)
    top[0].metric("Week", f"{start:%d %b} – {end:%d %b}")
    top[1].metric("Confirmed events", len(confirmed))
    top[2].metric("Unconfirmed", len(unconfirmed),
                  help="In the calendar for this window but with no company "
                       "announcement. These are never armed.")
    top[3].metric("Arms today", len(arming_today))

    # --- 1. the calendar ------------------------------------------------ #
    st.markdown("#### This week")
    if in_window:
        st.dataframe(_calendar_frame(in_window), width="stretch", hide_index=True)
        if unconfirmed:
            st.warning(
                "Not armed, and will not be: "
                + ", ".join(e.symbol for e in unconfirmed)
                + ". A position opened against a print that does not happen "
                "decays for nothing, so an unconfirmed row is refused rather "
                "than traded on a calendar's estimate.",
                icon=":material/warning:",
            )
    else:
        st.info(
            f"No calendar rows between {start} and {end}. The screener's LLM "
            "half can propose names, but only rows in "
            f"`{params.calendar_path}` are ever armed - add them there with a "
            "source before the week starts."
        )

    # --- 2. the live screen --------------------------------------------- #
    st.markdown("#### What the options are charging")
    st.caption(
        "Implied move is the ATM straddle in the expiry containing the print. "
        "Realised is the mean absolute reaction to the last four reports. Above "
        "1.00x the options charge more than those prints paid; below, less. "
        "Four quarters is a ranking device, not an edge estimate."
    )
    if st.button("Price the live chain", disabled=not confirmed,
                 help="One network round trip per confirmed event."):
        with st.spinner("reading chains…"):
            st.session_state["_events_screen"] = _screen(settings, params, confirmed)

    screened = st.session_state.get("_events_screen")
    if screened:
        st.caption(f"as at {screened['at']:%H:%M:%S}")
        frame = _screen_frame(screened["rows"])
        if not frame.empty:
            st.dataframe(frame, width="stretch", hide_index=True)
        for error in screened["errors"]:
            st.warning(error, icon=":material/error:")
    elif confirmed:
        st.caption("not priced yet")

    # --- 2b. what the book has been reading all week ---------------------- #
    st.markdown("#### The run-up")
    st.caption(
        "The direction model does not meet a name for the first time at 15:50 "
        "on arm day. From three days out, every watch cycle reads the wire and "
        "the retail stream, judges only what is NEW, and keeps a dated note "
        "when it is material. This is what the arm call reads alongside the "
        "afternoon's own headlines. A name stops being read the day it reports."
    )
    try:
        dossiers = _dossiers(settings, params, confirmed)
    except Exception as exc:  # noqa: BLE001 - a missing store is not an error
        dossiers = []
        st.caption(f"no watch store yet ({exc})")
    if dossiers:
        st.dataframe(
            pd.DataFrame(dossiers), width="stretch", hide_index=True,
        )
        for row in dossiers:
            if row["Notes"] == 0:
                continue
            with st.expander(f"{row['Symbol']} - what was logged, day by day"):
                for note in _dossier_notes(settings, params, row["Symbol"]):
                    st.markdown(
                        f"**{note.asof}** · salience {note.salience:.2f} · "
                        f"lean {note.lean} · {note.headlines} headline(s), "
                        f"{note.messages} post(s)"
                    )
                    st.caption(note.summary or "-")
                    if note.injection_noticed:
                        st.warning(
                            "the model reported instruction-like text in this "
                            "batch - the note stands, but read the source",
                            icon=":material/warning:",
                        )
    elif confirmed:
        st.info(
            "Nothing logged yet. The watch runs at 09:55, 13:00 and 15:30 ET "
            "inside `oaa run`; `oaa events watch` forces a poll now.",
            icon=":material/schedule:",
        )

    # --- 3. what the last cycle decided ---------------------------------- #
    st.markdown("#### Last arming cycle")
    rows = _journal_rows(settings)
    if not rows:
        st.info(
            "No arming cycle has been journalled on this account yet. Run "
            "`oaa events arm --dry-run` to see the full decision path without "
            "routing an order."
        )
    else:
        opened = [r for r in rows if str(r.get("action")) == "open"]
        skipped = [r for r in rows if str(r.get("action")) == "skip"]
        abstained = [
            r for r in skipped
            if str(_notes(r).get("llm_direction", "")) == "abstain"
        ]
        cols = st.columns(4)
        cols[0].metric("Opened", len(opened))
        cols[1].metric("Declined", len(skipped))
        cols[2].metric(
            "Abstention rate",
            f"{len(abstained) / len(rows):.0%}" if rows else "—",
            help="A model that never abstains is not filtering. Zero here means "
                 "the prompt or the confidence floor is wrong, not that every "
                 "print was readable.",
        )
        cols[3].metric("Journalled decisions", len(rows))
        if rows and not abstained:
            st.warning(
                "Every direction call was actionable. Check the confidence "
                "floor and the prompt before trusting the sizing.",
                icon=":material/warning:",
            )

        table = pd.DataFrame([{
            "When": str(r.get("ts", ""))[:19],
            "Symbol": r.get("symbol"),
            "Action": r.get("action"),
            "Direction": _notes(r).get("llm_direction", "—"),
            "Confidence": _notes(r).get("llm_confidence", "—"),
            "Why": str(r.get("rationale", ""))[:120],
        } for r in rows[:60]])
        st.dataframe(table, width="stretch", hide_index=True)

        with st.expander("The model's reasoning, per name"):
            for row in rows[:20]:
                notes = _notes(row)
                if not notes.get("llm_rationale"):
                    continue
                st.markdown(
                    f"**{row.get('symbol')}** · {notes.get('llm_direction')} "
                    f"at {notes.get('llm_confidence')} · crowd "
                    f"{notes.get('llm_crowding', '?')}"
                )
                st.caption(notes["llm_rationale"])
                evidence = notes.get("llm_evidence") or []
                if evidence:
                    st.caption("cited: " + "; ".join(str(e) for e in evidence))
                if notes.get("llm_injection_noticed"):
                    st.warning(
                        "The model reported instruction-like text in the "
                        "evidence block for this name. The call stands, but "
                        "the pack is worth reading.",
                        icon=":material/security:",
                    )
                st.divider()

    # --- 4. the parameters that produced all of the above ---------------- #
    with st.expander("Parameters"):
        left, right = st.columns(2)
        left.markdown(
            f"**Direction**  \n"
            f"confidence floor `{params.direction.min_confidence}`  \n"
            f"evidence required `{params.direction.require_evidence}`  \n"
            f"model `{params.direction.model or 'inherits agents.llm'}`  \n"
            f"seed `{params.direction.seed}`\n\n"
            f"**Screen**  \n"
            f"top N `{params.screen.top_n}`  \n"
            f"min implied move `{params.screen.min_implied_move_pct}%`  \n"
            f"max round-trip spread `{params.screen.max_relative_spread:.0%}` of premium  \n"
            f"per-contract price cap `{params.screen.max_option_price or 'none'}`"
        )
        right.markdown(
            f"**Sizing**  \n"
            f"nightly budget `{params.sizing.nightly_risk_budget_pct:.1%}` of equity  \n"
            f"per trade `{params.sizing.max_risk_per_trade_pct:.1%}` at full confidence  \n"
            f"floor multiple `{params.sizing.min_size_multiple}`  \n"
            f"max contracts `{params.sizing.max_contracts}`\n\n"
            f"**Structure**  \n"
            f"vertical debit spread, `{params.structure.long_delta}` / "
            f"`{params.structure.short_delta}` delta  \n"
            f"expiry window `{params.structure.dte_window[0]}-"
            f"{params.structure.dte_window[1]}` DTE  \n"
            f"max debit/width `{params.structure.max_debit_to_width:.0%}`  \n"
            f"arm `{params.schedule.arm_time}` ET, exit `{params.schedule.exit_time}` ET"
        )
        ta = params.technicals
        st.markdown(
            f"**Technicals** — {'on' if ta.enabled else ':red[off]'}  \n"
            f"Bollinger `{ta.bollinger_period}`/`{ta.bollinger_std}` — the setup: "
            f"squeeze = width at or below the `{ta.squeeze_max_percentile:.0%}` "
            f"percentile of its own `{ta.width_lookback}`-bar range "
            f"({'enforced' if ta.require_squeeze else 'measured, not enforced'})  \n"
            f"RSI `{ta.rsi_period}` — the veto, one-sided: no shorts below "
            f"`{ta.rsi_oversold:g}`, no longs above `{ta.rsi_overbought:g}`  \n"
            f"ATR `{ta.atr_period}` — the risk manager, never an entry gate: stop "
            f"`{ta.atr_stop_multiple:g}x` ATR on the underlying, size scaled from a "
            f"`{ta.atr_reference_pct:.1%}` reference, floor `{ta.atr_min_size_multiple}`"
        )
        st.caption(
            "The spread ceiling is the gate that removes the most candidates, "
            "and it should be: a vertical crosses four half-spreads on a round "
            "trip, which on a thin weekly is most of the debit. The ATR stop "
            "cannot be watched overnight — no cycle runs between the arm and "
            "the exit — so it governs the morning exit rather than protecting "
            "against the gap. The structure does that: a debit spread cannot "
            "lose more than the debit."
        )
