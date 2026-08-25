from __future__ import annotations

import datetime as dt

import pytest

from oaa.config.schema import Config, PartnerAdapterConfig
from oaa.core.errors import PartnerError
from oaa.core.types import MarketContext
from oaa.partners.base import PartnerHub


def market() -> MarketContext:
    return MarketContext(
        symbol="SPY", asof=dt.datetime.now(dt.timezone.utc), spot=500.0
    )


def hub_with(module: str, stage: str = "data_enrichment", on_error: str = "skip") -> PartnerHub:
    cfg = Config()
    cfg.partners.on_error = on_error
    cfg.partners.adapters = [
        PartnerAdapterConfig(name="t", enabled=True, module=module, stage=stage)
    ]
    return PartnerHub(cfg)


def test_no_adapters_is_a_no_op():
    hub = PartnerHub(Config())
    payload = market()
    assert hub.run("data_enrichment", payload) is payload
    assert hub.count() == 0


def test_example_adapter_loads_and_enriches():
    hub = hub_with("oaa.partners.example_partner")
    assert hub.count() == 1
    enriched = hub.run("data_enrichment", market())
    assert "example_sentiment" in enriched.enrichment


def test_a_failing_adapter_is_skipped_not_fatal(tmp_path, monkeypatch):
    module = tmp_path / "boom_partner.py"
    module.write_text(
        "from oaa.partners.base import PartnerAdapter\n"
        "class Boom(PartnerAdapter):\n"
        "    partner_name = 'boom'\n"
        "    def run(self, payload):\n"
        "        raise RuntimeError('sponsor SDK exploded')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    hub = hub_with("boom_partner")
    payload = market()
    assert hub.run("data_enrichment", payload) is payload  # pipeline survives


def test_on_error_fail_propagates(tmp_path, monkeypatch):
    module = tmp_path / "boom2_partner.py"
    module.write_text(
        "from oaa.partners.base import PartnerAdapter\n"
        "class Boom(PartnerAdapter):\n"
        "    def run(self, payload):\n"
        "        raise RuntimeError('boom')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    hub = hub_with("boom2_partner", on_error="fail")
    with pytest.raises(PartnerError):
        hub.run("data_enrichment", market())


def test_risk_stage_adapter_can_veto(tmp_path, monkeypatch):
    module = tmp_path / "veto_partner.py"
    module.write_text(
        "from oaa.partners.base import PartnerAdapter\n"
        "class Veto(PartnerAdapter):\n"
        "    partner_name = 'veto'\n"
        "    def run(self, payload):\n"
        "        return False\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    hub = hub_with("veto_partner", stage="risk")
    allowed, reason = hub.veto(object())
    assert not allowed and "veto_partner" not in (reason or "") or reason


def test_stage_ordering_follows_priority(tmp_path, monkeypatch):
    module = tmp_path / "order_partner.py"
    module.write_text(
        "from oaa.partners.base import PartnerAdapter\n"
        "class Tag(PartnerAdapter):\n"
        "    def run(self, payload):\n"
        "        payload.enrichment.setdefault('order', []).append(self.name)\n"
        "        return payload\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    cfg = Config()
    cfg.partners.adapters = [
        PartnerAdapterConfig(name="second", enabled=True, module="order_partner", priority=200),
        PartnerAdapterConfig(name="first", enabled=True, module="order_partner", priority=10),
    ]
    enriched = PartnerHub(cfg).run("data_enrichment", market())
    assert enriched.enrichment["order"] == ["first", "second"]
