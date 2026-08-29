"""Durable, once-a-day ATM implied-vol history for the LIVE providers.

Why this exists
---------------
`premium_gate.iv_rank_min` decides whether the carry book trades at all, and
IV rank is a percentile of today's implied vol within its own trailing
history. In replay that history is one observation per SESSION over a trailing
year. Live it used to be a plain list on the provider object, appended once per
`context()` call - a dozen times a session - and thrown away on restart. So:

  * the first cycles after every start had too little history to rank at all;
  * once populated, the "history" was a few hours of the same morning's polls,
    which is not the quantity the gate was tuned against;
  * a redeploy silently re-randomised the gate.

This store fixes the shape of the series, not the strategy: ONE observation per
symbol per calendar day, capped at a trailing year, persisted to disk so a
restart resumes rather than resets, and seeded from the same `IVModel` the
replay uses so the very first live session ranks against a real distribution
instead of standing the book down for a month while history accumulates.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaa.data.indicators import IV_RANK_MIN_OBSERVATIONS

log = logging.getLogger(__name__)

#: A trading year. Matches `IVModel.rank_lookback`.
DEFAULT_MAX_DAYS = 252


@dataclass
class IVHistoryStore:
    """symbol -> {ISO date: ATM IV}, one entry per day, persisted."""

    path: Path | None = None
    max_days: int = DEFAULT_MAX_DAYS
    _series: dict[str, dict[str, float]] = field(default_factory=dict)
    _dirty: bool = False

    # -- construction ------------------------------------------------------ #
    @classmethod
    def open(cls, run_dir: str | Path | None, max_days: int = DEFAULT_MAX_DAYS) -> IVHistoryStore:
        path = Path(run_dir) / "iv_history.json" if run_dir else None
        store = cls(path=path, max_days=max_days)
        store.load()
        return store

    # -- reading ----------------------------------------------------------- #
    def series(self, symbol: str) -> list[float]:
        """Chronological IV observations, oldest first."""
        days = self._series.get(symbol) or {}
        return [days[k] for k in sorted(days)]

    def observations(self, symbol: str) -> int:
        return len(self._series.get(symbol) or {})

    def needs_seed(self, symbol: str, minimum: int = IV_RANK_MIN_OBSERVATIONS) -> bool:
        return self.observations(symbol) < minimum

    # -- writing ----------------------------------------------------------- #
    def observe(self, symbol: str, value: float | None, day: dt.date | None = None) -> None:
        """Record today's ATM IV. Last write for a given day wins.

        Twelve scans a session must not become twelve observations: the series
        the rank is computed against is a daily one.
        """
        if value is None:
            return
        day = day or dt.datetime.now(dt.timezone.utc).date()
        days = self._series.setdefault(symbol, {})
        key = day.isoformat()
        if days.get(key) == float(value):
            return
        days[key] = float(value)
        for stale in sorted(days)[: max(0, len(days) - self.max_days)]:
            del days[stale]
        self._dirty = True

    def seed(self, symbol: str, dated: list[tuple[dt.date, float]]) -> int:
        """Backfill days that are not already recorded. Never overwrites a
        real observation with a modelled one."""
        days = self._series.setdefault(symbol, {})
        added = 0
        for day, value in dated:
            key = day.isoformat()
            if key in days or value is None:
                continue
            days[key] = float(value)
            added += 1
        if added:
            for stale in sorted(days)[: max(0, len(days) - self.max_days)]:
                del days[stale]
            self._dirty = True
        return added

    def seed_from_bars(self, symbol: str, bars: list[dict[str, Any]]) -> int:
        """Seed from the replay's own IV model over this symbol's daily bars.

        Imported inside the function: `oaa.backtest.ivmodel` imports
        `oaa.data.indicators`, so a module-level import here would close a
        cycle. The model is pure computation - no I/O, no chain access.
        """
        if not bars:
            return 0
        try:
            from oaa.backtest.ivmodel import IVModel
        except ImportError:  # pragma: no cover - backtest extras absent
            return 0
        try:
            series = IVModel().build(bars)["iv"]
        except Exception as exc:  # noqa: BLE001 - seeding must never break a cycle
            log.debug("%s: could not seed IV history (%s)", symbol, exc)
            return 0
        dated: list[tuple[dt.date, float]] = []
        for bar, value in zip(bars, series, strict=False):
            if value is None:
                continue
            stamp = bar.get("timestamp")
            day = getattr(stamp, "date", lambda: None)()
            if day is None:
                continue
            dated.append((day, float(value)))
        # Drop the final bar: today's real print is the observation for today.
        added = self.seed(symbol, dated[:-1])
        if added:
            log.info("%s: seeded %d modelled IV observations for the rank", symbol, added)
        return added

    # -- persistence ------------------------------------------------------- #
    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("IV history at %s is unreadable (%s) - starting empty", self.path, exc)
            return
        if isinstance(raw, dict):
            self._series = {
                str(sym): {str(d): float(v) for d, v in (days or {}).items()}
                for sym, days in raw.items()
                if isinstance(days, dict)
            }

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._series, indent=1, sort_keys=True))
            tmp.replace(self.path)
            self._dirty = False
        except OSError as exc:  # pragma: no cover
            log.warning("could not persist IV history to %s (%s)", self.path, exc)
