#!/usr/bin/env python3
"""Cumulative P&L over time for one or more saved backtest runs.

The metrics table gives you a single number per run. It does not show you WHEN
the money was made or lost - whether an edge accrued steadily or arrived in one
trade, whether a drawdown was a slow bleed or a cliff. That is what this is for,
and it is the picture to put in front of a judge next to the equity number.

    python scripts/plot_equity.py                                  # newest run
    python scripts/plot_equity.py --run runs/backtests/2026...     # one run
    python scripts/plot_equity.py --run A --run B --open           # overlay two
    python scripts/plot_equity.py --last 3                         # newest three

Reads `equity.csv` (timestamp,equity) from each run directory - written by every
saved backtest - so it costs no API calls and no re-run. Output is one
self-contained HTML file with no external assets, same as plot_trades.py.

Plotted as cumulative P&L in dollars from a zero baseline, not raw equity: on a
$100k account a $2,000 result is a 2% wiggle on an equity axis and invisible.

X is the observation ORDINAL, not wall-clock time. The replay samples a 15-minute
grid inside sessions, so real time would spend most of the axis on nights and
weekends. Ticks carry the date so the shape stays readable.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import sys
import webbrowser
from pathlib import Path

# Validated 2-series categorical slots (blue, orange) - light / dark.
# node scripts/validate_palette.js "#2a78d6,#eb6834" --mode light -> ALL PASS
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500"]


def _runs_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "runs").is_dir() and (parent / "src").is_dir():
            return parent / "runs" / "backtests"
    return Path.cwd() / "runs" / "backtests"


def _load(run_dir: Path) -> dict:
    csv_path = run_dir / "equity.csv"
    if not csv_path.exists():
        raise SystemExit(f"no equity.csv in {run_dir} - was the run saved?")
    stamps: list[str] = []
    equity: list[float] = []
    with csv_path.open() as fh:
        for row in csv.DictReader(fh):
            stamps.append(row["timestamp"])
            equity.append(float(row["equity"]))
    if not equity:
        raise SystemExit(f"{csv_path} has no rows")

    label = run_dir.name
    meta: dict = {}
    manifest = run_dir / "manifest.json"
    if manifest.exists():
        meta = json.loads(manifest.read_text())
        if meta.get("label"):
            label = meta["label"]

    # A run's own provenance decides what it IS. Two runs are only comparable
    # when profile, variant and the offline flag all match - all three have
    # silently produced "regressions" in this repo that were nothing of the kind.
    prov: dict = {}
    result = run_dir / "result.json"
    if result.exists():
        try:
            payload = json.loads(result.read_text())
            p = payload.get("provenance") or {}
            prov = {
                "profile": p.get("profile"),
                "variant": p.get("variant"),
                "offline": (p.get("request") or {}).get("offline"),
                "commit": (p.get("git_worktree") or {}).get("commit"),
                "dirty": (p.get("git_worktree") or {}).get("dirty"),
            }
        except (ValueError, OSError):
            prov = {}

    start = equity[0]
    pnl = [round(v - start, 2) for v in equity]
    peak, drawdown = pnl[0], []
    for v in pnl:
        peak = max(peak, v)
        drawdown.append(round(v - peak, 2))

    m = (meta.get("metrics") or {})
    return {
        "id": run_dir.name,
        "label": label,
        "stamps": stamps,
        "pnl": pnl,
        "drawdown": drawdown,
        "prov": prov,
        "metrics": {
            "net": m.get("net_pnl", pnl[-1]),
            "trades": m.get("trades"),
            "win_rate": m.get("win_rate"),
            "profit_factor": m.get("profit_factor"),
            "max_drawdown": m.get("max_drawdown"),
            "start": meta.get("start"),
            "end": meta.get("end"),
        },
    }


def _dates(stamps: list[str]) -> list[str]:
    out = []
    for s in stamps:
        try:
            out.append(dt.datetime.fromisoformat(s).strftime("%d %b"))
        except ValueError:
            out.append(s[:10])
    return out


def build(series: list[dict], title: str) -> str:
    payload = json.dumps(
        [
            {
                "label": s["label"],
                "pnl": s["pnl"],
                "drawdown": s["drawdown"],
                "dates": _dates(s["stamps"]),
                "metrics": s["metrics"],
                "prov": s["prov"],
            }
            for s in series
        ]
    )

    def prov_line(s: dict) -> str:
        p = s["prov"]
        if not p:
            return ""
        bits = [
            f"profile {p.get('profile') or '?'}",
            f"variant {p.get('variant') or 'baseline'}",
            f"offline {p.get('offline')}",
            f"commit {p.get('commit') or '?'}{'-dirty' if p.get('dirty') else ''}",
        ]
        return " · ".join(bits)

    rows = "".join(
        f"<tr><td><span class='swatch' style='background:var(--series-{i + 1})'></span>"
        f"{html.escape(s['label'])}</td>"
        f"<td class='num'>{s['metrics']['net']:,.2f}</td>"
        f"<td class='num'>{s['metrics']['trades'] if s['metrics']['trades'] is not None else '-'}</td>"
        f"<td class='num'>{(s['metrics']['win_rate'] or 0) * 100:.1f}%</td>"
        f"<td class='num'>{s['metrics']['profit_factor'] if s['metrics']['profit_factor'] is not None else '-'}</td>"
        f"<td class='num'>{(s['metrics']['max_drawdown'] or 0) * 100:.2f}%</td>"
        f"<td class='prov'>{html.escape(prov_line(s))}</td></tr>"
        for i, s in enumerate(series)
    )

    warn = ""
    keys = {(s["prov"].get("profile"), s["prov"].get("variant"), s["prov"].get("offline")) for s in series if s["prov"]}
    if len(series) > 1 and len(keys) > 1:
        warn = (
            "<p class='warn'><strong>These runs are not comparable.</strong> They "
            "differ in profile, variant or the offline flag - not only in strategy. "
            "Fix all three before reading the difference as a result.</p>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  color-scheme: light;
  --surface-1:#fcfcfb; --surface-2:#f3f3f1; --border:#e0e0dc;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#82817c;
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a; --series-4:#eda100;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --surface-1:#1a1a19; --surface-2:#232322; --border:#3a3a38;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8d8c85;
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface-1:#1a1a19; --surface-2:#232322; --border:#3a3a38;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8d8c85;
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
}}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--surface-1); color:var(--text-primary);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1100px; margin:0 auto; padding:32px 24px 64px }}
h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:-0.01em }}
.sub {{ color:var(--text-secondary); margin:0 0 28px; font-size:13px }}
.legend {{ display:flex; gap:20px; flex-wrap:wrap; margin:0 0 12px }}
.legend span {{ display:flex; align-items:center; gap:7px; font-size:13px; color:var(--text-secondary) }}
.swatch {{ width:10px; height:10px; border-radius:2px; display:inline-block; flex:none }}
figure {{ margin:0 0 28px }}
figcaption {{ font-size:12px; color:var(--text-muted); margin:6px 0 0 }}
svg {{ display:block; width:100%; height:auto; overflow:visible }}
.grid line {{ stroke:var(--border); stroke-width:1 }}
.zero {{ stroke:var(--text-muted); stroke-width:1; stroke-dasharray:3 3 }}
.tick {{ fill:var(--text-muted); font-size:11px }}
.axis-title {{ fill:var(--text-secondary); font-size:11px }}
.end-label {{ font-size:12px; font-weight:600 }}
table {{ border-collapse:collapse; width:100%; font-size:13px; margin-top:8px }}
th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--border) }}
th {{ color:var(--text-secondary); font-weight:600; font-size:12px }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums }}
td.prov {{ color:var(--text-muted); font-size:11px }}
.warn {{ background:var(--surface-2); border-left:3px solid var(--series-2);
  padding:10px 14px; font-size:13px; color:var(--text-secondary); border-radius:0 4px 4px 0 }}
#tip {{ position:fixed; pointer-events:none; opacity:0; transition:opacity .08s;
  background:var(--surface-2); border:1px solid var(--border); border-radius:6px;
  padding:8px 10px; font-size:12px; box-shadow:0 4px 14px rgba(0,0,0,.14); z-index:9 }}
#tip .r {{ display:flex; justify-content:space-between; gap:14px; font-variant-numeric:tabular-nums }}
#tip .d {{ color:var(--text-muted); margin-bottom:5px }}
h2 {{ font-size:14px; margin:28px 0 6px; color:var(--text-secondary); font-weight:600 }}
</style></head><body>
<div class="wrap">
  <h1>{html.escape(title)}</h1>
  <p class="sub">Cumulative P&amp;L in dollars from each run's own starting equity.</p>
  {warn}
  <div class="legend" id="legend"></div>
  <figure>
    <svg id="pnl" viewBox="0 0 900 360" role="img" aria-label="Cumulative profit and loss over time"></svg>
    <figcaption>Cumulative P&amp;L ($). X is the observation ordinal; ticks carry the date.</figcaption>
  </figure>
  <figure>
    <svg id="dd" viewBox="0 0 900 170" role="img" aria-label="Drawdown from peak"></svg>
    <figcaption>Drawdown from running peak ($). Separate panel, own axis - never a second scale on the chart above.</figcaption>
  </figure>
  <h2>The numbers, and what each run actually was</h2>
  <table><thead><tr><th>Run</th><th class="num">Net P&amp;L</th><th class="num">Trades</th>
    <th class="num">Win rate</th><th class="num">Profit factor</th><th class="num">Max DD</th>
    <th>Provenance</th></tr></thead><tbody>{rows}</tbody></table>
</div>
<div id="tip"></div>
<script>
const DATA = {payload};
const NF = new Intl.NumberFormat('en-US',{{minimumFractionDigits:0,maximumFractionDigits:0}});
const legend = document.getElementById('legend');
DATA.forEach((s,i)=>{{
  const el=document.createElement('span');
  el.innerHTML=`<span class="swatch" style="background:var(--series-${{i+1}})"></span>${{s.label}}`;
  legend.appendChild(el);
}});

function draw(svgId, key, height, invert){{
  const svg=document.getElementById(svgId);
  const W=900,H=height,P={{l:62,r:96,t:14,b:30}};
  const n=Math.max(...DATA.map(s=>s[key].length));
  let lo=0,hi=0;
  DATA.forEach(s=>s[key].forEach(v=>{{lo=Math.min(lo,v);hi=Math.max(hi,v)}}));
  if(hi===lo){{hi=lo+1}}
  // Snap to a round step so the axis reads $2,500 rather than $18,381. No
  // pre-padding: snapping to the step already leaves headroom, and padding
  // first pushed an axis that barely went negative down to -$10,000.
  const raw=(hi-lo)/6, mag=Math.pow(10,Math.floor(Math.log10(raw)));
  const step=[1,1.5,2,2.5,3,4,5,7.5,10].map(m=>m*mag).find(v=>v>=raw)||10*mag;
  lo=Math.floor(lo/step)*step; hi=Math.ceil(hi/step)*step;
  if(hi===lo){{hi=lo+step}}
  const x=i=>P.l+(i/Math.max(1,n-1))*(W-P.l-P.r);
  const y=v=>P.t+(1-(v-lo)/(hi-lo))*(H-P.t-P.b);
  let out='';
  // recessive gridlines + y ticks
  for(let v=lo; v<=hi+1e-9; v+=step){{
    const yy=y(v);
    out+=`<g class="grid"><line x1="${{P.l}}" y1="${{yy}}" x2="${{W-P.r}}" y2="${{yy}}"/></g>`;
    out+=`<text class="tick" x="${{P.l-8}}" y="${{yy+4}}" text-anchor="end">$${{NF.format(v)}}</text>`;
  }}
  if(lo<0&&hi>0) out+=`<line class="zero" x1="${{P.l}}" y1="${{y(0)}}" x2="${{W-P.r}}" y2="${{y(0)}}"/>`;
  // x ticks: 6 evenly spaced, labelled with the date
  const dates=DATA[0].dates;
  for(let k=0;k<6;k++){{
    const i=Math.round(k*(n-1)/5);
    out+=`<text class="tick" x="${{x(i)}}" y="${{H-8}}" text-anchor="middle">${{dates[i]||''}}</text>`;
  }}
  DATA.forEach((s,si)=>{{
    const d=s[key].map((v,i)=>`${{i?'L':'M'}}${{x(i).toFixed(1)}},${{y(v).toFixed(1)}}`).join(' ');
    out+=`<path d="${{d}}" fill="none" stroke="var(--series-${{si+1}})" stroke-width="2"
           stroke-linejoin="round" stroke-linecap="round"/>`;
    const last=s[key][s[key].length-1];
    out+=`<circle cx="${{x(s[key].length-1)}}" cy="${{y(last)}}" r="4"
           fill="var(--series-${{si+1}})" stroke="var(--surface-1)" stroke-width="2"/>`;
    out+=`<text class="end-label" x="${{x(s[key].length-1)+10}}" y="${{y(last)+4}}"
           fill="var(--series-${{si+1}})">$${{NF.format(last)}}</text>`;
  }});
  out+=`<line id="${{svgId}}-cross" x1="0" y1="${{P.t}}" x2="0" y2="${{H-P.b}}"
         stroke="var(--text-muted)" stroke-width="1" opacity="0"/>`;
  out+=`<rect id="${{svgId}}-hit" x="${{P.l}}" y="${{P.t}}" width="${{W-P.l-P.r}}"
         height="${{H-P.t-P.b}}" fill="transparent"/>`;
  svg.innerHTML=out;
  return {{svg,x,y,n,P,W,H}};
}}

const panels=[draw('pnl','pnl',360),draw('dd','drawdown',170)];
const tip=document.getElementById('tip');
panels.forEach(p=>{{
  const hit=p.svg.querySelector('rect[id$="-hit"]');
  const cross=p.svg.querySelector('line[id$="-cross"]');
  hit.addEventListener('pointermove',ev=>{{
    const r=p.svg.getBoundingClientRect();
    const sx=(ev.clientX-r.left)/r.width*p.W;
    let i=Math.round((sx-p.P.l)/(p.W-p.P.l-p.P.r)*(p.n-1));
    i=Math.max(0,Math.min(p.n-1,i));
    cross.setAttribute('x1',p.x(i));cross.setAttribute('x2',p.x(i));
    cross.setAttribute('opacity','0.5');
    let h=`<div class="d">${{DATA[0].dates[i]||''}}</div>`;
    DATA.forEach((s,si)=>{{
      const v=s.pnl[i]; if(v===undefined) return;
      h+=`<div class="r"><span><span class="swatch" style="background:var(--series-${{si+1}})"></span>
          ${{s.label}}</span><span>$${{NF.format(v)}}</span></div>`;
    }});
    tip.innerHTML=h;tip.style.opacity='1';
    tip.style.left=Math.min(window.innerWidth-190,ev.clientX+14)+'px';
    tip.style.top=(ev.clientY+14)+'px';
  }});
  hit.addEventListener('pointerleave',()=>{{tip.style.opacity='0';cross.setAttribute('opacity','0')}});
}});
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="append", default=[],
                    help="Run directory. Repeat to overlay runs.")
    ap.add_argument("--last", type=int, default=0,
                    help="Overlay the N newest saved runs.")
    ap.add_argument("--out", default=None, help="Output HTML path")
    ap.add_argument("--open", action="store_true", help="Open in a browser")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    root = _runs_root()
    if args.run:
        dirs = [Path(r) for r in args.run]
    else:
        saved = sorted((d for d in root.iterdir() if (d / "equity.csv").exists()),
                       key=lambda d: d.name, reverse=True)
        if not saved:
            raise SystemExit(f"no saved runs with an equity.csv under {root}")
        dirs = saved[: max(1, args.last)]

    if len(dirs) > 4:
        raise SystemExit(
            f"{len(dirs)} runs asked for; the validated palette carries 4. "
            "Plot fewer, or facet them."
        )

    series = [_load(d if d.is_absolute() else Path.cwd() / d) for d in dirs]
    title = args.title or (
        series[0]["label"] if len(series) == 1
        else " vs ".join(s["label"] for s in series)
    )
    out = Path(args.out) if args.out else (dirs[0] if len(dirs) == 1 else root) / "equity.html"
    out.write_text(build(series, title), encoding="utf-8")
    print(f"wrote {out}")
    for s in series:
        m = s["metrics"]
        print(f"  {s['label']:<34} net ${m['net']:>10,.2f}  trades {m['trades']}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    sys.exit(main())
