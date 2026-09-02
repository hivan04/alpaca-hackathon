"""The evidence trail.

Three sinks, all append-only:
  * journal.jsonl - every decision, including the ones that declined to trade
  * equity.csv    - a dense equity curve for the deck and the dashboard
  * oaa.sqlite    - queryable positions/orders for the report command

Instrumentation is written from the first commit, not bolted on at the end.
It is simultaneously the debugging tool, the demo footage and the thing the
judges read to understand why the agent traded the way it did.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot, Decision

log = get_logger("telemetry")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    cycle       TEXT,
    action      TEXT,
    symbol      TEXT,
    strategy    TEXT,
    structure   TEXT,
    quantity    INTEGER,
    net_price   REAL,
    max_loss    REAL,
    max_profit  REAL,
    approved    INTEGER,
    reason      TEXT,
    thesis      TEXT,
    order_id    TEXT,
    status      TEXT,
    payload     TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts          TEXT PRIMARY KEY,
    equity      REAL,
    cash        REAL,
    positions   INTEGER,
    day_pl      REAL,
    day_pl_pct  REAL
);
CREATE TABLE IF NOT EXISTS fills (
    order_id        TEXT PRIMARY KEY,
    client_order_id TEXT,
    ts              TEXT,
    symbol          TEXT,
    status          TEXT,
    filled_qty      REAL,
    filled_price    REAL,
    idea_id         TEXT,
    payload         TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
"""


class Journal:
    def __init__(
        self,
        journal_path: str | Path,
        db_path: str | Path,
        equity_path: str | Path,
    ) -> None:
        self.journal_path = Path(journal_path)
        self.db_path = Path(db_path)
        self.equity_path = Path(equity_path)
        self._lock = threading.Lock()
        for path in (self.journal_path, self.db_path, self.equity_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- writes -------------------------------------------------------------- #
    def record(self, decision: Decision) -> None:
        payload = decision.model_dump(mode="json")
        with self._lock:
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")

            idea = decision.idea
            verdict = decision.verdict
            fill = decision.fill
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO decisions VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision.id,
                        decision.ts.isoformat(),
                        decision.cycle,
                        decision.action.value,
                        decision.symbol,
                        decision.strategy,
                        idea.structure.value if idea else None,
                        idea.quantity if idea else None,
                        idea.net_price if idea else None,
                        idea.max_loss if idea else None,
                        idea.max_profit if idea else None,
                        int(verdict.approved) if verdict else None,
                        "; ".join(verdict.reasons) if verdict else decision.rationale,
                        idea.thesis if idea else decision.rationale,
                        fill.order_id if fill else None,
                        fill.status if fill else None,
                        json.dumps(payload, default=str),
                    ),
                )
                if fill:
                    conn.execute(
                        "INSERT OR REPLACE INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            fill.order_id,
                            fill.client_order_id,
                            decision.ts.isoformat(),
                            fill.symbol,
                            fill.status,
                            fill.filled_qty,
                            fill.filled_avg_price,
                            idea.id if idea else None,
                            json.dumps(fill.model_dump(mode="json"), default=str),
                        ),
                    )

    def snapshot(self, account: AccountSnapshot) -> None:
        ts = account.asof.isoformat()
        row = [
            ts,
            round(account.equity, 2),
            round(account.cash, 2),
            len(account.positions),
            account.day_pl,
            account.day_pl_pct,
        ]
        with self._lock:
            fresh = not self.equity_path.exists() or self.equity_path.stat().st_size == 0
            with self.equity_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if fresh:
                    writer.writerow(
                        ["ts", "equity", "cash", "positions", "day_pl", "day_pl_pct"]
                    )
                writer.writerow(row)
            with self._connect() as conn:
                conn.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?,?)", row)

    def event(self, kind: str, **fields: Any) -> None:
        """Free-form structured event - startup, halts, partner calls, errors."""
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "kind": kind,
            **fields,
        }
        with self._lock, self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    # -- reads --------------------------------------------------------------- #
    def equity_series(self, limit: int = 5000) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM equity ORDER BY ts ASC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_snapshot(self) -> dict[str, Any]:
        """The most recent account snapshot, or {} before the first one.

        `equity_series` is ordered ASC for the curve, so `equity_series(1)`
        returns the OLDEST row - the first $100,000 of the week, forever. Any
        caller that wants "how much money is there right now" wants this.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM equity ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else {}

    def decisions(self, limit: int = 200, action: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM decisions"
        params: list[Any] = []
        if action:
            query += " WHERE action = ?"
            params.append(action)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def events(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Replay structured events from the append-only JSONL journal.

        The gate rejection log lives here rather than in SQLite: it is the
        highest-value artefact for judging (it shows the agent DECLINING trades
        and why), and an append-only file is the least corruptible place for it.
        """
        if not self.journal_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.journal_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind is None or record.get("kind") == kind:
                    rows.append(record)
        return list(reversed(rows))[:limit]

    def fills(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            return {
                "decisions": conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
                "fills": conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
                "snapshots": conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0],
                "approved": conn.execute(
                    "SELECT COUNT(*) FROM decisions WHERE approved = 1"
                ).fetchone()[0],
            }

    # -- session-scoped reads -------------------------------------------- #
    def decisions_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Every decision with `start <= ts < end`, oldest first.

        The daily report needs a whole session and nothing else. `decisions()`
        takes a row limit, which on a busy day silently truncates the tail of
        the session - the opposite of what a report of the session should do.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def fills_between(self, start: str, end: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fills WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def equity_between(self, start: str, end: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM equity WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def events_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Structured events in a UTC window, oldest first, no row cap.

        Streams the JSONL rather than reading it into memory twice the way
        `events()` does: a week of hourly watch cycles is a large file and the
        report reads it once a day.
        """
        if not self.journal_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.journal_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("kind") is None:
                    continue
                ts = str(record.get("ts") or "")
                if start <= ts < end:
                    rows.append(record)
        return rows
