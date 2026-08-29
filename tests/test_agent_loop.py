"""The trading agent's loop, against the provider contract rather than a vendor.

The regression these cover is expensive and was found live. On 28 Aug the agent
called `llm._client.messages.create(...)` - the Anthropic wire shape - while the
configured live provider was Featherless. Every cycle raised
`'Client' object has no attribute 'messages'`, fell back to deterministic rules,
and said so only in a log line. The agent had also declared itself available by
checking `hasattr(llm, "_client")`, which is true of every provider.

So the tests below assert two things a passing suite has to guarantee:

  1. the loop drives whatever `LLMClient` it is given, through `run_tools`, and
  2. a provider that cannot do tools is never declared available.
"""

from __future__ import annotations

from typing import Any

import pytest

from oaa.agents.llm import LLMClient, LLMUnavailable, NullClient
from oaa.agents.trading_agent import AgentRun, TradingAgent
from oaa.config.schema import LLMConfig


class _RecordingClient(LLMClient):
    """A provider with NO `.messages` attribute of any kind - like Featherless."""

    provider = "featherless"
    supports_tools = True

    def __init__(self) -> None:
        super().__init__(LLMConfig(provider="featherless", model="test"))
        self.seen: dict[str, Any] = {}

    def complete(self, system: str, user: str, tools: list[dict] | None = None) -> str:
        return ""

    def run_tools(self, system, user, tools, call_tool, max_turns=6, on_turn=None) -> str:
        self.seen = {"system": system, "user": user, "tools": tools, "max_turns": max_turns}
        if on_turn is not None:
            on_turn(1)
        call_tool("read_account", {})
        if on_turn is not None:
            on_turn(2)
        return "done"


class _Spec:
    def __init__(self, mutating: bool) -> None:
        self.mutating = mutating


def _agent(client: LLMClient, dispatch: Any = None) -> TradingAgent:
    """A TradingAgent with its collaborators stubbed - we are testing the loop."""
    agent = TradingAgent.__new__(TradingAgent)
    agent.orch = type("Orch", (), {"llm": client})()
    agent.max_turns = 4
    agent._result_limit = 4000
    agent._caching = False
    agent.tools = type("Belt", (), {"_specs": {"read_account": _Spec(False),
                                               "place_order": _Spec(True)}})()
    agent.dispatch = dispatch or (lambda name, args: {"ok": True, "tool": name})
    return agent


# --------------------------------------------------------------------------- #
def test_the_loop_runs_on_a_provider_with_no_anthropic_surface():
    """The 28 Aug failure, pinned: no `.messages`, and the cycle still runs."""
    client = _RecordingClient()
    assert not hasattr(client, "messages")

    run = AgentRun(cycle="carry_scan", started=None)
    narrative = _agent(client)._loop("system", "user", [{"name": "read_account"}], run)

    assert narrative == "done"
    assert run.turns == 2
    assert [c["tool"] for c in run.tool_calls] == ["read_account"]
    assert client.seen["max_turns"] == 4


def test_tool_calls_are_recorded_with_their_mutating_flag():
    """The journal's record of what the model actually did must survive the move."""
    client = _RecordingClient()

    def run_tools(system, user, tools, call_tool, max_turns=6, on_turn=None):
        call_tool("place_order", {"symbol": "SPY"})
        return "placed"

    client.run_tools = run_tools  # type: ignore[method-assign]
    run = AgentRun(cycle="intraday_scan", started=None)
    _agent(client)._loop("system", "user", [], run)

    assert run.tool_calls == [{
        "tool": "place_order", "arguments": {"symbol": "SPY"},
        "mutating": True, "error": False,
    }]


def test_a_failing_tool_is_recorded_then_re_raised_for_the_provider():
    """The provider owns the error protocol; we own the record that it happened."""
    client = _RecordingClient()

    def boom(name: str, args: dict) -> Any:
        raise RuntimeError("broker said no")

    run = AgentRun(cycle="carry_scan", started=None)
    agent = _agent(client, dispatch=boom)

    def run_tools(system, user, tools, call_tool, max_turns=6, on_turn=None):
        with pytest.raises(RuntimeError):
            call_tool("read_account", {})
        return "handled"

    client.run_tools = run_tools  # type: ignore[method-assign]
    agent._loop("system", "user", [], run)

    assert run.tool_calls[0]["error"] is True


def test_prompt_caching_is_only_applied_to_anthropic():
    """Its wire shape is meaningless to an OpenAI-dialect provider."""
    client = _RecordingClient()
    agent = _agent(client)
    agent._caching = True
    agent._loop("system", "user", [{"name": "read_account"}], AgentRun("c", None))

    assert client.seen["system"] == "system", "system must stay a plain string"
    assert "cache_control" not in client.seen["tools"][0]


# --------------------------------------------------------------------------- #
def test_availability_asks_the_contract_not_the_internals():
    """`hasattr(llm, "_client")` was true of every provider, including broken ones."""
    class HasAClientButNoToolLoop(LLMClient):
        provider = "openai"

        def __init__(self) -> None:
            super().__init__(LLMConfig(provider="openai", model="test"))
            self._client = object()

        def complete(self, system, user, tools=None) -> str:
            return ""

    assert not _agent(HasAClientButNoToolLoop()).available
    assert not _agent(NullClient(LLMConfig(provider=None, model="x"))).available
    assert _agent(_RecordingClient()).available


def test_a_provider_without_a_tool_loop_says_so_clearly():
    class Bare(LLMClient):
        provider = "bare"

        def complete(self, system, user, tools=None) -> str:
            return ""

    with pytest.raises(LLMUnavailable, match="no agentic tool loop"):
        Bare(LLMConfig(provider=None, model="x")).run_tools("s", "u", [], lambda *_: None)
