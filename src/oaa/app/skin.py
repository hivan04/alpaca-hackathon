"""The dashboard's visual language: fonts, chrome, and the small components.

Streamlit's defaults announce themselves - the generic sans, the loud primary
colour on every widget, the toolbar and hamburger in the corner. This module
overrides them so the operator view reads as an instrument rather than a demo.

Three rules, and they are the whole design:

1. **Two typefaces.** Space Grotesk for headings, IBM Plex Mono for everything
   that is a number, a label, a symbol or a timestamp - which on this page is
   almost everything. Numbers are always tabular so columns align down the page.
2. **One accent.** Teal is brand chrome and "look here". It never means
   direction. Profit is green, loss is red, caution is amber, and nothing else
   is allowed to use those three.
3. **Hairlines, not shadows.** A 1px border at low contrast separates surfaces.

A note on the selectors below: Streamlit's generated class names change between
releases, so every rule here targets a `data-testid` attribute, which is the
part of the DOM Streamlit treats as public. That is why the file is readable and
why it should survive an upgrade. It is still CSS against someone else's markup:
if a widget stops looking right after a Streamlit bump, this is the file.
"""

from __future__ import annotations

import html
from typing import Any

from oaa.app.theme import is_dark, palette

_FONTS = (
    "https://fonts.googleapis.com/css2?"
    "family=Space+Grotesk:wght@500;600;700&"
    "family=IBM+Plex+Mono:wght@400;500;600&display=swap"
)

_MONO = "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace"
_DISPLAY = "'Space Grotesk', ui-sans-serif, -apple-system, 'Segoe UI', sans-serif"

#: Session-state key recording which mode the current page was styled for.
_FLAG = "_skin_mode"


def _tokens(dark: bool) -> dict[str, str]:
    c = palette(dark)
    tint = "255,255,255" if dark else "0,0,0"
    return {
        "bg": c["surface"],
        "panel": c["plane"],
        "raised": c["raised"],
        "line": c["grid"],
        "text": c["text"],
        "muted": c["text_secondary"],
        "dim": c["muted"],
        "accent": c["accent"],
        "good": c["good"],
        "bad": c["critical"],
        "warn": c["warning"],
        "hover": f"rgba({tint},{0.035 if dark else 0.025})",
        "neutral_pill": f"rgba({tint},{0.05 if dark else 0.04})",
    }


def css(dark: bool | None = None) -> str:
    """The stylesheet, as one `<style>` element. Exposed so tests can assert on it.

    Two rules govern the SHAPE of what this returns, and breaking either one
    prints the stylesheet onto the page as visible text instead of applying it:

    1. **It must begin with `<style>`, as the very first character.** Streamlit
       runs markdown through a CommonMark parser before the HTML reaches the
       browser. `<style>` opens a "type 1" raw-HTML block, which runs to the
       closing `</style>` no matter what is in between. Any other leading tag -
       `<link>`, say - opens a "type 7" block instead, and **type 7 ends at the
       first blank line**. Everything after that blank line is then parsed as
       markdown and rendered as paragraphs of CSS source.
    2. **No blank lines inside**, enforced below rather than trusted to
       whoever edits the sheet next. It is belt and braces for rule 1, and it
       costs nothing.

    The webfonts come in via `@import` for the same reason: a `<link>` tag
    cannot lead, and `@import` has to be the first thing in a stylesheet
    anyway, so the two constraints agree.
    """
    t = _tokens(is_dark() if dark is None else dark)
    sheet = f"""@import url('{_FONTS}');
:root {{
  --bg:{t['bg']}; --panel:{t['panel']}; --raised:{t['raised']}; --line:{t['line']};
  --text:{t['text']}; --muted:{t['muted']}; --dim:{t['dim']};
  --accent:{t['accent']}; --good:{t['good']}; --bad:{t['bad']}; --warn:{t['warn']};
  --hover:{t['hover']}; --neutral-pill:{t['neutral_pill']};
  --mono:{_MONO}; --display:{_DISPLAY};
}}

/* ---- foundations ---------------------------------------------------- */
html, body, [data-testid="stAppViewContainer"] {{
  background: var(--bg);
  /* Set the text colour here rather than leaning on theme.textColor: a viewer
     whose browser has an Appearance preference saved can override the config,
     and a page that goes dark-on-dark is worse than one that ignores them. */
  color: var(--text);
  font-family: var(--mono);
  font-feature-settings: "tnum" 1, "cv02" 1;
  -webkit-font-smoothing: antialiased;
}}
h1, h2, h3, h4 {{ font-family: var(--display); letter-spacing:-.01em; color:var(--text); }}
p, li, td, th, label, span {{ color: inherit; }}
[data-testid="stAppViewContainer"] * {{ font-variant-numeric: tabular-nums; }}
code, pre, [data-testid="stCode"] {{ font-family: var(--mono) !important; }}

/* Streamlit's own furniture. The running process is the source of truth for
   this page; the hamburger's "Rerun"/"Clear cache" are footguns mid-session. */
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stStatusWidget"] {{ display:none !important; }}
/* stHeader is the bar those live in. Hiding only its CONTENTS leaves the bar
   itself in place - sticky, opaque, ~3.5rem tall, painted over the top of the
   page - so the first thing on the page gets sliced in half. Emptying it is not
   enough; it has to go. */
[data-testid="stHeader"] {{ display:none !important; }}
/* With the bar gone nothing reserves space at the top, so the padding here is
   the only thing between the nameplate and the window edge. */
.block-container {{ padding-top:2.4rem; padding-bottom:5rem; max-width:1420px; }}

/* ---- sidebar -------------------------------------------------------- */
/* There is no sidebar. `initial_sidebar_state="collapsed"` only closes it;
   this removes it, and the arrow that would reopen an empty panel. */
[data-testid="stSidebar"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] {{ display:none !important; }}

/* ---- section labels -------------------------------------------------- */
.oaa-eyebrow {{
  font-family: var(--mono); font-size:.68rem; font-weight:500;
  letter-spacing:.16em; text-transform:uppercase; color:var(--dim);
  margin:1.9rem 0 .55rem; display:block;
}}
h2, [data-testid="stHeading"] h2 {{ font-size:1.02rem !important; }}
h3, [data-testid="stHeading"] h3 {{ font-size:.92rem !important; }}

/* ---- masthead -------------------------------------------------------- */
.oaa-masthead {{
  display:flex; align-items:center; gap:13px;
  /* No mark: the wordmark is the identity, flush with the content column. */
  /* No rule underneath: the masthead sits in the left column of a two-column
     row, so a border-bottom would stop dead before the profile toggle rather
     than spanning the page. The tab strip below already separates them. */
  padding:0 0 .2rem; margin-bottom:.9rem;
}}
.oaa-wordmark {{ font-family:var(--display); font-size:1.25rem; font-weight:600;
                 line-height:1.2; color:var(--text); }}
.oaa-sub {{ font-size:.7rem; color:var(--dim); letter-spacing:.08em; text-transform:uppercase; }}
.oaa-masthead-right {{ margin-left:auto; display:flex; align-items:center; gap:18px; }}

/* ---- status pills ---------------------------------------------------- */
.oaa-pill {{
  display:inline-flex; align-items:center; gap:6px;
  font-family:var(--mono); font-size:.68rem; font-weight:500;
  letter-spacing:.08em; text-transform:uppercase;
  padding:3px 10px; border-radius:20px;
  background:var(--neutral-pill); color:var(--muted);
  border:1px solid transparent;
}}
.oaa-pill .dot {{ width:5px; height:5px; border-radius:50%; background:currentColor; flex:none; }}
.oaa-pill.live {{ color:var(--good); background:color-mix(in srgb, var(--good) 11%, transparent);
                  border-color:color-mix(in srgb, var(--good) 26%, transparent); }}
.oaa-pill.warn {{ color:var(--warn); background:color-mix(in srgb, var(--warn) 11%, transparent);
                  border-color:color-mix(in srgb, var(--warn) 28%, transparent); }}
.oaa-pill.bad  {{ color:var(--bad);  background:color-mix(in srgb, var(--bad) 11%, transparent);
                  border-color:color-mix(in srgb, var(--bad) 26%, transparent); }}

/* ---- tabs as a segmented control ------------------------------------- */
[data-testid="stTabs"] [role="tablist"] {{
  gap:4px; background:var(--panel); border:1px solid var(--line);
  border-radius:9px; padding:4px; width:fit-content; margin-bottom:.5rem;
}}
[data-testid="stTabs"] [role="tablist"]::before,
[data-testid="stTabs"] [role="tablist"]::after {{ display:none; }}
[data-testid="stTabs"] [role="tab"] {{
  font-family:var(--mono); font-size:.78rem; font-weight:500;
  padding:.4rem 1.05rem; border-radius:6px; color:var(--muted);
  border:none !important; background:none;
}}
[data-testid="stTabs"] [role="tab"]:hover {{ color:var(--text); }}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {{
  background:var(--raised); color:var(--text);
}}
[data-testid="stTabs"] [data-baseweb="tab-highlight"],
[data-testid="stTabs"] [data-baseweb="tab-border"] {{ display:none !important; }}

/* ---- metrics: the loudest generic-Streamlit tell ---------------------- */
[data-testid="stMetric"] {{
  background:var(--panel); border:1px solid var(--line);
  border-radius:9px; padding:.85rem 1rem;
}}
[data-testid="stMetricLabel"] p {{
  font-family:var(--mono) !important; font-size:.64rem !important; font-weight:500 !important;
  letter-spacing:.14em; text-transform:uppercase; color:var(--dim) !important;
}}
[data-testid="stMetricValue"] {{
  font-family:var(--display); font-size:1.45rem !important; font-weight:600;
  color:var(--text);
}}
[data-testid="stMetricDelta"] {{ font-family:var(--mono); font-size:.72rem;
                                color:var(--muted); }}

/* ---- surfaces: tables, expanders, inputs, charts ---------------------- */
[data-testid="stDataFrame"], [data-testid="stTable"], [data-testid="stPlotlyChart"] {{
  border:1px solid var(--line); border-radius:9px; overflow:hidden; background:var(--panel);
}}
[data-testid="stDataFrame"] * {{ font-family:var(--mono) !important; font-size:.78rem; }}
[data-testid="stDataFrame"] td {{ color:var(--text); }}
[data-testid="stExpander"] {{
  border:1px solid var(--line) !important; border-radius:9px; background:var(--panel);
  box-shadow:none !important;
}}
[data-testid="stExpander"] summary {{ font-family:var(--mono); font-size:.8rem; }}
[data-testid="stExpander"] summary:hover {{ background:var(--hover); }}
[data-testid="stAlert"] {{ border-radius:9px; font-size:.82rem; }}
[data-baseweb="select"] > div, [data-testid="stTextInput"] input {{
  background:var(--panel) !important; border-color:var(--line) !important;
  font-family:var(--mono) !important; font-size:.8rem !important;
}}
[data-testid="stButton"] button {{
  font-family:var(--mono); font-size:.78rem; border-radius:7px;
  border:1px solid var(--line); background:var(--panel); color:var(--muted);
}}
[data-testid="stButton"] button:hover {{ color:var(--text); border-color:var(--muted); background:var(--raised); }}
hr {{ border-color:var(--line); }}

/* ---- key/value strip -------------------------------------------------- */
.oaa-kv {{ display:flex; flex-wrap:wrap; gap:0 30px; margin:.1rem 0 .9rem; }}
.oaa-kv div {{ font-size:.73rem; color:var(--muted); }}
.oaa-kv b {{ color:var(--text); font-weight:500; }}
"""
    body = "\n".join(line for line in sheet.splitlines() if line.strip())
    return f"<style>\n{body}\n</style>"


def inject() -> None:
    """Put the stylesheet on the page. Call once, at the top of every render.

    This MUST run on every script run, not once per session. Streamlit rebuilds
    the whole element tree from scratch each rerun and only renders what the
    script emits *this* time; an element skipped because a session-state flag
    said "already done" simply is not on the page. Guarding this with a
    persistent flag styles the first paint and then silently drops the
    stylesheet the moment anything is clicked - which reads as "the theme did
    not apply" and sent a good half hour down the wrong drain.

    The flag is kept only to record which mode was drawn, for `is_dark`.
    """
    import streamlit as st

    st.session_state[_FLAG] = "dark" if is_dark() else "light"
    st.markdown(css(), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# components
# --------------------------------------------------------------------------- #
def eyebrow(text: str) -> None:
    """An uppercase section label. Replaces st.subheader where it is a label."""
    import streamlit as st

    st.markdown(f'<span class="oaa-eyebrow">{html.escape(text)}</span>',
                unsafe_allow_html=True)


def pill(text: str, tone: str = "") -> str:
    """A status pill, as HTML. `tone` is one of '', 'live', 'warn', 'bad'."""
    tone = tone if tone in ("live", "warn", "bad") else ""
    return (f'<span class="oaa-pill {tone}"><span class="dot"></span>'
            f"{html.escape(text)}</span>")


def masthead(title: str, subtitle: str = "", right: str = "") -> None:
    """The wordmark, and whatever the caller puts to its right.

    `subtitle` and `right` are optional and omitted entirely when empty - the
    top of the page is a nameplate, not a status readout. The account identity
    that used to live here is still on every tab, in `_identity_banner`, where
    it is read rather than glanced at.
    """
    import streamlit as st

    sub = (f'<div class="oaa-sub">{html.escape(subtitle)}</div>'
           if subtitle else "")
    aside = f'<div class="oaa-masthead-right">{right}</div>' if right else ""
    st.markdown(
        f'<div class="oaa-masthead">'
        f'<div><div class="oaa-wordmark">{html.escape(title)}</div>{sub}</div>'
        f"{aside}</div>",
        unsafe_allow_html=True,
    )


def kv(pairs: list[tuple[str, Any]]) -> None:
    """A dense key/value strip - the things you glance at, not read."""
    import streamlit as st

    body = "".join(
        f"<div>{html.escape(str(k))} <b>{html.escape(str(v))}</b></div>"
        for k, v in pairs
    )
    st.markdown(f'<div class="oaa-kv">{body}</div>', unsafe_allow_html=True)
