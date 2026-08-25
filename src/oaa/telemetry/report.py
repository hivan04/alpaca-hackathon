"""Render the performance report as text, JSON or a standalone HTML page.

The HTML page is self-contained (no CDN) so it can be committed, opened
offline, or dropped straight into the demo video.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oaa.telemetry.metrics import PerformanceReport

_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#0d1117; --fg:#e6edf3; --dim:#7d8590; --line:#21262d;
        --up:#3fb950; --down:#f85149; --accent:#58a6ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:2rem; background:var(--bg); color:var(--fg);
        font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
h1 {{ font-size:1.35rem; margin:0 0 .25rem; }}
.sub {{ color:var(--dim); font-size:.85rem; margin-bottom:1.75rem; }}
.grid {{ display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
         margin-bottom:2rem; }}
.card {{ background:#161b22; border:1px solid var(--line); border-radius:10px; padding:1rem 1.1rem; }}
.card .label {{ color:var(--dim); font-size:.72rem; text-transform:uppercase;
                letter-spacing:.06em; margin-bottom:.35rem; }}
.card .value {{ font-size:1.5rem; font-weight:600; font-variant-numeric:tabular-nums; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}
svg {{ width:100%; height:260px; background:#161b22; border:1px solid var(--line);
       border-radius:10px; }}
table {{ width:100%; border-collapse:collapse; margin-top:1.5rem; font-size:.85rem; }}
th,td {{ text-align:left; padding:.5rem .6rem; border-bottom:1px solid var(--line); }}
th {{ color:var(--dim); font-weight:500; text-transform:uppercase; font-size:.7rem;
      letter-spacing:.05em; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.section {{ margin-top:2.25rem; }}
.section h2 {{ font-size:.95rem; color:var(--accent); margin:0 0 .5rem; }}
</style></head><body>
<h1>{title}</h1>
<div class="sub">{subtitle}</div>
<div class="grid">{cards}</div>
{chart}
{tables}
</body></html>"""


def _card(label: str, value: str, cls: str = "") -> str:
    return (
        f'<div class="card"><div class="label">{label}</div>'
        f'<div class="value {cls}">{value}</div></div>'
    )


def _sparkline(equity: list[float]) -> str:
    if len(equity) < 2:
        return ""
    width, height, pad = 900, 260, 16
    lo, hi = min(equity), max(equity)
    span = (hi - lo) or 1
    step = (width - 2 * pad) / (len(equity) - 1)
    points = " ".join(
        f"{pad + i * step:.1f},{height - pad - (v - lo) / span * (height - 2 * pad):.1f}"
        for i, v in enumerate(equity)
    )
    colour = "#3fb950" if equity[-1] >= equity[0] else "#f85149"
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="Equity curve">'
        f'<polyline fill="none" stroke="{colour}" stroke-width="2" points="{points}"/>'
        f"</svg>"
    )


def _table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(
            f'<td class="num">{c}</td>' if i else f"<td>{c}</td>"
            for i, c in enumerate(row)
        ) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="section"><h2>{title}</h2>'
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_html(
    report: PerformanceReport,
    equity_rows: list[dict[str, Any]],
    title: str = "Options Alpha Agents",
    subtitle: str = "",
) -> str:
    trend = "up" if report.absolute_pl >= 0 else "down"
    cards = "".join([
        _card("Equity", f"${report.end_equity:,.0f}"),
        _card("P&L", f"{'+' if report.absolute_pl >= 0 else ''}${report.absolute_pl:,.0f}", trend),
        _card("Return", f"{report.total_return_pct:+.2%}", trend),
        _card("Max drawdown", f"{report.max_drawdown_pct:.2%}", "down"),
        _card("Sharpe", "n/a" if report.sharpe is None else f"{report.sharpe:.2f}"),
        _card("Orders filled", f"{report.filled_orders}/{report.total_orders}"),
        _card("Decisions", f"{report.approved}/{report.decisions} approved"),
    ])
    equity = [float(r["equity"]) for r in equity_rows if r.get("equity") is not None]

    tables = _table(
        "By strategy",
        ["Strategy", "Decisions", "Approved", "Symbols"],
        [
            [name, str(b["decisions"]), str(b["approved"]), str(len(b["symbols"]))]
            for name, b in sorted(report.by_strategy.items())
        ],
    ) + _table(
        "Why trades were declined",
        ["Rule", "Count"],
        [[rule, str(n)] for rule, n in sorted(
            report.rejection_reasons.items(), key=lambda kv: -kv[1])],
    )

    return _HTML.format(
        title=title,
        subtitle=subtitle or f"{report.trading_days} trading days, {report.snapshots} snapshots",
        cards=cards,
        chart=_sparkline(equity),
        tables=tables,
    )


def write_report(
    report: PerformanceReport,
    equity_rows: list[dict[str, Any]],
    out_dir: str | Path,
    title: str = "Options Alpha Agents",
) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "report.json"
    html_path = out / "report.html"
    json_path.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    html_path.write_text(render_html(report, equity_rows, title=title), encoding="utf-8")
    return {"json": json_path, "html": html_path}
