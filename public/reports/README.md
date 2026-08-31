# Published daily reports

The sessions the **Daily Reports** tab reads on a deployed host. `reports/` is
gitignored - it is generated, one pair of files per session, and it dirties the
tree every afternoon - so a deploy host clones the repo and gets none of it.
Without this directory the tab boots empty with no error anywhere.

Copy sessions in with:

    python scripts/publish_reports.py --all
    python scripts/publish_reports.py --list
    python scripts/publish_reports.py 2026-08-31 [<date> ...]

Two files per date, both small: `<date>.json` is what the page renders and
`<date>.md` is what the download button hands over. Neither is compressed - the
pair is tens of KB against a backtest's 50MB, and staying readable in a diff is
worth more than the bytes.

The page prefers the local `reports/` copy of a date when there is one, so
re-running `oaa daily-report` for a session is a correction rather than a
second artefact, and is never shadowed by what was published before it.
