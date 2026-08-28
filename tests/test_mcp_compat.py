"""Reading MCP tool metadata across SDK majors.

`mcp` 1.x named the field `inputSchema`; 2.x renamed it to `input_schema`.
`pyproject.toml` pins `mcp>=1.2`, so which one a host resolves is not under this
repo's control - and the mismatch is silent until a real server is contacted,
because fakes tend to be written in whichever spelling the author had open.
"""

from __future__ import annotations

import pytest

from oaa.core.mcp_compat import (
    tool_description,
    tool_input_schema,
    tool_result_is_error,
    tool_structured_content,
    unwrap_tool_payload,
)

SCHEMA = {"type": "object", "properties": {"symbol": {"type": "string"}}}


class _Modern:
    input_schema = SCHEMA
    description = "modern"


class _Legacy:
    inputSchema = SCHEMA
    description = "legacy"


class _Bare:
    description = None


@pytest.mark.parametrize("tool", [_Modern(), _Legacy(), {"input_schema": SCHEMA},
                                  {"inputSchema": SCHEMA}])
def test_both_spellings_and_raw_dicts_resolve(tool):
    assert tool_input_schema(tool) == SCHEMA


def test_a_tool_with_no_schema_is_callable_rather_than_fatal():
    """An unreadable schema should mean 'takes no arguments', not a crash in
    tool discovery that takes the whole agent cycle with it."""
    assert tool_input_schema(_Bare()) == {"type": "object", "properties": {}}
    assert tool_input_schema({}) == {"type": "object", "properties": {}}


def test_the_empty_schema_is_not_shared_between_callers():
    """It is handed to code that mutates schemas; a shared dict would leak."""
    first, second = tool_input_schema({}), tool_input_schema({})
    first["properties"]["injected"] = True
    assert second == {"type": "object", "properties": {}}


def test_descriptions_are_capped_and_never_none():
    assert tool_description(_Bare()) == ""
    assert tool_description({"description": "x" * 5000}, 600) == "x" * 600
    assert tool_description(_Modern()) == "modern"


# --------------------------------------------------------------------------- #
# the dangerous rename
# --------------------------------------------------------------------------- #
class _ModernError:
    is_error = True
    content: list = []


class _LegacyError:
    isError = True
    content: list = []


@pytest.mark.parametrize("result", [_ModernError(), _LegacyError(),
                                    {"is_error": True}, {"isError": True}])
def test_a_failed_tool_call_is_seen_in_either_spelling(result):
    """Reading only `isError` against a 2.x result returns False for every
    failure, so the error TEXT gets parsed as data and the agent reasons on a
    string that says the call failed. Nothing in the logs would say so."""
    assert tool_result_is_error(result) is True


@pytest.mark.parametrize("result", [_Modern(), {"input_schema": SCHEMA}, {}])
def test_a_successful_result_is_not_mistaken_for_an_error(result):
    assert tool_result_is_error(result) is False


class _ModernStructured:
    structured_content = {"equity": 100000.0}


class _LegacyStructured:
    structuredContent = {"equity": 100000.0}


@pytest.mark.parametrize("result", [_ModernStructured(), _LegacyStructured(),
                                    {"structured_content": {"equity": 100000.0}},
                                    {"structuredContent": {"equity": 100000.0}}])
def test_structured_content_resolves_in_either_spelling(result):
    assert tool_structured_content(result) == {"equity": 100000.0}


def test_absent_structured_content_is_none_so_the_caller_falls_back_to_text():
    assert tool_structured_content(_Bare()) is None
    assert tool_structured_content({}) is None


# --------------------------------------------------------------------------- #
# Alpaca's MCP response envelope
# --------------------------------------------------------------------------- #
ACCOUNT = {"account_number": "PA3CEO0Q2VQK", "equity": "100000", "id": "fb00a3fb"}
ENVELOPE = {
    "_alpaca_mcp_security": {"trust": "untrusted_tool_output"},
    "data": ACCOUNT,
}


def test_the_alpaca_envelope_is_stripped():
    """Without this, `payload.get("account_number")` is None and
    `payload.get("equity")` is 0 - so `AlpacaMcpBroker.account()` reports a flat
    broke account rather than an error, and percent-of-equity risk limits size
    against nothing."""
    assert unwrap_tool_payload(ENVELOPE) == ACCOUNT


def test_a_bare_payload_is_returned_untouched():
    assert unwrap_tool_payload(ACCOUNT) == ACCOUNT
    assert unwrap_tool_payload([1, 2]) == [1, 2]
    assert unwrap_tool_payload("text") == "text"


def test_a_payload_that_merely_HAS_a_data_field_is_not_unwrapped():
    """Conservative on purpose: unwrapping only when the envelope key is the
    sole non-metadata key. Otherwise a legitimate response carrying its own
    `data` alongside other fields would silently lose them."""
    real = {"data": {"x": 1}, "count": 1}
    assert unwrap_tool_payload(real) == real


def test_nested_envelopes_unwrap_but_cannot_loop():
    assert unwrap_tool_payload({"data": {"result": ACCOUNT}}) == ACCOUNT

    class _Recursive(dict):
        def __getitem__(self, key):
            return self

    assert unwrap_tool_payload(_Recursive(data=1), max_depth=3) is not None
