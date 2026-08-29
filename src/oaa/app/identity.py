"""Which account is this page pointed at?

The single most expensive mistake available this week is running against the
wrong Alpaca account: dev keys on the judged page (the deck shows an empty
account) or judged keys on a test page (junk orders land permanently in the
history judges read). See `claude/repo-architecture.md`.

So every page of the dashboard prints its resolved identity to the terminal the
moment it renders, and shows the same thing on screen. Not a log line buried in
a file - the terminal the operator is already watching.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass
from typing import Any

from oaa.config.loader import Settings

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


@dataclass
class Identity:
    page: str
    profile: str
    key_masked: str
    key_source: str
    secret_set: bool
    paper: bool
    judged_account_id: str | None
    #: The account THIS profile is supposed to be holding - the dev account on
    #: dev, the judged account on judged. `judged_account_id` is the judged one
    #: whichever profile is active, so it is the wrong thing to label a page
    #: with and the right thing to warn with.
    expected_account_id: str | None
    broker: str
    data_provider: str
    configured: bool

    @property
    def is_judged(self) -> bool:
        return self.profile == "judged"

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "profile": self.profile,
            "api_key": self.key_masked,
            "key_source": self.key_source,
            "secret_set": self.secret_set,
            "paper": self.paper,
            "judged_account_id": self.judged_account_id,
            "expected_account_id": self.expected_account_id,
            "broker": self.broker,
            "data_provider": self.data_provider,
            "configured": self.configured,
        }

    def one_line(self) -> str:
        return (
            f"[{self.page}] profile={self.profile} key={self.key_masked} "
            f"({self.key_source}) paper={self.paper} broker={self.broker}"
        )


def resolve(settings: Settings, page: str) -> Identity:
    creds = settings.credentials
    cfg = settings.config

    dev_pair_set = bool(os.getenv("ALPACA_DEV_API_KEY") and os.getenv("ALPACA_DEV_SECRET_KEY"))
    if cfg.profile == "dev" and dev_pair_set:
        source = "ALPACA_DEV_API_KEY"
    elif cfg.profile == "dev":
        source = "ALPACA_API_KEY (dev pair unset - FALLING BACK)"
    else:
        source = "ALPACA_API_KEY"

    return Identity(
        page=page,
        profile=cfg.profile,
        key_masked=creds.masked(),
        key_source=source,
        secret_set=bool(creds.secret_key),
        paper=cfg.broker.paper,
        judged_account_id=creds.account_id,
        expected_account_id=creds.expected_account_id,
        broker=cfg.broker.primary,
        data_provider=cfg.data.provider,
        configured=creds.configured,
    )


def print_banner(identity: Identity, stream: Any = None) -> None:
    """Print the resolved identity to the terminal running the dashboard."""
    out = stream or sys.stdout
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    colour = RED if identity.is_judged else GREEN
    label = "JUDGED ACCOUNT" if identity.is_judged else "dev account"

    lines = [
        "",
        f"{DIM}{'-' * 68}{RESET}",
        f"{BOLD}{CYAN}  {identity.page.upper()}{RESET}{DIM}   {stamp}{RESET}",
        f"{DIM}{'-' * 68}{RESET}",
        f"  profile          {colour}{BOLD}{identity.profile}{RESET}  ({label})",
        f"  API key          {BOLD}{identity.key_masked}{RESET}   from {identity.key_source}",
        f"  secret key       {'set' if identity.secret_set else RED + 'MISSING' + RESET}",
        f"  paper trading    {identity.paper}",
        f"  expected account {identity.expected_account_id or '(unset)'}",
        f"  judged account   {identity.judged_account_id or '(ALPACA_JUDGED_ACCOUNT_ID unset)'}",
        f"  broker / data    {identity.broker} / {identity.data_provider}",
    ]
    if not identity.configured:
        lines.append(f"  {RED}{BOLD}NO CREDENTIALS RESOLVED - check .env{RESET}")
    if identity.is_judged:
        lines.append(
            f"  {RED}{BOLD}LIVE JUDGED KEYS ARE ACTIVE ON THIS PAGE.{RESET} "
            f"{YELLOW}Anything this page submits lands in the judged history.{RESET}"
        )
    lines.append(f"{DIM}{'-' * 68}{RESET}")
    print("\n".join(lines), file=out, flush=True)


# --------------------------------------------------------------------------- #
# live verification
# --------------------------------------------------------------------------- #
def verify(settings: Settings) -> dict[str, Any]:
    """Ask Alpaca which account these keys actually open.

    The banner above reports what the ENVIRONMENT says. This reports what the
    broker says, and the two disagreeing is the failure the banner exists to
    catch but cannot see on its own: a key can be perfectly well formed, and
    resolved from exactly the right variable, and still belong to the other
    account.

    Never called on render - it is a network round trip behind a button.
    """
    out: dict[str, Any] = {
        "profile": settings.config.profile,
        "expected": settings.credentials.expected_account_id,
        "actual": None, "ok": None, "error": None,
    }
    try:
        from oaa.brokers.alpaca_rest import AlpacaRestBroker

        snapshot = AlpacaRestBroker(settings.config, settings.credentials).account()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["actual"] = snapshot.account_id
    out["equity"] = snapshot.equity
    out["buying_power"] = snapshot.buying_power
    out["options_level"] = snapshot.options_trading_level
    out["positions"] = len(snapshot.positions or [])
    out["open_orders"] = snapshot.open_orders
    expected = (out["expected"] or "").strip().upper()
    actual = (snapshot.account_id or "").strip().upper()
    # No expectation recorded is not a pass. It is an unanswered question.
    out["ok"] = bool(expected) and expected == actual
    return out
