"""Which book owns which position.

Once the carry book became resident, "flat" stopped being a property of the
account and became a property of a *book*. The 15:15 cutoff has to liquidate
the transient books while leaving a multi-session iron condor untouched, so
something has to remember which symbol belongs to whom.

Two design decisions worth stating, because both are safety properties:

  * **The ledger is persisted.** A restart at 15:10 must not lose the fact that
    four option legs are carry positions, or the cutoff would liquidate the
    resident book.
  * **Unattributed positions are treated as transient.** A leg the ledger has
    never seen is, by definition, something the system did not deliberately
    decide to hold overnight. Closing it at 15:15 is the conservative error;
    carrying it into the close is the unrecoverable one.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaa.core.logging import get_logger

log = get_logger("firewall.ledger")


@dataclass
class LedgerEntry:
    symbol: str
    book: str
    strategy: str = ""
    idea_id: str = ""
    opened_on: str = ""
    expiry: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionLedger:
    """symbol -> owning book, persisted as JSON beside the journal."""

    path: Path | None = None
    entries: dict[str, LedgerEntry] = field(default_factory=dict)

    # -- persistence ----------------------------------------------------- #
    @classmethod
    def load(cls, path: str | Path | None) -> PositionLedger:
        target = Path(path) if path else None
        ledger = cls(path=target)
        if target is None or not target.exists():
            return ledger
        try:
            raw = json.loads(target.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read the position ledger (%s) - starting empty", exc)
            return ledger
        for symbol, row in (raw.get("entries") or {}).items():
            ledger.entries[symbol.upper()] = LedgerEntry(
                symbol=symbol.upper(),
                book=str(row.get("book", "intraday")),
                strategy=str(row.get("strategy", "")),
                idea_id=str(row.get("idea_id", "")),
                opened_on=str(row.get("opened_on", "")),
                expiry=row.get("expiry"),
                meta=row.get("meta") or {},
            )
        return ledger

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"entries": {s: vars(e) for s, e in self.entries.items()}},
                indent=2, default=str,
            ))
        except OSError as exc:  # noqa: BLE001
            log.warning("could not persist the position ledger: %s", exc)

    # -- writes ----------------------------------------------------------- #
    def register(self, idea: Any, book: str | None = None) -> None:
        """Record every leg of an opened structure against its book."""
        owner = book or getattr(idea, "book", "intraday")
        today = dt.date.today().isoformat()
        for leg in getattr(idea, "legs", []):
            symbol = str(leg.symbol).upper()
            self.entries[symbol] = LedgerEntry(
                symbol=symbol,
                book=owner,
                strategy=getattr(idea, "strategy", ""),
                idea_id=getattr(idea, "id", ""),
                opened_on=today,
                expiry=(leg.quote.expiry.isoformat() if getattr(leg, "quote", None) else None),
                meta={"structure": getattr(getattr(idea, "structure", None), "value", "")},
            )
        self.save()

    def forget(self, symbols: list[str] | str) -> None:
        names = [symbols] if isinstance(symbols, str) else symbols
        for symbol in names:
            self.entries.pop(str(symbol).upper(), None)
        self.save()

    def reconcile(self, live_symbols: list[str]) -> list[str]:
        """Drop entries for positions that no longer exist. Returns what went."""
        live = {s.upper() for s in live_symbols}
        gone = [s for s in self.entries if s not in live]
        for symbol in gone:
            self.entries.pop(symbol, None)
        if gone:
            self.save()
        return gone

    # -- reads ------------------------------------------------------------ #
    def book_of(self, symbol: str) -> str:
        entry = self.entries.get(str(symbol).upper())
        # Unattributed => transient. See the module docstring.
        return entry.book if entry else "intraday"

    def is_resident(self, symbol: str) -> bool:
        return self.book_of(symbol) == "carry"

    def symbols_for(self, book: str) -> list[str]:
        return sorted(s for s, e in self.entries.items() if e.book == book)

    def split(self, positions: list[Any]) -> tuple[list[Any], list[Any]]:
        """(resident, transient) split of a live position list."""
        resident, transient = [], []
        for position in positions:
            (resident if self.is_resident(position.symbol) else transient).append(position)
        return resident, transient

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.entries.values():
            counts[entry.book] = counts.get(entry.book, 0) + 1
        return {"tracked_legs": len(self.entries), "by_book": counts}
