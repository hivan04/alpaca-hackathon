"""Which build of the dashboard this is: the operator's, or the public one.

There is one dashboard, not two codebases. The public build is the same
`main()` with the controls removed, selected by an environment variable that
`public_dashboard.py` sets before importing anything.

Read at call time, never cached. A cached answer would be resolved once at
import and then be wrong for the rest of the process, which is exactly the
class of bug that leaves a Run button on a public page.

What the public build removes, and why each one:

    Control tab             flips books on and off on a REAL account
    Run a new backtest      spawns a replay on the host, burning API budget
    Price the live chain    one network round trip per confirmed event
    Refresh from Alpaca     same, per click
    dev/judged switch       the dev account's runs are nobody else's business
    identity banner         masked key, key source and account id

What it keeps is the whole point of publishing it: saved backtest history and
the live positions of the judged account.
"""

from __future__ import annotations

import os

#: Set to 1/true/yes/on by `public_dashboard.py`, or in Streamlit Cloud's
#: secrets, to select the read-only build.
ENV_VAR = "OAA_PUBLIC"

_TRUE = {"1", "true", "yes", "on"}

#: The public build never shows the dev account.
PUBLIC_PROFILE = "judged"


def is_public() -> bool:
    """True when this process is serving the public, read-only dashboard."""
    return os.environ.get(ENV_VAR, "").strip().lower() in _TRUE


def is_operator() -> bool:
    """True for the local dashboard - every control present."""
    return not is_public()
