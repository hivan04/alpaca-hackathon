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
        return self.orch.llm.provider not in ("null", None) and hasattr(
            self.orch.llm, "_client"
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
        client = self.orch.llm._client  # noqa: SLF001 - deliberate, see LLMClient
        llm_cfg = self.cfg.agents.llm
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]

        # The system prompt and the tool schemas are byte-identical on every
        # turn, and a cycle runs six of them. Marking the last tool definition
        # cacheable marks everything above it too, so turns 2..n pay the cached
        # rate on the bulk of their input.
        system_blocks, cached_schemas = self._with_caching(system, schemas)

        for turn in range(self.max_turns):
            run.turns = turn + 1
            response = client.messages.create(
                model=llm_cfg.model,
                max_tokens=llm_cfg.max_tokens,
                temperature=llm_cfg.temperature,
                system=system_blocks,
                messages=messages,
                tools=cached_schemas,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                return "".join(
                    b.text for b in response.content if getattr(b, "type", "") == "text"
                )

            results = []
            for block in tool_uses:
                arguments = dict(block.input or {})
                spec = self.tools._specs.get(block.name)  # noqa: SLF001
                mutating = bool(spec and spec.mutating)
                try:
                    output = self.dispatch(block.name, arguments)
                    payload = summarise_tool_result(output, self._result_limit)
                    is_error = False
                except Exception as exc:  # noqa: BLE001
                    payload, is_error = f"tool error: {exc}", True
                run.tool_calls.append({
                    "tool": block.name,
                    "arguments": arguments,
                    "mutating": mutating,
                    "error": is_error,
                })
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": payload,
                    "is_error": is_error,
                })
            messages.append({"role": "user", "content": results})
            log.debug("agent turn %d: %d tool call(s)", turn + 1, len(tool_uses))

        return "reached the tool-turn limit without a final answer"

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
