# Published backtest runs

The runs the **public** dashboard reads. `runs/` is gitignored and ~240MB; a
deployed host clones the repo and gets none of it, so without this directory
the public page boots with an empty backtest history.

Copy runs in with:

    python scripts/publish_runs.py --all
    python scripts/publish_runs.py --list
    python scripts/publish_runs.py <run-id> [<run-id> ...]

Three files per run - `manifest.json`, `result.json.gz`, `equity.csv` - which
is everything `list_runs` and `load_run` read.

`result.json` is **gzipped** here. Raw, the store runs to 333MB and a single
wide-universe run to 50MB; it compresses about 28x, which is the difference
between publishing every backtest and publishing three. `load_run` reads
either form and prefers a plain local file, so a re-run is never shadowed by
an older published copy.
