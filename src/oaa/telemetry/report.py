"""Render the performance report as text, JSON or a standalone HTML page.

The HTML page is what the judges open: it is the body of `oaa serve`'s "/" and
the artefact `write_report` drops next to every run. It is deliberately
**self-contained** - no CDN, no webfont, no script - so it can be committed,
opened offline on a plane, and dropped straight into the demo video without a
network round trip deciding whether the page has a typeface. That constraint is
why the type stack below is system fonts rather than the webfonts the operator
dashboard loads; everything else about the visual language is the same.

Colour means one thing here. Teal is chrome. Green is profit, red is loss, and
nothing else is allowed to use them - so a glance at the page cannot mislead.
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
:root {{
  --bg:#0a0e14; --panel:#10151f; --raised:#141b28; --line:#1c2430;
  --text:#e8edf4; --muted:#6b7688; --dim:#414c5e;
  --accent:#4cc4de; --up:#3ddc97; --down:#ff5c5c;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --display:"Space Grotesk",ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
}}
@media (prefers-color-scheme: light) {{
  :root {{
    --bg:#f6f7f9; --panel:#ffffff; --raised:#f0f2f5; --line:#e3e6eb;
    --text:#12161f; --muted:#6b7280; --dim:#9aa1ac;
    --accent:#1388a8; --up:#178a5c; --down:#d1373c;
  }}
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; padding:2.5rem 1.5rem 5rem; background:var(--bg); color:var(--text);
  font-family:var(--mono); font-size:14px; line-height:1.55;
  font-variant-numeric:tabular-nums; -webkit-font-smoothing:antialiased;
}}
.wrap {{ max-width:1080px; margin:0 auto; }}

header {{ display:flex; align-items:center; gap:13px; padding-bottom:1.1rem;
          border-bottom:1px solid var(--line); margin-bottom:1.6rem; }}
h1 {{ font-family:var(--display); font-size:1.2rem; font-weight:600; margin:0; letter-spacing:-.01em; }}
.sub {{ color:var(--dim); font-size:.7rem; letter-spacing:.08em; text-transform:uppercase; }}
.stamp {{ margin-left:auto; font-size:.72rem; color:var(--muted); }}
.stamp .dot {{ width:6px; height:6px; border-radius:50%; background:var(--up);
               display:inline-block; margin-right:6px; }}

.grid {{ display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit,minmax(138px,1fr));
         margin-bottom:1.6rem; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:9px;
         padding:.85rem 1rem; }}
.card .label {{ color:var(--dim); font-size:.63rem; text-transform:uppercase;
                letter-spacing:.14em; margin-bottom:.4rem; }}
.card .value {{ font-family:var(--display); font-size:1.35rem; font-weight:600;
                letter-spacing:-.01em; white-space:nowrap; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}

figure {{ margin:0 0 .4rem; background:var(--panel); border:1px solid var(--line);
          border-radius:9px; overflow:hidden; }}
figure svg {{ display:block; width:100%; height:270px; }}
figcaption {{ color:var(--dim); font-size:.66rem; letter-spacing:.12em;
              text-transform:uppercase; padding:.7rem 1rem; border-top:1px solid var(--line); }}

.section {{ margin-top:2rem; }}
.section h2 {{ font-family:var(--display); font-size:.68rem; font-weight:600; color:var(--dim);
               letter-spacing:.16em; text-transform:uppercase; margin:0 0 .6rem; }}
.table-wrap {{ border:1px solid var(--line); border-radius:9px; overflow:hidden; background:var(--panel); }}
table {{ width:100%; border-collapse:collapse; font-size:.8rem; }}
thead th {{ text-align:left; font-size:.63rem; text-transform:uppercase; letter-spacing:.12em;
            color:var(--dim); font-weight:500; padding:.7rem 1rem;
            border-bottom:1px solid var(--line); background:var(--raised); }}
tbody tr {{ border-bottom:1px solid var(--line); }}
tbody tr:last-child {{ border-bottom:none; }}
td {{ padding:.62rem 1rem; vertical-align:middle; }}
td.num {{ text-align:right; }}
th.num {{ text-align:right; }}

.pill {{ display:inline-flex; align-items:center; gap:6px; font-size:.66rem; font-weight:500;
         letter-spacing:.08em; text-transform:uppercase; padding:3px 9px; border-radius:20px;
         background:rgba(127,140,160,.12); color:var(--muted); }}
.pill .d {{ width:5px; height:5px; border-radius:50%; background:currentColor; }}
.pill.yes {{ color:var(--up); background:rgba(61,220,151,.11); }}
.pill.no {{ color:var(--down); background:rgba(255,92,92,.11); }}
footer {{ margin-top:2.5rem; padding-top:1rem; border-top:1px solid var(--line);
          color:var(--dim); font-size:.68rem; letter-spacing:.06em; }}
</style></head><body><div class="wrap">
<header>
  <div><h1>{title}</h1><div class="sub">{subtitle}</div></div>
  <div class="stamp"><span class="dot"></span>{stamp}</div>
</header>
<div class="grid">{cards}</div>
{chart}
{tables}
<footer>Read-only. Orders are placed by the agent process, never from this page.</footer>
</div></body></html>"""



def _card(label: str, value: str, cls: str = "") -> str:
    return (
        f'<div class="card"><div class="label">{label}</div>'
        f'<div class="value {cls}">{value}</div></div>'
    )


def _sparkline(equity: list[float]) -> str:
    """The equity path as an inline SVG - no library, no script, no request.

    Drawn on a viewBox with `preserveAspectRatio="none"` so it stretches to the
    card. The filled area under the line is what makes a 12-point curve read as
    a shape rather than a scribble; the baseline is the starting equity, so the
    fill itself says whether the week is up.
    """
    if len(equity) < 2:
        return ""
    width, height, pad = 900, 270, 22
    lo, hi = min(equity), max(equity)
    span = (hi - lo) or 1
    step = (width - 2 * pad) / (len(equity) - 1)

    def _y(v: float) -> float:
        return height - pad - (v - lo) / span * (height - 2 * pad)

    pts = [(pad + i * step, _y(v)) for i, v in enumerate(equity)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (f"{pad:.1f},{height - pad:.1f} " + line +
            f" {pad + (len(equity) - 1) * step:.1f},{height - pad:.1f}")
    up = equity[-1] >= equity[0]
    colour = "var(--up)" if up else "var(--down)"
    base = _y(equity[0])
    return (
        '<figure><svg viewBox="0 0 900 270" preserveAspectRatio="none" '
        'role="img" aria-label="Equity curve since inception">'
        f'<defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">'
        f'<stop offset="0%" stop-color="{colour}" stop-opacity=".22"/>'
        f'<stop offset="100%" stop-color="{colour}" stop-opacity="0"/>'
        "</linearGradient></defs>"
        f'<polygon fill="url(#g)" points="{area}"/>'
        f'<line x1="{pad}" y1="{base:.1f}" x2="{width - pad}" y2="{base:.1f}" '
        'stroke="currentColor" stroke-opacity=".22" stroke-width="1" stroke-dasharray="3 4"/>'
        f'<polyline fill="none" stroke="{colour}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{line}"/>'
        "</svg>"
        "<figcaption>Equity since inception &nbsp;&middot;&nbsp; "
        f"{len(equity)} marks &nbsp;&middot;&nbsp; low ${lo:,.0f} / high ${hi:,.0f}"
        "</figcaption></figure>"
    )


def _table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    head = "".join(
        f'<th class="num">{h}</th>' if i else f"<th>{h}</th>"
        for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td class="num">{c}</td>' if i else f"<td>{c}</td>"
            for i, c in enumerate(row)
        ) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="section"><h2>{title}</h2><div class="table-wrap">'
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        "</div></div>"
    )


def render_html(
    report: PerformanceReport,
    equity_rows: list[dict[str, Any]],
    title: str = "Eventus Algorithm",
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
        _card("Decisions approved", f"{report.approved}/{report.decisions}"),
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

    stamp = f"{report.trading_days} trading days &middot; {report.snapshots} marks"
    return _HTML.format(
        title=title,
        subtitle=subtitle or stamp,
        stamp=stamp,
        cards=cards,
        chart=_sparkline(equity),
        tables=tables,
    )


def write_report(
    report: PerformanceReport,
    equity_rows: list[dict[str, Any]],
    out_dir: str | Path,
    title: str = "Eventus Algorithm",
) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "report.json"
    html_path = out / "report.html"
    json_path.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    html_path.write_text(render_html(report, equity_rows, title=title), encoding="utf-8")
    return {"json": json_path, "html": html_path}
