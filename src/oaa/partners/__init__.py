"""Technology-partner integration layer.

The hackathon publishes its sponsor technologies at kickoff, and the
Technology Implementation criterion explicitly references "other required
technologies". This package is where those land.

The design goal is that adding a partner on day one of a seven-day event costs
one file and one config block - never a change to the pipeline. Every pipeline
stage already calls `PartnerHub.run(stage, payload)`; an adapter that does not
exist is simply a stage with no adapters.

See docs/PARTNERS.md for the integration checklist.
"""

from oaa.partners.base import PartnerAdapter, PartnerHub, partner_registry

__all__ = ["PartnerAdapter", "PartnerHub", "partner_registry"]
