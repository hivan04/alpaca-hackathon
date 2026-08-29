"""The Positions tab: what each account is actually holding, and everything it
has ever done.

Read from ALPACA, not from the journal. The journal records what the agent
did; the account is also reachable by hand, by an earlier build, and by
anything else holding the keys - and at submission the judges read the account,
not our log of it. Where the two disagree the broker is right, so the broker is
what this page shows.

Both accounts side by side, because "which account is that position on" is the
question this week keeps asking.

Every render here is a network round trip, so it happens behind a button and
the result is held in session state until you ask again.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import streamlit as st

from oaa.app import identity as ident

ACCOUNTS = [("dev", "Backtesting account"), ("judged", "Competition account")]


def _fetch(settings: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"error": None, "fetched_at": dt.datetime.now()}
    try:
        from oaa.brokers.alpaca_rest import AlpacaRestBroker

        broker = AlpacaRestBroker(settings.config, settings.credentials)
        snapshot = broker.account()
        out["account"] = snapshot
        out["positions"] = [p.model_dump() for p in (snapshot.positions or [])]
        out["orders"] = broker.orders(limit=500)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _positions_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    keep = [c for c in (
        "symbol", "underlying", "expiry", "strike", "right", "qty",
        "avg_entry_price", "market_value", "unrealized_pl", "unrealized_plpc",
    ) if c in df.columns]
    return df[keep].sort_values("unrealized_pl", ascending=True)


def _orders_frame(rows: list[dict[str, Any]], filled_only: bool) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if filled_only and "filled_qty" in df:
        df = df[df["filled_qty"] > 0]
    if "submitted_at" in df:
        df = df.sort_values("submitted_at", ascending=False)
    keep = [c for c in (
        "submitted_at", "filled_at", "underlying", "symbol", "side", "qty",
        "filled_qty", "filled_avg_price", "notional", "status", "order_type",
        "client_order_id",
    ) if c in df.columns]
    return df[keep]


def _account_row(snapshot: Any) -> None:
    cols = st.columns(5)
    cols[0].metric("Equity", f"${snapshot.equity:,.2f}",
                   f"{snapshot.equity - snapshot.last_equity:+,.2f} today"
                   if snapshot.last_equity else None)
    cols[1].metric("Cash", f"${snapshot.cash:,.2f}")
    cols[2].metric("Buying power", f"${snapshot.buying_power:,.2f}")
    cols[3].metric("Open positions", len(snapshot.positions or []))
    cols[4].metric("Options level", snapshot.options_trading_level or "?")


def render_positions(settings_for: dict[str, Any]) -> None:
    st.subheader("Positions and trades")
    st.caption(
        "Straight from Alpaca - open positions and the full order history for "
        "both accounts. Anything the account did is here, whether this agent "
        "did it or not."
    )

    left, right = st.columns([1, 3])
    if left.button("Refresh from Alpaca", width="stretch"):
        for profile, _ in ACCOUNTS:
            settings = settings_for.get(profile)
            if settings is not None:
                st.session_state[f"pos::{profile}"] = _fetch(settings)
        st.rerun()
    filled_only = right.toggle(
        "Filled orders only", value=True,
        help="Off shows everything the account submitted, including cancelled "
             "and rejected orders - which is where a broken loop shows up first.",
    )

    for profile, label in ACCOUNTS:
        settings = settings_for.get(profile)
        st.divider()
        if settings is None:
            st.warning(f"**{label}** - profile could not be loaded.")
            continue

        who = ident.resolve(settings, f"positions:{profile}")
        st.markdown(
            f"### {label}\n"
            f"`{profile}` · key `{who.key_masked}` · account "
            f"`{who.expected_account_id or 'unset'}`"
        )

        data = st.session_state.get(f"pos::{profile}")
        if data is None:
            st.info("Not loaded yet - hit **Refresh from Alpaca**.")
            continue
        if data["error"]:
            st.error(f"Could not read this account: {data['error']}")
            continue

        _account_row(data["account"])
        st.caption(f"as of {data['fetched_at']:%H:%M:%S}")

        positions = _positions_frame(data["positions"])
        st.markdown("**Open positions**")
        if positions.empty:
            st.caption("Flat - nothing open.")
        else:
            st.dataframe(positions, width="stretch", hide_index=True)

        orders = _orders_frame(data["orders"], filled_only)
        st.markdown("**Order history**")
        if orders.empty:
            st.caption(
                "No orders on this account yet."
                if filled_only else "Nothing submitted on this account yet."
            )
        else:
            st.caption(f"{len(orders)} rows")
            st.dataframe(orders, width="stretch", hide_index=True)
            if "notional" in orders and orders["notional"].notna().any():
                st.caption(
                    f"traded notional: ${orders['notional'].fillna(0).sum():,.0f}"
                )
