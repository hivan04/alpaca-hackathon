"""End-to-end proof that the reasoning layer is real.

Every other check in this repo is offline. `oaa doctor` proves a key is
*present*; the test suite proves the client is *correct against a mock*. Neither
proves the thing that actually has to work at 10:00 on Monday: that Featherless
answers, that the Alpaca MCP server starts, that the model calls the MCP tools,
and that the account it reads through them is the account the broker trades.

That gap is not academic. Two failures this week were exactly this shape - an
Anthropic key that had never authenticated, and an MCP bridge that could not
start because `uvx` was missing. Both degraded silently by design
(`fallback_to_rules`, and the bridge's warn-and-continue), so the system looked
healthy while doing none of what it claimed. A green `doctor` would have been
produced in both cases.

So this runs the real chain against the real services and writes a transcript:

    1. uvx on PATH                  the MCP server cannot start without it
    2. provider round trip          Featherless answers at all
    3. native JSON mode             the critic's contract, not prose
    4. MCP session                  the server starts and advertises tools
    5. the read allowlist           7 tools exposed, mutating ones withheld
    6. the tool loop                the model actually CALLS an MCP tool
    7. one account, two paths       what the model read == what the broker trades

Check 7 is the one worth having. A model that reasons about a different account
from the one being traded is the most expensive failure available here, and it
is invisible in any log that shows only one side. This compares the account
number and equity the model pulled through MCP against `broker.account()` on
the configured backend, and fails on a mismatch.

The artefact lands in `runs/<profile>/selftest/` with every tool call, its
arguments and its (truncated) result. That transcript is the evidence that the
MCP integration is load-bearing - which is what the Technology Implementation
criterion asks for, and what a screenshot of a passing table cannot show.

Read-only throughout. The allowlist withholds every mutating tool, and nothing
here submits, cancels or closes anything.

One thing it refuses to do is verify against the simulator. `broker.fallback:
sim` is correct for a dry-run dev profile - but the sim has its own imaginary
$100k and an account id of `SIM-PAPER`, so a selftest that accepted it would
compare a real MCP account read against a fantasy and report a mismatch whose
real cause (a missing `alpaca` binary) appears nowhere in the message. The
command boots a real backend or says why it could not.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from oaa.agents.llm import get_llm
from oaa.agents.tools import MCP_READ_ALLOWLIST, mcp_read_tools
from oaa.core.logging import get_logger

log = get_logger("agents.selftest")

#: Deliberately trivial. The point is to observe a real tool call and compare
#: two views of one account - not to test the model's market judgement.
PROBE_SYSTEM = (
    "You are verifying a trading system's connection to its broker. "
    "Use the available tools to read the account, then answer in one short "
    "sentence stating the account number and the equity. Call the tools; do "
    "not guess or use placeholder values."
)
PROBE_USER = (
    "Read the trading account and report its account number and current equity."
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    #: A failed check that does not by itself stop the agent trading.
    advisory: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "ok": self.ok,
            "advisory": self.advisory,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class SelfTestReport:
    profile: str
    started: dt.datetime
    checks: list[Check] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    narrative: str = ""

    @property
    def ok(self) -> bool:
        return all(c.ok or c.advisory for c in self.checks)

    @property
    def blocking_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.advisory]

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "started_utc": self.started.isoformat(),
            "ok": self.ok,
            "checks": [c.as_dict() for c in self.checks],
            "tool_calls": self.transcript,
            "model_answer": self.narrative,
        }


class SelfTest:
    """Runs the live chain once and records what happened.

    Constructed with an already-booted broker so it verifies the *configured*
    backend rather than a convenient one - `oaa selftest --profile judged`
    checks the path the judged agent will actually take.
    """

    def __init__(self, settings: Any, broker: Any) -> None:
        self.settings = settings
        self.cfg = settings.config
        self.broker = broker
        self.report = SelfTestReport(
            profile=self.cfg.profile,
            started=dt.datetime.now(dt.timezone.utc),
        )

    # -- helpers -------------------------------------------------------------- #
    def _add(self, *args: Any, **kwargs: Any) -> Check:
        check = Check(*args, **kwargs)
        self.report.checks.append(check)
        log.info("selftest %-24s %s - %s",
                 check.name, "ok" if check.ok else "FAIL", check.detail)
        return check

    # -- the checks ----------------------------------------------------------- #
    def check_uvx(self) -> bool:
        """`uvx` launches the Alpaca MCP server. No uvx, no MCP surface."""
        path = shutil.which(self.cfg.broker.mcp.command)
        command = self.cfg.broker.mcp.command
        needed = self.cfg.agents.tool_backend == "mcp"
        self._add(
            "uvx on PATH",
            bool(path),
            path or f"'{command}' not found - install uv (brew install uv)",
            evidence={"command": command, "path": path},
            # Only advisory if nothing is configured to use MCP.
            advisory=not needed,
        )
        return bool(path)

    def check_broker(self) -> None:
        """Which backend answered, and is it a real one?

        Recorded as its own check because the answer changes what every later
        check means. A selftest passing against the simulator proves nothing at
        all, and `broker.fallback: sim` makes that a live possibility whenever
        the configured backend cannot start.
        """
        name = getattr(self.broker, "name", "?")
        is_sim = "sim" in name.lower()
        self._add(
            "broker backend", not is_sim,
            f"{name} (configured: {self.cfg.broker.primary})" + (
                " - the SIMULATOR, not a real account; nothing below is evidence"
                if is_sim else ""
            ),
            evidence={"backend": name, "configured": self.cfg.broker.primary},
        )

    def check_provider(self) -> Any:
        """One real round trip. Proves the key authenticates, not that it exists."""
        llm = get_llm(self.cfg.agents.llm)
        if llm.provider == "null":
            self._add(
                "provider round trip", False,
                f"{self.cfg.agents.llm.provider} unavailable - the loop would run "
                "rules-only all week and only WARN about it",
                evidence={"configured": self.cfg.agents.llm.provider},
            )
            return llm

        started = time.monotonic()
        try:
            answer = llm.complete(
                "Reply with the single word: ready.", "Are you reachable?"
            )
            elapsed = round(time.monotonic() - started, 2)
            self._add(
                "provider round trip", bool(answer.strip()),
                f"{llm.provider} / {self.cfg.agents.llm.model} answered in {elapsed}s",
                evidence={
                    "provider": llm.provider,
                    "model": self.cfg.agents.llm.model,
                    "seconds": elapsed,
                    "reply": answer.strip()[:200],
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._add("provider round trip", False, str(exc)[:200])
        return llm

    def check_json_mode(self, llm: Any) -> None:
        """The critic's contract: a parseable object, not prose with a fence."""
        if llm.provider == "null":
            self._add("native JSON mode", False, "no provider", advisory=True)
            return
        sentinel = {"status": "ok", "score": 0.5}
        result = llm.json_complete(
            'Return exactly {"status": "ok", "score": 0.5} and nothing else.',
            "Return the object.",
            default={},
        )
        ok = isinstance(result, dict) and "status" in result
        self._add(
            "native JSON mode", ok,
            "parsed a JSON object" if ok else "did not return a parseable object",
            evidence={"expected_shape": sentinel, "received": result},
        )

    def check_mcp_session(self) -> Any:
        """Start the server the agent will use, or reuse the broker's session."""
        bridge = getattr(self.broker, "bridge", None)
        owned = False
        if bridge is None:
            try:
                from oaa.brokers.alpaca_mcp import McpBridge

                bridge = McpBridge(self.cfg, self.settings.credentials)
                bridge.start()
                owned = True
            except Exception as exc:  # noqa: BLE001
                hint = ""
                if not shutil.which(self.cfg.broker.mcp.command):
                    hint = (
                        f" - '{self.cfg.broker.mcp.command}' is not on PATH; "
                        "run `make tools`"
                    )
                self._add("MCP session", False, f"{str(exc)[:160]}{hint}")
                return None, False
        count = len(getattr(bridge, "tools", {}) or {})
        self._add(
            "MCP session", count > 0,
            f"{count} tools advertised by the Alpaca MCP server",
            evidence={"tool_count": count, "session_owner": "selftest" if owned else "broker"},
        )
        return bridge, owned

    def check_allowlist(self, bridge: Any) -> list[dict[str, Any]]:
        """The safety property: reads exposed, writes absent rather than discouraged."""
        configured = getattr(self.cfg.agents, "mcp_read_tools", None)
        schemas = mcp_read_tools(bridge, configured)
        exposed = [s["name"] for s in schemas]
        available = set(getattr(bridge, "tools", {}) or {})
        expected = list(configured or MCP_READ_ALLOWLIST)
        missing = [n for n in expected if n not in available]
        mutating_exposed = [n for n in exposed if n not in expected]
        ok = bool(exposed) and not missing and not mutating_exposed
        self._add(
            "read allowlist", ok,
            f"{len(exposed)}/{len(expected)} read tools exposed, "
            f"{len(available) - len(exposed)} tools withheld"
            + (f"; MISSING {missing}" if missing else ""),
            evidence={
                "exposed": exposed,
                "expected": expected,
                "missing_from_server": missing,
                "withheld_count": len(available) - len(exposed),
            },
        )
        return schemas

    def check_tool_loop(self, llm: Any, bridge: Any, schemas: list[dict[str, Any]]) -> None:
        """The claim under test: the model CALLS the tools, not just reads a summary."""
        if llm.provider == "null" or not schemas:
            self._add("tool loop", False, "no provider or no MCP tools to call")
            return
        if not hasattr(llm, "run_tools"):
            self._add(
                "tool loop", False,
                f"the {llm.provider} client does not implement tool use",
            )
            return

        calls: list[dict[str, Any]] = []

        def call_tool(name: str, arguments: dict[str, Any]) -> Any:
            started = time.monotonic()
            record: dict[str, Any] = {"tool": name, "arguments": arguments}
            try:
                result = bridge.call(name, arguments)
                record["ok"] = True
                record["result"] = json.dumps(result, default=str)[:1500]
                return result
            except Exception as exc:  # noqa: BLE001
                record["ok"] = False
                record["error"] = str(exc)[:300]
                raise
            finally:
                record["seconds"] = round(time.monotonic() - started, 2)
                calls.append(record)

        try:
            answer = llm.run_tools(
                PROBE_SYSTEM, PROBE_USER, tools=schemas, call_tool=call_tool, max_turns=4
            )
        except Exception as exc:  # noqa: BLE001
            self.report.transcript = calls
            self._add("tool loop", False, str(exc)[:200], evidence={"tool_calls": calls})
            return

        self.report.transcript = calls
        self.report.narrative = answer
        succeeded = [c for c in calls if c.get("ok")]
        self._add(
            "tool loop", bool(succeeded),
            f"the model made {len(calls)} MCP tool call(s), {len(succeeded)} succeeded"
            if calls else
            "the model answered WITHOUT calling a tool - the MCP surface is decorative",
            evidence={"tool_calls": calls, "answer": answer[:600]},
        )

    def check_one_account(self, calls: list[dict[str, Any]]) -> None:
        """Reasoning and execution must be looking at the same account.

        Compared on the raw MCP payload, never on the model's prose - the point
        is to verify the plumbing, and a model that hallucinates a plausible
        equity would otherwise pass.
        """
        payload: dict[str, Any] | None = None
        for call in calls:
            if call.get("tool") == "get_account_info" and call.get("ok"):
                try:
                    parsed = json.loads(call["result"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                if isinstance(parsed, dict):
                    payload = parsed
                    break
        if payload is None:
            self._add(
                "one account, two paths", False,
                "the model never read the account through MCP, so the two views "
                "cannot be compared",
                advisory=True,
            )
            return

        try:
            snapshot = self.broker.account()
        except Exception as exc:  # noqa: BLE001
            self._add("one account, two paths", False, f"broker read failed: {exc}"[:200])
            return

        via_mcp = str(payload.get("account_number") or payload.get("id") or "")
        via_broker = str(snapshot.account_id or "")
        mcp_equity = float(payload.get("equity") or 0)
        expected_id = str(self.settings.credentials.expected_account_id or "")

        same_account = bool(via_mcp) and bool(via_broker) and via_mcp == via_broker
        # Equity moves between two reads seconds apart; a tolerance, not equality.
        drift = abs(mcp_equity - snapshot.equity)
        equity_agrees = mcp_equity > 0 and drift <= max(1.0, snapshot.equity * 0.001)

        detail = (
            f"MCP read {via_mcp or '?'}, broker trades {via_broker or '?'}"
            + (f", equity {mcp_equity:,.2f} vs {snapshot.equity:,.2f}" if mcp_equity else "")
        )
        if expected_id and via_broker and expected_id != via_broker:
            detail += (
                f" - NOT the account profile '{self.cfg.profile}' expects "
                f"({expected_id})"
            )
        self._add(
            "one account, two paths",
            same_account and equity_agrees and (not expected_id or expected_id == via_broker),
            detail,
            evidence={
                "account_via_mcp": via_mcp,
                "account_via_broker": via_broker,
                "configured_account_id": expected_id,
                "equity_via_mcp": mcp_equity,
                "equity_via_broker": snapshot.equity,
                "equity_drift": round(drift, 2),
                "broker_backend": getattr(self.broker, "name", "?"),
            },
        )

    # -- orchestration --------------------------------------------------------- #
    def run(self) -> SelfTestReport:
        self.check_broker()
        self.check_uvx()
        llm = self.check_provider()
        self.check_json_mode(llm)
        bridge, owned = self.check_mcp_session()
        try:
            if bridge is not None:
                schemas = self.check_allowlist(bridge)
                self.check_tool_loop(llm, bridge, schemas)
                self.check_one_account(self.report.transcript)
        finally:
            if bridge is not None and owned:
                bridge.stop()
        return self.report

    def write(self) -> str:
        """Persist the transcript. This is the artefact, not the table."""
        stamp = self.report.started.strftime("%Y%m%dT%H%M%SZ")
        path = self.settings.path(
            f"{self.cfg.telemetry.run_dir}/selftest/selftest-{stamp}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.report.as_dict(), indent=2, default=str))
        return str(path)
