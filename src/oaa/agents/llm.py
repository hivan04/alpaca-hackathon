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
every session and is re-run whenever a parameter moves. Pointing the replay at
a cheap model and the live loop at the good one is the whole point of the
split, and the run artefact records which model produced which verdicts.
"""

from __future__ import annotations

import abc
import json
import os
from typing import Any

from oaa.config.schema import LLMConfig
from oaa.core.logging import get_logger

log = get_logger("agents.llm")


class LLMUnavailable(RuntimeError):
    pass


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


class GeminiClient(LLMClient):
    """Google Gemini, via the `google-genai` SDK.

    Two things this provider gives that matter for a backtest specifically:

      * **native JSON mode.** `response_mime_type="application/json"` makes the
        model return a parseable object instead of prose with a code fence
        around it, so `json_complete` stops guessing.
      * **a seed.** Set `agents.llm.seed` (or the backtest's own) and repeated
        calls are far more reproducible. A backtest whose numbers move when you
        re-run it is not a backtest - and the replay caches every verdict on
        disk anyway, so the seed is the second line of defence, not the first.
    """

    provider = "gemini"

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        try:
            from google import genai
        except ImportError as exc:
            raise LLMUnavailable(
                "google-genai package missing - pip install -e '.[agents]'"
            ) from exc
        key = os.getenv(cfg.api_key_env or "GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise LLMUnavailable(
                f"{cfg.api_key_env or 'GEMINI_API_KEY'} is not set "
                "(GOOGLE_API_KEY is also accepted)"
            )
        self._genai = genai
        self._client = genai.Client(api_key=key)

    def _config(self, system: str, json_mode: bool = False) -> Any:
        from google.genai import types

        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "temperature": self.cfg.temperature,
            "max_output_tokens": self.cfg.max_tokens,
        }
        if self.cfg.seed is not None:
            kwargs["seed"] = self.cfg.seed
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        return types.GenerateContentConfig(**kwargs)

    def complete(
        self, system: str, user: str, tools: list[dict[str, Any]] | None = None
    ) -> str:
        if tools:
            # The live MCP agent loop is Anthropic-only. Gemini is wired up for
            # the critic, which needs no tools - saying so beats silently
            # dropping them and returning a toolless answer.
            raise LLMUnavailable(
                "the Gemini client does not implement tool use; point "
                "agents.llm at anthropic for the MCP agent cycles"
            )
        response = self._client.models.generate_content(
            model=self.cfg.model,
            contents=user,
            config=self._config(system),
        )
        return getattr(response, "text", "") or ""

    def json_complete(
        self, system: str, user: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ask for JSON natively rather than asking nicely in the prompt."""
        try:
            response = self._client.models.generate_content(
                model=self.cfg.model,
                contents=user,
                config=self._config(system, json_mode=True),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Gemini call failed (model=%s): %s", self.cfg.model, exc)
            return default or {}
        return _extract_json(getattr(response, "text", "") or "") or (default or {})


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
        if cfg.provider == "gemini":
            return GeminiClient(cfg)
    except LLMUnavailable as exc:
        if not cfg.fallback_to_rules:
            raise
        log.warning("LLM unavailable (%s) - running rules-only", exc)
    return NullClient(cfg)


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
