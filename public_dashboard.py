"""The public, read-only dashboard - the entry point a host deploys.

    streamlit run public_dashboard.py

It is the same dashboard as `oaa dashboard`, with everything that WRITES
removed: no Control tab, no backtest runner, no live-chain pricing, no account
switch, no key or account id on the page. What is left is the saved backtest
history and the judged account's live positions, which is what a reader is
here for.

The environment variable is set BEFORE `oaa.app.dashboard` is imported, and
`oaa.app.mode` reads it at call time rather than at import, so there is no
ordering in which a control can survive into this build.

The local dashboard is untouched: `make dashboard` never sets OAA_PUBLIC.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["OAA_PUBLIC"] = "1"

#: `runs/` is gitignored and 240MB - a deployed host has none of it. The runs
#: chosen for publication are copied into `public/runs/` by
#: `scripts/publish_runs.py` and committed, and the config's backtest output
#: directory is pointed there. `OAA_BACKTEST__OUTPUT_DIR` is the config
#: loader's own env overlay, not a special case added for this.
#:
#: The fallback matters for `make public-dashboard`: previewing the public
#: build locally should show the full local history, not an empty page.
if "OAA_BACKTEST__OUTPUT_DIR" not in os.environ:
    _published = Path(__file__).resolve().parent / "public" / "runs"
    if any(_published.glob("*/manifest.json")):
        os.environ["OAA_BACKTEST__OUTPUT_DIR"] = "public/runs"


def _bootstrap_path() -> None:
    """Put `<repo>/src` on sys.path when the package is not installed.

    `streamlit run` puts THIS file's directory on sys.path, not the repo's
    `src`. Deployed hosts install from `requirements.txt` into an interpreter
    that may not have `oaa` itself installed, so this is the difference
    between a working page and `ModuleNotFoundError` on boot.
    """
    try:
        import oaa  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    here = Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        candidate = parent / "src"
        if (candidate / "oaa" / "__init__.py").exists():
            sys.path.insert(0, str(candidate))
            return


_bootstrap_path()

from oaa.app.dashboard import main  # noqa: E402

main()
