"""`public_dashboard.py` must serve the REPO's source, not an installed copy.

The deploy failure this pins: Streamlit Community Cloud caches the environment
between reboots, `pyproject.toml` declares an installable package called
`oaa`, and the old bootstrap returned as soon as `import oaa` succeeded. A
push touching only `src/` therefore changed nothing on the page, while every
`git` command on the repo said it was current.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PAGE = REPO / "public_dashboard.py"

#: Everything above this line is the bootstrap; the line itself is the first
#: thing that needs the package resolved, and `main()` below it would want a
#: live Streamlit runtime.
CUT = "from oaa.app.dashboard import main"

EXERCISE = f"""
import sys
from pathlib import Path
page = Path({str(PAGE)!r})
head = page.read_text().split({CUT!r})[0]
ns = {{"__file__": str(page), "__name__": "public_dashboard"}}
exec(compile(head, str(page), "exec"), ns)
import oaa
print(oaa.__file__)
"""


def _run(site_packages: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the bootstrap with an optional decoy `oaa` ahead of everything."""
    prefix = f"import sys; sys.path[:0] = {[str(site_packages)] if site_packages else []!r}\n"
    proc = subprocess.run(
        [sys.executable, "-c", prefix + EXERCISE], cwd=REPO,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def _decoy(tmp_path: Path) -> Path:
    """A stand-in for a stale `oaa` left in a deploy host's site-packages."""
    pkg = tmp_path / "site-packages" / "oaa"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("STALE = True\n")
    return pkg.parent


def test_the_cut_marker_is_still_in_the_page() -> None:
    """If this import is renamed the other tests would pass vacuously."""
    assert PAGE.read_text().count(CUT) == 1


def test_the_repo_source_wins_over_a_stale_installed_copy(tmp_path: Path) -> None:
    resolved = _run(_decoy(tmp_path)).stdout.strip().splitlines()[-1]
    assert resolved.startswith(str(SRC)), (
        f"the page imported {resolved}, not the repo checkout - a push that "
        "touches only src/ would never reach the deployed page"
    )


def test_it_still_works_when_nothing_is_installed() -> None:
    resolved = _run().stdout.strip().splitlines()[-1]
    assert resolved.startswith(str(SRC))


def test_the_resolved_path_is_announced_for_the_deploy_log(tmp_path: Path) -> None:
    out = _run(_decoy(tmp_path)).stdout
    assert "[public_dashboard] oaa imported from" in out
    assert str(SRC) in out
