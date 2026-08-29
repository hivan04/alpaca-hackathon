"""The AI trading assistant.

An agentic loop where Claude drives: it queries Alpaca through the MCP server,
reasons over what comes back, and acts through stamped first-party tools. The
deterministic firewall and risk engine sit underneath every write, so autonomy
and safety are not in tension — the model has real latitude over *what* to
trade and none at all over *whether the rules apply*.

If no LLM is configured, `run_cycle` falls through to the orchestrator's
deterministic path. The system keeps trading either way; a missing API key
costs reasoning quality, not uptime.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oaa.agents import prompts
from oaa.agents.tools import ToolBelt, mcp_read_tools, summarise_tool_result
from oaa.core.logging import get_logger

log = get_logger("agents.trader")


@dataclass
class AgentRun:
    cycle: str
    started: dt.datetime
    phase: str = ""
    turns: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    narrative: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        mutating = sum(1 for c in self.tool_calls if c.get("mutating"))
        return (
            f"[agent:{self.cycle}] {self.turns} turn(s), "
            f"{len(self.tool_calls)} tool call(s) ({mutating} mutating)"
            + (f" | ERROR {self.error}" if self.error else "")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "phase": self.phase,
            "started": self.started.isoformat(),
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "narrative": self.narrative,
            "error": self.error,
        }


class TradingAgent:
    """Claude, wired to Alpaca through MCP reads and stamped first-party writes."""

    def __init__(self, orchestrator: Any, mcp_bridge: Any = None, max_turns: int = 12) -> None:
        self.orch = orchestrator
        self.cfg = orchestrator.cfg
        self.journal = orchestrator.journal
        self.tools = ToolBelt(orchestrator)
        self.max_turns = max_turns
        self.bridge = mcp_bridge or self._discover_bridge()
        self._mcp_schemas = (
            mcp_read_tools(self.bridge, getattr(self.cfg.agents, "mcp_read_tools", None))
            if self.bridge else []
        )
        self._result_limit = getattr(self.cfg.agents, "max_tool_result_chars", 4000)
        self._caching = bool(getattr(self.cfg.agents, "prompt_caching", True))

        log.info(
            "trading agent ready: %d first-party tools, %d MCP read tools, llm=%s",
            len(self.tools.specs()), len(self._mcp_schemas), self.orch.llm.provider,
        )

    # ------------------------------------------------------------------ #
    def _discover_bridge(self) -> Any:
        """Reuse the broker's MCP session when there is one; otherwise open one.

        Sharing matters: one stdio process, one set of credentials, and the
        agent sees exactly the account the broker is trading.
        """
        bridge = getattr(self.orch.broker, "bridge", None)
        if bridge is not None:
            return bridge
        if self.cfg.agents.tool_backend != "mcp":
            return None
        try:
            from oaa.brokers.alpaca_mcp import McpBridge

            bridge = McpBridge(self.cfg, self.orch.settings.credentials)
            bridge.start()
            return bridge
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "could not start an MCP session for the agent (%s) - "
                "running with first-party tools only", exc,
            )
            return None

    @property
    def available(self) -> bool:
        """Can this provider actually drive a tool loop?

        Asks the LLMClient contract, not the object's internals. The old check
        was `hasattr(llm, "_client")`, which every provider satisfies - the
        Featherless client holds an httpx session under that name - so the
        agent declared itself available and then died on the first turn.
        """
        return self.orch.llm.provider not in ("null", None) and bool(
            getattr(self.orch.llm, "supports_tools", False)
        )

    def schemas(self, include_mutating: bool = True) -> list[dict[str, Any]]:
        return self.tools.schemas(include_mutating) + self._mcp_schemas

    # ------------------------------------------------------------------ #
    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        """Route a tool call: first-party if we own it, MCP otherwise."""
        if self.tools.has(name):
            return self.tools.call(name, arguments)
        if self.bridge is None:
            raise KeyError(f"tool '{name}' is not available in this session")
        result = self.bridge.call(name, arguments)
        self.journal.event("agent_tool", tool=name, mutating=False, ok=True, via="mcp")
        return result

    # ------------------------------------------------------------------ #
    def run_cycle(self, cycle: str) -> AgentRun:
        """Run one agent-driven cycle, or fall back to the deterministic path."""
        phase = self.orch.firewall.phase().value
        run = AgentRun(
            cycle=cycle, started=dt.datetime.now(dt.timezone.utc), phase=phase
        )

        if not self.available:
            log.info("no LLM available - running the deterministic %s cycle", cycle)
            result = self.orch.run_cycle(_deterministic_action(cycle), cycle)
            run.narrative = result.summary()
            return run

        system, user = self._prompt_for(cycle, phase)
        include_mutating = cycle not in {"overnight_signal"}
        schemas = self.schemas(include_mutating=include_mutating)

        try:
            run.narrative = self._loop(system, user, schemas, run)
        except Exception as exc:  # noqa: BLE001
            run.error = str(exc)
            log.exception("agent cycle '%s' failed: %s", cycle, exc)
            # A broken agent must not cost the cycle. Fall back deterministically.
            try:
                result = self.orch.run_cycle(_deterministic_action(cycle), f"{cycle}-fallback")
                run.narrative = f"agent failed ({exc}); deterministic fallback: {result.summary()}"
            except Exception as inner:  # noqa: BLE001
                log.exception("deterministic fallback also failed: %s", inner)

        self.journal.event("agent_run", **run.as_dict())
        log.info(run.summary())
        return run

    # ------------------------------------------------------------------ #
    def _loop(
        self,
        system: str,
        user: str,
        schemas: list[dict[str, Any]],
        run: AgentRun,
    ) -> str:
        """One agent cycle, through the provider-agnostic `run_tools` contract.

        Everything vendor-shaped - message format, tool-call dialect, retries -
        lives in the LLMClient. What stays here is what is ours: dispatching a
        call through the ToolBelt, and recording every call on the AgentRun so
        the journal shows what the model actually did.
        """
        llm = self.orch.llm

        # Prompt caching is an Anthropic feature and its wire shape (system as
        # a block list, `cache_control` on a tool spec) is meaningless to an
        # OpenAI-dialect provider. Guarded, not assumed.
        system_arg: Any = system
        tool_specs = schemas
        if llm.provider == "anthropic":
            system_arg, tool_specs = self._with_caching(system, schemas)

        def call_tool(name: str, arguments: dict[str, Any]) -> str:
            spec = self.tools._specs.get(name)  # noqa: SLF001
            record = {
                "tool": name,
                "arguments": arguments,
                "mutating": bool(spec and spec.mutating),
                "error": False,
            }
            try:
                output = self.dispatch(name, arguments)
            except Exception:
                record["error"] = True
                run.tool_calls.append(record)
                raise
            run.tool_calls.append(record)
            return summarise_tool_result(output, self._result_limit)

        def on_turn(turn: int) -> None:
            run.turns = turn

        return llm.run_tools(
            system_arg, user, tool_specs, call_tool,
            max_turns=self.max_turns, on_turn=on_turn,
        )

    # ------------------------------------------------------------------ #
    def _with_caching(
        self, system: str, schemas: list[dict[str, Any]]
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Attach cache_control breakpoints, when enabled.

        Caveat worth knowing: the default cache lives about five minutes, so
        this saves within a cycle (one full-price turn, then cached reads) and
        not across cycles hours apart. That is where the cost is anyway — the
        per-turn re-send is what multiplies.
        """
        if not self._caching:
            return system, schemas

        cache = {"type": "ephemeral"}
        system_blocks = [{"type": "text", "text": system, "cache_control": cache}]
        if not schemas:
            return system_blocks, schemas

        marked = [dict(schema) for schema in schemas]
        marked[-1]["cache_control"] = cache
        return system_blocks, marked

    def _prompt_for(self, cycle: str, phase: str) -> tuple[str, str]:
        now = self.orch.firewall.clock.now().strftime("%Y-%m-%d %H:%M")
        table = {
            "carry_scan": prompts.TRADER_CARRY,
            "intraday_scan": prompts.TRADER_INTRADAY,
            "scan_and_trade": prompts.TRADER_INTRADAY,
            "intraday_cutoff": prompts.TRADER_CUTOFF,
        }
        template = table.get(cycle, prompts.TRADER_INTRADAY)
        pending = getattr(self.orch, "_open_ideas", {})
        proposals = "\n".join(
            f"  - {idea.id}: {idea.describe()} (max loss ${idea.max_loss or 0:,.0f})"
            for idea in list(pending.values())[-5:]
        ) or "  (none yet - scan first)"

        user = template.format(now=now, phase=phase, proposals=proposals)
        return prompts.TRADER_SYSTEM, user


def _deterministic_action(cycle: str) -> str:
    """Map an agent cycle onto the plain orchestrator handler."""
    return {
        "carry_scan": "carry_scan",
        "carry_verify": "carry_verify",
        "intraday_scan": "intraday_scan",
        "intraday_cutoff": "intraday_cutoff",
        "submission_flatten": "submission_flatten",
        "scan_and_trade": "scan_and_trade",
    }.get(cycle, "scan_and_trade")
