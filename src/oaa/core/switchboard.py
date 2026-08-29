"""Per-account strategy switches, readable and writable while the agent runs.

`config/*.yaml` says which books an account is CONFIGURED to trade. This says
which of them are switched ON right now, per profile, and it can be changed
from the dashboard without a restart and without editing a config file that is
also the run's provenance.

Two properties matter and both come from the file layout:

  per account   the file lives under the profile's own `telemetry.run_dir`, so
                `runs/dev/switchboard.json` and `runs/judged/switchboard.json`
                are different files. Nothing the operator does to the
                backtesting account can reach the judged one, which is the same
                separation the credentials and journals already have.

  no restart    the live loop re-reads the file at the top of every cycle if it
                has changed on disk. A book switched off mid-session stops
                opening at the next scan; positions it already has are still
                managed and closed, because abandoning open risk is never what
                an off switch should mean.

An entry that is absent falls back to the config's own `enabled` flag, so a
missing or deleted file leaves the agent behaving exactly as configured.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILENAME = "switchboard.json"


@dataclass
class Switchboard:
    path: Path | None = None
    _state: dict[str, bool] = field(default_factory=dict)
    _mtime: float | None = None
    _updated: dict[str, Any] = field(default_factory=dict)

    # -- construction ------------------------------------------------------ #
    @classmethod
    def open(cls, run_dir: str | Path | None) -> Switchboard:
        board = cls(path=Path(run_dir) / FILENAME if run_dir else None)
        board.reload_if_changed()
        return board

    # -- reading ------------------------------------------------------------ #
    def enabled(self, name: str, default: bool = True) -> bool:
        """Is this strategy switched on? Absent means 'as configured'."""
        value = self._state.get(name)
        return default if value is None else bool(value)

    def state(self) -> dict[str, bool]:
        return dict(self._state)

    @property
    def updated(self) -> dict[str, Any]:
        """Who last changed it and when - shown next to the toggles."""
        return dict(self._updated)

    def reload_if_changed(self) -> bool:
        """Re-read the file if it has changed. Returns True if it did."""
        if not self.path or not self.path.exists():
            if self._mtime is not None:      # the file was deleted: fall back
                self._state, self._mtime, self._updated = {}, None, {}
                return True
            return False
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            # A corrupt switchboard must not stop a trading loop. Falling back
            # to the config is the safe direction: it is what the operator
            # committed, and it is what provenance will say ran.
            log.warning("switchboard at %s is unreadable (%s) - using config", self.path, exc)
            return False
        strategies = raw.get("strategies") if isinstance(raw, dict) else None
        self._state = {str(k): bool(v) for k, v in (strategies or {}).items()}
        self._updated = {
            "at": raw.get("updated_at"), "by": raw.get("updated_by"),
        } if isinstance(raw, dict) else {}
        self._mtime = mtime
        return True

    # -- writing ------------------------------------------------------------ #
    def set(self, name: str, value: bool, actor: str = "dashboard") -> None:
        self.update({name: value}, actor=actor)

    def update(self, changes: dict[str, bool], actor: str = "dashboard") -> None:
        if not self.path:
            self._state.update({k: bool(v) for k, v in changes.items()})
            return
        self.reload_if_changed()             # never clobber another writer
        self._state.update({k: bool(v) for k, v in changes.items()})
        payload = {
            "strategies": self._state,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "updated_by": actor,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime
        self._updated = {"at": payload["updated_at"], "by": actor}
        log.info("switchboard %s: %s", self.path, changes)
