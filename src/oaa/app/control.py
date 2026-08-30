"""The Control tab: which books are live, on which account, right now.

Two accounts run at once - the backtesting account and the competition account -
and they are separate all the way down: separate credentials, separate journals,
separate switchboard files under their own `telemetry.run_dir`. This page is the
one place both are visible together, because the expensive mistake this week is
not a bad trade, it is a switch flipped on the wrong account.

Every row is one strategy. Every strategy has one toggle per account. A toggle
writes that account's `switchboard.json` immediately; the live agent re-reads it
at the top of its next cycle, so nothing here needs a restart.

Switching a book OFF stops it OPENING. Positions it already holds keep being
managed and closed by their own strategy's exit rules - an off switch that
abandoned open risk would be worse than the mistake it prevents.

Every book now has a real toggle. The events book gained one on 30 Aug, when it
moved from its own process into `oaa run` as two scheduled cycles (events_flatten
09:45, events_arm 15:50). It is still not a firewall tenant - it holds overnight
and leases no capital - but `oaa run` does now open positions for it, so a switch
here controls something real. `note` marks that distinction on the row rather
than removing the control.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from oaa.app import identity as ident
from oaa.core.switchboard import Switchboard

#: The books, in the order an operator thinks about them. `wired` is False for
#: a strategy that is registered but not yet listed in `config.strategies` - its
#: switch is remembered, and takes effect the moment it is wired in.
STRATEGIES: list[dict[str, Any]] = [
    {
        "name": "vol_carry",
        "title": "Carry",
        "blurb": "Sells rich implied vol as defined-risk structures, held days. "
                 "The measured book: positive in every honest run.",
    },
    {
        "name": "intraday_momentum",
        "title": "Intraday momentum",
        "blurb": "Buys the front expiry on a VWAP trigger, flat by 15:15. "
                 "Never measured in replay before 29 Aug - treat live runs as "
                 "the measurement.",
    },
    {
        "name": "event_premium",
        "title": "Event premium",
        "blurb": "Dormant unless a scheduled print prices above its realised "
                 "distribution. Standing down is the expected outcome.",
    },
    {
        "name": "earnings_event_directional",
        "title": "Earnings events",
        "blurb": "Buys a vertical debit spread into a CONFIRMED print, sized on "
                 "an LLM's confidence in the direction, closed the next "
                 "morning. Armed on a date rather than a signal.",
        # In `oaa run` since 30 Aug. Not a firewall tenant - it arms after the
        # 15:15 transient cutoff and holds overnight, so its cycles build their
        # own RiskEngine with firewall=None. The toggle is real: switching it
        # off means the 15:50 arm cycle stands down. The 09:45 flatten runs
        # regardless, because an off switch must never abandon open risk.
        "note": "in `oaa run`: 09:45 flatten · 15:50 arm · not a firewall "
                "tenant · `oaa events arm` still works for a manual or "
                "dry-run arm · see the Events tab",
    },
]

ACCOUNTS = [
    ("dev", "Backtesting account", "ALPACA_DEV_* keys"),
    ("judged", "Competition account", "ALPACA_* keys - the judged submission"),
]


def _board(settings: Any) -> Switchboard:
    return Switchboard.open(getattr(settings.config.telemetry, "run_dir", None))


def _configured(settings: Any) -> dict[str, bool]:
    return {ref.name: ref.enabled for ref in settings.config.strategies}


def _flip(profile: str, name: str, board: Switchboard) -> None:
    board.set(name, bool(st.session_state[f"sw::{profile}::{name}"]), actor="dashboard")


def render_control(settings_for: dict[str, Any]) -> None:
    """`settings_for` maps profile name -> loaded Settings."""
    st.subheader("Which books are live")
    st.caption(
        "Toggles take effect at the agent's next cycle - no restart. Each "
        "column writes its own account's switchboard; nothing here can reach "
        "the other account."
    )

    boards = {p: _board(s) for p, s in settings_for.items() if s is not None}
    configured = {p: _configured(s) for p, s in settings_for.items() if s is not None}

    # --- who am I about to change? ------------------------------------- #
    header = st.columns([3, 2, 2])
    header[0].markdown("**Strategy**")
    for col, (profile, label, note) in zip(header[1:], ACCOUNTS, strict=False):
        settings = settings_for.get(profile)
        if settings is None:
            col.markdown(f"**{label}**\n\n:red[not loaded]")
            continue
        who = ident.resolve(settings, f"control:{profile}")
        # The account THIS profile should hold - not `judged_account_id`, which
        # is the judged one whichever profile is active and would label the
        # backtesting column with the competition account number.
        account = who.expected_account_id or "unset"
        col.markdown(
            f"**{label}**  \n`{profile}` · key `{who.key_masked}` "
            f"from `{who.key_source}`  \naccount `{account}`"
            + ("" if who.configured else "  \n:red[no credentials]")
        )
        col.caption(note)
        checked = st.session_state.get(f"verify::{profile}")
        if checked:
            if checked.get("error"):
                col.warning(f"could not reach Alpaca: {checked['error']}", icon=":material/error:")
            elif checked["ok"]:
                col.success(
                    f"Alpaca confirms `{checked['actual']}` · equity "
                    f"${checked.get('equity', 0):,.0f} · options level "
                    f"{checked.get('options_level', '?')} · {checked.get('positions', 0)} open",
                    icon=":material/check:",
                )
            else:
                col.error(
                    f"KEY OPENS `{checked['actual']}`, expected "
                    f"`{checked['expected'] or 'nothing recorded'}` - do not trade this.",
                    icon=":material/warning:",
                )

    if st.button(
        "Verify both accounts with Alpaca",
        help="Asks the broker which account each key actually opens, and "
             "compares it with the one this profile expects. A well-formed key "
             "resolved from the right variable can still belong to the other "
             "account, and only this catches that.",
    ):
        for profile, _, _ in ACCOUNTS:
            settings = settings_for.get(profile)
            if settings is not None:
                st.session_state[f"verify::{profile}"] = ident.verify(settings)
        st.rerun()

    st.divider()

    wired = {n for s in configured.values() for n in s}
    for spec in STRATEGIES:
        name = spec["name"]
        row = st.columns([3, 2, 2])
        title = spec["title"] if name in wired else f"{spec['title']}  ·  not wired"
        row[0].markdown(f"**{title}**")
        row[0].caption(spec["blurb"])
        if spec.get("note"):
            row[0].caption(f":grey[{spec['note']}]")

        for col, (profile, label, _) in zip(row[1:], ACCOUNTS, strict=False):
            board = boards.get(profile)
            if board is None:
                col.write("—")
                continue
            default = configured[profile].get(name, False)
            current = board.enabled(name, default)
            col.toggle(
                label, value=current, key=f"sw::{profile}::{name}",
                on_change=_flip, args=(profile, name, board),
                help=f"config default: {'on' if default else 'off'}",
            )
            state = board.state()
            if name in state and state[name] != default:
                col.caption(":orange[overrides config]")

    st.divider()
    stamps = []
    for profile, label, _ in ACCOUNTS:
        board = boards.get(profile)
        if board and board.updated.get("at"):
            stamps.append(f"{label}: last changed {board.updated['at']} "
                          f"by {board.updated.get('by', '?')}")
    if stamps:
        st.caption(" · ".join(stamps))
    paths = " · ".join(str(b.path) for b in boards.values() if b.path)
    if paths:
        st.caption(f"switchboards: {paths}")
