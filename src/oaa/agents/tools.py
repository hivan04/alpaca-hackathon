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
                "Current state of the temporal firewall: the ET session phase, "
                "which book holds the capital lock, the verified budget, and "
                "whether the intraday book is locked out for the day. Call this "
                "FIRST in any cycle - it determines what you are allowed to do."
            ),
            input_schema=_obj({}),
            handler=self._firewall_status,
        ))

        self._add(ToolSpec(
            name="get_book_state",
            description=(
                "Account snapshot framed for the two-book model: equity, cash, "
                "Reg T (overnight, 2x) and day-trading (intraday, 4x) buying "
                "power, open positions split into equity and options, and the "
                "leverage headroom against the overnight limit."
            ),
            input_schema=_obj({}),
            handler=self._book_state,
        ))

        self._add(ToolSpec(
            name="list_pair_universe",
            description=(
                "The approved cointegrated pairs for the overnight book, with "
                "their offline screen statistics (p-value, half-life, seed hedge "
                "ratio). This universe is fixed offline on purpose; you cannot "
                "add to it."
            ),
            input_schema=_obj({}),
            handler=self._pair_universe,
        ))

        self._add(ToolSpec(
            name="compute_pair_signal",
            description=(
                "Run the Kalman filter and the two-stage gap model for one pair "
                "and return tonight's forecast: the dynamic hedge ratio, the "
                "spread z-score, the expected overnight return (q50) and the "
                "5th/95th percentile tails that set the option strikes. "
                "Read-only - computes, does not trade."
            ),
            input_schema=_obj(
                {"pair": {
                    "type": "string",
                    "description": "Pair name as 'LEFT/RIGHT', e.g. 'KO/PEP'.",
                }},
                ["pair"],
            ),
            handler=self._pair_signal,
        ))

        self._add(ToolSpec(
            name="propose_overnight_trade",
            description=(
                "Build a fully-specified overnight pairs trade for one pair: "
                "direction, share counts, the protective put and call strikes "
                "chosen from the modelled tails, the contractual maximum loss "
                "and the expected profit. Returns a proposal_id. This places NO "
                "order - it is the artefact you reason about before deciding."
            ),
            input_schema=_obj(
                {"pair": {"type": "string", "description": "Pair name as 'LEFT/RIGHT'."}},
                ["pair"],
            ),
            handler=self._propose,
        ))

        self._add(ToolSpec(
            name="run_firewall_verification",
            description=(
                "Execute the 15:54 ET pre-trade gate: re-poll Alpaca, confirm "
                "zero open positions and zero working orders, read FRESH Reg T "
                "buying power, size against it and acquire the capital lock. "
                "If rogue intraday positions are found it liquidates them and "
                "aborts the night. This must pass before any overnight order."
            ),
            input_schema=_obj({
                "target_trade_value": {
                    "type": "number",
                    "description": "Intended gross notional, to be downscaled if it exceeds Reg T.",
                }
            }),
            handler=self._verify,
            mutating=True,
        ))

        self._add(ToolSpec(
            name="submit_overnight_trade",
            description=(
                "Route a proposal to the broker. It passes through the temporal "
                "firewall and the deterministic risk engine first; either can "
                "refuse and you cannot override them. Execution is a "
                "rollback-safe combo over the Alpaca CLI: protective options "
                "first, then the equity legs, unwinding anything that filled if "
                "a critical step fails."
            ),
            input_schema=_obj(
                {"proposal_id": {
                    "type": "string",
                    "description": "The id returned by propose_overnight_trade.",
                }},
                ["proposal_id"],
            ),
            handler=self._submit,
            mutating=True,
        ))

        self._add(ToolSpec(
            name="liquidate_book",
            description=(
                "Cancel all working orders and liquidate a book to cash, then "
                "POLL until flat is confirmed. 'intraday' is the 15:15 hard "
                "cutoff; 'overnight' is the 09:35 exit. Returns whether flat "
                "was actually confirmed, not merely requested."
            ),
            input_schema=_obj(
                {"book": {"type": "string", "enum": ["intraday", "overnight"]}},
                ["book"],
            ),
            handler=self._liquidate,
            mutating=True,
        ))

        self._add(ToolSpec(
            name="get_recent_decisions",
            description=(
                "The decision journal: recent trades AND the trades that were "
                "declined, each with the rule that stopped it. Useful for "
                "checking whether a pattern is being repeatedly rejected."
            ),
            input_schema=_obj({
                "limit": {"type": "integer", "description": "How many rows (default 15)."}
            }),
            handler=self._decisions,
        ))

    # ------------------------------------------------------------------ #
    # handlers
    # ------------------------------------------------------------------ #
    def _firewall_status(self) -> dict[str, Any]:
        firewall = self.orch.firewall
        status = firewall.status()
        status["intraday_may_open"] = firewall.may_open(Book.INTRADAY)[0]
        status["overnight_may_open"] = firewall.may_open(Book.OVERNIGHT)[0]
        status["explanation"] = {
            "intraday": firewall.may_open(Book.INTRADAY)[1],
            "overnight": firewall.may_open(Book.OVERNIGHT)[1],
        }
        return status

    def _book_state(self) -> dict[str, Any]:
        account = self.orch.broker.account()
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
            "shorting_enabled": account.shorting_enabled,
            "is_flat": account.is_flat,
            "open_orders": account.open_orders,
            "leverage_headroom_vs_regt": account.leverage_headroom,
            "equity_positions": [
                {"symbol": p.symbol, "qty": p.qty, "market_value": p.market_value,
                 "unrealized_pl": p.unrealized_pl}
                for p in account.equity_positions()
            ],
            "option_positions": [
                {"symbol": p.symbol, "qty": p.qty, "underlying": p.underlying,
                 "expiry": p.expiry.isoformat() if p.expiry else None,
                 "strike": p.strike, "unrealized_pl": p.unrealized_pl}
                for p in account.option_positions()
            ],
        }

    def _pair_universe(self) -> dict[str, Any]:
        strategy = self._overnight_strategy()
        return {
            "pairs": [
                {
                    "pair": spec.name, "left": spec.left, "right": spec.right,
                    "seed_hedge_ratio": spec.hedge_ratio, "pvalue": spec.pvalue,
                    "half_life_days": spec.half_life_days, "notes": spec.notes,
                }
                for spec in strategy.pairs()
            ],
            "screen": strategy.p("pairs_meta", {}),
        }

    def _pair_signal(self, pair: str) -> dict[str, Any]:
        strategy = self._overnight_strategy()
        spec = self._find_pair(strategy, pair)
        ctx = self._context_for(strategy, [spec.left, spec.right])
        state = strategy._state_for(spec, ctx.require(spec.left), ctx.require(spec.right))
        kalman = state.kalman.state
        forecast = state.last_forecast
        return {
            "pair": spec.name,
            "kalman": kalman.as_dict(),
            "filter_ready": state.kalman.ready,
            "forecast": forecast.as_dict() if forecast else None,
            "model": state.model.summary(),
            "gate": strategy._entry_gate(spec, kalman, forecast, state) if forecast else "no forecast",
        }

    def _propose(self, pair: str) -> dict[str, Any]:
        strategy = self._overnight_strategy()
        spec = self._find_pair(strategy, pair)
        ctx = self._context_for(strategy, [spec.left, spec.right])
        idea = strategy._evaluate_pair(spec, ctx)
        if idea is None:
            return {
                "pair": spec.name,
                "proposal": None,
                "reason": "the pair did not pass its entry gates - see compute_pair_signal",
            }
        idea.book = "overnight"
        self._proposals[idea.id] = (strategy, idea)
        return {
            "proposal_id": idea.id,
            "pair": spec.name,
            "structure": idea.structure.value,
            "description": idea.describe(),
            "thesis": idea.thesis,
            "max_loss": idea.max_loss,
            "expected_profit": idea.max_profit,
            "confidence": idea.confidence,
            "legs": [
                {"symbol": leg.symbol, "side": leg.side.value,
                 "kind": leg.kind.value, "qty": leg.qty}
                for leg in idea.legs
            ],
            "meta": idea.meta,
        }

    def _verify(self, target_trade_value: float | None = None) -> dict[str, Any]:
        verdict = self.orch.firewall.run_overnight_verification(
            self.orch.broker, target_trade_value=target_trade_value
        )
        return verdict.as_dict()

    def _submit(self, proposal_id: str) -> dict[str, Any]:
        entry = self._proposals.get(proposal_id)
        if entry is None:
            return {"submitted": False, "error": f"unknown proposal_id '{proposal_id}'"}
        strategy, idea = entry

        allowed, why = self.orch.firewall.may_open(Book.OVERNIGHT)
        if not allowed:
            self.journal.record(Decision(
                cycle="agent", action=DecisionAction.SKIP, symbol=idea.symbol,
                strategy=strategy.name, idea=idea,
                rationale=f"firewall refused: {why}",
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

        from oaa.execution.combo import plan_from_idea

        outcome = self.orch.combo.execute(plan_from_idea(idea), risk_stamp=verdict.stamp)
        self.journal.record(Decision(
            cycle="agent",
            action=DecisionAction.OPEN if outcome.ok else DecisionAction.SKIP,
            symbol=idea.symbol, strategy=strategy.name, idea=idea, verdict=verdict,
            rationale=outcome.summary(),
            error=None if outcome.ok else outcome.summary(),
        ))
        if outcome.ok:
            self.orch.risk.record_open()
        return {
            "submitted": outcome.ok or outcome.dry_run,
            "dry_run": outcome.dry_run,
            "summary": outcome.summary(),
            "steps": [
                {"label": s.label, "status": s.status.value,
                 "order_id": s.fill.order_id if s.fill else None}
                for s in outcome.plan.ordered()
            ],
            "unwind_errors": outcome.unwind_errors,
        }

    def _liquidate(self, book: str) -> dict[str, Any]:
        target = Book(book)
        if target is Book.OVERNIGHT:
            report = self.orch.firewall.run_overnight_exit(self.orch.broker)
        else:
            report = self.orch.firewall.run_intraday_cutoff(self.orch.broker)
        return {
            "book": book,
            "confirmed_flat": report.confirmed_flat,
            "orders_cancelled": report.orders_cancelled,
            "positions_before": report.positions_before,
            "positions_after": report.positions_after,
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

    # ------------------------------------------------------------------ #
    def _overnight_strategy(self) -> Any:
        for strategy in self.orch.overnight:
            if hasattr(strategy, "pairs"):
                return strategy
        raise RuntimeError(
            "no overnight pairs strategy is enabled - check `strategies` in config"
        )

    @staticmethod
    def _find_pair(strategy: Any, pair: str) -> Any:
        wanted = pair.replace(" ", "").upper()
        for spec in strategy.pairs():
            if spec.name.upper() == wanted or f"{spec.right}/{spec.left}" == wanted:
                return spec
        known = ", ".join(s.name for s in strategy.pairs())
        raise ValueError(f"unknown pair '{pair}'. Approved universe: {known}")

    def _context_for(self, strategy: Any, symbols: list[str]) -> Any:
        from oaa.strategies.base import StrategyContext

        contexts = self.orch._gather_contexts(symbols)
        return StrategyContext(
            account=self.orch.broker.account(),
            config=self.orch.cfg,
            contexts=contexts,
            params=strategy.params,
            budget=self.orch.firewall.budget_for(Book.OVERNIGHT),
            firewall=self.orch.firewall,
        )


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
            "description": (tool.description or "")[:600],
            "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
        })

    mutating = sorted(
        set(available) - set(MCP_READ_ALLOWLIST) - set(MCP_READ_AVAILABLE)
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
