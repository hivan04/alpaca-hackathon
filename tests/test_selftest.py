"""`oaa selftest` — the command that proves the reasoning chain is real.

The command exists because two failures this week degraded silently: an
Anthropic key that had never authenticated, and an MCP bridge that could not
start. A verification command that could itself pass while the chain is broken
would be worse than none, so these tests are mostly about the FAILURE paths.

Everything here is offline. Fakes stand in for the provider, the MCP bridge and
the broker; what is under test is the selftest's judgement, not Featherless.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oaa.agents.selftest import SelfTest
from oaa.config.schema import Config
from oaa.core.types import AccountSnapshot

ACCOUNT_JSON = json.dumps({"account_number": "PA3TSH9YTFJL", "equity": "100000.00"})


class _Tool:
    """A tool object in one SDK spelling or the other.

    `mcp` 1.x used `inputSchema`, 2.x uses `input_schema`. The original fake
    hard-coded the 1.x name, so the whole suite passed green while `oaa
    selftest` blew up on the first real server. A fake that can only be right
    is worse than no fake - hence the parameter, and the test below that runs
    the happy path under both.
    """

    def __init__(self, name: str, spelling: str = "input_schema") -> None:
        self.name = name
        self.description = f"{name} description"
        setattr(self, spelling, {"type": "object", "properties": {}})


class FakeBridge:
    """Advertises the read allowlist plus a mutating tool that must stay hidden."""

    def __init__(self, tools: list[str] | None = None, result: str = ACCOUNT_JSON,
                 spelling: str = "input_schema") -> None:
        names = tools if tools is not None else [
            "get_account_info", "get_all_positions", "get_orders", "get_clock",
            "get_stock_latest_trade", "get_option_latest_quote", "get_news",
            "place_option_order", "close_position",
        ]
        self.tools = {n: _Tool(n, spelling) for n in names}
        self._result = result
        self.calls: list[str] = []

    def call(self, tool: str, arguments=None):
        self.calls.append(tool)
        return json.loads(self._result)

    def stop(self) -> None:
        return None


class FakeBroker:
    name = "fake"

    def __init__(self, bridge=None, account_id="PA3TSH9YTFJL", equity=100000.0) -> None:
        self.bridge = bridge
        self._snapshot = AccountSnapshot(account_id=account_id, equity=equity)

    def account(self) -> AccountSnapshot:
        return self._snapshot


class FakeLLM:
    """A provider that calls one tool and answers, unless told otherwise."""

    provider = "featherless"

    def __init__(self, call_tools: bool = True, tool: str = "get_account_info") -> None:
        self._call_tools = call_tools
        self._tool = tool

    def complete(self, system, user, tools=None):
        return "ready"

    def json_complete(self, system, user, default=None):
        return {"status": "ok", "score": 0.5}

    def run_tools(self, system, user, tools, call_tool, max_turns=6):
        if self._call_tools:
            call_tool(self._tool, {})
            return "The account is PA3TSH9YTFJL with equity 100000.00."
        return "The account is PA3TSH9YTFJL with equity 100000.00."


class FakeCredentials:
    def __init__(self, account_id: str = "PA3TSH9YTFJL",
                 expected_account_id: str | None = "PA3TSH9YTFJL") -> None:
        #: the submission field
        self.account_id = account_id
        #: what the ACTIVE profile should reach - the one selftest checks
        self.expected_account_id = expected_account_id
        self.profile = "judged"


class FakeSettings:
    def __init__(self, tmp_path: Path, config: Config,
                 credentials: FakeCredentials | None = None) -> None:
        self.config = config
        self.credentials = credentials or FakeCredentials()
        self._root = tmp_path

    def path(self, relative: str) -> Path:
        return self._root / relative


@pytest.fixture
def settings(tmp_path) -> FakeSettings:
    config = Config()
    config.telemetry.run_dir = "runs/test"
    return FakeSettings(tmp_path, config)


def _run(settings, llm, bridge, broker, monkeypatch, uvx: str | None = "/usr/bin/uvx"):
    monkeypatch.setattr("oaa.agents.selftest.get_llm", lambda cfg: llm)
    monkeypatch.setattr("oaa.agents.selftest.shutil.which", lambda name: uvx)
    return SelfTest(settings, broker).run()


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
def test_a_healthy_chain_passes_every_check(settings, monkeypatch):
    bridge = FakeBridge()
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch)
    assert report.ok, [c.as_dict() for c in report.checks if not c.ok]
    assert "get_account_info" in bridge.calls
    assert report.transcript and report.transcript[0]["tool"] == "get_account_info"


@pytest.mark.parametrize("spelling", ["input_schema", "inputSchema"])
def test_the_chain_works_against_either_mcp_sdk_major(settings, monkeypatch, spelling):
    """`pyproject.toml` pins `mcp>=1.2`, so a fresh install on a competition
    host can resolve to either major. Tool discovery must not care which."""
    bridge = FakeBridge(spelling=spelling)
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch)
    assert _check(report, "read allowlist").ok
    assert report.ok


def test_the_transcript_is_written_and_readable(settings, monkeypatch):
    bridge = FakeBridge()
    monkeypatch.setattr("oaa.agents.selftest.get_llm", lambda cfg: FakeLLM())
    monkeypatch.setattr("oaa.agents.selftest.shutil.which", lambda name: "/usr/bin/uvx")
    tester = SelfTest(settings, FakeBroker(bridge))
    tester.run()
    written = json.loads(Path(tester.write()).read_text())
    assert written["ok"] is True
    assert [c["check"] for c in written["checks"]][:3] == [
        "broker backend", "uvx on PATH", "provider round trip",
    ]
    # The artefact must carry the evidence, not just the verdicts.
    assert written["tool_calls"][0]["tool"] == "get_account_info"
    assert written["model_answer"]


# --------------------------------------------------------------------------- #
# the failures the command exists to catch
# --------------------------------------------------------------------------- #
def test_the_simulator_can_never_quietly_pass_a_selftest(settings, monkeypatch):
    """`broker.fallback: sim` is correct for a dry-run dev profile, so a missing
    `alpaca` binary really can hand the selftest an imaginary $100k account.
    Verifying against it would be worse than not verifying at all, so the
    backend is a check in its own right."""
    class SimBroker(FakeBroker):
        name = "sim"

    bridge = FakeBridge()
    report = _run(settings, FakeLLM(), bridge, SimBroker(bridge), monkeypatch)
    check = _check(report, "broker backend")
    assert not check.ok
    assert "SIMULATOR" in check.detail
    assert not report.ok


def test_a_real_backend_is_reported_and_passes(settings, monkeypatch):
    bridge = FakeBridge()
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch)
    check = _check(report, "broker backend")
    assert check.ok
    assert check.evidence["backend"] == "fake"


def test_a_missing_uvx_names_the_fix_when_the_mcp_session_fails(settings, monkeypatch):
    """'FileNotFoundError' is not an actionable message at 09:10 on a Monday."""
    from oaa.agents.selftest import SelfTest

    monkeypatch.setattr("oaa.agents.selftest.get_llm", lambda cfg: FakeLLM())
    monkeypatch.setattr("oaa.agents.selftest.shutil.which", lambda name: None)

    class NoBridgeBroker(FakeBroker):
        bridge = None

    def boom(*args, **kwargs):
        raise FileNotFoundError("uvx")

    monkeypatch.setattr("oaa.brokers.alpaca_mcp.McpBridge", boom, raising=False)
    report = SelfTest(settings, NoBridgeBroker(None)).run()
    check = _check(report, "MCP session")
    assert not check.ok
    assert "make tools" in check.detail


def test_a_model_that_never_calls_a_tool_fails_the_loop(settings, monkeypatch):
    """The decorative-MCP case: a plausible answer, no tool call behind it.
    This is precisely what a screenshot of a working agent would hide."""
    bridge = FakeBridge()
    report = _run(settings, FakeLLM(call_tools=False), bridge, FakeBroker(bridge), monkeypatch)
    loop = _check(report, "tool loop")
    assert not loop.ok
    assert "WITHOUT calling a tool" in loop.detail
    assert not report.ok


def test_reasoning_and_execution_on_different_accounts_fails(settings, monkeypatch):
    """The most expensive failure available here, and invisible in a log that
    shows only one side."""
    bridge = FakeBridge()
    broker = FakeBroker(bridge, account_id="PA0000DEVACCT")
    report = _run(settings, FakeLLM(), bridge, broker, monkeypatch)
    check = _check(report, "one account, two paths")
    assert not check.ok
    assert check.evidence["account_via_mcp"] != check.evidence["account_via_broker"]


def test_the_wrong_account_for_this_profile_fails_by_name(settings, monkeypatch):
    """The judged profile reaching the dev account is the failure this check
    exists for, and the message has to name which account was expected."""
    bridge = FakeBridge(result=json.dumps(
        {"account_number": "PA3CEO0Q2VQK", "equity": "100000.00"}))
    broker = FakeBroker(bridge, account_id="PA3CEO0Q2VQK")
    report = _run(settings, FakeLLM(), bridge, broker, monkeypatch)
    check = _check(report, "one account, two paths")
    assert not check.ok
    assert "PA3TSH9YTFJL" in check.detail


def test_an_unset_expected_account_does_not_fail_the_check(settings, monkeypatch):
    """Setting the dev account ID is optional. Not having one means 'nothing to
    compare', which must not read as 'wrong account' - the two views still have
    to agree with each other."""
    settings.credentials = FakeCredentials(expected_account_id=None)
    bridge = FakeBridge()
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch)
    assert _check(report, "one account, two paths").ok


def test_an_equity_mismatch_fails_even_when_the_account_matches(settings, monkeypatch):
    bridge = FakeBridge()
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge, equity=42.0), monkeypatch)
    assert not _check(report, "one account, two paths").ok


def test_the_models_prose_is_never_the_evidence(settings, monkeypatch):
    """The model says the right account number while the tool returned another.
    Comparing prose would pass this; comparing the MCP payload must not."""
    bridge = FakeBridge(result=json.dumps({"account_number": "PAWRONG", "equity": "1.0"}))
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch)
    check = _check(report, "one account, two paths")
    assert not check.ok
    assert check.evidence["account_via_mcp"] == "PAWRONG"


def test_a_dead_provider_is_a_blocking_failure(settings, monkeypatch):
    """`fallback_to_rules` means a dead key never stops the agent - it just
    silently removes the reasoning. The selftest is where that must be loud."""
    class Null:
        provider = "null"

    bridge = FakeBridge()
    report = _run(settings, Null(), bridge, FakeBroker(bridge), monkeypatch)
    check = _check(report, "provider round trip")
    assert not check.ok and not check.advisory
    assert "rules-only" in check.detail


def test_missing_uvx_blocks_when_the_agent_is_configured_for_mcp(settings, monkeypatch):
    settings.config.agents.tool_backend = "mcp"
    bridge = FakeBridge()
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch, uvx=None)
    check = _check(report, "uvx on PATH")
    assert not check.ok and not check.advisory


def test_missing_uvx_is_only_advisory_without_the_mcp_backend(settings, monkeypatch):
    settings.config.agents.tool_backend = "rest"
    bridge = FakeBridge()
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch, uvx=None)
    check = _check(report, "uvx on PATH")
    assert not check.ok and check.advisory


def test_a_mutating_tool_leaking_into_the_allowlist_fails(settings, monkeypatch):
    settings.config.agents.mcp_read_tools = ["get_account_info", "place_option_order"]
    bridge = FakeBridge()
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch)
    check = _check(report, "read allowlist")
    assert "place_option_order" in check.evidence["exposed"]
    # The allowlist check reports what is exposed; the guarantee it enforces is
    # that nothing appears there which the config did not name.
    assert check.evidence["expected"] == ["get_account_info", "place_option_order"]


def test_a_server_missing_an_expected_read_tool_fails(settings, monkeypatch):
    bridge = FakeBridge(tools=["get_account_info"])
    report = _run(settings, FakeLLM(), bridge, FakeBroker(bridge), monkeypatch)
    check = _check(report, "read allowlist")
    assert not check.ok
    assert "get_clock" in check.evidence["missing_from_server"]
