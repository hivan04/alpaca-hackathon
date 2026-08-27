#!/usr/bin/env python3
"""Turn `pip freeze` output into the pinned requirements.txt.

`requirements.txt` exists because submission forms ask for one. It is not the
source of truth - `pyproject.toml` is - and a raw `pip freeze` dump is actively
dangerous as a portable artefact, for one reason:

    pip freeze records what resolved for ONE interpreter on ONE platform.

Three of the pins are Python-3.10-only backports with no distribution for 3.11+,
so an unmarked freeze fails the whole install on a newer Python with
"No matching distribution found for backports.asyncio.runner". This script
re-adds the `; python_version < "3.11"` markers and groups the pins by the
extra they belong to, so the file is readable and survives a regenerate.

    make requirements

Usage:  gen_requirements.py <freeze.txt> <out.txt>
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

# Backports that exist only for <3.11. Unmarked, these break the install.
BACKPORTS = {"backports.asyncio.runner", "exceptiongroup", "tomli"}

# Kept in their own block so a runtime-only install can drop them wholesale.
DEV_ONLY = {
    "pytest", "pytest-asyncio", "ruff", "mypy", "freezegun", "mypy-extensions",
    "iniconfig", "pluggy", "coverage",
}

DIRECT: dict[str, tuple[str, list[str]]] = {
    "runtime": (
        "core runtime",
        ["alpaca-py", "pydantic", "pydantic-settings", "PyYAML", "python-dotenv",
         "typer", "rich", "httpx", "pandas", "numpy", "tenacity", "structlog"],
    ),
    "agents": (
        "[agents] - the reasoning layer. anthropic drives LIVE trading;\n"
        "# google-genai drives the BACKTEST critic (see backtest.critic.llm).",
        ["anthropic", "google-genai", "mcp"],
    ),
    "app": (
        "[app] - the read-only FastAPI page behind `oaa serve`",
        ["fastapi", "uvicorn", "jinja2"],
    ),
    "dashboard": (
        "[dashboard] - the Streamlit operator dashboard behind `oaa dashboard`",
        ["streamlit", "plotly"],
    ),
    "dev": (
        "[dev] - tests and linting; drop this block for a runtime-only install",
        ["pytest", "pytest-asyncio", "ruff", "mypy", "freezegun"],
    ),
}

HEADER = """\
# requirements.txt - GENERATED, and not the source of truth.
#
# The source of truth for this project's dependencies is pyproject.toml:
# [project].dependencies plus the agents / app / dashboard / dev extras.
# The supported install is:
#
#     pip install -e '.[all]'        (what `make install` and the Dockerfile do)
#
# This file exists because submission forms ask for one. It is a full pinned
# snapshot of a working `.[all]` environment. It does NOT install the `oaa`
# package itself - after installing from here, still run
# `pip install -e . --no-deps` so `import oaa` and the `oaa` command work.
#
# Regenerate:  make requirements
# Generated:   {stamp} on Python {py} / {platform}.
#
# PORTABILITY: `pip freeze` records what resolved for ONE interpreter on ONE
# platform, and the backports below have no distribution for 3.11+ -
# `backports.asyncio.runner` fails the install outright with "No matching
# distribution found". They carry `; python_version < "3.11"` markers so this
# file works on 3.10 through 3.13.
#
# If a pin still fails on your interpreter, do not fight it: use
# `pip install -e '.[all]'`, which re-resolves for the Python you have.
"""


def normalise(name: str) -> str:
    return name.lower().replace("_", ".").replace("-", ".")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    freeze_path, out_path = Path(argv[1]), Path(argv[2])

    pins: dict[str, str] = {}
    for line in freeze_path.read_text().splitlines():
        if "==" not in line or line.startswith("-"):
            continue
        name, _ = line.split("==", 1)
        pins[normalise(name)] = line.strip()

    def pin(name: str) -> str:
        key = normalise(name)
        if key not in pins:
            return f"# {name}  (not present in this environment)"
        line = pins[key]
        if key in {normalise(b) for b in BACKPORTS}:
            return f'{line} ; python_version < "3.11"'
        return line

    named = {normalise(n) for _, names in DIRECT.values() for n in names}
    skip = named | {normalise(d) for d in DEV_ONLY} | {"oaa"}
    transitive = sorted((pin(k) for k in pins if k not in skip), key=str.lower)

    lines = [
        HEADER.format(
            stamp=dt.date.today().isoformat(),
            py=".".join(str(v) for v in sys.version_info[:2]),
            platform=sys.platform,
        ),
        "# --- direct dependencies -------------------------------------------------",
        "",
    ]
    for comment, names in DIRECT.values():
        lines.append(f"# {comment}")
        lines.extend(pin(n) for n in names)
        lines.append("")
    lines += [
        "# --- transitive dependencies, pinned for reproducibility -----------------",
        "",
        *transitive,
        "",
    ]
    out_path.write_text("\n".join(lines))
    print(f"{out_path}: {len(pins)} pins ({len(transitive)} transitive)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
