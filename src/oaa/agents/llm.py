"""LLM access for the reasoning agents.

Two rules, both learned from the failure mode this event punishes hardest:

  1. The LLM is never on the critical path for safety. It scores and explains;
     the deterministic risk engine decides.
  2. If the provider is unreachable, the loop degrades to rules and keeps
     trading. Downtime costs P&L; a missing paragraph of reasoning does not.

Three providers, and the live loop and the backtest may use DIFFERENT ones.
`agents.llm` is what the live agent runs on; `backtest.critic.llm` overrides it
for replay only. The reason is cost shape rather than preference: one live
cycle is a handful of calls a day, while a replay scores every candidate over
every session and is re-run whenever a parameter moves.

Both now point at Featherless. The split survives because it is still the right
shape - the replay runs at temperature 0 with a seed and a smaller token budget,
and the run artefact records which model produced which verdicts - but there is
no longer a second vendor, a second key or a second SDK to keep alive during a
seven-day event. Gemini was removed on 28 Aug for exactly that reason.
"""

from __future__ import annotations

import abc
import json
import os
import time
from typing import Any

from oaa.config.schema import LLMConfig
from oaa.core.logging import get_logger

log = get_logger("agents.llm")


class LLMUnavailable(RuntimeError):
    pass


class _Transient(RuntimeError):
    """A failure worth retrying. Never escapes the client."""


class LLMClient(abc.ABC):
    provider = "base"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    @abc.abstractmethod
    def complete(self, system: str, user: str, tools: list[dict[str, Any]] | None = None) -> str: ...

    def json_complete(
        self, system: str, user: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ask for JSON and parse defensively - models add prose."""
        try:
            raw = self.complete(
                system + "\n\nRespond with a single JSON object and nothing else.", user
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM call failed: %s", exc)
            return default or {}
        return _extract_json(raw) or (default or {})


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable(
                "anthropic package missing - pip install -e '.[agents]'"
            ) from exc
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key, timeout=cfg.timeout_seconds)
        self._accepts = _accepted_params(self._client.messages.create)

    def _kwargs(self, **kwargs: Any) -> dict[str, Any]:
        """Drop parameters this SDK version does not accept.

        `temperature` was accepted by `messages.create` in the 0.x SDK and is
        not in 1.x. The pin in pyproject.toml is `anthropic>=0.34`, so a fresh
        install resolves to 1.x and every call raised TypeError - which
        `json_complete` swallowed, so the critic silently ran on the heuristic
        fallback and the only evidence was a WARNING line. Filtering against
        the real signature works on both, and fails loudly if something else
        breaks.
        """
        if not self._accepts:
            return kwargs
        dropped = [k for k in kwargs if k not in self._accepts]
        for name in dropped:
            kwargs.pop(name)
        if dropped:
            log.debug("anthropic SDK does not accept %s - dropped", ", ".join(dropped))
        return kwargs

    def complete(self, system: str, user: str, tools: list[dict[str, Any]] | None = None) -> str:
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if tools:
            kwargs["tools"] = tools
        response = self._client.messages.create(**self._kwargs(**kwargs))
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

    def run_tools(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        call_tool: Any,
        max_turns: int = 6,
    ) -> str:
        """Agentic loop: let the model call Alpaca MCP tools until it answers.

        `call_tool(name, arguments)` is normally McpBridge.call, so the model
        is driving the same MCP server the broker uses.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for turn in range(max_turns):
            response = self._client.messages.create(
                **self._kwargs(
                    model=self.cfg.model,
                    max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                    system=system,
                    messages=messages,
                    tools=tools,
                )
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                return "".join(
                    b.text for b in response.content if getattr(b, "type", "") == "text"
                )

            results = []
            for block in tool_uses:
                try:
                    output = call_tool(block.name, dict(block.input or {}))
                    payload = json.dumps(output, default=str)[:8000]
                    is_error = False
                except Exception as exc:  # noqa: BLE001
                    payload, is_error = f"tool error: {exc}", True
                    log.warning("tool %s failed: %s", block.name, exc)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": payload,
                    "is_error": is_error,
                })
            messages.append({"role": "user", "content": results})
            log.debug("agent turn %d: %d tool call(s)", turn + 1, len(tool_uses))
        return "reached the tool-turn limit without a final answer"


class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMUnavailable("openai package missing") from exc
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise LLMUnavailable("OPENAI_API_KEY is not set")
        self._client = OpenAI(api_key=key, timeout=cfg.timeout_seconds)

    def complete(self, system: str, user: str, tools: list[dict[str, Any]] | None = None) -> str:
        response = self._client.chat.completions.create(
            model=self.cfg.model,
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""


class FeatherlessClient(LLMClient):
    """Featherless AI - the hackathon's inference partner.

    Featherless serves open-weight models behind an OpenAI-compatible REST
    surface, so this client is deliberately written against `httpx` (already a
    core dependency) rather than the OpenAI SDK: one fewer package to install
    on a competition host, and the wire format is small enough to own.

    Why it is the LIVE provider rather than a second opinion:

      * It is the reasoning layer the agent actually runs on. Every critic
        verdict, macro read and agent cycle in the judged account is produced
        here, so the technology is load-bearing, not decorative.
      * `run_tools` implements OpenAI-style tool calling, which is what lets
        the model drive the Alpaca MCP tools directly. That is the bridge
        between the strategy layer and Alpaca: the model reads the account,
        the chain and the positions through the same seven-tool allowlist the
        broker uses, and narrates what it did. It still cannot authorise a
        trade - `RiskEngine` signs every ticket, and the router refuses
        unsigned ones.

    Everything else about the LLM contract is unchanged: if Featherless is
    unreachable the loop degrades to deterministic rules and keeps trading.
    """

    provider = "featherless"
    default_base_url = "https://api.featherless.ai/v1"
    #: Worth trying again: cold starts, rate limits, gateway blips.
    RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a core dep
            raise LLMUnavailable("httpx package missing") from exc
        env_name = cfg.api_key_env or "FEATHERLESS_API_KEY"
        key = os.getenv(env_name)
        if not key:
            raise LLMUnavailable(f"{env_name} is not set")
        self.base_url = (cfg.base_url or self.default_base_url).rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=cfg.timeout_seconds,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        self._json_mode = True

    # -- wire ---------------------------------------------------------------- #
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /chat/completions with a short retry on transient failures.

        Serverless inference cold-starts and rate-limits; a 429 or a 5xx one
        second into a 09:45 cycle must not cost the cycle its reasoning.
        """
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            try:
                response = self._client.post("/chat/completions", json=payload)
                if response.status_code >= 400:
                    detail = f"featherless returned {response.status_code}: {response.text[:200]}"
                    # A 4xx that is not a rate limit is a REQUEST problem - a
                    # model that will not accept `response_format`, a bad
                    # model id, a dead key. Retrying it three times just
                    # spends the cycle's latency budget arriving at the same
                    # answer, so it fails immediately and lets the caller
                    # choose a different shape of request.
                    if response.status_code not in self.RETRYABLE_STATUS:
                        raise LLMUnavailable(detail)
                    raise _Transient(detail)
                return response.json()
            except LLMUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt == self.cfg.max_retries - 1:
                    break
                delay = self.cfg.retry_backoff_seconds * (2**attempt)
                log.warning(
                    "featherless call failed (attempt %d/%d): %s - retrying in %.1fs",
                    attempt + 1, self.cfg.max_retries, exc, delay,
                )
                time.sleep(delay)
        raise LLMUnavailable(f"featherless unreachable: {last}") from last

    def _messages(self, system: str, user: str) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _body(self, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if self.cfg.seed is not None:
            body["seed"] = self.cfg.seed
        body.update(extra)
        return body

    @staticmethod
    def _text(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content") or ""

    # -- the LLMClient contract ---------------------------------------------- #
    def complete(self, system: str, user: str, tools: list[dict[str, Any]] | None = None) -> str:
        body = self._body(self._messages(system, user))
        if tools:
            body["tools"] = _openai_tools(tools)
        return self._text(self._post(body))

    def json_complete(
        self, system: str, user: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Native JSON mode, with the prompt-nicely path as the fallback.

        Not every open-weight model on Featherless supports `response_format`.
        The first rejection flips `_json_mode` off for the life of the process
        so we pay for that discovery once, not on every critic call.
        """
        messages = self._messages(
            system + "\n\nRespond with a single JSON object and nothing else.", user
        )
        if self._json_mode:
            try:
                data = self._post(
                    self._body(messages, response_format={"type": "json_object"})
                )
                return _extract_json(self._text(data)) or (default or {})
            except LLMUnavailable as exc:
                log.warning("featherless JSON mode failed (%s) - falling back to prompted JSON", exc)
                self._json_mode = False
        try:
            return _extract_json(self._text(self._post(self._body(messages)))) or (default or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("featherless call failed: %s", exc)
            return default or {}

    def run_tools(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        call_tool: Any,
        max_turns: int = 6,
    ) -> str:
        """Agentic loop over Alpaca's MCP tools, in the OpenAI tool-call dialect.

        `call_tool(name, arguments)` is McpBridge.call, so the model is driving
        the same MCP server the broker reads through.
        """
        messages: list[dict[str, Any]] = self._messages(system, user)
        specs = _openai_tools(tools)
        for turn in range(max_turns):
            data = self._post(self._body(messages, tools=specs))
            choices = data.get("choices") or []
            message = (choices[0].get("message") if choices else None) or {}
            calls = message.get("tool_calls") or []
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                **({"tool_calls": calls} if calls else {}),
            })
            if not calls:
                return message.get("content") or ""

            for call in calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                try:
                    payload = json.dumps(call_tool(name, arguments), default=str)[:8000]
                except Exception as exc:  # noqa: BLE001
                    payload = f"tool error: {exc}"
                    log.warning("tool %s failed: %s", name, exc)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": name,
                    "content": payload,
                })
            log.debug("featherless agent turn %d: %d tool call(s)", turn + 1, len(calls))
        return "reached the tool-turn limit without a final answer"

    def teardown(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001, S110
            pass


class NullClient(LLMClient):
    """No provider configured. The system runs rules-only and says so."""

    provider = "null"

    def complete(self, system: str, user: str, tools: list[dict[str, Any]] | None = None) -> str:
        return ""


def get_llm(cfg: LLMConfig) -> LLMClient:
    if cfg.provider is None:
        return NullClient(cfg)
    try:
        if cfg.provider == "anthropic":
            return AnthropicClient(cfg)
        if cfg.provider == "openai":
            return OpenAIClient(cfg)
        if cfg.provider == "featherless":
            return FeatherlessClient(cfg)
    except LLMUnavailable as exc:
        if not cfg.fallback_to_rules:
            raise
        log.warning("LLM unavailable (%s) - running rules-only", exc)
    return NullClient(cfg)


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped tool specs -> the OpenAI function-calling shape.

    `agents/tools.py` and the MCP bridge both emit `{name, description,
    input_schema}` because Anthropic was the first provider wired up. Rather
    than fork the tool registry per provider, the OpenAI-dialect clients
    translate at the edge. A spec that is already in the OpenAI shape passes
    through untouched, so a future provider can hand its own specs straight in.
    """
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "function" and "function" in tool:
            converted.append(tool)
            continue
        converted.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", "") or "",
                "parameters": tool.get("input_schema")
                or {"type": "object", "properties": {}},
            },
        })
    return converted


def _accepted_params(func: Any) -> set[str]:
    """The keyword arguments this SDK build actually takes, or empty if unknown."""
    import inspect

    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - C-level callables
        return set()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return set()
    return set(parameters)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None
