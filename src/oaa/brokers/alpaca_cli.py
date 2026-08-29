"""Alpaca CLI backend - `alpaca` binary driven over subprocess.

This exists because the hackathon explicitly rewards CLI-tool usage, and
because a shell-shaped broker is trivially auditable: every call is a command
you can paste into a terminal and rerun. Output is JSON on stdout.

Install:  go install github.com/alpacahq/cli/cmd/alpaca@latest
          brew install alpacahq/tap/cli
Auth:     alpaca profile login --api-key      (or ALPACA_API_KEY/ALPACA_SECRET_KEY)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from oaa.brokers.alpaca_rest import _dry_fill
from oaa.brokers.base import Broker, broker_registry
from oaa.core.errors import BrokerError
from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot, Fill, OrderTicket, PositionSnapshot, Right
from oaa.options.occ import is_occ, parse_occ

log = get_logger("brokers.cli")


@broker_registry.register("cli")
class AlpacaCliBroker(Broker):
    name = "alpaca-cli"
    supports_multileg = True

    def __init__(self, cfg: Any, credentials: Any = None) -> None:
        super().__init__(cfg, credentials)
        self.binary = cfg.broker.cli.binary
        self.timeout = cfg.broker.cli.timeout_seconds
        self.profile = cfg.broker.cli.profile

    # -- lifecycle -------------------------------------------------------- #
    def connect(self) -> None:
        if shutil.which(self.binary) is None:
            raise BrokerError(
                f"'{self.binary}' not on PATH. Install with:\n"
                "  brew install alpacahq/tap/cli\n"
                "  go install github.com/alpacahq/cli/cmd/alpaca@latest"
            )
        self.run(["account", "get"])

    # -- process plumbing -------------------------------------------------- #
    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        creds = self.credentials
        if creds and creds.configured:
            # Env keys take precedence over the stored profile, so the active
            # OAA profile decides which account the CLI touches.
            env["ALPACA_API_KEY"] = creds.api_key
            env["ALPACA_SECRET_KEY"] = creds.secret_key
        env["ALPACA_LIVE_TRADE"] = "false" if self.cfg.broker.paper else "true"
        env["ALPACA_OUTPUT"] = "json"
        return env

    def command(self, args: list[str]) -> list[str]:
        cmd = [self.binary, *args]
        if self.profile:
            cmd += ["--profile", self.profile]
        return cmd

    def run(self, args: list[str], parse: bool = True) -> Any:
        cmd = self.command(args)
        log.debug("$ %s", " ".join(cmd))
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env=self._env(),
            check=False,
        )
        if proc.returncode != 0:
            raise BrokerError(
                f"`{' '.join(cmd)}` exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
            )
        if not parse:
            return proc.stdout
        text = (proc.stdout or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise BrokerError(f"non-JSON output from CLI: {text[:200]}") from exc

    # -- read ------------------------------------------------------------- #
    def account(self) -> AccountSnapshot:
        acct = self.run(["account", "get"])
        positions = self.run(["position", "list"]) or []
        return AccountSnapshot(
            account_id=str(acct.get("account_number") or acct.get("id") or ""),
            equity=float(acct.get("equity") or 0),
            last_equity=float(acct.get("last_equity") or 0),
            cash=float(acct.get("cash") or 0),
            buying_power=float(acct.get("buying_power") or 0),
            options_buying_power=_f(acct.get("options_buying_power")),
            regt_buying_power=_f(acct.get("regt_buying_power")),
            daytrading_buying_power=_f(acct.get("daytrading_buying_power")),
            multiplier=_f(acct.get("multiplier")),
            shorting_enabled=acct.get("shorting_enabled"),
            daytrade_count=_i(acct.get("daytrade_count")),
            pattern_day_trader=acct.get("pattern_day_trader"),
            options_trading_level=_i(acct.get("options_trading_level")),
            positions=[_position(p) for p in positions],
        )

    def is_market_open(self) -> bool:
        try:
            return bool((self.run(["clock"]) or {}).get("is_open"))
        except BrokerError:
            return False

    # -- write ------------------------------------------------------------ #
    def submit(self, ticket: OrderTicket) -> Fill:
        if ticket.dry_run:
            log.info("DRY RUN $ %s", " ".join(self.command(self._submit_args(ticket))))
            return _dry_fill(ticket)
        payload = self.run(self._submit_args(ticket))
        return _fill(payload, ticket)

    def _submit_args(self, ticket: OrderTicket) -> list[str]:
        # Options trade in whole contracts and carry the size on the ticket;
        # equities and spot crypto carry an ABSOLUTE, possibly fractional size
        # on the leg. Reading ticket.quantity for all three sent "--qty 1" for
        # a 0.0043 BTC order - a ticket for one whole bitcoin. The size lives
        # wherever the instrument put it.
        leg0 = ticket.legs[0] if ticket.legs else None
        if leg0 is not None and leg0.is_equity and leg0.qty:
            size = _fmt_qty(leg0.qty)
        else:
            size = str(ticket.quantity)
        args = [
            "order", "submit",
            "--qty", size,
            "--type", ticket.order_type,
            "--time-in-force", ticket.time_in_force,
            "--client-order-id", ticket.client_order_id,
        ]
        if ticket.order_type == "limit" and ticket.limit_price is not None:
            args += ["--limit-price", f"{ticket.limit_price:.2f}"]

        if ticket.is_multileg:
            legs = [
                {
                    "symbol": leg.symbol,
                    "ratio_qty": str(leg.ratio),
                    "side": leg.side.value,
                    "position_intent": leg.resolved_intent().value,
                }
                for leg in ticket.legs
            ]
            args += ["--order-class", "mleg", "--legs", json.dumps(legs)]
        else:
            leg = ticket.legs[0]
            args += ["--symbol", leg.symbol, "--side", leg.side.value]
            if leg.is_option:
                # position_intent is an options concept. Alpaca rejects it on an
                # equity or crypto order outright.
                args += ["--position-intent", leg.resolved_intent().value]
        return args

    def cancel(self, order_id: str) -> bool:
        try:
            self.run(["order", "cancel", "--order-id", order_id])
            return True
        except BrokerError as exc:
            log.warning("cancel failed: %s", exc)
            return False

    def cancel_all(self) -> int:
        try:
            return len(self.run(["order", "cancel-all"]) or [])
        except BrokerError:
            return 0

    def close_position(self, symbol: str, qty: float | None = None) -> Fill | None:
        args = ["position", "close", "--symbol", symbol]
        if qty:
            # Never int() this: a crypto position is fractional, and
            # int(0.0043) is 0 - a close that closes nothing and leaves the
            # book exposed through Monday's open.
            args += ["--qty", _fmt_qty(abs(qty))]
        try:
            return _fill(self.run(args), None)
        except BrokerError as exc:
            log.error("close failed for %s: %s", symbol, exc)
            return None

    def order_by_client_id(self, client_order_id: str) -> Fill | None:
        try:
            return _fill(
                self.run(["order", "get-by-client-id", "--client-order-id", client_order_id]),
                None,
            )
        except BrokerError:
            return None

    # -- data passthrough --------------------------------------------------- #
    def option_chain(self, underlying: str, **flags: str) -> Any:
        args = ["data", "option", "chain", "--underlying-symbol", underlying,
                "--feed", self.cfg.data.option_feed]
        for key, value in flags.items():
            args += [f"--{key.replace('_', '-')}", str(value)]
        return self.run(args)


def _fmt_qty(qty: float) -> str:
    """Whole numbers stay whole; fractions keep enough precision for crypto."""
    return str(int(qty)) if float(qty).is_integer() else f"{qty:.9f}".rstrip("0")


def _position(p: dict[str, Any]) -> PositionSnapshot:
    symbol = str(p.get("symbol", ""))
    underlying = expiry = strike = right = None
    if is_occ(symbol):
        occ = parse_occ(symbol)
        underlying, expiry, strike = occ.root, occ.expiry, occ.strike
        right = Right(occ.right.value)
    return PositionSnapshot(
        symbol=symbol,
        qty=float(p.get("qty") or 0),
        avg_entry_price=float(p.get("avg_entry_price") or 0),
        market_value=float(p.get("market_value") or 0),
        unrealized_pl=float(p.get("unrealized_pl") or 0),
        unrealized_plpc=float(p.get("unrealized_plpc") or 0),
        asset_class=str(p.get("asset_class") or "us_option"),
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        right=right,
    )


def _fill(payload: dict[str, Any], ticket: OrderTicket | None) -> Fill:
    return Fill(
        order_id=str(payload.get("id", "")),
        client_order_id=payload.get("client_order_id"),
        symbol=str(payload.get("symbol") or (ticket.symbol if ticket else "")),
        status=str(payload.get("status", "unknown")),
        filled_qty=float(payload.get("filled_qty") or 0),
        filled_avg_price=_f(payload.get("filled_avg_price")),
        legs=payload.get("legs") or [],
        raw=payload,
    )


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
