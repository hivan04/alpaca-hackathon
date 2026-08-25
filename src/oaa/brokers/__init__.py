"""Broker backends. All four satisfy the same protocol.

    rest -> alpaca-py SDK        (default; the only one with streaming)
    cli  -> alpaca CLI subprocess (hackathon requirement: CLI tools)
    mcp  -> Alpaca MCP server     (hackathon requirement: MCP server)
    sim  -> in-process simulator  (dry runs and tests; never touches the wire)
"""

from oaa.brokers.base import Broker, broker_registry
from oaa.brokers.factory import get_broker

__all__ = ["Broker", "broker_registry", "get_broker"]
