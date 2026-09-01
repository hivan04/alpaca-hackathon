"""Structured tools for the AI assistant.

This is the hackathon's core theme made concrete: the assistant talks to Alpaca
directly and acts through typed tools rather than by having Python decide for it.

The tool surface is deliberately split in two, and the split is the safety model:

**Read tools come from Alpaca's MCP server.** Account state, positions, orders,
option chains, quotes, market clock — the agent queries Alpaca itself, live, and
reasons over what it gets back. That is real autonomy, not a scripted pipeline
with a language model bolted on top.

**Write tools are first-party and stamped.** Every order-placing tool in this
file routes through the temporal firewall and the deterministic risk engine
before anything reaches the broker, and execution goes out over the Alpaca CLI
so each order is a shell command you can replay. The MCP server's own
`place_option_order` is *not* exposed to the model. It cannot be: an agent that
can place a raw order can bypass the 15:54 verification, and the entire point
of the firewall is that nothing can.

So the agent decides. The deterministic layer still gates.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from oaa.core.logging import get_logger
from oaa.core.mcp_compat import tool_description, tool_input_schema
from oaa.core.types import Decision, DecisionAction, TradeIdea
from oaa.firewall.lock import Book

log = get_logger("agents.tools")

#: MCP tools the model may call. Read-only by construction — anything that can
#: mutate the account is absent, not merely discouraged.
#:
#: Kept deliberately small. Every schema here is re-sent on every turn of every
#: cycle, and Alpaca's MCP server is OpenAPI-generated, so the definitions are
#: verbose. Twenty-four tools cost roughly three times what seven do, for tools
#: the agent never calls.
#:
#: `get_option_chain` is the notable omission: it has the fattest schema on the
#: server AND returns enormous results, and the strategy already fetches chains
#: through the data provider. `get_news` is deliberately kept — checking for
#: headline risk before an overnight hold is real reasoning value.
MCP_READ_ALLOWLIST: tuple[str, ...] = (
    "get_account_info",
    "get_all_positions",
    "get_orders",
    "get_clock",
    "get_stock_latest_trade",
    "get_option_latest_quote",
    "get_news",
)

#: Everything else the server exposes read-only. Not wired in by default, but
#: `agents.mcp_read_tools` can name any of these when the agent genuinely needs
#: one — widening the surface is a config change, not a code change.
MCP_READ_AVAILABLE: tuple[str, ...] = (
    "get_account_config",
    "get_portfolio_history",
    "get_account_activities",
    "get_open_position",
    "get_order_by_id",
    "get_calendar",
    "get_asset",
    "get_all_assets",
    "get_option_contracts",
    "get_option_contract",
    "get_option_chain",
    "get_option_snapshot",
    "get_option_latest_trade",
    "get_stock_bars",
    "get_stock_snapshot",
    "get_stock_latest_quote",
    "get_most_active_stocks",
    "get_market_movers",
    "search_alpaca_docs",
)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    mutating: bool = False

    def as_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


class ToolBelt:
    """First-party tools bound to a live orchestrator."""

    def __init__(self, orchestrator: Any) -> None:
        self.orch = orchestrator
        self.journal = orchestrator.journal
        self._proposals: dict[str, tuple[Any, TradeIdea]] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._register_all()

    # ------------------------------------------------------------------ #
    def specs(self, include_mutating: bool = True) -> list[ToolSpec]:
        return [
            spec for spec in self._specs.values()
            if include_mutating or not spec.mutating
        ]

    def schemas(self, include_mutating: bool = True) -> list[dict[str, Any]]:
        return [spec.as_anthropic() for spec in self.specs(include_mutating)]

    def has(self, name: str) -> bool:
        return name in self._specs

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"unknown tool '{name}'")
        args = arguments or {}
        started = dt.datetime.now(dt.timezone.utc)
        try:
            result = spec.handler(**args)
            ok, error = True, None
        except Exception as exc:  # noqa: BLE001
            result, ok, error = {"error": str(exc)}, False, str(exc)
            log.warning("tool %s failed: %s", name, exc)
        self.journal.event(
            "agent_tool", tool=name, mutating=spec.mutating, ok=ok,
            arguments=args, error=error,
            seconds=round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 3),
        )
        return result

    def _add(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    # ------------------------------------------------------------------ #
    # registrations
    # ------------------------------------------------------------------ #
    def _register_all(self) -> None:
        self._add(ToolSpec(
            name="get_firewall_status",
            description=(
                "Current state of the capital firewall: the ET session phase, the "
                "carry book's reserved capital, which transient book holds the "
                "lease and for how much, and whether the transient books are "
                "locked out. Call this FIRST in any cycle - it determines what "
                "you are allowed to do."
            ),
            input_schema=_obj({}),
            handler=self._firewall_status,
        ))

        self._add(ToolSpec(
            name="get_book_state",
            description=(
                "Account snapshot framed for the three-book model: equity, cash, "
                "Reg T and day-trading buying power, and open positions SPLIT into "
                "resident carry legs and transient legs using the position ledger."
            ),
            input_schema=_obj({}),
            handler=self._book_state,
        ))

        self._add(ToolSpec(
            name="scan_carry_candidates",
            description=(
                "Run the carry book's four hard gates (premium, trend, event, "
                "macro) across its universe and return, for each symbol, either a "
                "fully-priced structure or the gate that vetoed it and why. "
                "Read-only - it computes and explains, it does not trade. "
                "The carry book trades a FIXED universe of index and sector ETFs "
                "and NOTHING else - single names such as AAPL, NVDA or TSLA are "
                "not on this book and passing them evaluates nothing. OMIT the "
                "argument to scan the whole book, which is almost always what "
                "you want."
            ),
            input_schema=_obj({
                "symbols": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "Optional subset, and only ever a subset OF THE BOOK'S OWN "
                        "UNIVERSE. Symbols outside it are returned under "
                        "`not_in_universe` and are NOT evaluated - do not read "
                        "their absence as a veto. Omit to scan everything."
                    ),
                }
            }),
            handler=self._scan_carry,
        ))

        self._add(ToolSpec(
            name="scan_intraday_candidates",
            description=(
                "Run the intraday book's signal stack (VWAP trigger, Bollinger "
                "width filter, RSI veto, catalyst confirmation, spread gate, "
                "time-of-day gate) on SPY/QQQ and return the surviving structure "
                "or the vetoing gate. Read-only."
            ),
            input_schema=_obj({}),
            handler=self._scan_intraday,
        ))

        self._add(ToolSpec(
            name="propose_trade",
            description=(
                "Build one fully-specified structure for a symbol on a named book "
                "and return a proposal_id: legs, strikes, expiry, net credit or "
                "debit, contractual max loss, modelled round-trip cost, and the "
                "gate metrics behind it. This places NO order - it is the artefact "
                "you reason about before deciding."
            ),
            input_schema=_obj(
                {
                    "symbol": {"type": "string"},
                    "book": {
                        "type": "string",
                        "enum": ["carry", "intraday", "opportunistic"],
                    },
                },
                ["symbol"],
            ),
            handler=self._propose,
        ))

        self._add(ToolSpec(
            name="run_carry_verification",
            description=(
                "Execute the 15:45 ET sign-off: re-poll Alpaca, confirm zero "
                "residual TRANSIENT positions and zero working orders, read FRESH "
                "Reg T buying power and confirm the resident carry book's margin "
                "is covered with headroom. Failure disables the transient books "
                "for the following session."
            ),
            input_schema=_obj({}),
            handler=self._verify,
            mutating=True,
        ))

        self._add(ToolSpec(
            name="submit_proposal",
            description=(
                "Route a proposal to the broker. It passes through the capital "
                "firewall and the deterministic risk engine first; either can "
                "refuse and you cannot override them. Execution goes out over the "
                "Alpaca CLI, atomically where the venue supports a combo and "
                "otherwise legged long-first with a reverse unwind."
            ),
            input_schema=_obj(
                {"proposal_id": {"type": "string", "description": "From propose_trade."}},
                ["proposal_id"],
            ),
            handler=self._submit,
            mutating=True,
        ))

        self._add(ToolSpec(
            name="liquidate_book",
            description=(
                "Cancel all working orders and liquidate a book to cash, then POLL "
                "until flat is CONFIRMED. 'transient' is the 15:15 cutoff and "
                "leaves resident carry legs untouched; 'all' is the submission "
                "flatten and closes everything. Returns whether flat was actually "
                "confirmed, not merely requested."
            ),
            input_schema=_obj(
                {"scope": {"type": "string", "enum": ["transient", "all"]}},
                ["scope"],
            ),
            handler=self._liquidate,
            mutating=True,
        ))

        self._add(ToolSpec(
            name="get_recent_decisions",
            description=(
                "The decision journal: recent trades AND the trades that were "
                "declined, each with the rule that stopped it."
            ),
            input_schema=_obj({
                "limit": {"type": "integer", "description": "How many rows (default 15)."}
            }),
            handler=self._decisions,
        ))

        self._add(ToolSpec(
            name="get_gate_rejections",
            description=(
                "The gate-by-gate rejection log: which gate vetoed each candidate "
                "and every metric it measured. Expect the spread gate to dominate "
                "on the intraday book - that is the finding, not a bug."
            ),
            input_schema=_obj({
                "limit": {"type": "integer"},
                "book": {"type": "string", "enum": ["carry", "intraday", "opportunistic"]},
            }),
            handler=self._rejections,
        ))

    # ------------------------------------------------------------------ #
    # handlers
    # ------------------------------------------------------------------ #
    def _firewall_status(self) -> dict[str, Any]:
        firewall = self.orch.firewall
        status = firewall.status()
        status["may_open"] = {
            book.value: firewall.may_open(book)[0] for book in Book
        }
        status["explanation"] = {
            book.value: firewall.may_open(book)[1] for book in Book
        }
        return status

    def _book_state(self) -> dict[str, Any]:
        account = self.orch.broker.account()
        ledger = self.orch.firewall.ledger
        resident, transient = ledger.split(account.positions)

        def row(position: Any) -> dict[str, Any]:
            return {
                "symbol": position.symbol,
                "qty": position.qty,
                "underlying": position.underlying,
                "expiry": position.expiry.isoformat() if position.expiry else None,
                "strike": position.strike,
                "market_value": position.market_value,
                "unrealized_pl": position.unrealized_pl,
                "book": ledger.book_of(position.symbol),
            }

        return {
            "account_id": account.account_id,
            "equity": account.equity,
            "cash": account.cash,
            "day_pl": account.day_pl,
            "day_pl_pct": account.day_pl_pct,
            "regt_buying_power": account.regt_buying_power,
            "daytrading_buying_power": account.daytrading_buying_power,
            "options_buying_power": account.options_buying_power,
            "options_trading_level": account.options_trading_level,
            "open_orders": account.open_orders,
            "leverage_headroom_vs_regt": account.leverage_headroom,
            "carry_requirement": self.orch.firewall.carry_requirement(account),
            "resident_positions": [row(p) for p in resident],
            "transient_positions": [row(p) for p in transient],
        }

    # ------------------------------------------------------------------ #
    def _scan_carry(self, symbols: list[str] | None = None) -> dict[str, Any]:
        return self._scan(self.orch.carry, symbols)

    def _scan_intraday(self) -> dict[str, Any]:
        return self._scan(self.orch.intraday + self.orch.opportunistic, None)

    def _scan(self, strategies: list[Any], symbols: list[str] | None) -> dict[str, Any]:
        if not strategies:
            return {"candidates": [], "note": "no strategies enabled on that book"}
        wanted = [s.upper() for s in symbols] if symbols else sorted(
            {s for strat in strategies for s in strat.universe()}
        )
        # Symbols the caller asked for that no strategy on this book trades.
        # These are NOT vetoes and must never be reported as though the gates
        # looked at them. On 1 Sep the model, primed by discovery, called
        # scan_carry_candidates(["AAPL","MSFT","TSLA","NVDA","AMZN"]) - not one
        # of which is in vol_carry's 14-ETF universe. Every symbol was skipped
        # by the filter below, the tool returned an empty candidate list with no
        # explanation, and the model wrote "no candidates passed the carry
        # book's four hard gates" into the journal. Nothing had been evaluated.
        # An empty result and a rejected result are different answers and the
        # tool has to say which one it is.
        universe = {s for strat in strategies for s in strat.universe()}
        skipped = sorted(set(wanted) - universe)

        contexts = self.orch._gather_contexts(sorted(set(wanted) & universe))
        account = self.orch.broker.account()
        out: list[dict[str, Any]] = []
        for strategy in strategies:
            for symbol, market in contexts.items():
                if symbol not in set(strategy.universe()):
                    continue
                ctx = self.orch._context(account, contexts, strategy, 0.0, market)
                ideas = strategy.generate(ctx)
                if ideas:
                    for idea in ideas:
                        self._proposals[idea.id] = (strategy, idea)
                        out.append({
                            "symbol": symbol, "strategy": strategy.name,
                            "proposal_id": idea.id, "passed": True,
                            "structure": idea.structure.value,
                            "description": idea.describe(),
                            "max_loss": idea.max_loss, "max_profit": idea.max_profit,
                            "thesis": idea.thesis, "gates": idea.meta.get("gates"),
                        })
                else:
                    out.append({
                        "symbol": symbol, "strategy": strategy.name,
                        "passed": False,
                        "note": "vetoed - see get_gate_rejections for the gate and metrics",
                    })
        result: dict[str, Any] = {"candidates": out}
        if skipped:
            result["not_in_universe"] = skipped
            result["universe"] = sorted(universe)
            result["note"] = (
                f"{len(skipped)} requested symbol(s) are NOT on this book and were "
                f"not evaluated: {', '.join(skipped)}. This is not a veto - no gate "
                f"ran on them. Re-call with symbols from `universe`, or omit the "
                f"argument to scan the whole book."
            )
        if not out:
            result["note"] = (result.get("note", "") + " No symbol on this book "
                              "produced a candidate or a gate rejection.").strip()
        return result

    def _propose(self, symbol: str, book: str = "carry") -> dict[str, Any]:
        pool = {
            "carry": self.orch.carry,
            "intraday": self.orch.intraday,
            "opportunistic": self.orch.opportunistic,
        }.get(book, self.orch.carry)
        if not pool:
            return {"proposal": None, "reason": f"no strategy enabled on the {book} book"}

        contexts = self.orch._gather_contexts([symbol.upper()])
        market = contexts.get(symbol.upper())
        if market is None:
            return {"proposal": None, "reason": f"no market data for {symbol}"}

        account = self.orch.broker.account()
        for strategy in pool:
            ctx = self.orch._context(account, contexts, strategy, 0.0, market)
            for idea in strategy.generate(ctx):
                idea.book = strategy.capital_book
                self._proposals[idea.id] = (strategy, idea)
                return {
                    "proposal_id": idea.id,
                    "symbol": idea.symbol,
                    "book": idea.book,
                    "strategy": strategy.name,
                    "structure": idea.structure.value,
                    "description": idea.describe(),
                    "thesis": idea.thesis,
                    "net_price": idea.net_price,
                    "max_loss": idea.max_loss,
                    "max_profit": idea.max_profit,
                    "probability_of_profit": idea.probability_of_profit,
                    "confidence": idea.confidence,
                    "legs": [
                        {"symbol": leg.symbol, "side": leg.side.value, "ratio": leg.ratio}
                        for leg in idea.legs
                    ],
                    "meta": idea.meta,
                }
        return {
            "proposal": None,
            "reason": f"{symbol} did not pass the {book} book's gates - see get_gate_rejections",
        }

    def _verify(self) -> dict[str, Any]:
        return self.orch.firewall.run_carry_verification(self.orch.broker).as_dict()

    def _submit(self, proposal_id: str) -> dict[str, Any]:
        entry = self._proposals.get(proposal_id)
        if entry is None:
            return {"submitted": False, "error": f"unknown proposal_id '{proposal_id}'"}
        strategy, idea = entry
        book = Book.parse(idea.book)

        allowed, why = self.orch.firewall.may_open(book)
        if not allowed:
            self.journal.record(Decision(
                cycle="agent", action=DecisionAction.SKIP, symbol=idea.symbol,
                strategy=strategy.name, idea=idea, rationale=f"firewall refused: {why}",
            ))
            return {"submitted": False, "blocked_by": "firewall", "reason": why}

        account = self.orch.broker.account()
        verdict = self.orch.risk.evaluate(idea, account)
        if not verdict.approved:
            self.journal.record(Decision(
                cycle="agent", action=DecisionAction.SKIP, symbol=idea.symbol,
                strategy=strategy.name, idea=idea, verdict=verdict,
                rationale="risk engine refused",
            ))
            return {
                "submitted": False,
                "blocked_by": "risk_engine",
                "reasons": verdict.reasons,
                "checks": verdict.checks,
            }

        if self.orch.cfg.execution.multileg_mode == "legged" and idea.structure.is_multileg:
            from oaa.execution.combo import plan_from_idea

            outcome = self.orch.combo.execute(plan_from_idea(idea), risk_stamp=verdict.stamp)
            ok, summary = outcome.ok or outcome.dry_run, outcome.summary()
        else:
            execution = self.orch.executor.execute(idea, verdict)
            ok = execution.ok
            summary = execution.error or (
                execution.fill.status if execution.fill else "no fill"
            )

        self.journal.record(Decision(
            cycle="agent",
            action=DecisionAction.OPEN if ok else DecisionAction.SKIP,
            symbol=idea.symbol, strategy=strategy.name, idea=idea, verdict=verdict,
            rationale=summary, error=None if ok else summary,
        ))
        if ok:
            self.orch._record_open(idea, strategy)
        return {"submitted": ok, "summary": summary, "book": idea.book}

    def _liquidate(self, scope: str = "transient") -> dict[str, Any]:
        if scope == "all":
            report = self.orch.firewall.run_submission_flatten(self.orch.broker)
        else:
            report = self.orch.firewall.run_intraday_cutoff(self.orch.broker)
        return {
            "scope": scope,
            "confirmed_flat": report.confirmed_flat,
            "orders_cancelled": report.orders_cancelled,
            "positions_before": report.positions_before,
            "positions_after": report.positions_after,
            "resident_untouched": report.resident_untouched,
            "attempts": report.attempts,
            "errors": report.errors,
            "summary": report.summary(),
        }

    def _decisions(self, limit: int = 15) -> dict[str, Any]:
        rows = self.journal.decisions(limit)
        return {
            "decisions": [
                {"ts": r["ts"], "symbol": r.get("symbol"), "strategy": r.get("strategy"),
                 "action": r.get("action"), "approved": r.get("approved"),
                 "reason": (r.get("reason") or "")[:240]}
                for r in rows
            ]
        }

    def _rejections(self, limit: int = 20, book: str | None = None) -> dict[str, Any]:
        events = getattr(self.journal, "events", None)
        rows = events("gate_rejection", limit) if callable(events) else []
        if book:
            rows = [r for r in rows if r.get("book") == book]
        return {"rejections": rows[:limit]}


def mcp_read_tools(
    bridge: Any,
    allowlist: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Alpaca MCP tool schemas, filtered to the read-only allowlist.

    The filter is the safety property. `place_option_order`, `close_position`,
    `cancel_all_orders` and friends exist on the server and are deliberately
    withheld from the model — an agent that can place a raw order can bypass
    the 15:54 verification, and the whole point of that gate is that nothing can.

    It is also the cost lever: these schemas are re-sent on every turn.
    """
    names = tuple(allowlist) if allowlist else MCP_READ_ALLOWLIST
    available = getattr(bridge, "tools", {}) or {}

    schemas: list[dict[str, Any]] = []
    for name in names:
        tool = available.get(name)
        if tool is None:
            continue
        schemas.append({
            "name": name,
            "description": tool_description(tool, 600),
            "input_schema": tool_input_schema(tool),
        })

    mutating = sorted(
        set(available) - set(MCP_READ_ALLOWLIST) - set(MCP_READ_AVAILABLE)
    )
    missing = [n for n in names if n not in available]
    if missing:
        # The 30 Aug failure: a server-side toolset filter naming toolsets the
        # server did not recognise registered ZERO tools, and this function
        # dutifully returned an empty list at INFO. The agent then ran with no
        # MCP surface, which is a silent, total degradation of the read layer.
        # WARNING, so `telemetry.console: focused` cannot hide it, and so the
        # cause is named rather than left to be inferred from a count.
        log.warning(
            "MCP server is missing %d of the %d allowlisted read tools (%s). "
            "The agent will run with a reduced read surface. Check "
            "`oaa mcp-tools` and `broker.mcp.toolsets`.",
            len(missing), len(names), ", ".join(missing),
        )
    log.info(
        "MCP surface for the agent: %d read tools exposed, %d read tools available "
        "but unused, %d mutating tools withheld",
        len(schemas), len(set(available) & set(MCP_READ_AVAILABLE)), len(mutating),
    )
    return schemas


def summarise_tool_result(result: Any, limit: int = 4000) -> str:
    """Cap what comes back from a tool.

    An unbounded option chain or news dump can be tens of thousands of tokens,
    and it is re-sent on every subsequent turn of the cycle. Truncation is
    visible to the model rather than silent.
    """
    try:
        text = json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"
