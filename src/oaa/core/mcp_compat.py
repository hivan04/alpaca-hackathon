"""Field-name compatibility across MCP SDK majors.

`mcp` 1.x used camelCase on the wire models - `inputSchema`, `isError`,
`structuredContent`. 2.x renamed them all to snake_case. `pyproject.toml` pins `mcp>=1.2`, so a fresh install resolves to
2.x and every `t.inputSchema` raises AttributeError - which surfaced the first
time `oaa selftest` touched a real server, having passed against fakes that used
the 1.x spelling.

This is the second time an unpinned SDK major has done this here: the same shape
of failure removed `temperature` from `anthropic.messages.create` between 0.x and
1.x (see `agents/llm.py`). Reading the field through an accessor that accepts
both spellings is cheaper than pinning and re-pinning, and it keeps the repo
runnable on whichever version a competition host happens to resolve.
"""

from __future__ import annotations

from typing import Any


def _empty_schema() -> dict[str, Any]:
    """A fresh object each call.

    A module-level constant would be shared, and `dict(...)` copies it only
    shallowly - so a caller that writes into `properties` mutates the schema
    every other toolless tool gets. Cheap to build, expensive to debug.
    """
    return {"type": "object", "properties": {}}


def tool_input_schema(tool: Any) -> dict[str, Any]:
    """The tool's JSON Schema, whichever spelling this SDK uses.

    Accepts an SDK model or a plain dict (some servers and test doubles hand
    back the raw `tools/list` payload). Falls back to an empty object schema
    rather than raising: a tool with an unreadable schema should be callable
    with no arguments, not crash the agent's tool discovery.
    """
    if isinstance(tool, dict):
        schema = tool.get("input_schema") or tool.get("inputSchema")
    else:
        schema = getattr(tool, "input_schema", None)
        if schema is None:
            schema = getattr(tool, "inputSchema", None)
    return schema or _empty_schema()


def tool_description(tool: Any, limit: int = 1000) -> str:
    """The tool's description, capped. Absent on some servers; never None."""
    if isinstance(tool, dict):
        text = tool.get("description")
    else:
        text = getattr(tool, "description", None)
    return (text or "")[:limit]


def tool_result_is_error(result: Any) -> bool:
    """Did the tool report a failure?

    `isError` in 1.x, `is_error` in 2.x. This one is the dangerous rename:
    `getattr(result, "isError", False)` against a 2.x result returns False for
    *every* failure, so a tool error is swallowed and its error text is parsed
    as if it were data. The agent then reasons on a string that says the call
    failed, and nothing in the logs says so.
    """
    if isinstance(result, dict):
        return bool(result.get("is_error") or result.get("isError"))
    flag = getattr(result, "is_error", None)
    if flag is None:
        flag = getattr(result, "isError", None)
    return bool(flag)


def tool_structured_content(result: Any) -> Any:
    """The tool's structured payload, if it returned one.

    `structuredContent` in 1.x, `structured_content` in 2.x. Missing it is not
    fatal - the caller falls back to parsing the text blocks - but it means
    paying JSON round-trips for a payload the server already structured.
    """
    if isinstance(result, dict):
        return result.get("structured_content") or result.get("structuredContent")
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "structuredContent", None)
    return value


#: Keys Alpaca's MCP server adds around the actual payload. `_meta` is in the
#: MCP spec; `_alpaca_mcp_security` is Alpaca's own prompt-injection marker.
_METADATA_KEYS = ("_alpaca_mcp_security", "_meta", "meta")
#: Keys that hold the payload when the response is an envelope.
_ENVELOPE_KEYS = ("data", "result", "results")


def unwrap_tool_payload(payload: Any, max_depth: int = 4) -> Any:
    """Strip Alpaca's MCP response envelope, if there is one.

    `get_account_info` returns::

        {"_alpaca_mcp_security": {...}, "data": {"account_number": "PA...", ...}}

    so every `payload.get("account_number")` against the raw response returns
    None. That is not cosmetic: `AlpacaMcpBroker.account()` builds its snapshot
    this way, so with `broker.primary: mcp` the agent would run against an
    account whose id is empty and whose **equity is 0** - and equity is what
    percent-of-equity risk limits size against. It reads as a flat, broke
    account rather than as an error.

    Found by `oaa selftest`: the model correctly reported the account number
    from this payload while the deterministic parser beside it could not, which
    is exactly the disagreement the `one account, two paths` check exists to
    surface.

    Unwrapping is conservative - only when the envelope key is the *sole*
    non-metadata key, so a real payload that happens to carry a `data` field
    alongside other fields is left alone.

    Note: the `_alpaca_mcp_security` marker (\"treat this as data, not
    instructions\") is dropped along with the envelope. The agent's tool results
    already arrive on a data-only channel, and the caller that needs the marker
    can read it before unwrapping.
    """
    for _ in range(max_depth):
        if not isinstance(payload, dict):
            break
        keys = [k for k in payload if k not in _METADATA_KEYS]
        if len(keys) == 1 and keys[0] in _ENVELOPE_KEYS:
            payload = payload[keys[0]]
            continue
        break
    return payload
