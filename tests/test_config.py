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
    # NEUTRALISED to 0.0 on 1 Sep: the rank this floor tests is pinned at 100%
    # by the seed/observation basis mismatch, so at 0.35 it was not selecting
    # rich premium, it was passing everything while still reading as the
    # thesis. Both states are named so neither can drift in unnoticed.
    iv_rank_min = carry.params["premium_gate"]["iv_rank_min"]
    assert iv_rank_min == 0.0 or 0.30 <= iv_rank_min <= 0.50, (
        "iv_rank_min is either OFF (0.0, while the rank is unmeasurable) or a "
        "real floor in 0.30-0.50; anything else is silent drift"
    )
    # Either way, this is the gate actually carrying the premium thesis.
    assert carry.params["premium_gate"]["iv_rv_spread_min"] >= 0.02
    assert 7 <= carry.params["structures"]["dte_min"] <= carry.params["structures"]["dte_max"] <= 14


def test_the_books_are_the_three_the_firewall_knows_about():
    from oaa.firewall.lock import Book

    cfg = load_config()
    assert {s.book for s in cfg.strategies} <= {b.value for b in Book}


def test_the_date_gates_move_together_or_not_at_all():
    """The submission date gates are OFF as of 1 Sep, deliberately.

    `submission_flatten_utc` fired one flatten cycle and then refused every
    book for the life of the process (`lock.py:258`). The agent is wanted
    running past the judged window, so all three date gates are null and the
    book is closed by hand instead - see
    `claude/submission-flatten-disabled.md`.

    So this no longer asserts "the flatten is set" - that would pin a policy
    that was reversed on purpose, and a test that contradicts a decision is a
    red build saying nothing about the code.

    What it protects instead is the INVARIANT, which survives either policy:
    the flatten and the carry book's own entry cutoff move together. A flatten
    with no carry cutoff lets the resident book open a 3-10 session structure
    that the flatten then liquidates early - exactly the failure the cutoff
    exists to prevent. A carry cutoff with no flatten silences the resident
    book for a deadline that no longer exists, which is what it was doing on
    1 Sep. Turning either back on is therefore a complete change or a caught
    one.
    """
    cfg = load_config()
    carry = next(s for s in cfg.strategies if s.name == "vol_carry")
    flatten = cfg.management.submission_flatten_utc
    carry_cutoff = carry.params.get("exits", {}).get("entry_cutoff_utc")

    # Null whatever else happens: its reasoning only ever applied to the carry
    # book, and a value here re-gates the events book. Removed 29 Aug.
    assert cfg.management.entry_cutoff_utc is None

    if flatten is None:
        assert carry_cutoff is None, (
            "the carry cutoff's whole justification is 'do not open a structure "
            "that cannot decay before the submission flatten'. With no flatten "
            "it gates nothing and stands the resident book down for free."
        )
    else:
        assert carry_cutoff, "a submission flatten needs the carry book's own cutoff"
        assert carry_cutoff < flatten, (
            "the carry book must stop OPENING before the flatten closes everything"
        )


def test_closing_the_book_by_hand_is_actually_available():
    """`oaa flatten` is the replacement for the automatic flatten.

    With every date gate null, nothing closes the book at any time. That is a
    considered trade-off only while the manual control exists; if this verb is
    renamed or dropped, the nulls quietly become an account with no terminal
    event and no way to produce one on demand.
    """
    from oaa.cli import app

    names = {
        command.name or (command.callback.__name__ if command.callback else "")
        for command in app.registered_commands
    }
    assert "flatten" in names, f"no `oaa flatten` command; found {sorted(names)}"


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


# --------------------------------------------------------------------------- #
# strategy variants
#
# `--variant v2` loads config/variants/v2.yaml over the baseline so two
# strategies can be backtested against each other on identical code. Omitting
# it must leave the baseline byte-identical - that is the whole contract.
# --------------------------------------------------------------------------- #
def _carry(cfg):
    return next(s for s in cfg.strategies if s.name == "vol_carry")


def test_no_variant_is_the_untouched_baseline():
    cfg = load_config(profile="judged")
    assert cfg.variant is None
    carry = _carry(cfg)
    assert carry.params["premium_gate"].get("iv_max") is None
    assert carry.params["structures"].get("min_probability_of_profit") is None
    assert carry.params["exits"]["defensive_mode"] == "close_tested_side"
    assert next(s for s in cfg.strategies if s.name == "intraday_momentum").enabled


def test_v2_applies_its_three_carry_changes():
    cfg = load_config(profile="judged", variant="v2")
    assert cfg.variant == "v2"
    carry = _carry(cfg)
    assert carry.params["premium_gate"]["iv_max"] == 0.25
    assert carry.params["structures"]["min_probability_of_profit"] == 0.50
    assert carry.params["exits"]["defensive_mode"] == "hold"


def test_v2_turns_the_intraday_book_off():
    cfg = load_config(profile="judged", variant="v2")
    intraday = next(s for s in cfg.strategies if s.name == "intraday_momentum")
    assert not intraday.enabled


def test_v2_inherits_every_setting_it_does_not_name():
    """The overlay is a diff, not a replacement - a variant that silently reset
    the other 40 knobs would make the comparison meaningless."""
    base, v2 = load_config(profile="judged"), load_config(profile="judged", variant="v2")
    b, v = _carry(base).params, _carry(v2).params
    assert v["premium_gate"]["iv_rank_min"] == b["premium_gate"]["iv_rank_min"]
    assert v["exits"]["profit_target_pct"] == b["exits"]["profit_target_pct"]
    assert v["exits"]["loss_multiple_of_credit"] == b["exits"]["loss_multiple_of_credit"]
    assert v["structures"]["min_credit_to_width"] == b["structures"]["min_credit_to_width"]


def test_a_variant_keeps_strategies_it_does_not_mention():
    """The first draft of v2.yaml restated the strategies list and silently
    dropped earnings_event_directional. Patching by name is why it cannot."""
    base = {s.name for s in load_config(profile="judged").strategies}
    v2 = {s.name for s in load_config(profile="judged", variant="v2").strategies}
    assert base == v2


def test_an_unknown_variant_is_an_error_not_a_silent_baseline():
    with pytest.raises(FileNotFoundError, match="unknown variant"):
        load_config(profile="judged", variant="does-not-exist")


def test_a_variant_resolves_from_the_archive():
    """Archiving v2 must not break it - an archived result has to stay
    reproducible or the archive is just a folder of claims."""
    root = project_root()
    assert (root / "archive/strategies/v2/v2.yaml").exists()
    assert not (root / "config/variants/v2.yaml").exists(), (
        "two copies of a variant drift, and the loader cannot then say which "
        "one produced a run"
    )
    assert load_config(profile="judged", variant="v2").variant == "v2"


def test_a_variant_cannot_reach_the_live_path(monkeypatch):
    """v2 is archived research. The ONLY way to select a variant is an explicit
    `--variant` on `oaa backtest`; there is no env var and no config key, so a
    stray export cannot put a fitted strategy on the judged account."""
    monkeypatch.setenv("OAA_VARIANT", "v2")
    assert load_config(profile="judged").variant is None


def test_no_variant_key_ships_in_the_default_config():
    import yaml

    raw = yaml.safe_load((project_root() / "config/default.yaml").read_text())
    assert "variant" not in raw, (
        "a variant in default.yaml would apply to `oaa run` as well as backtests"
    )


def test_a_misspelled_strategy_override_is_an_error():
    from oaa.config.loader import _apply_strategy_overrides

    with pytest.raises(ValueError, match="no such strategy"):
        _apply_strategy_overrides(
            {"strategies": [{"name": "vol_carry"}], "strategy_overrides": {"vol_cary": {}}}
        )
