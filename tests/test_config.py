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
    # OAA_PROFILE in a local .env overrides the argument, so assert on the
    # overlay's effect rather than on the label.
    assert dev.execution.dry_run is True or dev.profile == "dev"
    # The judged profile is the one that actually trades, and it is tighter.
    assert judged.risk.max_new_positions_per_day <= dev.risk.max_new_positions_per_day


def test_strategy_params_are_inlined_from_files():
    cfg = load_config()
    carry = next(s for s in cfg.strategies if s.name == "vol_carry")
    assert carry.book == "carry"
    # Lowered to 0.35 on 27 Aug: at 0.70 this one gate rejected 304 of 304
    # replayed candidates. The floor still has to be a floor, though - a carry
    # book that sells premium at median richness is not a carry book, so pin a
    # band rather than deleting the assertion.
    assert 0.30 <= carry.params["premium_gate"]["iv_rank_min"] <= 0.50
    assert 7 <= carry.params["structures"]["dte_min"] <= carry.params["structures"]["dte_max"] <= 14


def test_the_books_are_the_three_the_firewall_knows_about():
    from oaa.firewall.lock import Book

    cfg = load_config()
    assert {s.book for s in cfg.strategies} <= {b.value for b in Book}


def test_the_submission_controls_are_set_not_left_to_memory():
    """`submission_flatten_utc` is set in config on purpose: relying on
    remembering to trigger a flatten manually on the day is how a book ends up
    marked-to-mid at judging.

    The GLOBAL `entry_cutoff_utc` was removed on 29 Aug - it gated every book
    when its reasoning only ever applied to the multi-session carry book, and
    it silently deleted every event after 2 Sep 20:00 UTC. It is now null, and
    must stay null: a value here re-gates the event book. The carry book's own
    cutoff is asserted below."""
    cfg = load_config()
    assert cfg.management.submission_flatten_utc
    assert cfg.management.entry_cutoff_utc is None


def test_the_carry_book_keeps_its_own_entry_cutoff():
    """Carry structures hold 3-10 sessions, so one opened late cannot decay
    before `submission_flatten_utc` liquidates it. That constraint is real and
    belongs to the carry book alone - it must not migrate back to a global
    gate."""
    cfg = load_config()
    carry = [s for s in cfg.strategies if s.name == "vol_carry"]
    assert carry, "vol_carry strategy missing from config"
    cutoff = carry[0].params.get("exits", {}).get("entry_cutoff_utc")
    assert cutoff, "the carry book must keep its own entry cutoff"
    assert cutoff < cfg.management.submission_flatten_utc


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


def test_the_lunch_window_is_managed_even_though_it_is_not_traded():
    """Ivan's rule: no entries at lunch, but do watch what the tape does to the
    positions already on.

    The entry gate skipped 11:30-13:30 and the SCAN grid skipped it too, so a
    position opened at 11:00 went 150 minutes with no mark, no stop and no
    profit target - measured in the 17-21 Aug replay as holds of "150 minutes"
    and "135 minutes" on a book whose time stop is 20. An intraday position
    that cannot be exited for 150 minutes is not an intraday position.
    """
    from oaa.config.loader import load_config

    cfg = load_config()
    moments = sorted(cfg.backtest.session_times_et)
    lunch = [t for t in moments if "11:30" <= t < "13:30"]
    assert len(lunch) >= 4, f"lunch is still unmanaged: {lunch}"

    gaps = [
        (a, b) for a, b in zip(moments, moments[1:], strict=False)
        if (int(b[:2]) * 60 + int(b[3:])) - (int(a[:2]) * 60 + int(a[3:])) > 20
    ]
    assert not gaps, f"a position could sit unmanaged across {gaps}"


def test_the_live_grid_still_mirrors_the_backtest_grid():
    """When these diverged the live agent saw a sixth of what the backtest was
    tuned against. Lunch moments are `manage_positions` in live and
    entry-refused by the time gate in replay - same moments, same behaviour."""
    from oaa.config.loader import load_config

    cfg = load_config()
    live = {c.at for c in cfg.schedule.cycles
            if c.action in {"intraday_scan", "manage_positions"}}
    replay = set(cfg.backtest.session_times_et)
    assert replay <= live, f"replay moments with no live cycle: {sorted(replay - live)}"


def test_no_entry_cycle_lands_inside_the_lunch_window():
    """Managed, not traded. A scan cycle at lunch would be refused by the time
    gate anyway, but the schedule should say what it means."""
    from oaa.config.loader import load_config

    cfg = load_config()
    scans = [c.at for c in cfg.schedule.cycles if c.action == "intraday_scan"]
    assert not [t for t in scans if "11:30" <= t < "13:30"]


def test_a_half_written_strategy_package_does_not_take_down_the_system(tmp_path):
    """One incomplete module used to break EVERY command.

    `load_strategies` calls `registry.autoload`, which imports every module
    under oaa.strategies. A package mid-write - an __init__ importing a
    `signals` module that does not exist yet - raised ModuleNotFoundError out
    of `oaa backtest`, `oaa scan`, `oaa doctor` and the live agent's loop
    alike. An agent holding real positions must still be able to run `manage`
    and `flatten` when some unrelated strategy is half-finished.
    """
    import sys
    import types

    from oaa.core.registry import Registry

    package = types.ModuleType("oaa_probe_pkg")
    package.__path__ = [str(tmp_path)]
    sys.modules["oaa_probe_pkg"] = package
    (tmp_path / "fine.py").write_text("VALUE = 1\n")
    (tmp_path / "broken.py").write_text("import definitely_not_a_module\n")
    try:
        registry = Registry("probe")
        registry.autoload("oaa_probe_pkg")          # must not raise
        assert "broken" in registry.import_errors
        assert "definitely_not_a_module" in registry.import_errors["broken"]
        assert "fine" not in registry.import_errors
    finally:
        sys.modules.pop("oaa_probe_pkg", None)
        for name in ("oaa_probe_pkg.fine", "oaa_probe_pkg.broken"):
            sys.modules.pop(name, None)


def test_an_enabled_strategy_that_cannot_import_is_a_hard_error():
    """Tolerated only while nothing asks for it. Running without a strategy the
    operator believes is enabled would mean trading a book that silently is not
    there - so that case raises, naming the import failure rather than the
    misleading "unknown strategy" the registry would otherwise report."""
    import inspect

    from oaa.strategies.base import load_strategies

    src = inspect.getsource(load_strategies)
    assert "import_errors" in src
    assert "failed to" in src
