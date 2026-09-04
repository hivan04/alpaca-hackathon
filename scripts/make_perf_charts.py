import sqlite3, datetime as dt, math
from zoneinfo import ZoneInfo
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

ET = ZoneInfo("America/New_York")
BASE = 100_000.0

# ---- design tokens -------------------------------------------------------- #
SURFACE   = "#000000"   # black ground, as asked
AQUA      = "#4cc4de"   # Eventus accent, the aqua-blue highlight
AQUA_WASH = "#4cc4de"
GRID      = "#8a929b"   # light grey gridlines
AXIS      = "#5a6169"
INK       = "#e6e9ec"   # primary text
INK_2     = "#9aa3ad"   # secondary text
SANS      = "Poppins"
MONO      = "DejaVu Sans Mono"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": SANS,
    "text.color": INK, "axes.labelcolor": INK_2,
    "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.edgecolor": AXIS,
})

# ---- data ----------------------------------------------------------------- #
con = sqlite3.connect("runs/judged/oaa.sqlite"); con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute("select ts, equity from equity order by ts")]
for r in rows:
    r["dt"] = dt.datetime.fromisoformat(r["ts"]).astimezone(ET)

# Regular trading hours only: overnight gaps are dead time, not flat performance.
rth = [r for r in rows if dt.time(9, 30) <= r["dt"].time() <= dt.time(16, 0)]
x   = list(range(len(rth)))
eq  = [r["equity"] for r in rth]
pnl = [v - BASE for v in eq]

peak, dd = -1e18, []
for v in eq:
    peak = max(peak, v)
    dd.append((v - peak) / peak * 100.0)

# session boundaries for the x axis
days, starts = [], []
for i, r in enumerate(rth):
    d = r["dt"].date()
    if not days or days[-1] != d:
        days.append(d); starts.append(i)
labels = [d.strftime("%-d %b") for d in days]

trough_i = min(range(len(dd)), key=lambda i: dd[i])


def dress(ax, ylabel):
    ax.set_facecolor(SURFACE)
    ax.grid(True, which="major", color=GRID, linewidth=0.6, alpha=0.45, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS); ax.spines[side].set_linewidth(0.8)
    ax.set_xticks(starts); ax.set_xticklabels(labels, fontfamily=MONO, fontsize=9)
    ax.tick_params(length=0, pad=8)
    for lbl in ax.get_yticklabels():
        lbl.set_fontfamily(MONO); lbl.set_fontsize(9)
    ax.set_ylabel(ylabel, fontsize=9, labelpad=12, color=INK_2)
    ax.set_xlim(-len(x) * 0.01, len(x) * 1.03)
    # session dividers
    for s in starts[1:]:
        ax.axvline(s, color=GRID, linewidth=0.6, alpha=0.22, zorder=1)


def titles(fig, title, subtitle):
    fig.text(0.055, 0.945, title, fontsize=16, fontweight="semibold", color=INK, va="top")
    fig.text(0.055, 0.878, subtitle, fontsize=9.5, color=INK_2, va="top", fontfamily=MONO)


# ---- 1. cumulative P&L ---------------------------------------------------- #
fig, ax = plt.subplots(figsize=(9.6, 5.2), dpi=200)
fig.subplots_adjust(left=0.115, right=0.905, top=0.775, bottom=0.135)
dress(ax, "Cumulative P&L  ($)")
ax.axhline(0, color=AXIS, linewidth=1.0, zorder=2)
ax.fill_between(x, 0, pnl, color=AQUA_WASH, alpha=0.10, linewidth=0, zorder=3)
ax.plot(x, pnl, color=AQUA, linewidth=2.0, solid_joinstyle="round",
        solid_capstyle="round", zorder=4)
ax.plot([x[-1]], [pnl[-1]], "o", markersize=8, color=AQUA,
        markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
ax.annotate(f"+${pnl[-1]:,.0f}", (x[-1], pnl[-1]), xytext=(14, 0),
            textcoords="offset points", va="center", ha="left", fontsize=11.5,
            fontfamily=MONO, color=INK, fontweight="bold", annotation_clip=False)
ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
ax.set_ylim(min(pnl) - 180, max(pnl) + 320)
titles(fig, "Cumulative P&L",
       "Judged paper account  ·  31 Aug – 4 Sep 2026  ·  regular trading hours  ·  base $100,000")
fig.text(0.055, 0.045,
         "Source: judged journal equity snapshots (~5 min). Overnight and pre/post-market excluded.",
         fontsize=7.5, color="#6b737c", fontfamily=MONO)
fig.savefig("out_pnl.png")
plt.close(fig)

# ---- 2. drawdown ---------------------------------------------------------- #
fig, ax = plt.subplots(figsize=(9.6, 5.2), dpi=200)
fig.subplots_adjust(left=0.115, right=0.905, top=0.775, bottom=0.135)
dress(ax, "Drawdown from peak  (%)")
ax.axhline(0, color=AXIS, linewidth=1.0, zorder=2)
ax.fill_between(x, 0, dd, color=AQUA_WASH, alpha=0.14, linewidth=0, zorder=3)
ax.plot(x, dd, color=AQUA, linewidth=2.0, solid_joinstyle="round",
        solid_capstyle="round", zorder=4)
ax.plot([x[trough_i]], [dd[trough_i]], "o", markersize=8, color=AQUA,
        markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
ax.annotate(f"{dd[trough_i]:.2f}%  ·  3 Sep 14:24 ET",
            (x[trough_i], dd[trough_i]), xytext=(-14, -18),
            textcoords="offset points", ha="right", va="top",
            fontsize=10.5, fontfamily=MONO, color=INK, fontweight="bold")
ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.2f}"))
ax.set_ylim(min(dd) * 1.30, abs(min(dd)) * 0.10)
ax.plot([x[-1]], [dd[-1]], "o", markersize=8, color=AQUA,
        markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
ax.annotate(f"{dd[-1]:.2f}%", (x[-1], dd[-1]), xytext=(14, 0),
            textcoords="offset points", va="center", ha="left", fontsize=11.5,
            fontfamily=MONO, color=INK, fontweight="bold", annotation_clip=False)
titles(fig, "Drawdown from running peak",
       "Judged paper account  ·  31 Aug – 4 Sep 2026  ·  max drawdown −0.63%")
fig.text(0.055, 0.045,
         "Marked equity, not realised loss. The book is flat at the 4 Sep close; -0.26% is the distance still owed to the 3 Sep intraday peak.",
         fontsize=7.5, color="#6b737c", fontfamily=MONO)
fig.savefig("out_dd.png")
plt.close(fig)

print("rth points", len(x), "sessions", labels)
print("pnl end", round(pnl[-1], 2), "max dd", round(min(dd), 4))
