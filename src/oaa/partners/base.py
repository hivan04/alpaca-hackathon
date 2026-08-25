"""Partner adapter protocol and the hub that runs them.

Stages, in pipeline order:

    data_enrichment  after market data is fetched, before strategies see it
                     -> add fields to MarketContext.enrichment
    signal           alongside the built-in strategies
                     -> contribute Signals or whole TradeIdeas
    reasoning        inside the agent loop
                     -> add context, tools or a second opinion to the LLM step
    risk             after the deterministic risk engine
                     -> may only VETO, never approve; the core engine is final
    execution        around order routing
                     -> smart routing, TCA, alternative venues
    telemetry        after every decision
                     -> ship events to an observability partner
    ui               dashboard panels

An adapter that raises is skipped (config: partners.on_error), because a
sponsor SDK falling over at 14:00 on Thursday must not take the trading loop
with it.
"""

from __future__ import annotations

import abc
import importlib
import os
import time
from typing import Any

from oaa.config.schema import Config, PartnerAdapterConfig
from oaa.core.errors import PartnerError
from oaa.core.logging import get_logger
from oaa.core.registry import Registry

log = get_logger("partners")


class PartnerAdapter(abc.ABC):
    """Base class for a sponsor technology plugged into one pipeline stage."""

    #: Human-facing name of the partner technology.
    partner_name: str = "unnamed"
    #: What this adapter contributes, one line - shown in `oaa partners`.
    contribution: str = ""
    #: Env vars that must be present for the adapter to run.
    required_env: tuple[str, ...] = ()

    def __init__(self, spec: PartnerAdapterConfig, config: Config) -> None:
        self.spec = spec
        self.config = config
        self.params = spec.params or {}
        self._ready: bool | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def stage(self) -> str:
        return self.spec.stage

    # -- lifecycle ---------------------------------------------------------- #
    def setup(self) -> None:
        """Create clients, open sessions. Called once at startup."""
        return None

    def teardown(self) -> None:
        """Release anything setup() acquired."""
        return None

    def available(self) -> bool:
        """False when credentials are missing - skipped rather than crashing."""
        if self._ready is None:
            missing = [key for key in self.required_env if not os.getenv(key)]
            if missing:
                log.warning(
                    "partner '%s' disabled: missing env %s", self.name, ", ".join(missing)
                )
            self._ready = not missing
        return bool(self._ready)

    def secret(self, key: str, default: str | None = None) -> str | None:
        """Read a credential by env-var name from the adapter's params."""
        env_name = self.params.get(key) or key
        return os.getenv(str(env_name), default)

    # -- the work ------------------------------------------------------------ #
    @abc.abstractmethod
    def run(self, payload: Any) -> Any:
        """Transform and return the payload.

        Contract: return the payload (mutated or replaced). Returning None
        means "no change" and the hub keeps the previous value. An adapter at
        the `risk` stage returns a falsy value to veto.
        """

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "partner": self.partner_name,
            "stage": self.stage,
            "priority": self.spec.priority,
            "enabled": self.spec.enabled,
            "available": self.available(),
            "contribution": self.contribution,
        }


partner_registry: Registry[PartnerAdapter] = Registry("partner adapter")


class PartnerHub:
    """Loads adapters from config and runs them per stage."""

    def __init__(self, config: Config, journal: Any = None) -> None:
        self.config = config
        self.journal = journal
        self.adapters: dict[str, list[PartnerAdapter]] = {}
        self._load()

    def _load(self) -> None:
        partners = self.config.partners
        if not partners.enabled:
            log.debug("partner layer disabled")
            return
        for spec in partners.adapters:
            if not spec.enabled:
                continue
            try:
                adapter = self._instantiate(spec)
                adapter.setup()
            except Exception as exc:  # noqa: BLE001
                message = f"partner '{spec.name}' failed to load: {exc}"
                if partners.on_error == "fail":
                    raise PartnerError(message) from exc
                log.error(message)
                continue
            self.adapters.setdefault(spec.stage, []).append(adapter)
            log.info("partner loaded: %s -> stage '%s'", spec.name, spec.stage)

        for stage in self.adapters:
            self.adapters[stage].sort(key=lambda a: a.spec.priority)

    def _instantiate(self, spec: PartnerAdapterConfig) -> PartnerAdapter:
        module = importlib.import_module(spec.module)
        # Prefer an explicitly registered name; otherwise take the single
        # PartnerAdapter subclass defined in the module.
        if spec.name in partner_registry:
            return partner_registry.get(spec.name)(spec, self.config)
        candidates = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, PartnerAdapter)
            and obj is not PartnerAdapter
            and obj.__module__ == module.__name__
        ]
        if len(candidates) != 1:
            raise PartnerError(
                f"{spec.module} must define exactly one PartnerAdapter subclass "
                f"(found {len(candidates)}), or register itself under '{spec.name}'"
            )
        return candidates[0](spec, self.config)

    # -- execution ------------------------------------------------------------ #
    def run(self, stage: str, payload: Any) -> Any:
        """Chain every adapter registered at `stage` over the payload."""
        for adapter in self.adapters.get(stage, []):
            if not adapter.available():
                continue
            started = time.monotonic()
            try:
                result = adapter.run(payload)
                if result is not None:
                    payload = result
                elapsed = time.monotonic() - started
                log.debug("partner %s (%s) ok in %.2fs", adapter.name, stage, elapsed)
                if self.journal:
                    self.journal.event(
                        "partner", partner=adapter.name, stage=stage,
                        ok=True, seconds=round(elapsed, 3),
                    )
            except Exception as exc:  # noqa: BLE001
                if self.journal:
                    self.journal.event(
                        "partner", partner=adapter.name, stage=stage,
                        ok=False, error=str(exc),
                    )
                if self.config.partners.on_error == "fail":
                    raise PartnerError(f"partner '{adapter.name}' failed: {exc}") from exc
                log.error("partner '%s' failed at stage '%s': %s", adapter.name, stage, exc)
        return payload

    def veto(self, payload: Any) -> tuple[bool, str | None]:
        """Risk stage: any adapter returning falsy vetoes the trade."""
        for adapter in self.adapters.get("risk", []):
            if not adapter.available():
                continue
            try:
                if not adapter.run(payload):
                    return False, f"vetoed by partner '{adapter.name}'"
            except Exception as exc:  # noqa: BLE001
                if self.config.partners.on_error == "fail":
                    raise PartnerError(str(exc)) from exc
                log.error("partner risk check '%s' errored: %s", adapter.name, exc)
        return True, None

    def stages(self) -> dict[str, list[dict[str, Any]]]:
        return {
            stage: [a.describe() for a in adapters]
            for stage, adapters in sorted(self.adapters.items())
        }

    def count(self) -> int:
        return sum(len(v) for v in self.adapters.values())

    def teardown(self) -> None:
        for adapters in self.adapters.values():
            for adapter in adapters:
                try:
                    adapter.teardown()
                except Exception as exc:  # noqa: BLE001
                    log.debug("teardown failed for %s: %s", adapter.name, exc)
