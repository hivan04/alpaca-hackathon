"""LLM access for the reasoning agents.

Two rules, both learned from the failure mode this event punishes hardest:

  1. The LLM is never on the critical path for safety. It scores and explains;
     the deterministic risk engine decides.
  2. If the provider is unreachable, the loop degrades to rules and keeps
     trading. Downtime costs P&L; a missing paragraph of reasoning does not.
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
        response = self._client.messages.create(**kwargs)
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
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature,
                system=system,
                messages=messages,
                tools=tools,
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
    except LLMUnavailable as exc:
        if not cfg.fallback_to_rules:
            raise
        log.warning("LLM unavailable (%s) - running rules-only", exc)
    return NullClient(cfg)


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
