"""The Daily Reports tab: what one session did, and what to change.

`oaa daily-report` writes `reports/<profile>/<date>.md` and a `.json` sidecar
at 16:20 ET. The markdown is for a human reading a terminal; this page is the
same session rendered for someone who did not run it - the numbers first, the
refusals next, and the critique last.

Read-only by construction. Nothing here fetches, prices or submits: it opens
files that a completed cycle already wrote. That is why the tab is on the
public build as well as the operator one - a reader's whole question is "what
did it do today, and does it know what is wrong with itself", and this is the
only page that answers the second half.

Where the files come from
-------------------------
`reports/` is gitignored, so a deployed host clones the repo and has none of
it. `scripts/publish_reports.py` copies chosen days into `public/reports/`,
which IS committed, and this page reads whichever it finds - preferring the
local store so a freshly regenerated day is never shadowed by an older
published copy of itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from oaa.app import mode
from oaa.app.theme import is_dark, palette, style

#: Committed, and read when the local store has nothing for a date. Relative to
#: the repo root.
PUBLISHED = Path("public") / "reports"

#: A critique the model did not write says so in its author string. The page
#: says it too, because "here is what to improve" carries very different weight
#: depending on which one wrote it.
FALLBACK_MARKER = "deterministic"


# --------------------------------------------------------------------------- #
# where the files are
# --------------------------------------------------------------------------- #
def _repo_root() -> Path:
    """`<repo>` from this file: src/oaa/app/reports_page.py -> parents[3].

    `Settings.root` is the right answer when the package is run from the tree
    and the wrong one when it is installed into site-packages - there
    `project_root()` falls back to `Path.cwd()`, which on a deploy host is not
    reliably the repo. Both are searched; this one does not depend on cwd.
    """
    return Path(__file__).resolve().parents[3]


def report_dirs(settings: Any) -> list[Path]:
    """Every directory that may hold reports for this profile, best first.

    Local before published, deliberately: re-running `oaa daily-report` for a
    date is a correction, and a correction must not be shadowed by the copy
    that was published before it.
    """
    profile = getattr(settings.config, "profile", "judged")
    base = getattr(settings.config.telemetry, "report_dir", "reports")
    roots = [settings.path(base) / profile]
    for root in dict.fromkeys((settings.root, _repo_root())):
        roots.append(root / PUBLISHED / profile)
    return [d for d in dict.fromkeys(roots) if d.is_dir()]


def available(settings: Any) -> list[str]:
    """The dates on offer, newest first. A date counted once, however many
    directories hold it."""
    dates: set[str] = set()
    for directory in report_dirs(settings):
        for f in directory.iterdir():
            if f.suffix in (".json", ".md") and f.is_file():
                dates.add(f.stem)
    return sorted(dates, reverse=True)


def load_report(settings: Any, date: str) -> dict[str, Any]:
    """The payload for one date: parsed json, raw markdown, and which store.

    A missing or unreadable sidecar is not an error - the markdown alone still
    renders, and a page that refuses to show anything because one file is
    malformed is worse than one that shows what it has.
    """
    out: dict[str, Any] = {"date": date, "data": None, "markdown": "",
                           "source": None, "error": None}
    for directory in report_dirs(settings):
        js, md = directory / f"{date}.json", directory / f"{date}.md"
        if out["data"] is None and js.exists():
            try:
                out["data"] = json.loads(js.read_text(encoding="utf-8"))
                out["source"] = directory
            except (OSError, json.JSONDecodeError) as exc:
                out["error"] = f"{js.name}: {exc}"
        if not out["markdown"] and md.exists():
            try:
                out["markdown"] = md.read_text(encoding="utf-8")
                out["source"] = out["source"] or directory
            except OSError as exc:  # noqa: PERF203 - one bad file, not the page
                out["error"] = f"{md.name}: {exc}"
        if out["data"] is not None and out["markdown"]:
            break
    return out


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #
def _ideas_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Ideas that were BUILT and PRICED, then refused.

    On a session that fills nothing these rows are the entire record of what
    the system did, which is why they are a table rather than a footnote.
    """
    return pd.DataFrame([{
        "Time (ET)": r.get("ts", "—"),
        "Symbol": r.get("symbol", "—"),
        "Strategy": r.get("strategy", "—"),
        "Structure": r.get("structure", "—"),
        "Qty": r.get("quantity"),
        "Net price": r.get("net_price"),
        "Max loss": r.get("max_loss"),
        "Risk approved": "yes" if r.get("risk_approved") else "no",
        "Why it did not trade": r.get("reason") or "—",
    } for r in rows])


def _counts_frame(counts: dict[str, Any], label: str) -> pd.DataFrame:
    rows = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(counts.values()) or 1
    return pd.DataFrame([{
        label: name, "Rejections": n, "Share": f"{n / total:.0%}",
    } for name, n in rows])


def _gate_chart(counts: dict[str, Any]) -> Any:
    """Rejections by gate, biggest first.

    Horizontal bars: gate names are words of very different lengths, and a
    rotated x-axis label is harder to read than a longer chart. One series, so
    one colour - the accent is chrome here, not a category.
    """
    colours = palette(is_dark())
    rows = sorted(counts.items(), key=lambda kv: kv[1])
    fig = go.Figure(go.Bar(
        x=[n for _, n in rows], y=[g for g, _ in rows], orientation="h",
        marker_color=colours["series"][0],
        hovertemplate="%{y}: %{x} rejections<extra></extra>",
    ))
    fig = style(fig, colours, height=max(220, 30 * len(rows) + 60))
    fig.update_layout(hovermode="closest")
    fig.update_yaxes(gridcolor="rgba(0,0,0,0)", title="")
    fig.update_xaxes(showgrid=True, gridcolor=colours["grid"])
    return fig


def _strategy_frame(by_strategy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Strategy": name,
        "Ideas": b.get("ideas", 0),
        "Opened": b.get("opened", 0),
        "Closed": b.get("closed", 0),
        "Declined": b.get("declined", 0),
        "Risk-approved, unsent": b.get("near_misses", 0),
        "Realised P&L": b.get("realised_pl", 0.0),
    } for name, b in by_strategy.items()])


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
def render_reports(settings: Any) -> None:
    st.subheader("Daily reports")
    st.caption(
        "One exchange day - 04:00 to 04:00 ET, not a UTC day - read back out "
        "of the journal at 16:20 ET: what the books built, what they refused "
        "and why, and a written critique of what to change. The declined "
        "ideas are the report: this book fills nothing on most sessions, so a "
        "page that counted only fills would be blank on exactly the days "
        "there is most to learn from."
    )

    dates = available(settings)
    if not dates:
        st.info(
            "No daily reports here yet. The 16:20 ET cycle writes one per "
            "session; `oaa daily-report --profile judged` generates a past "
            "date by hand."
            + ("" if mode.is_operator() else
               " On a deployed host the published set comes from "
               "`scripts/publish_reports.py`."),
            icon=":material/description:",
        )
        return

    left, right = st.columns([1, 3], vertical_alignment="bottom")
    date = left.selectbox("Session", dates, index=0,
                          help="Newest first. One file per exchange day.")
    report = load_report(settings, date)
    data, session = report["data"], (report["data"] or {}).get("session", {})
    if report["error"]:
        st.warning(report["error"], icon=":material/error:")
    if report["source"] is not None:
        right.caption(
            f"{len(dates)} session(s) on file · read from "
            f"`{_relative(report['source'], settings)}`"
        )

    if data is None:
        # The sidecar is what this page is built from. Without it there is
        # still the markdown the same run wrote, and showing that beats an
        # empty tab.
        st.info("No `.json` sidecar for this date - showing the written report.")
        st.markdown(report["markdown"] or "_nothing on file_")
        return

    _headline(session)
    _refusals(session)
    _funnel(session)
    _attribution(session)
    _critique(data.get("critique") or {})
    _footer(report, session)


def _relative(path: Path, settings: Any) -> str:
    for root in (settings.root, _repo_root()):
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)


def _headline(session: dict[str, Any]) -> None:
    pl = session.get("day_pl", 0.0) or 0.0
    cols = st.columns(5)
    cols[0].metric(
        "Day P&L", f"{pl:+,.2f}",
        delta=f"{(session.get('day_pl_pct') or 0.0):+.2f}%",
        delta_color="normal" if pl else "off",
    )
    cols[1].metric("Fills", len(session.get("fills") or []),
                   help="Orders that actually filled. Zero is the common case "
                        "and is not by itself a failure.")
    cols[2].metric("Priced, then declined", len(session.get("potential") or []),
                   help="Ideas the books built and priced before refusing "
                        "them. The chain was read for every one of these.")
    cols[3].metric("Risk-approved, unsent", len(session.get("near_misses") or []),
                   help="The risk engine signed the ticket and no order "
                        "exists. The highest-value row on a session that "
                        "filled nothing.")
    cols[4].metric("Gate rejections", session.get("gate_rejections", 0),
                   help="Candidates refused by a gate before becoming an idea.")

    cycles = session.get("cycles_run") or {}
    st.caption(
        f"{session.get('open_positions_at_close', 0)} position(s) open at the "
        f"close · {session.get('snapshots', 0)} equity snapshots · "
        f"{len(session.get('symbols_examined') or [])} symbols examined · "
        + (", ".join(f"{k} ×{v}" for k, v in cycles.items()) if cycles
           else "no cycles recorded")
    )
    if session.get("errors"):
        st.error(
            f"{len(session['errors'])} error(s) during the session - see the "
            "session log below.", icon=":material/error:",
        )


def _refusals(session: dict[str, Any]) -> None:
    near = session.get("near_misses") or []
    if near:
        st.markdown("#### Signed off, and never sent")
        st.caption(
            "The risk engine approved these and no order exists. An approving "
            "verdict carries no reasons, so the critic's own rationale is the "
            "only record of why the trade did not happen."
        )
        for row in near:
            st.warning(
                f"**{row.get('symbol')}** {row.get('structure')} ×"
                f"{row.get('quantity')} at {row.get('net_price')} · max loss "
                f"{_money(row.get('max_loss'))} · {row.get('ts')} ET",
                icon=":material/report:",
            )
            if row.get("thesis"):
                with st.expander(f"{row.get('symbol')} - the thesis it was "
                                 "built on"):
                    st.caption(row["thesis"])
                    if row.get("reason"):
                        st.caption(f"**Rationale:** {row['reason']}")

    ideas = session.get("potential") or []
    st.markdown("#### Built, priced, refused")
    if not ideas:
        st.caption("No idea reached pricing this session.")
        return
    st.caption(
        "Each of these cost a chain read. A rejection that is arithmetic - "
        "sizing, premium - is knowable before the chain is priced, so a book "
        "that keeps appearing here is a book to fix rather than a market to "
        "wait out."
    )
    frame = _ideas_frame(ideas)
    st.dataframe(
        frame, width="stretch", hide_index=True,
        column_config={
            "Net price": st.column_config.NumberColumn(format="%.3f"),
            "Max loss": st.column_config.NumberColumn(format="$%.0f"),
        },
    )
    with st.expander("The thesis behind each"):
        for row in ideas:
            if not row.get("thesis"):
                continue
            st.markdown(f"**{row.get('symbol')}** · {row.get('strategy')} · "
                        f"{row.get('ts')} ET")
            st.caption(row["thesis"])
            st.divider()


def _funnel(session: dict[str, Any]) -> None:
    by_gate = session.get("rejections_by_gate") or {}
    if not by_gate:
        return
    st.markdown("#### Where candidates died")
    st.caption(
        "Which gate refused what, aggregated over the session. The order is a "
        "cost signal as much as a strategy one: a gate that vetoes late has "
        "already spent the requests the earlier gates saved."
    )
    st.plotly_chart(_gate_chart(by_gate), width="stretch",
                    config={"displayModeBar": False})

    left, right = st.columns(2)
    with left:
        st.markdown("**By reason**")
        reasons = session.get("rejections_by_reason") or {}
        if reasons:
            st.dataframe(_counts_frame(reasons, "Reason"), width="stretch",
                         hide_index=True, height=320)
        else:
            st.caption("no reasons recorded")
    with right:
        st.markdown("**By book**")
        books = session.get("rejections_by_book") or {}
        if books:
            st.dataframe(_counts_frame(books, "Book"), width="stretch",
                         hide_index=True)
        else:
            st.caption("no books recorded")
        symbols = session.get("symbols_examined") or []
        if symbols:
            st.caption("Examined: " + ", ".join(symbols))


def _attribution(session: dict[str, Any]) -> None:
    by_strategy = session.get("by_strategy") or {}
    if not by_strategy:
        return
    st.markdown("#### By strategy")
    st.dataframe(
        _strategy_frame(by_strategy), width="stretch", hide_index=True,
        column_config={
            "Realised P&L": st.column_config.NumberColumn(format="%+.2f"),
        },
    )


def _critique(critique: dict[str, Any]) -> None:
    st.markdown("#### Where the algorithm can improve")
    author = str(critique.get("author") or "unknown")
    bullets = critique.get("bullets") or []
    if FALLBACK_MARKER in author.lower():
        st.caption(
            f"Written by **{author}**. The deterministic critique is "
            "arithmetic - it restates the binding constraint out of the gate "
            "funnel - so the section is never blank and never pretends a "
            "model wrote it."
        )
    else:
        st.caption(f"Written by **{author}**, reading this session only.")
    if not bullets:
        st.caption("no critique recorded")
        return
    for bullet in bullets:
        st.markdown(f"- {bullet}")


def _footer(report: dict[str, Any], session: dict[str, Any]) -> None:
    notes = list(dict.fromkeys(session.get("notes") or []))
    errors = session.get("errors") or []
    if notes or errors:
        with st.expander(f"Session log ({len(errors)} error(s), "
                         f"{len(notes)} note(s))"):
            for err in errors:
                st.markdown(f"- **error** — {err}")
            for note in notes:
                st.markdown(f"- {note}")

    if report["markdown"]:
        with st.expander("The written report, as generated"):
            st.markdown(report["markdown"])
        st.download_button(
            "Download this report (.md)", report["markdown"],
            file_name=f"{report['date']}.md", mime="text/markdown",
        )
    generated = (report["data"] or {}).get("generated_at")
    if generated:
        st.caption(f"generated {str(generated)[:19]} UTC")


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"
