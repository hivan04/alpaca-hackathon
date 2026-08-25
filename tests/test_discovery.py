"""Discovery: scoring, tradability filters, the candidate pool, the macro lens."""

from __future__ import annotations

import datetime as dt

import pytest

from oaa.discovery.filters import TradabilityFilter, filter_symbols
from oaa.discovery.macro import MacroLens, MacroView
from oaa.discovery.score import score_snapshot
from oaa.discovery.sources import SourceResult
from oaa.discovery.universe import CandidatePool


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def result(name: str, values: dict[str, float], replayable: bool = False, **detail) -> SourceResult:
    return SourceResult(name=name, values=values, detail=detail, replayable=replayable)


def test_scores_are_rank_based_not_value_based():
    """A stock with 20x the volume is first, not 'twenty times hotter'."""
    snapshot = score_snapshot([
        result("most_actives", {"AAA": 400_000_000, "BBB": 20_000_000, "CCC": 19_000_000})
    ])
    scores = [snapshot.symbols[s].score for s in ("AAA", "BBB", "CCC")]
    assert scores == sorted(scores, reverse=True)
    # BBB is barely ahead of CCC on volume but ranks squarely between.
    assert scores[1] == pytest.approx(0.5, abs=0.01)


def test_a_failed_source_does_not_shrink_every_score():
    """Weights renormalise over the sources that actually returned."""
    healthy = score_snapshot([result("news", {"AAA": 5.0, "BBB": 1.0})])
    with_failure = score_snapshot([
        result("news", {"AAA": 5.0, "BBB": 1.0}),
        SourceResult(name="movers", error="CLI exploded"),
    ])
    assert with_failure.symbols["AAA"].score == pytest.approx(healthy.symbols["AAA"].score)
    assert "movers" in with_failure.source_errors


def test_replayable_only_drops_live_sources():
    """Anything feeding a backtest must be reconstructable for a past date."""
    snapshot = score_snapshot(
        [
            result("news", {"AAA": 3.0}, replayable=True),
            result("most_actives", {"BBB": 1e9}, replayable=False),
        ],
        replayable_only=True,
    )
    assert "AAA" in snapshot.symbols
    assert "BBB" not in snapshot.symbols


def test_breadth_is_extracted_from_movers():
    snapshot = score_snapshot([
        result("movers", {"AAA": 8.0}, __breadth__={"gainers": 18, "losers": 2})
    ])
    assert snapshot.breadth_ratio == pytest.approx(0.9)


def test_news_driven_needs_velocity_not_volume():
    snapshot = score_snapshot([
        result("news", {"AAA": 6.0, "BBB": 1.1},
               AAA={"velocity": 6.0, "headlines": ["big news"]},
               BBB={"velocity": 1.1, "headlines": []})
    ])
    driven = {s.symbol for s in snapshot.news_driven()}
    assert driven == {"AAA"}          # BBB is always in the news; that is not news


# --------------------------------------------------------------------------- #
# tradability filters
# --------------------------------------------------------------------------- #
class FakeRunner:
    def __init__(self, assets: dict, optionable: set[str]):
        self.assets = assets
        self.optionable = optionable
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["asset", "get"]:
            return self.assets.get(args[-1], {})
        if args[:2] == ["option", "contracts"]:
            symbol = args[args.index("--underlying-symbols") + 1]
            return {"option_contracts": [{"symbol": "X"}] if symbol in self.optionable else []}
        return {}


def runner_for(symbols, shortable=True, tradable=True, borrow=True):
    return FakeRunner(
        assets={
            s: {"tradable": tradable, "shortable": shortable, "easy_to_borrow": borrow}
            for s in symbols
        },
        optionable=set(symbols),
    )


def test_unshortable_names_are_rejected():
    """The pairs trade shorts a leg. This one bites silently otherwise."""
    runner = runner_for(["AAA"], shortable=False)
    survivors, verdicts = filter_symbols(["AAA"], runner, TradabilityFilter())
    assert survivors == []
    assert any("shortable" in r for r in verdicts[0].reasons)


def test_leveraged_products_are_rejected():
    runner = runner_for(["TQQQ"])
    survivors, verdicts = filter_symbols(["TQQQ"], runner, TradabilityFilter())
    assert survivors == []
    assert any("leveraged" in r for r in verdicts[0].reasons)


def test_names_without_options_are_rejected():
    runner = FakeRunner(
        assets={"AAA": {"tradable": True, "shortable": True, "easy_to_borrow": True}},
        optionable=set(),
    )
    survivors, verdicts = filter_symbols(["AAA"], runner, TradabilityFilter())
    assert survivors == []
    assert any("options" in r for r in verdicts[0].reasons)


def test_price_bounds_apply():
    runner = runner_for(["AAA", "BBB"])
    rules = TradabilityFilter(min_price=10.0, max_price=100.0)
    survivors, _ = filter_symbols(
        ["AAA", "BBB"], runner, rules, price_hint={"AAA": 3.0, "BBB": 50.0}
    )
    assert survivors == ["BBB"]


def test_a_clean_symbol_survives():
    runner = runner_for(["AAA"])
    survivors, _ = filter_symbols(["AAA"], runner, TradabilityFilter(), {"AAA": 120.0})
    assert survivors == ["AAA"]


# --------------------------------------------------------------------------- #
# candidate pool
# --------------------------------------------------------------------------- #
def pool_at(tmp_path, seeds=None) -> CandidatePool:
    return CandidatePool.load(tmp_path / "pool.json", accumulate_days=5, seeds=seeds or [])


def test_the_pool_accumulates_across_days(tmp_path):
    pool = pool_at(tmp_path)
    pool.observe({"AAA": 0.9}, asof=dt.date(2026, 9, 1))
    pool.observe({"BBB": 0.8}, asof=dt.date(2026, 9, 2))
    # AAA was not hot on day two but is still a candidate.
    assert set(pool.entries) == {"AAA", "BBB"}


def test_persistence_outranks_intensity(tmp_path):
    pool = pool_at(tmp_path)
    for day in range(1, 5):
        pool.observe({"STEADY": 0.4}, asof=dt.date(2026, 9, day))
    pool.observe({"SPIKE": 0.99}, asof=dt.date(2026, 9, 4))
    # Seen on four days beats a single loud spike.
    assert pool.candidates()[0] == "STEADY"


def test_stale_unapproved_names_are_evicted(tmp_path):
    pool = pool_at(tmp_path)
    pool.observe({"OLD": 0.9}, asof=dt.date(2026, 9, 1))
    pool.observe({"NEW": 0.9}, asof=dt.date(2026, 9, 20))
    assert "OLD" not in pool.entries


def test_approved_names_are_never_evicted(tmp_path):
    """Additive-only: a pair that passed cointegration stays."""
    pool = pool_at(tmp_path)
    pool.observe({"KEEP": 0.9}, asof=dt.date(2026, 9, 1))
    pool.mark_screened(["KEEP"], approved=["KEEP"])
    pool.observe({"NEW": 0.9}, asof=dt.date(2026, 9, 30))
    assert "KEEP" in pool.entries


def test_seeds_lead_the_screen_order_and_survive(tmp_path):
    pool = pool_at(tmp_path, seeds=["KO", "PEP"])
    pool.observe({"HOT": 0.99}, asof=dt.date(2026, 9, 1))
    assert pool.candidates()[:2] == ["KO", "PEP"]
    pool.observe({"OTHER": 0.5}, asof=dt.date(2026, 9, 30))
    assert "KO" in pool.candidates()


def test_the_pool_round_trips_to_disk(tmp_path):
    pool = pool_at(tmp_path)
    pool.observe({"AAA": 0.7}, asof=dt.date(2026, 9, 1))
    pool.save()
    reloaded = pool_at(tmp_path)
    assert reloaded.entries["AAA"].best_score == pytest.approx(0.7)


# --------------------------------------------------------------------------- #
# the macro lens
# --------------------------------------------------------------------------- #
def snapshot_with(news: dict[str, float], breadth: dict[str, int] | None = None):
    detail = {s: {"velocity": v, "headlines": [f"{s} headline"]} for s, v in news.items()}
    if breadth:
        detail["__breadth__"] = breadth
    return score_snapshot([result("news", news, replayable=True, **detail)])


def test_a_shared_catalyst_is_not_flagged():
    """Both legs newsy = sector move = the spread is intact. Flagging it would
    cost a session for nothing."""
    lens = MacroLens(cfg=None)
    view = lens.view(
        snapshot_with({"SNDK": 6.0, "MU": 5.5}),
        strategies=["overnight_pairs"],
        pairs=[("SNDK", "MU")],
    )
    assert "SNDK" not in view.flagged_symbols
    assert "MU" not in view.flagged_symbols
    assert view.shared_themes


def test_an_idiosyncratic_catalyst_flags_only_the_moving_leg():
    lens = MacroLens(cfg=None)
    view = lens.view(
        snapshot_with({"SNDK": 8.0, "MU": 1.0}),
        strategies=["overnight_pairs"],
        pairs=[("SNDK", "MU")],
    )
    assert "SNDK" in view.flagged_symbols
    assert "MU" not in view.flagged_symbols
    assert "quiet" in view.flagged_symbols["SNDK"]


def test_the_rules_view_reads_breadth():
    lens = MacroLens(cfg=None)
    risk_on = lens.view(snapshot_with({"A": 1.0}, {"gainers": 18, "losers": 2}))
    risk_off = lens.view(snapshot_with({"A": 1.0}, {"gainers": 2, "losers": 18}))
    assert risk_on.regime == "risk_on"
    assert risk_off.regime == "risk_off"


def test_the_lens_may_only_widen_a_collar_never_narrow_one():
    lens = MacroLens(cfg=None)
    view = lens._parse(
        {"regime": "risk_on", "collar_widening": 0.2}, [], MacroView()
    )
    assert view.collar_widening == 1.0


def test_garbage_from_the_model_falls_back_rather_than_widening_risk():
    lens = MacroLens(cfg=None)
    fallback = MacroView(regime="risk_off", overnight_risk=0.8)
    view = lens._parse(
        {"regime": "banana", "overnight_risk": "very high", "guidance": {"x": "YOLO"}},
        ["overnight_pairs"], fallback,
    )
    assert view.regime == "risk_off"          # kept the fallback
    assert view.overnight_risk == 0.8
    assert view.guidance == {}                # invalid stance dropped


def test_stances_map_to_size_not_just_on_off():
    view = MacroView(guidance={"s": "reduce"})
    assert view.size_multiplier("s") == 0.5
    assert view.may_trade("s") is True
    assert MacroView(guidance={"s": "stand_down"}).may_trade("s") is False
    assert MacroView().size_multiplier("unknown") == 1.0


def test_a_disabled_lens_is_permissive():
    class Cfg:
        class macro:
            enabled = False
        macro = macro()

    view = MacroLens(cfg=Cfg()).view(snapshot_with({"A": 9.0}), ["s"])
    assert view.may_trade("s")
    assert not view.flagged_symbols
