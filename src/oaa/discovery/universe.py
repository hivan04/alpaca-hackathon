"""The candidate pool.

Discovery runs daily. The tradable universe does not change daily — that
distinction is the whole discipline here.

**The pool accumulates.** A name that was hot on Tuesday and quiet on Wednesday
is still a valid candidate; taking only today's top twenty would throw away
everything learned yesterday.

**Approval is additive-only.** Pairs enter the live universe when they pass
cointegration and are never removed just because they fell off today's buzz
list. Churning the tradable set is chasing, and chasing is precisely what
destroys a mean-reversion strategy.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from oaa.core.logging import get_logger

log = get_logger("discovery.universe")


@dataclass
class PoolEntry:
    symbol: str
    first_seen: str
    last_seen: str
    appearances: int = 1
    best_score: float = 0.0
    last_score: float = 0.0
    screened: bool = False
    approved: bool = False
    notes: str = ""

    @property
    def persistence(self) -> int:
        """How many separate days this name has shown up.

        A symbol seen on four of the last five days is a durable theme. One seen
        once is a headline. The screen should prefer the former.
        """
        return self.appearances


@dataclass
class CandidatePool:
    path: Path
    accumulate_days: int = 10
    max_symbols: int = 40
    entries: dict[str, PoolEntry] = field(default_factory=dict)
    seeds: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        path: str | Path,
        accumulate_days: int = 10,
        max_symbols: int = 40,
        seeds: list[str] | None = None,
    ) -> CandidatePool:
        pool = cls(
            path=Path(path),
            accumulate_days=accumulate_days,
            max_symbols=max_symbols,
            seeds=[s.upper() for s in (seeds or [])],
        )
        if pool.path.exists():
            try:
                raw = json.loads(pool.path.read_text(encoding="utf-8"))
                pool.entries = {
                    key: PoolEntry(**value) for key, value in (raw.get("entries") or {}).items()
                }
                log.debug("loaded %d pool entries from %s", len(pool.entries), pool.path)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                log.warning("could not read the candidate pool (%s) - starting fresh", exc)
        return pool

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "accumulate_days": self.accumulate_days,
            "entries": {k: asdict(v) for k, v in self.entries.items()},
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("candidate pool saved: %d symbols -> %s", len(self.entries), self.path)

    # ------------------------------------------------------------------ #
    def observe(self, symbol_scores: dict[str, float], asof: dt.date | None = None) -> list[str]:
        """Fold today's ranking in. Returns the symbols that are new."""
        today = (asof or dt.date.today()).isoformat()
        added: list[str] = []

        for symbol, score in symbol_scores.items():
            symbol = symbol.upper()
            entry = self.entries.get(symbol)
            if entry is None:
                self.entries[symbol] = PoolEntry(
                    symbol=symbol, first_seen=today, last_seen=today,
                    best_score=round(score, 4), last_score=round(score, 4),
                )
                added.append(symbol)
                continue
            if entry.last_seen != today:
                entry.appearances += 1
            entry.last_seen = today
            entry.last_score = round(score, 4)
            entry.best_score = max(entry.best_score, round(score, 4))

        self._evict(asof or dt.date.today())
        if added:
            log.info("candidate pool: %d new symbol(s) - %s", len(added), ", ".join(added[:10]))
        return added

    def _evict(self, today: dt.date) -> None:
        """Drop stale, never-approved names. Approved ones are kept for good."""
        cutoff = today - dt.timedelta(days=self.accumulate_days)
        for symbol, entry in list(self.entries.items()):
            if entry.approved or symbol in self.seeds:
                continue
            try:
                last = dt.date.fromisoformat(entry.last_seen)
            except ValueError:
                continue
            if last < cutoff:
                del self.entries[symbol]

    # ------------------------------------------------------------------ #
    def candidates(self, limit: int | None = None) -> list[str]:
        """Symbols to hand the cointegration screen.

        Seeds first (the hand-picked economically-linked names), then discovered
        symbols ranked by persistence before score — a name seen on four days
        beats one that spiked once, however loudly it spiked.
        """
        discovered = sorted(
            (e for e in self.entries.values() if e.symbol not in self.seeds),
            key=lambda e: (-e.appearances, -e.best_score),
        )
        ordered = list(self.seeds) + [e.symbol for e in discovered]
        # Preserve order while de-duplicating.
        seen: set[str] = set()
        unique = [s for s in ordered if not (s in seen or seen.add(s))]
        return unique[: (limit or self.max_symbols)]

    def mark_screened(self, symbols: list[str], approved: list[str]) -> None:
        approved_set = {s.upper() for s in approved}
        for symbol in symbols:
            entry = self.entries.get(symbol.upper())
            if entry is None:
                continue
            entry.screened = True
            if entry.symbol in approved_set:
                entry.approved = True

    def stats(self) -> dict[str, Any]:
        return {
            "symbols": len(self.entries),
            "screened": sum(1 for e in self.entries.values() if e.screened),
            "approved": sum(1 for e in self.entries.values() if e.approved),
            "seeds": len(self.seeds),
            "path": str(self.path),
        }

    def table(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = sorted(
            self.entries.values(), key=lambda e: (-e.appearances, -e.best_score)
        )[:limit]
        return [
            {
                "symbol": e.symbol,
                "days_seen": e.appearances,
                "best_score": e.best_score,
                "last_score": e.last_score,
                "first_seen": e.first_seen,
                "last_seen": e.last_seen,
                "screened": e.screened,
                "approved": e.approved,
            }
            for e in rows
        ]
