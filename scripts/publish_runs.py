#!/usr/bin/env python3
"""Copy chosen backtest runs into `public/runs/` so the public build can read them.

`runs/` is gitignored and around 240MB - a deployed host clones the repo and
gets none of it, so the public dashboard would boot with an empty history.
This copies a hand-picked few (a run is well under 1MB: manifest, result and
equity curve) into a directory that IS committed.

Hand-picked deliberately. The published history is the argument the page
makes; it should be the runs that represent the strategy, not the last five
things that happened to be executed.

    python scripts/publish_runs.py --all
    python scripts/publish_runs.py --list
    python scripts/publish_runs.py 20260830-152751__DIA-EEM 20260830-013506__wing-fix
    python scripts/publish_runs.py --latest 3
    python scripts/publish_runs.py --clear

Ids may be prefixes; an ambiguous one is refused rather than guessed.

`result.json` is written out **gzipped**. Raw, the store is 333MB and a single
wide-universe run is 50MB - more than a deploy host should clone and more than
GitHub is happy to hold. It compresses about 28x, so every backtest ever run
fits in ~12MB. `load_run` reads either form, preferring a plain local file so
a re-run is never shadowed by an older published copy.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "runs" / "backtests"
TARGET = REPO / "public" / "runs"

#: The three files `list_runs` and `load_run` actually read. Anything else a
#: run directory accumulates stays local - the point is a small, committable
#: directory, not a second copy of the run store.
WANTED = ("manifest.json", "result.json", "equity.csv")

#: Compressed on the way out. The other two are small and stay readable in a
#: diff, which is worth more than the few KB gzip would save on them.
COMPRESS = {"result.json"}


def _size(directory: Path) -> int:
    return sum(f.stat().st_size for f in directory.iterdir() if f.is_file())


def _runs(root: Path) -> list[Path]:
    return sorted((m.parent for m in root.glob("*/manifest.json")), reverse=True)


def _describe(directory: Path) -> str:
    try:
        data = json.loads((directory / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return directory.name
    metrics = data.get("metrics") or {}
    net = metrics.get("net_pnl", metrics.get("net", "?"))
    size = sum(f.stat().st_size for f in directory.iterdir() if f.is_file())
    return (
        f"{directory.name}\n"
        f"    {data.get('start')} to {data.get('end')}  "
        f"{len(data.get('symbols') or [])} symbols  net {net}  "
        f"{size / 1024:.0f}KB"
    )


def _resolve(token: str, available: list[Path]) -> Path:
    exact = [d for d in available if d.name == token]
    if exact:
        return exact[0]
    matches = [d for d in available if d.name.startswith(token)]
    if not matches:
        sys.exit(f"no run matches {token!r} - try --list")
    if len(matches) > 1:
        names = "\n  ".join(d.name for d in matches)
        sys.exit(f"{token!r} is ambiguous:\n  {names}")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", help="run ids, or unambiguous prefixes")
    parser.add_argument("--list", action="store_true", help="show what is available")
    parser.add_argument("--latest", type=int, metavar="N", help="publish the newest N")
    parser.add_argument("--all", action="store_true",
                        help="publish every run in the store")
    parser.add_argument("--clear", action="store_true",
                        help="empty public/runs/ before copying")
    args = parser.parse_args()

    if not SOURCE.exists():
        sys.exit(f"{SOURCE} does not exist - nothing has been backtested here.")
    available = _runs(SOURCE)

    if args.list:
        print(f"{len(available)} runs in {SOURCE.relative_to(REPO)}:\n")
        for directory in available:
            print("  " + _describe(directory))
        published = _runs(TARGET)
        print(f"\ncurrently published ({len(published)}):")
        for directory in published:
            print(f"  {directory.name}")
        return

    if args.clear:
        for directory in _runs(TARGET):
            shutil.rmtree(directory)
            print(f"removed  {directory.name}")

    chosen = [_resolve(t, available) for t in args.ids]
    if args.all:
        chosen = list(available)
    if args.latest:
        chosen += [d for d in available[: args.latest] if d not in chosen]
    if not chosen:
        if args.clear:
            return
        parser.error("name at least one run, or pass --all / --latest N (or --list)")

    TARGET.mkdir(parents=True, exist_ok=True)
    total = 0
    for directory in chosen:
        destination = TARGET / directory.name
        destination.mkdir(exist_ok=True)
        raw = 0
        for name in WANTED:
            source = directory / name
            if not source.exists():
                print(f"  ! {directory.name} has no {name}")
                continue
            raw += source.stat().st_size
            if name in COMPRESS:
                packed = destination / f"{name}.gz"
                # mtime=0 so re-publishing an unchanged run produces an
                # identical file. Without it every run rewrites the gzip
                # header and git sees 79 modified files each time.
                with gzip.GzipFile(packed, "wb", compresslevel=9, mtime=0) as out:
                    out.write(source.read_bytes())
                (destination / name).unlink(missing_ok=True)
                total += packed.stat().st_size
            else:
                shutil.copy2(source, destination / name)
                total += source.stat().st_size
        print(f"published  {directory.name}  "
              f"({raw / 1024 / 1024:.1f}MB -> {_size(destination) / 1024 / 1024:.2f}MB)")

    print(f"\n{len(chosen)} run(s), {total / 1024 / 1024:.1f}MB in "
          f"{TARGET.relative_to(REPO)} - commit it.")


if __name__ == "__main__":
    main()
