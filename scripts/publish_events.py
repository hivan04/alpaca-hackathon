#!/usr/bin/env python3
"""Copy watch dossiers into `public/events/watch/` so the deployed page has some.

The Events tab's "The run-up" section is the only place a reader sees what the
model actually found on this week's names - the dated notes the watch cycles
wrote. Those live under `runs/events/watch/`, and `runs/` is gitignored with no
leading slash, so it matches at ANY depth: a deploy host clones the repo, finds
no dossiers, and the section renders "Nothing logged yet" on a page where a
great deal was in fact logged. This copies them into a directory that IS
committed.

    python scripts/publish_events.py --all
    python scripts/publish_events.py --list
    python scripts/publish_events.py AVGO LULU
    python scripts/publish_events.py --clear

Only the live store is published, never `reported/`: a name whose print is
behind us is retired evidence, and putting it on a public page beside names
still being watched invites reading post-print commentary as a pre-print call.

Not compressed. A dossier is a few KB against a backtest's 50MB, and staying
readable in a diff is worth more than the bytes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "runs" / "events" / "watch"
TARGET = REPO / "public" / "events" / "watch"


def _symbols(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(f.stem for f in directory.glob("*.json"))


def _describe(directory: Path, symbol: str) -> str:
    try:
        data = json.loads((directory / f"{symbol}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return f"{symbol}  (unreadable dossier)"
    notes = data.get("notes") or []
    last = notes[-1].get("asof") if notes else "-"
    return (f"{symbol:<6} {len(notes):>3} note(s)  "
            f"{len(data.get('seen') or []):>4} item(s) read  last {last}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="tickers, e.g. AVGO LULU")
    parser.add_argument("--list", action="store_true", help="show what is available")
    parser.add_argument("--all", action="store_true", help="publish every dossier")
    parser.add_argument("--clear", action="store_true",
                        help="empty the published directory first")
    args = parser.parse_args()

    available = _symbols(SOURCE)
    if args.list:
        print(f"{len(available)} dossier(s) in {SOURCE.relative_to(REPO)}:\n")
        for symbol in available:
            print("  " + _describe(SOURCE, symbol))
        published = _symbols(TARGET)
        print(f"\ncurrently published ({len(published)}):")
        for symbol in published:
            print(f"  {symbol}")
        return

    if not SOURCE.is_dir() and not args.clear:
        sys.exit(f"{SOURCE} does not exist - no watch cycle has run here.")

    if args.clear and TARGET.is_dir():
        for f in sorted(TARGET.glob("*.json")):
            f.unlink()
            print(f"removed  {f.name}")

    chosen: list[str] = []
    for token in args.symbols:
        symbol = token.upper()
        if symbol not in available:
            sys.exit(f"no dossier for {symbol!r} in {SOURCE.relative_to(REPO)} "
                     "- try --list")
        chosen.append(symbol)
    if args.all:
        chosen = list(available)
    if not chosen:
        if args.clear:
            return
        parser.error("name at least one symbol, or pass --all (or --list)")

    TARGET.mkdir(parents=True, exist_ok=True)
    total = 0
    for symbol in chosen:
        f = SOURCE / f"{symbol}.json"
        shutil.copy2(f, TARGET / f.name)
        total += f.stat().st_size
        print("published  " + _describe(SOURCE, symbol))

    print(f"\n{len(chosen)} dossier(s), {total / 1024:.0f}KB in "
          f"{TARGET.relative_to(REPO)} - commit it.")


if __name__ == "__main__":
    main()
