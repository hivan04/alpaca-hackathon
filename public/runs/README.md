# Published backtest runs

The runs the **public** dashboard reads. `runs/` is gitignored and ~240MB; a
deployed host clones the repo and gets none of it, so without this directory
the public page boots with an empty backtest history.

Copy runs in with:

    python scripts/publish_runs.py --list
    python scripts/publish_runs.py <run-id> [<run-id> ...]

Three files per run - `manifest.json`, `result.json`, `equity.csv` - which is
everything `list_runs` and `load_run` read. Well under 1MB each.

Choose them deliberately. This directory *is* the argument the public page
makes about the strategy; it is not a mirror of the local run store.
