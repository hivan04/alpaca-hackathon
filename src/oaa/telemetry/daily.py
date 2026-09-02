"""The end-of-day evaluator.

One trading session in, one report out: what the algorithm *could* have
executed, what it actually filled, what the day cost or made, and - through
Featherless - a short list of things to change tomorrow.

Three ideas shape this file.

**The declined trades are the report.** On most sessions this book fills
nothing: every gate is a reason not to trade and the funnel is where the day's
information is. A report that only counted fills would be blank on exactly the
days there is most to learn, so the gate funnel and the near-misses (an idea
that was fully priced and then vetoed) are first-class, not an appendix.

**It reads the journal, never the broker.** The journal is append-only and
already the evidence trail; regenerating yesterday's report must give
yesterday's numbers, which a live account query cannot promise. That also makes
the whole thing testable and re-runnable.

**Degradation is visible.** With no reasoning provider the critique still
appears - written by `_deterministic_bullets` - and the report says which one
wrote it. A silent drop to rules is the failure mode this repo keeps finding.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from oaa.core.logging import get_logger

log = get_logger("telemetry.daily")

#: Journal `kind`s worth naming in the report. Everything else is still
#: counted, just not narrated.
_NOTABLE_EVENTS = (
    "cycle_error",
    "agent_degraded",
    "firewall_cutoff",
    "firewall_lock",
    "events_arm",
    "events_flatten",
    "events_watch",
    "discovery",
    "macro_view",
    "switchboard",
    "operator_note",
)


def session_bounds(day: dt.date, timezone: str = "America/New_York") -> tuple[str, str]:
    """UTC string bounds for one exchange-local calendar day.

    Returned without an offset suffix on purpose: the journal writes some
    timestamps with `Z` and some with `+00:00`, and both sort correctly against
    a bare `YYYY-MM-DDTHH:MM:SS` bound.
    """
    tz = ZoneInfo(timezone)
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz).astimezone(dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


@dataclass
class PotentialTrade:
    """An idea the system built and then declined to send."""

    ts: str
    symbol: str
    strategy: str
    structure: str | None
    quantity: int | None
    net_price: float | None
    max_loss: float | None
    max_profit: float | None
    reason: str
    thesis: str
    #: The risk engine's verdict. True here with `action == "skip"` is the
    #: interesting row: risk signed the ticket and the trade still did not go.
    risk_approved: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class FilledOrder:
    ts: str
    symbol: str
    status: str
    filled_qty: float | None
    filled_price: float | None
    order_id: str

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class DailySession:
    """Everything the report and the critic are allowed to know about a day."""

    date: str
    profile: str
    timezone: str = "America/New_York"

    # P&L
    open_equity: float | None = None
    close_equity: float | None = None
    day_pl: float | None = None
    day_pl_pct: float | None = None
    snapshots: int = 0
    open_positions_at_close: int | None = None

    # what happened
    cycles_run: dict[str, int] = field(default_factory=dict)
    orders_sent: int = 0
    fills: list[FilledOrder] = field(default_factory=list)
    closes: list[dict[str, Any]] = field(default_factory=list)
    potential: list[PotentialTrade] = field(default_factory=list)

    # the funnel
    gate_rejections: int = 0
    rejections_by_gate: dict[str, int] = field(default_factory=dict)
    rejections_by_reason: dict[str, int] = field(default_factory=dict)
    rejections_by_book: dict[str, int] = field(default_factory=dict)
    symbols_examined: list[str] = field(default_factory=list)

    # per-book attribution
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)

    # health
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reasoning_available: bool = True

    @property
    def traded(self) -> bool:
        return bool(self.fills)

    @property
    def near_misses(self) -> list[PotentialTrade]:
        """Ideas the RISK ENGINE approved that still did not reach the broker.

        The closest thing this system has to a trade it should have taken, and
        the first place to look when a session fills nothing.
        """
        return [p for p in self.potential if p.risk_approved]

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload["fills"] = [f.as_dict() for f in self.fills]
        payload["potential"] = [p.as_dict() for p in self.potential]
        payload["traded"] = self.traded
        payload["near_misses"] = [p.as_dict() for p in self.near_misses]
        return payload

    def headline(self) -> str:
        pl = "n/a" if self.day_pl is None else f"{self.day_pl:+,.2f}"
        return (
            f"{self.date} ({self.profile}): P&L {pl}, "
            f"{len(self.fills)} fill(s), {len(self.potential)} declined idea(s) "
            f"({len(self.near_misses)} risk-approved), "
            f"{self.gate_rejections} gate rejection(s)"
        )


def _iso_local(ts: str, timezone: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(ZoneInfo(timezone)).strftime("%H:%M:%S")


def _first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    for stop in (" - ", ". "):
        head = text.split(stop)[0]
        if 12 < len(head) < len(text):
            text = head
            break
    return text[:limit]


def collect_session(
    journal: Any,
    day: dt.date,
    profile: str = "default",
    timezone: str = "America/New_York",
) -> DailySession:
    """Read one session out of the journal. No network, no broker."""
    start, end = session_bounds(day, timezone)
    session = DailySession(date=day.isoformat(), profile=profile, timezone=timezone)

    # ---- P&L, from the equity snapshots ---------------------------------- #
    equity = journal.equity_between(start, end)
    session.snapshots = len(equity)
    if equity:
        session.open_equity = float(equity[0]["equity"])
        session.close_equity = float(equity[-1]["equity"])
        last = equity[-1]
        # `day_pl` is the broker's own figure and is what the account shows;
        # the first-to-last snapshot difference is only a fallback, because the
        # first snapshot of the day is rarely taken at the open.
        session.day_pl = (
            float(last["day_pl"])
            if last.get("day_pl") is not None
            else session.close_equity - session.open_equity
        )
        if last.get("day_pl_pct") is not None:
            session.day_pl_pct = float(last["day_pl_pct"])
        elif session.open_equity:
            session.day_pl_pct = session.day_pl / session.open_equity
        if last.get("positions") is not None:
            session.open_positions_at_close = int(last["positions"])

    # ---- decisions: what was sent, and what was declined ------------------ #
    decisions = journal.decisions_between(start, end)
    cycles: Counter[str] = Counter()
    per_strategy: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "ideas": 0, "opened": 0, "closed": 0, "declined": 0,
            "near_misses": 0, "realised_pl": 0.0,
        }
    )

    for row in decisions:
        action = str(row.get("action") or "")
        strategy = str(row.get("strategy") or "unknown")
        cycles[str(row.get("cycle") or "manual")] += 1
        bucket = per_strategy[strategy]
        bucket["ideas"] += 1

        if action == "open":
            session.orders_sent += 1
            bucket["opened"] += 1
        elif action == "close":
            bucket["closed"] += 1
            payload = _payload(row)
            realised = payload.get("realized_pl") or payload.get("realised_pl")
            if realised is not None:
                bucket["realised_pl"] += float(realised)
            session.closes.append(
                {
                    "at": _iso_local(str(row.get("ts")), timezone),
                    "symbol": row.get("symbol"),
                    "strategy": strategy,
                    "realised_pl": realised,
                    "reason": _first_sentence(row.get("reason") or ""),
                }
            )
        elif action == "skip":
            bucket["declined"] += 1
            approved = row.get("approved")
            approved = None if approved is None else bool(approved)
            if approved:
                bucket["near_misses"] = bucket.get("near_misses", 0) + 1
            # An empty `reason` means the risk engine raised no objection - the
            # veto came from somewhere further down, and the critic's rationale
            # is the only record of it.
            reason = row.get("reason") or _payload(row).get("rationale") or ""
            session.potential.append(
                PotentialTrade(
                    ts=_iso_local(str(row.get("ts")), timezone),
                    symbol=str(row.get("symbol") or ""),
                    strategy=strategy,
                    structure=row.get("structure"),
                    quantity=row.get("quantity"),
                    net_price=row.get("net_price"),
                    max_loss=row.get("max_loss"),
                    max_profit=row.get("max_profit"),
                    reason=_first_sentence(reason, 220) or "(no reason recorded)",
                    thesis=_first_sentence(row.get("thesis") or "", 220),
                    risk_approved=approved,
                )
            )
    session.cycles_run = dict(cycles)
    session.by_strategy = {k: dict(v) for k, v in sorted(per_strategy.items())}

    # ---- fills ------------------------------------------------------------ #
    for row in journal.fills_between(start, end):
        session.fills.append(
            FilledOrder(
                ts=_iso_local(str(row.get("ts")), timezone),
                symbol=str(row.get("symbol") or ""),
                status=str(row.get("status") or ""),
                filled_qty=row.get("filled_qty"),
                filled_price=row.get("filled_price"),
                order_id=str(row.get("order_id") or ""),
            )
        )

    # ---- the gate funnel and the day's health ----------------------------- #
    by_gate: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    by_book: Counter[str] = Counter()
    symbols: set[str] = set()

    for event in journal.events_between(start, end):
        kind = event.get("kind")
        if kind == "gate_rejection":
            session.gate_rejections += 1
            by_gate[str(event.get("vetoed_by") or "unknown")] += 1
            by_reason[_first_sentence(event.get("reason") or "unstated")] += 1
            by_book[str(event.get("book") or "unknown")] += 1
            if event.get("symbol"):
                symbols.add(str(event["symbol"]))
            continue
        if kind == "cycle_error":
            session.errors.append(
                f"{event.get('cycle')}: {_first_sentence(event.get('error') or '', 200)}"
            )
            continue
        if kind == "agent_degraded":
            session.reasoning_available = False
            session.errors.append(
                f"reasoning layer degraded: {_first_sentence(event.get('reason') or '', 200)}"
            )
            continue
        if kind in _NOTABLE_EVENTS:
            note = _describe_event(event)
            if note:
                session.notes.append(note)

    session.rejections_by_gate = dict(by_gate.most_common())
    session.rejections_by_reason = dict(by_reason.most_common(12))
    session.rejections_by_book = dict(by_book.most_common())
    session.symbols_examined = sorted(symbols)
    return session


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("payload")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _describe_event(event: dict[str, Any]) -> str | None:
    kind = event.get("kind")
    if kind == "events_arm":
        opened = event.get("opened") or []
        declined = event.get("declined") or {}
        return (
            f"events arm: {len(opened)} opened, {len(declined)} declined "
            f"(abstention {float(event.get('abstention_rate') or 0):.0%})"
        )
    if kind == "events_flatten":
        closed = event.get("closed") or []
        return f"events flatten: {len(closed)} position(s) closed" if closed else None
    if kind == "events_watch":
        noted = event.get("noted") or []
        return f"watch noted {', '.join(noted)}" if noted else None
    if kind == "firewall_cutoff":
        return (
            f"firewall cutoff ({event.get('scope')}): "
            f"{event.get('orders_cancelled')} order(s) cancelled, "
            f"{event.get('positions_before')} -> {event.get('positions_after')} position(s)"
        )
    if kind == "firewall_lock":
        return f"{event.get('book')} book leased ${float(event.get('budget') or 0):,.0f}"
    if kind == "macro_view":
        return f"macro: {event.get('regime')}, vol {event.get('vol_expectation')}"
    if kind == "discovery":
        snapshot = event.get("snapshot") or {}
        errors = snapshot.get("source_errors") or {}
        return f"discovery source failure: {', '.join(errors)}" if errors else None
    if kind == "switchboard":
        return f"switchboard: on={event.get('turned_on')} off={event.get('turned_off')}"
    if kind == "operator_note":
        text = str(event.get("text") or "").strip()
        return f"operator note: {text}" if text else None
    return None


# ====================================================================== #
# THE CRITIC
# ====================================================================== #

_CRITIC_SYSTEM = """You are the post-session reviewer for an options trading \
algorithm that trades a paper account through Alpaca. You are given one \
session's evidence: the ideas it built, the gates that vetoed them, the orders \
it filled, and the day's P&L.

Your job is to name what to CHANGE, not to summarise what happened - the \
reader already has the numbers above your section.

Rules:
- Reply with bullet points only. Every line starts with "- ". No preamble, no \
heading, no closing paragraph.
- Between 4 and 8 bullets.
- Each bullet names one concrete, checkable change: a parameter to move and in \
which direction, a gate whose ordering is wasting work, a book to stand down, \
a piece of evidence the system is not collecting.
- Quote the session's own numbers when you make a claim.
- A session that declined every trade is not automatically a failure. If the \
gates behaved correctly, say which gate earned its keep and move on to the \
next-most-binding constraint.
- Do not invent data. If the evidence cannot support a recommendation, say \
what would need to be measured instead."""

#: How much of the session is shown to the model. A whole day of gate
#: rejections is thousands of tokens of near-duplicates; the aggregate plus a
#: sample of the declined ideas carries the same information.
_CRITIC_MAX_POTENTIAL = 12


def _critic_brief(session: DailySession) -> str:
    brief = {
        "date": session.date,
        "profile": session.profile,
        "pnl": {
            "open_equity": session.open_equity,
            "close_equity": session.close_equity,
            "day_pl": session.day_pl,
            "day_pl_pct": session.day_pl_pct,
            "open_positions_at_close": session.open_positions_at_close,
        },
        "orders_sent": session.orders_sent,
        "fills": [f.as_dict() for f in session.fills],
        "closes": session.closes,
        "gate_funnel": {
            "total_rejections": session.gate_rejections,
            "by_gate": session.rejections_by_gate,
            "by_book": session.rejections_by_book,
            "top_reasons": session.rejections_by_reason,
        },
        "symbols_examined": session.symbols_examined,
        "declined_ideas_sample": [
            p.as_dict() for p in session.potential[:_CRITIC_MAX_POTENTIAL]
        ],
        "declined_ideas_total": len(session.potential),
        "risk_approved_but_unsent": [p.as_dict() for p in session.near_misses],
        "by_strategy": session.by_strategy,
        "cycles_run": session.cycles_run,
        "errors": session.errors,
        "notes": session.notes[:20],
    }
    return json.dumps(brief, indent=1, default=str)


def _parse_bullets(text: str, limit: int = 10) -> list[str]:
    """Pull bullets out of whatever shape the model replied in."""
    bullets: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        for marker in ("- ", "* ", "• "):
            if line.startswith(marker):
                line = line[len(marker):].strip()
                break
        else:
            # "1. foo" / "1) foo" - models drift into numbering under pressure.
            head = line.split(" ", 1)
            if len(head) == 2 and head[0].rstrip(".)").isdigit():
                line = head[1].strip()
            else:
                continue
        line = line.strip("*_ ").strip()
        # A bare marker or a stray character is not a recommendation. The bar is
        # deliberately low - the model's own brevity is not this function's
        # business, only that the line carries something.
        if len(line) > 2:
            bullets.append(line)
    return bullets[:limit]


def _deterministic_bullets(session: DailySession) -> list[str]:
    """The critique when there is no reasoning provider.

    Deliberately arithmetic rather than clever: it restates the binding
    constraint from the funnel. Its purpose is that the report is never blank
    and never silently pretends a model wrote it.
    """
    bullets: list[str] = []
    if session.errors:
        bullets.append(
            f"{len(session.errors)} error(s) were journalled this session - "
            f"first: {session.errors[0]}. Fix before reading anything else here."
        )
    if session.gate_rejections:
        gate, count = next(iter(session.rejections_by_gate.items()))
        share = count / session.gate_rejections
        bullets.append(
            f"`{gate}` vetoed {count} of {session.gate_rejections} candidates "
            f"({share:.0%}) - it is the binding constraint. Either its threshold "
            f"is mis-set for this regime, or every gate after it ran for nothing "
            f"and should be re-ordered behind it."
        )
        if session.rejections_by_reason:
            reason, rcount = next(iter(session.rejections_by_reason.items()))
            bullets.append(
                f'Most repeated single reason ({rcount}x): "{reason}". '
                f"Measure the distribution of that quantity across the universe "
                f"before moving its threshold."
            )
    if session.near_misses:
        names = ", ".join(sorted({p.symbol for p in session.near_misses}))
        bullets.append(
            f"{len(session.near_misses)} idea(s) ({names}) passed the risk engine "
            f"and never reached the broker. Trace where the ticket died - this is "
            f"the highest-value single check on a session that filled nothing."
        )
    if session.potential and not session.fills:
        bullets.append(
            f"{len(session.potential)} idea(s) were fully priced and then "
            f"declined, and nothing filled. Priced-then-rejected is the "
            f"expensive path - the veto that killed them should run before the "
            f"chain is pulled."
        )
    if not session.potential and not session.fills:
        bullets.append(
            "No idea reached pricing today. The constraint is upstream of the "
            "risk gates - check discovery breadth and the entry-window clock."
        )
    if session.fills:
        bullets.append(
            f"{len(session.fills)} fill(s) executed. Compare filled price with "
            f"the idea's `net_price` to measure slippage against the mid the "
            f"gate approved."
        )
    if session.day_pl is not None:
        bullets.append(
            f"Day P&L {session.day_pl:+,.2f}"
            + (f" ({session.day_pl_pct:+.2%})" if session.day_pl_pct is not None else "")
            + f" across {session.snapshots} snapshot(s)."
        )
    if not session.reasoning_available:
        bullets.append(
            "The reasoning layer was unavailable this session - the whole day "
            "ran on deterministic rules. Check `oaa doctor` before the next open."
        )
    return bullets or ["Nothing was recorded for this session."]


def critique(session: DailySession, llm: Any = None) -> tuple[list[str], str]:
    """Bullet-point improvements for the session. Returns (bullets, author).

    `author` is what wrote them - the model id, or `deterministic`. It is
    printed in the report, because a critique whose provenance is unclear is
    worse than no critique.
    """
    if llm is None or getattr(llm, "provider", "null") == "null":
        return _deterministic_bullets(session), "deterministic (no reasoning provider)"

    model = getattr(getattr(llm, "cfg", None), "model", None) or "unknown model"
    author = f"{getattr(llm, 'provider', '?')} / {model}"
    try:
        raw = llm.complete(_CRITIC_SYSTEM, _critic_brief(session))
    except Exception as exc:  # noqa: BLE001 - a dead critic must not cost the report
        log.warning("daily critic failed (%s) - falling back to the arithmetic one", exc)
        return (
            _deterministic_bullets(session),
            f"deterministic (fallback - {author} failed: {exc})",
        )

    bullets = _parse_bullets(raw)
    if not bullets:
        log.warning("daily critic returned no usable bullets - falling back")
        return _deterministic_bullets(session), f"deterministic (fallback - {author} returned prose)"
    return bullets, author


# ====================================================================== #
# THE WRITER
# ====================================================================== #

def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    lines += ["| " + " | ".join(rows_) + " |" for rows_ in (map(str, r) for r in rows)]
    return lines


def render_markdown(session: DailySession, bullets: list[str], author: str) -> str:
    pl = "n/a" if session.day_pl is None else f"{session.day_pl:+,.2f}"
    pct = "" if session.day_pl_pct is None else f" ({session.day_pl_pct:+.2%})"
    out: list[str] = [
        f"# Daily report - {session.date} ({session.profile})",
        "",
        f"*Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC} "
        f"from the {session.profile} journal.*",
        "",
        "## Result",
        "",
        *_table(
            ["Measure", "Value"],
            [
                ["Day P&L", f"**{pl}**{pct}"],
                ["Equity open -> close",
                 f"{_money(session.open_equity)} -> {_money(session.close_equity)}"],
                ["Positions at close",
                 "n/a" if session.open_positions_at_close is None
                 else session.open_positions_at_close],
                ["Orders sent", session.orders_sent],
                ["Orders filled", len(session.fills)],
                ["Ideas priced then declined", len(session.potential)],
                ["...of which risk-approved", len(session.near_misses)],
                ["Gate rejections", session.gate_rejections],
                ["Symbols examined", len(session.symbols_examined)],
                ["Cycles run", sum(session.cycles_run.values())],
                ["Account snapshots", session.snapshots],
            ],
        ),
        "",
    ]

    # -- orders filled ------------------------------------------------------ #
    out += ["## Orders filled", ""]
    if session.fills:
        out += _table(
            ["Time", "Symbol", "Status", "Qty", "Price", "Order id"],
            [[f.ts, f.symbol, f.status,
              "n/a" if f.filled_qty is None else f"{f.filled_qty:g}",
              "n/a" if f.filled_price is None else f"{f.filled_price:,.2f}",
              f.order_id[:12]] for f in session.fills],
        )
    else:
        out.append("No orders filled this session.")
    out.append("")

    if session.closes:
        out += ["## Positions closed", ""]
        out += _table(
            ["Time", "Symbol", "Strategy", "Realised P&L", "Why"],
            [[c["at"], c["symbol"], c["strategy"],
              "n/a" if c.get("realised_pl") is None else f"{float(c['realised_pl']):+,.2f}",
              c["reason"]] for c in session.closes],
        )
        out.append("")

    # -- potential executions ---------------------------------------------- #
    out += [
        "## Potential executions",
        "",
        "Ideas the system built and priced, then declined to send. This is the "
        "set of trades it *could* have made, with the reason each was refused.",
        "",
    ]
    if session.potential:
        out += _table(
            ["Time", "Symbol", "Strategy", "Structure", "Qty", "Net", "Max loss",
             "Risk OK", "Declined because"],
            [[p.ts, p.symbol, p.strategy, p.structure or "-",
              "-" if p.quantity is None else p.quantity,
              "-" if p.net_price is None else f"{p.net_price:,.2f}",
              "-" if p.max_loss is None else f"{p.max_loss:,.0f}",
              "-" if p.risk_approved is None else ("**yes**" if p.risk_approved else "no"),
              p.reason or "-"] for p in session.potential],
        )
        if session.near_misses:
            count = len(session.near_misses)
            out += [
                "",
                f"> **{count} of these {'was' if count == 1 else 'were'} approved by "
                f"the risk engine and still did not reach the broker** "
                f"({', '.join(sorted({p.symbol for p in session.near_misses}))}). "
                f"A signed ticket that never becomes an order is either a "
                f"deliberate downstream veto or a defect - it is the first thing "
                f"to check on a session that filled nothing.",
            ]
    else:
        out.append("No idea reached pricing this session.")
    out.append("")

    # -- the funnel --------------------------------------------------------- #
    out += ["## Gate funnel", ""]
    if session.gate_rejections:
        out += ["### Which gate refused", ""]
        out += _table(
            ["Gate", "Rejections", "Share"],
            [[gate, count, f"{count / session.gate_rejections:.0%}"]
             for gate, count in session.rejections_by_gate.items()],
        )
        out.append("")
        if session.rejections_by_book:
            out += ["### By book", ""]
            out += _table(
                ["Book", "Rejections"],
                [[book, count] for book, count in session.rejections_by_book.items()],
            )
            out.append("")
        out += ["### Most repeated reasons", ""]
        out += [f"- **{count}x** - {reason}" for reason, count in session.rejections_by_reason.items()]
        out.append("")
    else:
        out += ["No gate rejections recorded.", ""]

    # -- attribution -------------------------------------------------------- #
    if session.by_strategy:
        out += ["## By strategy", ""]
        out += _table(
            ["Strategy", "Ideas", "Opened", "Closed", "Declined", "Risk-approved but unsent",
             "Realised P&L"],
            [[name, b["ideas"], b["opened"], b["closed"], b["declined"],
              b.get("near_misses", 0), f"{b['realised_pl']:+,.2f}"]
             for name, b in session.by_strategy.items()],
        )
        out.append("")

    # -- health ------------------------------------------------------------- #
    if session.errors or session.notes:
        out += ["## Session log", ""]
        for err in session.errors:
            out.append(f"- **error** - {err}")
        for note in dict.fromkeys(session.notes):
            out.append(f"- {note}")
        out.append("")

    # -- the critique ------------------------------------------------------- #
    out += [
        "## Where the algorithm can improve",
        "",
        f"*Written by: {author}*",
        "",
    ]
    out += [f"- {b}" for b in bullets]
    out += [
        "",
        "---",
        "",
        f"<sub>{session.headline()} - generated by `oaa daily-report`.</sub>",
        "",
    ]
    return "\n".join(out)


def write_daily_report(
    session: DailySession,
    bullets: list[str],
    author: str,
    out_dir: str | Path,
) -> dict[str, Path]:
    """Write `<out_dir>/<date>.md` and `<date>.json`. Overwrites by design.

    Re-running a day must replace it, not accumulate copies: the report is a
    pure function of the journal, so a second run of the same date is a
    correction, never a new artefact.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    md_path = directory / f"{session.date}.md"
    json_path = directory / f"{session.date}.json"

    md_path.write_text(render_markdown(session, bullets, author), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "session": session.as_dict(),
                "critique": {"author": author, "bullets": bullets},
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    log.info("daily report written: %s", md_path)
    return {"markdown": md_path, "json": json_path}


def generate_daily_report(
    journal: Any,
    day: dt.date,
    out_dir: str | Path,
    profile: str = "default",
    timezone: str = "America/New_York",
    llm: Any = None,
) -> tuple[DailySession, dict[str, Path]]:
    """Collect, critique, write. The one entry point the CLI and the runner share."""
    session = collect_session(journal, day, profile=profile, timezone=timezone)
    bullets, author = critique(session, llm)
    paths = write_daily_report(session, bullets, author, out_dir)
    return session, paths
