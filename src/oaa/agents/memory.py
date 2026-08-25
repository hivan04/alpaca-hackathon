"""Agent memory.

Yesterday's realised outcomes get folded into today's prompts, so the system
is not making the same mistake five days running. Small, deliberately: a
week-long event does not give enough samples for anything cleverer.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from oaa.core.logging import get_logger

log = get_logger("agents.memory")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    symbol     TEXT,
    strategy   TEXT,
    structure  TEXT,
    pnl        REAL,
    pnl_pct    REAL,
    held_days  REAL,
    thesis     TEXT,
    outcome    TEXT,
    notes      TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcomes_ts ON outcomes(ts);
"""


class Memory:
    def __init__(self, path: str | Path, lookback_days: int = 7) -> None:
        self.path = Path(path)
        self.lookback_days = lookback_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(_SCHEMA)

    def record(
        self,
        symbol: str,
        strategy: str,
        structure: str,
        pnl: float,
        pnl_pct: float,
        held_days: float,
        thesis: str = "",
        notes: dict[str, Any] | None = None,
    ) -> None:
        outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "flat")
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO outcomes "
                "(ts,symbol,strategy,structure,pnl,pnl_pct,held_days,thesis,outcome,notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    symbol, strategy, structure, round(pnl, 2), round(pnl_pct, 5),
                    round(held_days, 2), thesis, outcome,
                    json.dumps(notes or {}, default=str),
                ),
            )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.lookback_days)
        ).isoformat()
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM outcomes WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats_by_strategy(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in self.recent(limit=500):
            bucket = out.setdefault(
                row["strategy"], {"n": 0, "wins": 0, "pnl": 0.0}
            )
            bucket["n"] += 1
            bucket["wins"] += 1 if row["outcome"] == "win" else 0
            bucket["pnl"] += row["pnl"] or 0.0
        for bucket in out.values():
            bucket["win_rate"] = round(bucket["wins"] / bucket["n"], 3) if bucket["n"] else 0.0
            bucket["pnl"] = round(bucket["pnl"], 2)
        return out

    def as_prompt(self, limit: int = 8) -> str:
        """Compact summary for injection into a critic prompt."""
        rows = self.recent(limit)
        if not rows:
            return ""
        lines = [
            f"  {r['symbol']:<6} {r['strategy']:<24} {r['outcome']:<5} "
            f"{r['pnl']:+8.2f} ({r['pnl_pct']:+.1%}) after {r['held_days']:.1f}d"
            for r in rows
        ]
        stats = self.stats_by_strategy()
        if stats:
            lines.append("  --")
            lines += [
                f"  {name}: {b['wins']}/{b['n']} wins, net {b['pnl']:+.2f}"
                for name, b in sorted(stats.items())
            ]
        return "\n".join(lines)
