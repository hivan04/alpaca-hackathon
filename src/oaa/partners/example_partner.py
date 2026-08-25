"""Template partner adapter. Copy this file when a sponsor is announced.

Checklist for a new partner (target: under 30 minutes on kickoff day):

  1. cp example_partner.py <partner>.py
  2. set partner_name, contribution, required_env
  3. implement setup() and run()
  4. add a block under `partners.adapters` in config/default.yaml
  5. add the credential to .env.example (name only, never the value)
  6. `oaa partners` to confirm it loads, `oaa scan --dry-run` to see it fire

Adapter contract: `run` receives the stage payload and returns it, mutated or
replaced. Returning None means "no change". Never raise for a missing optional
field - degrade and let the pipeline continue.
"""

from __future__ import annotations

from typing import Any

from oaa.core.logging import get_logger
from oaa.core.types import MarketContext
from oaa.partners.base import PartnerAdapter, partner_registry

log = get_logger("partners.example")


@partner_registry.register("example_partner")
class ExamplePartner(PartnerAdapter):
    partner_name = "Example Technology Partner"
    contribution = "Adds a sentiment score to each symbol's MarketContext."
    required_env = ()  # e.g. ("PARTNER_EXAMPLE_API_KEY",)

    def setup(self) -> None:
        self.endpoint = self.params.get("endpoint", "")
        self.api_key = self.secret("api_key_env")
        # Real adapters build their client here:
        #   self.client = PartnerSDK(api_key=self.api_key, base_url=self.endpoint)
        log.debug("example partner ready (endpoint=%s)", self.endpoint or "<unset>")

    def run(self, payload: Any) -> Any:
        """Stage `data_enrichment`: payload is a MarketContext."""
        if not isinstance(payload, MarketContext):
            return payload

        # --- replace with a real call --------------------------------------- #
        payload.enrichment["example_sentiment"] = 0.0
        payload.enrichment["example_source"] = self.partner_name
        # --------------------------------------------------------------------- #

        return payload

    def teardown(self) -> None:
        pass
