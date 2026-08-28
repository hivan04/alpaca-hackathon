#!/usr/bin/env python3
"""Per-instrument price charts with the trades drawn on them.

A metrics table says a book lost money. It does not say whether the entries
were early, whether the exits gave back a move that kept going, or whether the
signal fired into chop. That is what a picture is for, and it is the artefact
worth putting in front of a judge - "here is every decision the agent made and
what the tape did next" is a much stronger claim than a Sharpe ratio.

    python scripts/plot_trades.py                          # newest run
    python scripts/plot_trades.py --run runs/backtests/2026...
    python scripts/plot_trades.py --symbols SPY,QQQ --open

Bars come from the backtest cache, so this costs no API calls when the window
has already been replayed. One self-contained HTML, one panel per instrument:

    price          the intraday close, the tape the agent was reading
    VWAP           the anchor the entry signal is measured against
    entry marker   triangle, pointing the way the position was pointing
    exit marker    green if the trade made money, red if it did not
    connector      entry to exit, so the hold is visible as a span
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import webbrowser
from pathlib import Path

DEFAULT_RUNS = Path("runs/backtests")


def _newest_run(root: Path) -> Path:
    runs = sorted(p for p in root.glob("*") if (p / "result.json").exists())
    if not runs:
        sys.exit(f"no runs with a result.json under {root}")
    return runs[-1]


def _parse(stamp: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None


def _session_vwap(bars: list[dict]) -> list[float | None]:
    """VWAP restarted each session - the anchor the entry actually used."""
    out: list[float | None] = []
    day = None
    pv = vol = 0.0
    for bar in bars:
        stamp = bar["timestamp"]
        if stamp.date() != day:
            day, pv, vol = stamp.date(), 0.0, 0.0
        volume = float(bar.get("volume") or 0.0)
        typical = (float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3
        pv += typical * volume
        vol += volume
        out.append(pv / vol if vol else None)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", help="run directory; default is the newest")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS))
    parser.add_argument("--symbols", help="comma separated; default is every traded symbol")
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--timeframe", default=None,
                        help="bar size; defaults to data.intraday_timeframe")
    parser.add_argument("--out", default=None, help="output HTML; default is inside the run")
    parser.add_argument("--online", action="store_true",
                        help="allow network fetches for bars the cache is missing")
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    from oaa.backtest.feed import HistoricalFeed
    from oaa.config.loader import load_settings

    run = Path(args.run) if args.run else _newest_run(Path(args.runs_root))
    result = json.loads((run / "result.json").read_text())
    trades = result.get("trades") or []
    if not trades:
        sys.exit(f"{run.name} recorded no trades - nothing to plot")

    # `make bt` and most of the CLI examples pass --no-save, which writes no run
    # directory at all. The newest run ON DISK is then an older one, and the
    # chart silently describes a backtest nobody just ran. Say which run this is
    # and how old it is, every time.
    stamp = _parse(str(result.get("provenance", {}).get("generated_at", "")))
    age = ""
    if stamp is not None:
        minutes = (dt.datetime.now(stamp.tzinfo) - stamp).total_seconds() / 60
        age = (f", {minutes:.0f} min ago" if minutes < 90
               else f", {minutes / 60:.1f} HOURS ago")
    if not args.run:
        print(f"plotting the newest SAVED run: {run.name}{age}")
        if age and "HOURS" in age:
            print("  ^ if that is not the run you just executed, it was run with "
                  "--no-save and never written to disk. Re-run without --no-save.")

    wanted = (
        [s.strip().upper() for s in args.symbols.split(",")] if args.symbols
        else sorted({t["symbol"] for t in trades})
    )
    by_symbol: dict[str, list[dict]] = {s: [] for s in wanted}
    for trade in trades:
        if trade["symbol"] in by_symbol:
            by_symbol[trade["symbol"]].append(trade)

    settings = load_settings(profile=args.profile)
    timeframe = args.timeframe or settings.config.data.intraday_timeframe
    feed = HistoricalFeed(
        api_key=settings.credentials.api_key,
        secret_key=settings.credentials.secret_key,
        cache_dir=settings.path(settings.config.backtest.cache_dir),
        stock_feed=settings.config.data.stock_feed,
        offline=not args.online,
    )

    # The replayed window, taken from the run itself. Deriving it from the
    # trade timestamps looked equivalent and is not: the bar cache is keyed on
    # the exact window that was requested, so a window even one day wider is a
    # different key and every lookup misses on an otherwise warm cache.
    request = (result.get("provenance") or {}).get("request") or {}
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    window_start = request.get("start") or manifest.get("start")
    window_end = request.get("end") or manifest.get("end")
    if not (window_start and window_end):
        stamps = [_parse(t["opened_at"]) for t in trades]
        stamps += [_parse(t["closed_at"]) for t in trades]
        stamps = [s for s in stamps if s]
        window_start = (min(stamps).date()).isoformat()
        window_end = (max(stamps).date()).isoformat()
    start = dt.date.fromisoformat(str(window_start))
    end = dt.date.fromisoformat(str(window_end))

    if (request.get("source") or manifest.get("source")) == "synthetic":
        print("NOTE: this run used the SYNTHETIC source - its prices were "
              "invented, and no real bars exist for it. Plot a run made with "
              "--source alpaca to see the tape the agent would actually read.")

    panels = [s for s in wanted if by_symbol[s]]
    titles = [
        f"{s} - {len(by_symbol[s])} trade(s), "
        f"net {sum(float(t.get('net_pnl') or 0) for t in by_symbol[s]):+,.0f}"
        for s in panels
    ]
    figure = make_subplots(
        rows=len(panels), cols=1, subplot_titles=titles,
        vertical_spacing=min(0.08, 1.2 / max(len(panels), 1)),
    )

    missing: list[str] = []
    for row, symbol in enumerate(panels, start=1):
        # Intraday bars first, daily as the fallback. A carry-only replay never
        # fetches intraday bars - the runner only pulls them when an enabled
        # strategy reads them - so a carry run has no 1-minute cache at all,
        # and its holds are days long anyway. Plotting a multi-day condor on a
        # daily line loses nothing.
        bars, used = [], timeframe
        for candidate in (timeframe, "1Day"):
            try:
                rows = feed.bars(symbol, start, end, candidate)
            except Exception:  # noqa: BLE001, S112
                continue
            rows = [b for b in rows if b.get("close") is not None]
            if rows:
                bars, used = rows, candidate
                break
        if not bars:
            missing.append(f"{symbol} (no cached {timeframe} or 1Day bars)")
            continue
        if used != timeframe:
            print(f"  {symbol}: no {timeframe} bars cached, drawn on 1Day")

        times = [b["timestamp"] for b in bars]
        closes = [float(b["close"]) for b in bars]
        figure.add_trace(
            go.Scatter(x=times, y=closes, name=f"{symbol} price", mode="lines",
                       line={"width": 1.1, "color": "#5b6b7c"}, showlegend=False,
                       hovertemplate="%{x|%b %d %H:%M}<br>%{y:.2f}<extra></extra>"),
            row=row, col=1,
        )
        if used != "1Day":
            figure.add_trace(
                go.Scatter(x=times, y=_session_vwap(bars), name="VWAP", mode="lines",
                           line={"width": 1.0, "dash": "dot", "color": "#c9a227"},
                           showlegend=row == 1, legendgroup="vwap",
                           hovertemplate="VWAP %{y:.2f}<extra></extra>"),
                row=row, col=1,
            )

        def _price_at(
            when: dt.datetime | None,
            _times: list = times,
            _closes: list = closes,
        ) -> float | None:
            """Last close at or before the moment - the price the agent saw."""
            if when is None:
                return None
            prior = [c for t, c in zip(_times, _closes, strict=False) if t <= when]
            return prior[-1] if prior else None

        for trade in by_symbol[symbol]:
            opened, closed = _parse(trade["opened_at"]), _parse(trade["closed_at"])
            entry, exit_px = _price_at(opened), _price_at(closed)
            if entry is None or exit_px is None:
                continue
            net = float(trade.get("net_pnl") or 0.0)
            won = net > 0
            # Direction is only meaningful for a directional structure. An iron
            # condor is delta-neutral by construction, so a triangle pointing
            # anywhere is a claim the trade never made.
            structure = str(trade.get("structure") or "")
            directional = int(trade.get("leg_count") or 1) <= 2 and "condor" not in structure
            bullish = "crossed above" in (trade.get("thesis") or "").lower()
            marker = ("triangle-up" if bullish else "triangle-down") if directional \
                else "diamond"
            label = ("long call" if bullish else "long put") if directional \
                else structure.replace("_", " ") or "neutral"
            figure.add_trace(
                go.Scatter(
                    x=[opened, closed], y=[entry, exit_px], mode="lines",
                    line={"width": 1.6, "color": "#2e7d32" if won else "#c62828"},
                    opacity=0.55, showlegend=False, hoverinfo="skip",
                ),
                row=row, col=1,
            )
            figure.add_trace(
                go.Scatter(
                    x=[opened], y=[entry], mode="markers", showlegend=False,
                    marker={"symbol": marker, "size": 11, "color": "#1565c0",
                            "line": {"width": 1, "color": "white"}},
                    hovertemplate=(
                        f"<b>{trade['trade_id']} ENTRY</b><br>"
                        f"{label}<br>"
                        f"%{{x|%b %d %H:%M}}<br>spot %{{y:.2f}}<br>"
                        f"premium {trade.get('entry_price')} x {trade.get('quantity')}"
                        "<extra></extra>"
                    ),
                ),
                row=row, col=1,
            )
            figure.add_trace(
                go.Scatter(
                    x=[closed], y=[exit_px], mode="markers", showlegend=False,
                    marker={"symbol": "x", "size": 9,
                            "color": "#2e7d32" if won else "#c62828",
                            "line": {"width": 1, "color": "white"}},
                    hovertemplate=(
                        f"<b>{trade['trade_id']} EXIT</b><br>"
                        f"%{{x|%b %d %H:%M}}<br>spot %{{y:.2f}}<br>"
                        f"net {net:+,.2f} ({float(trade.get('return_on_risk') or 0):+.1%})<br>"
                        f"{trade.get('exit_reason', '')}"
                        "<extra></extra>"
                    ),
                ),
                row=row, col=1,
            )

    metrics = result.get("metrics", {})
    figure.update_layout(
        height=max(340, 300 * len(panels)),
        title=(
            f"{run.name}<br><sub>{start}  to  {end} &nbsp;|&nbsp; {metrics.get('trades', 0)} trades, "
            f"net {float(metrics.get('net_pnl') or 0):+,.2f}, "
            f"win rate {float(metrics.get('win_rate') or 0):.0%} - "
            "blue triangle = directional entry (up = long call), "
            "diamond = neutral structure, x = exit, "
            "green = winner, red = loser</sub>"
        ),
        template="plotly_white", hovermode="closest",
        margin={"l": 60, "r": 30, "t": 110, "b": 50},
    )
    # Gaps between sessions are not flat price - hide the overnight hours so a
    # 15-minute hold does not render as a hairline next to a 17-hour void.
    figure.update_xaxes(rangebreaks=[
        {"bounds": [16, 9.5], "pattern": "hour"},
        {"bounds": ["sat", "mon"]},
    ])

    out = Path(args.out) if args.out else run / "trades.html"
    figure.write_html(out, include_plotlyjs="cdn")
    print(f"wrote {out}  ({len(panels)} instrument panels, {len(trades)} trades)")
    if missing:
        print("no bars for: " + ", ".join(missing))
        print("  the cache is keyed on the replayed window - re-run with --online "
              "to fetch, or point --run at the window you replayed")
    if args.open_browser:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
