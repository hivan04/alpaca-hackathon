from __future__ import annotations

import pytest

from oaa.config.loader import _deep_merge, load_config, project_root
from oaa.config.schema import Config


def test_default_config_loads():
    cfg = load_config()
    assert cfg.meta.project
    assert cfg.universe.active()
    assert cfg.risk.max_risk_per_trade_pct > 0


def test_profile_overlay_changes_dry_run():
    dev = load_config(profile="dev")
    judged = load_config(profile="judged")
    assert dev.profile == "dev"
    assert judged.profile == "judged"
    # The judged profile is the one that actually trades.
    assert judged.execution.dry_run is False
    assert judged.risk.max_new_positions_per_day < dev.risk.max_new_positions_per_day


def test_strategy_params_are_inlined_from_files():
    cfg = load_config()
    condor = next(s for s in cfg.strategies if s.name == "vol_carry_condor")
    assert condor.params["structure"]["type"] == "iron_condor"
    assert "short_put_delta" in condor.params["structure"]


def test_unknown_keys_are_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"nonsense_key": 1})


def test_deep_merge_is_recursive():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    out = _deep_merge(base, {"a": {"c": 99}})
    assert out == {"a": {"b": 1, "c": 99}, "d": 3}
    assert base["a"]["c"] == 2  # original untouched


def test_project_root_has_config_dir():
    assert (project_root() / "config" / "default.yaml").exists()


def test_partners_for_stage_filters_and_sorts():
    cfg = load_config()
    # The example adapter ships disabled, so no stage should be populated.
    assert cfg.partners.for_stage("data_enrichment") == []
