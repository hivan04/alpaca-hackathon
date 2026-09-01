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
        # ABSOLUTE, deliberately. A relative value is resolved against
        # `Settings.root`, which `project_root()` finds by walking up from the
        # INSTALLED `oaa/config/loader.py` looking for a directory holding both
        # `config/` and `src/` - and falls back to `Path.cwd()` when the
        # package is installed into site-packages rather than run from the
        # tree. On a deploy host those are not always the repo root, and the
        # only symptom is a backtest tab with no history and no error.
        # `Settings.path` returns an absolute value untouched.
        os.environ["OAA_BACKTEST__OUTPUT_DIR"] = str(_published)


def _bootstrap_path() -> None:
    """Make the REPO's `src` win over any installed copy of the package.

    `streamlit run` puts THIS file's directory on sys.path, not the repo's
    `src`, so something has to. Deployed hosts install from
    `requirements.txt` into an interpreter that may not have `oaa` itself, so
    this is also the difference between a working page and a
    `ModuleNotFoundError` on boot.

    It used to `try: import oaa` first and return if that worked, which is the
    wrong way round on a deploy host. `pyproject.toml` declares an installable
    package named `oaa`, so an installer that honours it - Streamlit Community
    Cloud's does - puts a COPY in site-packages. That environment is cached
    between reboots and only rebuilt when the dependency files change, so a
    push that touches only `src/` leaves the stale copy installed, `import oaa`
    succeeds against it, and this function returned before the freshly cloned
    source was ever reachable. The page then serves code from a commit ago
    while every `git` command on the repo says it is current - which is
    exactly how the 1 Sep `OccSymbol.underlying` fix appeared not to deploy.

    The repo checkout is the source of truth, so it goes on the front of
    `sys.path` unconditionally and any copy already imported from elsewhere is
    dropped so the next import resolves here.
    """
    here = Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        candidate = parent / "src"
        if not (candidate / "oaa" / "__init__.py").exists():
            continue

        src = str(candidate)
        sys.path.insert(0, src)

        # An `oaa` imported before this ran would keep its own `__path__` and
        # go on serving submodules from site-packages, so the entry on the
        # front of sys.path would change nothing. Nothing imports it this
        # early today; dropping it is what keeps that true.
        for name in [n for n in sys.modules if n == "oaa" or n.startswith("oaa.")]:
            module_file = getattr(sys.modules[name], "__file__", None) or ""
            if not module_file.startswith(src):
                del sys.modules[name]
        return


_bootstrap_path()


def _announce_source() -> None:
    """Say in the deploy log which copy of the package is actually live.

    A page serving stale code looks identical to a page serving current code,
    and there is no way to tell them apart from the outside. One line naming
    the resolved file settles it, and it is a module path - not a key, an
    account id or anything else the public build keeps off the page and out of
    the log.
    """
    import oaa

    print(f"[public_dashboard] oaa imported from {getattr(oaa, '__file__', '?')}")


_announce_source()

from oaa.app.dashboard import main  # noqa: E402

main()
