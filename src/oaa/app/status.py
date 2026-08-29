"""One question, answered in one command: is the agent live, and what has it done?

`oaa status` exists because the alternative is three terminals and a guess.
On the night of 28 Aug the judged process was up, had run every cycle, and had
still done nothing tradeable - and finding that out took reading pm2 logs, a
JSONL journal and a config file. Every fact needed to reach that conclusion is
gathered here instead:

  * whether a process is actually running, and since when
  * how stale the journal is, which catches a process that is up but wedged
  * the last discovery: what was scanned, what survived, and WHY the rest did not
  * the regime read and which books it stood down
  * whether the reasoning layer ran or silently degraded to rules
  * the last equity mark and open positions

Read-only by construction: it opens files and asks the process table. It cannot
place, size or cancel anything.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import re
import shutil
import subprocess
from typing import Any

UTC = dt.timezone.utc

#: A journal quieter than this DURING A SESSION means wedged, not idle. Outside
#: one it means nothing at all: the runner is schedule-driven, so between the
#: 16:10 report and the next morning's discover it writes nothing by design -
#: and a whole weekend of silence is correct behaviour, not a fault.
STALE_AFTER = dt.timedelta(minutes=45)


def _age(ts: Any) -> dt.timedelta | None:
    """Age of a timestamp, in either spelling the journal writes.

    `Journal.event` writes `+00:00`; `Journal.record` writes a trailing `Z`,
    which `fromisoformat` cannot parse before Python 3.11. Reading only the
    first spelling made this command report "last journal entry never" while
    the loop was writing a decision every 60 seconds - the exact false negative
    it exists to prevent.
    """
    if not ts:
        return None
    text = str(ts)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return dt.datetime.now(UTC) - when


def human_age(delta: dt.timedelta | None) -> str:
    if delta is None:
        return "never"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d ago"


# --------------------------------------------------------------------------- #
def processes(profile: str) -> list[dict[str, Any]]:
    """Running agent processes, from pm2 when it is managing them, else `ps`.

    pm2 is asked first because that is how the runbook starts the judged loop
    and it knows uptime and restart count - a process that has restarted eleven
    times is not 'up' in any useful sense. The `ps` fallback keeps the command
    honest on a host where the loop was started by hand.
    """
    found: list[dict[str, Any]] = []
    if shutil.which("pm2"):
        try:
            raw = subprocess.run(
                ["pm2", "jlist"], capture_output=True, text=True, timeout=10, check=False
            ).stdout
            for entry in json.loads(raw or "[]"):
                name = str(entry.get("name") or "")
                if not name.startswith("oaa"):
                    continue
                env = entry.get("pm2_env") or {}
                started = env.get("pm_uptime")
                found.append({
                    "source": "pm2",
                    "name": name,
                    "pid": entry.get("pid"),
                    "status": env.get("status"),
                    "restarts": env.get("restart_time"),
                    "uptime": human_age(
                        dt.timedelta(milliseconds=max(0, _now_ms() - int(started)))
                        if started else None
                    ),
                })
        except Exception:  # noqa: BLE001 - a status command never raises
            pass

    if not found:
        try:
            out = subprocess.run(
                ["ps", "-eo", "pid,etime,args"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout
        except Exception:  # noqa: BLE001
            out = ""
        for line in out.splitlines()[1:]:
            parts = line.split(None, 2)
            if len(parts) < 3 or not _looks_like_agent(parts[2]):
                continue
            pid, elapsed, args = parts
            found.append({
                "source": "ps", "name": args[:60], "pid": pid,
                "status": "online", "restarts": None, "uptime": elapsed,
            })
    # Both books share a process table; the profile is shown per row rather
    # than filtered on, because "the judged loop is down and the dev one is up"
    # is exactly the state this command exists to make obvious.
    for row in found:
        row["profile"] = _profile_of(str(row["name"]))
    return found


def _profile_of(name: str) -> str:
    """Which account a process is trading, from its pm2 name or its argv."""
    if "judged" in name:
        return "judged"
    if "dashboard" in name:
        return ""
    if "dev" in name:
        return "dev"
    return ""


#: `oaa run` (any spelling) or a pm2-named process - and never this command,
#: a dashboard, or the grep looking for them.
_AGENT_PROCESS = re.compile(r"(oaa(?:\.cli)?\s+run\b|oaa-(?:judged|dev)\b)")


def _looks_like_agent(args: str) -> bool:
    if "streamlit" in args or "grep" in args:
        return False
    return bool(_AGENT_PROCESS.search(args))


def _now_ms() -> int:
    return int(dt.datetime.now(UTC).timestamp() * 1000)


# --------------------------------------------------------------------------- #
def session(settings: Any) -> dict[str, Any]:
    """Where the trading day is: the phase, and when the next one opens.

    Liveness has to be read against this. A process that has written nothing
    for twelve hours is wedged at 11am on a Tuesday and perfectly healthy at
    2am on a Saturday, and a status command that cannot tell those apart cries
    wolf every weekend until nobody reads it.
    """
    try:
        from oaa.firewall.clock import Phase, SessionClock, SessionTimes

        fw = getattr(getattr(settings, "config", None), "firewall", None)
        times = SessionTimes.from_config(fw.times) if fw is not None else SessionTimes()
        clock = SessionClock(times=times)
        now = clock.now()
        phase = clock.phase()
        return {
            "phase": phase.value,
            "open": phase is not Phase.CLOSED,
            "now_et": now.strftime("%a %d %b %H:%M %Z"),
            "next_open": _next_open(now, times.market_open),
        }
    except Exception:  # noqa: BLE001 - a status command never raises
        return {}


def _next_open(now: dt.datetime, market_open: dt.time) -> str:
    """The next weekday open. No holiday calendar - say so rather than lie."""
    candidate = now.replace(
        hour=market_open.hour, minute=market_open.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    return candidate.strftime("%a %d %b %H:%M %Z")


def collect(settings: Any, journal: Any, profile: str) -> dict[str, Any]:
    """Everything `oaa status` shows, as plain data. Never raises."""
    events = journal.events(limit=400)
    # A journal line is either a structured event (has `kind`) or a recorded
    # decision (has `action`). Both are activity; only the first has a kind.
    for record in events:
        record.setdefault("kind", "decision" if record.get("action") else "unknown")
    latest = events[0] if events else {}
    by_kind: dict[str, dict[str, Any]] = {}
    for record in events:                       # events are newest-first
        by_kind.setdefault(str(record.get("kind")), record)

    procs = processes(profile)
    online = [p for p in procs if str(p.get("status", "")).lower() in ("online", "")]
    journal_age = _age(latest.get("ts"))
    market = session(settings)

    state = "offline"
    if online:
        if not market.get("open", True):
            # Market shut: uptime is the whole story. Silence is the schedule
            # working, so it is never held against the process here.
            state = "idle"
        elif journal_age is None or journal_age < STALE_AFTER:
            state = "live"
        else:
            state = "stale"
    elif journal_age is not None and journal_age < STALE_AFTER:
        # Something wrote recently but no process is visible - a loop started
        # outside this host's process table, or one that just exited.
        state = "unknown"

    for row in procs:                       # a stubbed/older row may lack it
        row.setdefault("profile", _profile_of(str(row.get("name", ""))))
    running_profiles = sorted({p["profile"] for p in online if p.get("profile")})

    return {
        "profile": profile,
        "identity": _identity(settings),
        "state": state,
        "session": market,
        # A process trading an account you are NOT looking at is the single
        # most expensive confusion available this week - see identity.py.
        "other_profiles": [p for p in running_profiles if p != profile],
        "processes": procs,
        "journal_age": journal_age,
        "journal_last_ts": latest.get("ts"),
        "journal_path": str(getattr(journal, "journal_path", "")),
        "counts": _counts(journal),
        "events_today": _events_today(events),
        "discovery": _discovery(by_kind.get("discovery")),
        "macro": _macro(by_kind.get("macro_view")),
        "agent": _agent(by_kind.get("agent_run")),
        "report": by_kind.get("report") or {},
        "startup": by_kind.get("startup") or {},
        "decisions": _decisions(journal),
        "recent": [
            {"ts": r.get("ts"), "kind": r.get("kind")} for r in events[:12]
        ],
    }


def _identity(settings: Any) -> dict[str, Any]:
    """Which account these numbers belong to, and which env var chose it.

    The profile is resolved from `--profile`, then `OAA_PROFILE`, then the
    config default - so a bare `oaa status` can perfectly reasonably show the
    dev account while a judged process runs beside it. Printing the resolved
    key and its source is how that stops being a surprise.
    """
    if settings is None:
        return {}
    try:
        from oaa.app.identity import resolve

        ident = resolve(settings, "status")
        return {
            "profile": ident.profile,
            "key": ident.key_masked,
            "key_source": ident.key_source,
            "account_id": ident.expected_account_id,
            "paper": ident.paper,
            "configured": ident.configured,
        }
    except Exception:  # noqa: BLE001
        return {}


def _counts(journal: Any) -> dict[str, int]:
    try:
        return journal.counts()
    except Exception:  # noqa: BLE001
        return {}


def _decisions(journal: Any, limit: int = 8) -> list[dict[str, Any]]:
    try:
        return journal.decisions(limit)
    except Exception:  # noqa: BLE001
        return []


def _events_today(events: list[dict[str, Any]]) -> dict[str, int]:
    today = dt.datetime.now(UTC).date().isoformat()
    return dict(collections.Counter(
        str(r.get("kind")) for r in events if str(r.get("ts", "")).startswith(today)
    ))


def _discovery(record: dict[str, Any] | None) -> dict[str, Any]:
    """The screening story: scanned, survived, and the reasons for the rest."""
    if not record:
        return {}
    snapshot = record.get("snapshot") or {}
    rejected = record.get("rejected") or []
    reasons = collections.Counter(
        _normalise_reason((r.get("reasons") or ["unstated"])[0]) for r in rejected
    )
    top = sorted(
        (snapshot.get("symbols") or []),
        key=lambda s: -(s.get("score") or 0),
    )[:6]
    return {
        "ts": record.get("ts"),
        "scanned": len(snapshot.get("symbols") or []),
        "tradable": record.get("tradable") or [],
        "new_symbols": record.get("new_symbols") or [],
        "pool": record.get("pool") or {},
        "rejected": len(rejected),
        "reasons": reasons.most_common(6),
        "top": [(s.get("symbol"), s.get("score")) for s in top],
        "source_errors": snapshot.get("source_errors") or {},
    }


def _normalise_reason(reason: str) -> str:
    """Group rejections by cause, not by value.

    "price 0.40 below 10.00" and "price 0.01 below 10.00" are one finding -
    the filter's price floor - and counting them separately buries it.
    """
    priced = re.match(r"price [\d.]+ (below|above) ([\d.]+)", str(reason))
    if priced:
        return f"price {priced.group(1)} ${priced.group(2)}"
    return re.sub(r"\b\d+(?:\.\d+)?\b", "", str(reason)).replace("  ", " ").strip()


def _macro(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    guidance = record.get("guidance") or {}
    return {
        "ts": record.get("ts"),
        "regime": record.get("regime"),
        "vol_expectation": record.get("vol_expectation"),
        "overnight_risk": record.get("overnight_risk"),
        "guidance": guidance,
        "stood_down": [k for k, v in guidance.items() if v == "stand_down"],
    }


def _agent(record: dict[str, Any] | None) -> dict[str, Any]:
    """Did the reasoning layer actually run, or did it quietly fall back?"""
    if not record:
        return {}
    calls = record.get("tool_calls") or []
    return {
        "ts": record.get("ts"),
        "cycle": record.get("cycle"),
        "turns": record.get("turns"),
        "tool_calls": len(calls),
        "mutating": sum(1 for c in calls if c.get("mutating")),
        "error": record.get("error"),
        "degraded": bool(record.get("error")),
        "narrative": (record.get("narrative") or "")[:400],
    }
