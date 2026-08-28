"""Featherless AI - the live reasoning provider.

These tests never touch the network. A fake transport stands in for the HTTP
layer, which is the only honest way to test a provider client: the thing worth
asserting is the *wire contract* we send and how we behave when the far end
misbehaves, not that someone else's server is up.

The cases mirror the three failure modes that would actually cost P&L during
the judged week:

  * a 429 on a cold start (retry, do not lose the cycle's reasoning),
  * a model that rejects `response_format` (fall back to prompted JSON once,
    not on every call),
  * the provider being down entirely (degrade to rules, keep trading).
"""

from __future__ import annotations

import json

import httpx
import pytest

from oaa.agents.llm import (
    FeatherlessClient,
    LLMUnavailable,
    NullClient,
    _openai_tools,
    get_llm,
)
from oaa.config.schema import LLMConfig


def _cfg(**kwargs) -> LLMConfig:
    base = {
        "provider": "featherless",
        "model": "Qwen/Qwen3-32B",
        "max_retries": 3,
        "retry_backoff_seconds": 0.0,  # tests do not sleep
    }
    base.update(kwargs)
    return LLMConfig(**base)


def _client(monkeypatch, handler) -> FeatherlessClient:
    monkeypatch.setenv("FEATHERLESS_API_KEY", "fk-test")
    client = FeatherlessClient(_cfg())
    client._client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer fk-test"},
    )
    return client


def _reply(text: str = "", tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


# --------------------------------------------------------------------------- #
# the wire contract
# --------------------------------------------------------------------------- #
def test_posts_openai_shaped_chat_completions(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_reply("hello"))

    client = _client(monkeypatch, handler)
    assert client.complete("sys", "usr") == "hello"
    assert seen["url"] == "https://api.featherless.ai/v1/chat/completions"
    assert seen["auth"] == "Bearer fk-test"
    assert seen["body"]["model"] == "Qwen/Qwen3-32B"
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]


def test_base_url_is_configurable(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "fk-test")
    assert FeatherlessClient(_cfg(base_url="https://proxy.local/v1/")).base_url == (
        "https://proxy.local/v1"
    )


def test_missing_key_is_unavailable_not_a_crash(monkeypatch):
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    with pytest.raises(LLMUnavailable):
        FeatherlessClient(_cfg())
    # ...and the factory turns that into a rules-only run rather than a stop.
    assert isinstance(get_llm(_cfg(fallback_to_rules=True)), NullClient)


# --------------------------------------------------------------------------- #
# resilience - the reason this client exists rather than a raw SDK call
# --------------------------------------------------------------------------- #
def test_retries_a_rate_limit_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=_reply("recovered"))

    client = _client(monkeypatch, handler)
    assert client.complete("sys", "usr") == "recovered"
    assert calls["n"] == 3


def test_exhausted_retries_return_the_default_not_an_exception(monkeypatch):
    client = _client(monkeypatch, lambda request: httpx.Response(503, text="down"))
    assert client.json_complete("sys", "usr", default={"score": 0}) == {"score": 0}


# --------------------------------------------------------------------------- #
# JSON mode
# --------------------------------------------------------------------------- #
def test_json_mode_is_requested_natively(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_reply('{"verdict": "pass", "score": 0.7}'))

    client = _client(monkeypatch, handler)
    assert client.json_complete("sys", "usr") == {"verdict": "pass", "score": 0.7}
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_json_mode_rejection_falls_back_once_and_stays_fallen_back(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, text="response_format unsupported for this model")
        return httpx.Response(200, json=_reply('```json\n{"score": 1}\n```'))

    client = _client(monkeypatch, handler)
    assert client.json_complete("sys", "usr") == {"score": 1}
    assert client.json_complete("sys", "usr") == {"score": 1}
    # The second call must not pay for the discovery again.
    assert sum("response_format" in b for b in bodies) == 1


# --------------------------------------------------------------------------- #
# tool calling - the bridge from the agent to Alpaca's MCP tools
# --------------------------------------------------------------------------- #
def test_anthropic_tool_specs_are_translated():
    converted = _openai_tools([
        {"name": "get_account", "description": "read", "input_schema": {"type": "object"}}
    ])
    assert converted == [{
        "type": "function",
        "function": {
            "name": "get_account",
            "description": "read",
            "parameters": {"type": "object"},
        },
    }]
    # An already-OpenAI-shaped spec passes through untouched.
    native = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    assert _openai_tools(native) == native


def test_run_tools_executes_a_call_and_feeds_the_result_back(monkeypatch):
    turns = {"n": 0}
    second_request: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        turns["n"] += 1
        if turns["n"] == 1:
            return httpx.Response(200, json=_reply(tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_account", "arguments": '{"verbose": true}'},
            }]))
        second_request.update(json.loads(request.content))
        return httpx.Response(200, json=_reply("equity is 100000"))

    client = _client(monkeypatch, handler)
    invoked: list[tuple] = []

    def call_tool(name, arguments):
        invoked.append((name, arguments))
        return {"equity": 100000}

    answer = client.run_tools(
        "sys", "usr",
        tools=[{"name": "get_account", "description": "", "input_schema": {"type": "object"}}],
        call_tool=call_tool,
    )
    assert answer == "equity is 100000"
    assert invoked == [("get_account", {"verbose": True})]
    tool_message = second_request["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_1"
    assert json.loads(tool_message["content"]) == {"equity": 100000}


def test_a_failing_tool_is_reported_to_the_model_not_raised(monkeypatch):
    turns = {"n": 0}
    second_request: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        turns["n"] += 1
        if turns["n"] == 1:
            return httpx.Response(200, json=_reply(tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "boom", "arguments": "{}"},
            }]))
        second_request.update(json.loads(request.content))
        return httpx.Response(200, json=_reply("I could not read the account"))

    client = _client(monkeypatch, handler)

    def call_tool(name, arguments):
        raise RuntimeError("mcp server gone")

    answer = client.run_tools("sys", "usr", tools=[{"name": "boom"}], call_tool=call_tool)
    assert answer == "I could not read the account"
    assert "mcp server gone" in second_request["messages"][-1]["content"]


def test_tool_turn_limit_terminates(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply(tool_calls=[{
            "id": "c", "type": "function",
            "function": {"name": "get_account", "arguments": "{}"},
        }]))

    client = _client(monkeypatch, handler)
    answer = client.run_tools(
        "sys", "usr", tools=[{"name": "get_account"}],
        call_tool=lambda n, a: {}, max_turns=2,
    )
    assert "tool-turn limit" in answer


# --------------------------------------------------------------------------- #
# the factory
# --------------------------------------------------------------------------- #
def test_get_llm_builds_a_featherless_client(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "fk-test")
    client = get_llm(_cfg())
    assert isinstance(client, FeatherlessClient)
    assert client.provider == "featherless"
