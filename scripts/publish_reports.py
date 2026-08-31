#!/usr/bin/env python3
"""Copy chosen daily reports into `public/reports/` so the deployed page has some.

`reports/` is gitignored - it is generated, one pair of files per session, and
it dirties the tree every afternoon. A deploy host clones the repo and gets
none of it, so the Daily Reports tab would boot empty with no error anywhere.
This copies chosen dates into a directory that IS committed.

    python scripts/publish_reports.py --all
    python scripts/publish_reports.py --list
    python scripts/publish_reports.py 2026-08-31
    python scripts/publish_reports.py --latest 5
    python scripts/publish_reports.py --clear
    python scripts/publish_reports.py --all --profile dev   # rarely what you want

Both files travel together: the `.json` sidecar is what the page renders and
the `.md` is what the download button hands over, and a date with only one of
them renders half a page. Neither is compressed - the pair is a few tens of KB
against a backtest's 50MB, and staying readable in a diff is worth more than
the bytes.

The default profile is `judged`, because the public build serves the judged
account and nothing else. Publishing `dev` reports puts the throwaway
account's sessions on a public page; it is possible, and it is deliberate.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "reports"
TARGET = REPO / "public" / "reports"

#: The page reads the sidecar and offers the markdown for download. A date is
#: only publishable if it has the sidecar; the markdown alone renders as a
#: fallback, so it is copied when present rather than required.
REQUIRED = ".json"
ALSO = ".md"


def _dates(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted((f.stem for f in directory.glob(f"*{REQUIRED}")), reverse=True)


def _describe(directory: Path, date: str) -> str:
    import json

    try:
        data = json.loads((directory / f"{date}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return f"{date}  (unreadable sidecar)"
    session = data.get("session") or {}
    author = (data.get("critique") or {}).get("author", "?")
    return (
        f"{date}  P&L {session.get('day_pl', 0):+,.2f}  "
        f"{len(session.get('fills') or [])} fill(s)  "
        f"{len(session.get('potential') or [])} declined  "
        f"{session.get('gate_rejections', 0)} gate rejections  "
        f"critique: {author}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dates", nargs="*", help="ISO dates, e.g. 2026-08-31")
    parser.add_argument("--profile", default="judged",
                        help="which account's reports (default: judged)")
    parser.add_argument("--list", action="store_true", help="show what is available")
    parser.add_argument("--latest", type=int, metavar="N", help="publish the newest N")
    parser.add_argument("--all", action="store_true", help="publish every session")
    parser.add_argument("--clear", action="store_true",
                        help="empty this profile's published directory first")
    args = parser.parse_args()

    source = SOURCE / args.profile
    target = TARGET / args.profile
    if not source.is_dir():
        sys.exit(f"{source} does not exist - no session has been reported here.")
    available = _dates(source)

    if args.list:
        print(f"{len(available)} report(s) in {source.relative_to(REPO)}:\n")
        for date in available:
            print("  " + _describe(source, date))
        published = _dates(target)
        print(f"\ncurrently published ({len(published)}):")
        for date in published:
            print(f"  {date}")
        return

    if args.clear and target.is_dir():
        for f in sorted(target.iterdir()):
            if f.is_file():
                f.unlink()
                print(f"removed  {f.name}")

    chosen = []
    for token in args.dates:
        if token not in available:
            sys.exit(f"no report for {token!r} in {source.relative_to(REPO)} "
                     "- try --list")
        chosen.append(token)
    if args.all:
        chosen = list(available)
    if args.latest:
        chosen += [d for d in available[: args.latest] if d not in chosen]
    if not chosen:
        if args.clear:
            return
        parser.error("name at least one date, or pass --all / --latest N (or --list)")

    target.mkdir(parents=True, exist_ok=True)
    total = 0
    for date in chosen:
        copied = []
        for suffix in (REQUIRED, ALSO):
            f = source / f"{date}{suffix}"
            if not f.exists():
                print(f"  ! {date} has no {suffix} file")
                continue
            shutil.copy2(f, target / f.name)
            total += f.stat().st_size
            copied.append(suffix)
        print(f"published  {date}  ({', '.join(copied) or 'nothing'})")

    print(f"\n{len(chosen)} report(s), {total / 1024:.0f}KB in "
          f"{target.relative_to(REPO)} - commit it.")


if __name__ == "__main__":
    main()
