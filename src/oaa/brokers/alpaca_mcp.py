"""Alpaca MCP server backend.

The MCP server (`uvx alpaca-mcp-server`) exposes ~72 tools generated from
Alpaca's OpenAPI specs. This backend speaks MCP to it, which is what the
hackathon's Technology Implementation criterion asks for, and it doubles as the
tool surface the LLM agents call - same server, same session.

The MCP protocol is async; this class owns a background event loop so the rest
of the codebase can stay synchronous.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from oaa.brokers.alpaca_rest import _dry_fill
from oaa.brokers.base import Broker, broker_registry
from oaa.core.errors import BrokerError
from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot, Fill, OrderTicket, PositionSnapshot, Right
from oaa.options.occ import is_occ, parse_occ

log = get_logger("brokers.mcp")


class McpBridge:
    """Sync facade over an MCP stdio (or HTTP) session.

    Runs one asyncio loop on a daemon thread and marshals calls onto it, so
    `call("get_account_info")` is an ordinary blocking function.
    """

    def __init__(self, cfg: Any, credentials: Any = None) -> None:
        self.cfg = cfg
        self.credentials = credentials
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._stack: Any = None
        self._tools: dict[str, Any] = {}

    # -- env / transport ---------------------------------------------------- #
    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        creds = self.credentials
        if creds and creds.configured:
            env["ALPACA_API_KEY"] = creds.api_key
            env["ALPACA_SECRET_KEY"] = creds.secret_key
        env["ALPACA_PAPER_TRADE"] = "true" if self.cfg.broker.paper else "false"
        toolsets = self.cfg.broker.mcp.toolsets
        if toolsets:
            env["ALPACA_TOOLSETS"] = ",".join(toolsets)
        return env

    # -- lifecycle ---------------------------------------------------------- #
    def start(self, timeout: float = 60.0) -> None:
        if self._session is not None:
            return
        try:
            import mcp  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise BrokerError(
                "the `mcp` package is required for the MCP backend - "
                "run `pip install -e '.[agents]'`"
            ) from exc

        ready = threading.Event()
        error: list[BaseException] = []

        def runner() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._open(ready, error))
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=runner, name="oaa-mcp", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            raise BrokerError("MCP server did not become ready in time")
        if error:
            raise BrokerError(f"MCP server failed to start: {error[0]}")

    async def _open(self, ready: threading.Event, error: list[BaseException]) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        try:
            self._stack = AsyncExitStack()
            mcp_cfg = self.cfg.broker.mcp
            if mcp_cfg.transport == "streamable-http":
                from mcp.client.streamable_http import streamablehttp_client

                read, write, _ = await self._stack.enter_async_context(
                    streamablehttp_client(mcp_cfg.url)
                )
            else:
                params = StdioServerParameters(
                    command=mcp_cfg.command, args=list(mcp_cfg.args), env=self._env()
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))

            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            listed = await self._session.list_tools()
            self._tools = {t.name: t for t in listed.tools}
            log.info("MCP session ready: %d tools", len(self._tools))
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)
        finally:
            ready.set()

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._session = None
        self._loop = None

    # -- calling ------------------------------------------------------------ #
    @property
    def tools(self) -> dict[str, Any]:
        return self._tools

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Anthropic-shaped tool definitions, for handing to the agent layer."""
        return [
            {
                "name": t.name,
                "description": (t.description or "")[:1000],
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            }
            for t in self._tools.values()
        ]

    def call(self, tool: str, arguments: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        if self._session is None or self._loop is None:
            raise BrokerError("MCP session is not running - call start() first")
        if tool not in self._tools:
            raise BrokerError(
                f"MCP server does not expose '{tool}'. Available: "
                f"{', '.join(sorted(self._tools)[:15])}..."
            )
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool, arguments or {}), self._loop
        )
        result = future.result(timeout)
        return _unwrap(result)


def _unwrap(result: Any) -> Any:
    """MCP returns content blocks; we want the payload."""
    if getattr(result, "isError", False):
        raise BrokerError(f"MCP tool error: {_text(result)}")
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    text = _text(result)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _text(result: Any) -> str:
    return "\n".join(
        block.text for block in (getattr(result, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


@broker_registry.register("mcp")
class AlpacaMcpBroker(Broker):
    name = "alpaca-mcp"
    supports_multileg = True

    def __init__(self, cfg: Any, credentials: Any = None) -> None:
        super().__init__(cfg, credentials)
        self.bridge = McpBridge(cfg, credentials)

    def connect(self) -> None:
        self.bridge.start()
        self.bridge.call("get_account_info")

    def close(self) -> None:
        self.bridge.stop()

    # -- read ------------------------------------------------------------- #
    def account(self) -> AccountSnapshot:
        acct = _obj(self.bridge.call("get_account_info"))
        raw_positions = self.bridge.call("get_all_positions")
        positions = raw_positions if isinstance(raw_positions, list) else \
            (raw_positions or {}).get("positions", [])
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
            positions=[_position(p) for p in positions if isinstance(p, dict)],
        )

    def is_market_open(self) -> bool:
        try:
            return bool(_obj(self.bridge.call("get_clock")).get("is_open"))
        except Exception:  # noqa: BLE001
            return False

    # -- write ------------------------------------------------------------ #
    def submit(self, ticket: OrderTicket) -> Fill:
        args: dict[str, Any] = {
            "qty": str(ticket.quantity),
            "type": ticket.order_type,
            "time_in_force": ticket.time_in_force,
            "client_order_id": ticket.client_order_id,
        }
        if ticket.order_type == "limit" and ticket.limit_price is not None:
            args["limit_price"] = f"{ticket.limit_price:.2f}"

        if ticket.is_multileg:
            args["order_class"] = "mleg"
            args["legs"] = [
                {
                    "symbol": leg.symbol,
                    "ratio_qty": str(leg.ratio),
                    "side": leg.side.value,
                    "position_intent": leg.resolved_intent().value,
                }
                for leg in ticket.legs
            ]
        else:
            leg = ticket.legs[0]
            args |= {
                "symbol": leg.symbol,
                "side": leg.side.value,
                "position_intent": leg.resolved_intent().value,
            }

        if ticket.dry_run:
            log.info("DRY RUN mcp place_option_order %s", json.dumps(args, default=str))
            return _dry_fill(ticket)

        payload = _obj(self.bridge.call("place_option_order", args))
        return Fill(
            order_id=str(payload.get("id", "")),
            client_order_id=payload.get("client_order_id"),
            symbol=str(payload.get("symbol") or ticket.symbol),
            status=str(payload.get("status", "unknown")),
            filled_qty=float(payload.get("filled_qty") or 0),
            filled_avg_price=_f(payload.get("filled_avg_price")),
            legs=payload.get("legs") or [],
            raw=payload,
        )

    def cancel(self, order_id: str) -> bool:
        try:
            self.bridge.call("cancel_order_by_id", {"order_id": order_id})
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel failed: %s", exc)
            return False

    def cancel_all(self) -> int:
        try:
            result = self.bridge.call("cancel_all_orders")
            return len(result) if isinstance(result, list) else 0
        except Exception:  # noqa: BLE001
            return 0

    def close_position(self, symbol: str, qty: float | None = None) -> Fill | None:
        args: dict[str, Any] = {"symbol_or_asset_id": symbol}
        if qty:
            args["qty"] = str(int(abs(qty)))
        try:
            payload = _obj(self.bridge.call("close_position", args))
            return Fill(
                order_id=str(payload.get("id", "")),
                symbol=symbol,
                status=str(payload.get("status", "unknown")),
                raw=payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("close_position failed: %s", exc)
            return None

    # -- data passthrough --------------------------------------------------- #
    def option_chain(self, underlying: str, **kwargs: Any) -> Any:
        return self.bridge.call(
            "get_option_chain", {"underlying_symbol": underlying, **kwargs}
        )


def _obj(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


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
