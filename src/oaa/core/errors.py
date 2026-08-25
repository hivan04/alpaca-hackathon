"""Exception hierarchy. Everything the system raises descends from OaaError."""

from __future__ import annotations


class OaaError(Exception):
    """Base for all first-party errors."""


class ConfigError(OaaError):
    """Malformed or missing configuration."""


class DataError(OaaError):
    """Market data unavailable, stale, or nonsensical."""


class BrokerError(OaaError):
    """The broker rejected a request or is unreachable."""


class StrategyError(OaaError):
    """A strategy could not produce a valid plan."""


class RiskRejection(OaaError):
    """The risk engine refused a trade. Not a bug - the system working."""

    def __init__(self, reason: str, rule: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule = rule


class PartnerError(OaaError):
    """A technology-partner adapter failed."""
