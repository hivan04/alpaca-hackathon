"""The replay harness: the clock, the modelled chain, and no lookahead.

The properties tested here are the ones that decide whether a backtest number
is worth anything. A chain model with a bug produces a plausible equity curve,
which is the most dangerous failure mode available, so each modelled piece is
pinned to a property rather than to a golden number.
"""

from __future__ import annotations

import collections
import datetime as dt

import pytest

from oaa.backtest.chain import (  # noqa: I001
    ChainModel,
    listed_expiries,
    strike_ladder,
    tier_for,
)
from oaa.backtest.engine import BacktestEngine
from oaa.backtest.ivmodel import IVModel
from oaa.backtest.runner import BacktestRequest, run_backtest
from oaa.backtest.source import HistoricalContextSource, headline_sentiment, synthetic_bars
from oaa.config.loader import load_config, load_settings
from oaa.core import clock
from oaa.core.types import MarketContext, Right

START = dt.date(2026, 6, 1)
END = dt.date(2026, 8, 20)


# --------------------------------------------------------------------------- #
# the freezable clock
# --------------------------------------------------------------------------- #
def test_the_clock_freezes_and_always_releases():
    moment = dt.datetime(2026, 6, 4, 14, 0, tzinfo=dt.timezone.utc)
    assert not clock.is_frozen()
    with clock.frozen(moment):
        assert clock.utcnow() == moment
        assert clock.today() == dt.date(2026, 6, 4)
    assert not clock.is_frozen()


def test_a_bare_date_freezes_inside_the_session():
    with clock.frozen(dt.date(2026, 6, 4)):
        assert clock.today() == dt.date(2026, 6, 4)
        assert clock.utcnow().tzinfo is not None


# --------------------------------------------------------------------------- #
# the modelled chain
# --------------------------------------------------------------------------- #
def test_expiries_are_fridays_and_weeklies_are_a_tier_privilege():
    weekly = listed_expiries(dt.date(2026, 6, 1), 3, 45, weeklies=True)
    monthly = listed_expiries(dt.date(2026, 6, 1), 3, 45, weeklies=False)
    assert weekly and all(d.weekday() == 4 for d in weekly)
    assert all(15 <= d.day <= 21 for d in monthly)
    assert len(monthly) < len(weekly)


def test_an_index_etf_lists_an_expiry_inside_two_days_on_every_weekday():
    """A Fridays-only calendar silently disabled the entire intraday book.

    That book buys 0-2 DTE. On a Monday or a Tuesday a Fridays-only ladder has
    NOTHING inside that window, so the book's own filter was being applied to a
    chain that could not contain a qualifying contract - reported forever as
    "no contracts survived the liquidity filter", which reads as a liquidity
    problem and is in fact an empty shelf.
    """
    monday = dt.date(2026, 6, 1)
    for offset in range(5):                       # Mon .. Fri
        day = monday + dt.timedelta(days=offset)
        daily = listed_expiries(day, 0, 2, weeklies=True, weekdays=(0, 1, 2, 3, 4))
        mwf = listed_expiries(day, 0, 2, weeklies=True, weekdays=(0, 2, 4))
        assert daily, f"a daily-expiry name has no 0-2 DTE contract on {day:%a}"
        assert mwf, f"an M/W/F name has no 0-2 DTE contract on {day:%a}"


def test_the_daily_series_is_near_dated_only():
    """The Monday/Wednesday and daily series are near-dated in reality, and
    bounding them is also what stops the replay building five times the chain
    for contracts no strategy here would ever look at."""
    far = listed_expiries(
        dt.date(2026, 6, 1), 0, 45, weeklies=True, weekdays=(0, 1, 2, 3, 4),
        daily_horizon=7,
    )
    assert all(d.weekday() == 4 for d in far if (d - dt.date(2026, 6, 1)).days > 7)


def test_a_weekly_only_name_never_gets_a_midweek_expiry():
    single_name = listed_expiries(
        dt.date(2026, 6, 1), 0, 45, weeklies=False, weekdays=(0, 1, 2, 3, 4)
    )
    assert single_name and all(d.weekday() == 4 for d in single_name)


def test_the_strike_ladder_brackets_spot_and_is_capped():
    ladder = strike_ladder(560.0, 0.14, step=1.0, max_per_side=10)
    assert min(ladder) <= 560.0 <= max(ladder)
    assert len(ladder) <= 21


def test_puts_are_richer_than_equidistant_calls():
    """A standard equity skew. Without it, a short-put book looks underpaid."""
    model = ChainModel()
    years = 14 / 365
    put_iv = model.iv_at(0.18, 500.0, 450.0, years)
    call_iv = model.iv_at(0.18, 500.0, 550.0, years)
    assert put_iv > call_iv


def test_the_spread_widens_away_from_the_money():
    model = ChainModel()
    tier = tier_for("SPY", model.tier_map, model.tiers)
    assert model.half_spread(2.0, moneyness=2.0, tier=tier) > model.half_spread(
        2.0, moneyness=0.0, tier=tier
    )


def test_liquidity_decays_so_the_config_filters_actually_bind():
    model = ChainModel()
    tier = tier_for("CRM", model.tier_map, model.tiers)
    atm_oi, _ = model.liquidity(0.0, 14, tier)
    wing_oi, _ = model.liquidity(2.5, 14, tier)
    assert atm_oi > wing_oi
    assert wing_oi < 250  # the default min_open_interest refuses this contract


def test_a_built_chain_quotes_both_rights_with_a_crossable_spread():
    model = ChainModel()
    asof = dt.datetime(2026, 6, 3, 14, 0, tzinfo=dt.timezone.utc)
    chain = model.build("SPY", 560.0, asof, 0.14)
    assert chain
    assert {q.right for q in chain} == {Right.CALL, Right.PUT}
    for quote in chain:
        assert quote.bid is not None and quote.ask is not None
        assert quote.ask > quote.bid
        assert quote.expiry > asof.date()


def test_repricing_at_expiry_is_intrinsic_only():
    """Intrinsic once there is genuinely no time left - AFTER the close on the
    expiry day, or any time past it."""
    from zoneinfo import ZoneInfo

    model = ChainModel()
    expiry = dt.date(2026, 6, 19)
    for asof in (
        dt.datetime(2026, 6, 19, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        dt.datetime(2026, 6, 22, 11, 0, tzinfo=ZoneInfo("America/New_York")),
    ):
        mark = model.reprice(
            "SPY", spot=560.0, asof=asof, atm_iv=0.14,
            strike=550.0, expiry=expiry, is_call=True, tier_symbol="SPY",
        )
        assert mark["mid"] == pytest.approx(10.0)


def test_a_zero_dte_contract_still_carries_time_value_during_the_session():
    """The defect that made every intraday trade a -100%.

    `build` priced a 0 DTE contract with half a day of life; `reprice` used
    whole days, hit zero, and returned pure INTRINSIC. So the book bought
    premium and marked it away fifteen minutes later - for an at-the-money long
    that is the entire position. Measured 17-21 Aug: 28 trades, 28 losers, the
    big ones all exiting "stop 15% of premium hit" at -88% to -100%.
    """
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    model = ChainModel()
    expiry = dt.date(2026, 6, 19)
    atm = {"strike": 560.0, "expiry": expiry, "is_call": True, "tier_symbol": "SPY"}

    morning = model.reprice("SPY", 560.0, dt.datetime(2026, 6, 19, 11, 0, tzinfo=et),
                            0.14, **atm)
    afternoon = model.reprice("SPY", 560.0, dt.datetime(2026, 6, 19, 15, 0, tzinfo=et),
                              0.14, **atm)

    assert morning["mid"] > 0.10, "an ATM 0 DTE option at 11:00 is not worthless"
    assert afternoon["mid"] > 0.0
    assert afternoon["mid"] < morning["mid"], "and it decays through the session"


def test_the_entry_price_and_the_mark_agree_on_the_same_contract():
    """One clock, one surface. The two paths disagreeing by half a day of vega
    is a guaranteed loss on every front-expiry trade, and it shows up as a
    strategy failure rather than as the pricing fault it is."""
    from zoneinfo import ZoneInfo

    from oaa.backtest.chain import DEFAULT_TIER_MAP

    moment = dt.datetime(2026, 6, 19, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    model = ChainModel(tier_map=DEFAULT_TIER_MAP, min_dte=0, max_dte=7)
    chain = model.build("SPY", 560.0, moment, 0.14)
    front = [
        q for q in chain
        if q.expiry == moment.date() and q.right.value == "call"
    ]
    assert front, "no 0 DTE calls listed"
    quote = min(front, key=lambda q: abs(q.strike - 560.0))
    mark = model.reprice(
        quote.symbol, 560.0, moment, 0.14,
        strike=quote.strike, expiry=quote.expiry, is_call=True, tier_symbol="SPY",
    )
    assert mark["mid"] == pytest.approx(quote.last, rel=0.02), (
        f"built at {quote.last} and marked at {mark['mid']} on the same moment"
    )


# --------------------------------------------------------------------------- #
# the IV model
# --------------------------------------------------------------------------- #
def _bars(symbol: str = "SPY"):
    return synthetic_bars(symbol, dt.date(2024, 1, 1), END)


def test_the_iv_rv_spread_is_not_a_constant():
    """A constant multiple of realised vol makes the premium gate meaningless."""
    bars = _bars()
    series = IVModel().build(bars)
    spreads = [
        iv - rv
        for iv, rv in zip(series["iv"][60:], series["rv"][60:], strict=True)
        if iv is not None and rv is not None
    ]
    assert len(spreads) > 100
    assert max(spreads) - min(spreads) > 0.02
    assert min(spreads) < 0 < max(spreads)


def test_iv_rank_spans_its_range_and_waits_for_history():
    series = IVModel().build(_bars())
    ranks = [r for r in series["iv_rank"] if r is not None]
    assert series["iv_rank"][5] is None      # not enough history is a veto
    assert min(ranks) < 0.2 and max(ranks) > 0.8


# --------------------------------------------------------------------------- #
# the context source
# --------------------------------------------------------------------------- #
def _source(**kwargs) -> HistoricalContextSource:
    return HistoricalContextSource(
        {"SPY": _bars("SPY"), "QQQ": _bars("QQQ")},
        start=START, end=END, **kwargs,
    )


def test_a_context_never_contains_the_session_it_is_dated_in():
    """The single easiest way to write a beautiful, meaningless backtest."""
    source = _source()
    for moment, contexts in source:
        for context in contexts.values():
            assert context.asof == moment
            for bar in context.bars:
                assert bar["timestamp"].date() < moment.date()
        break


def test_the_spot_is_the_session_open_not_its_close():
    source = _source()
    moment, contexts = next(iter(source))
    history = source.histories["SPY"]
    index = next(
        i for i in range(len(history.bars)) if history.date_at(i) == moment.date()
    )
    assert contexts["SPY"].spot == pytest.approx(history.bars[index]["open"], rel=1e-6)


def test_only_headlines_published_before_the_session_are_visible():
    early = dt.datetime(2026, 6, 15, 12, 0, tzinfo=dt.timezone.utc)   # 08:00 ET
    late = dt.datetime(2026, 6, 15, 20, 0, tzinfo=dt.timezone.utc)    # 16:00 ET
    news = [
        {"created_at": early.isoformat(), "headline": "SPY beats", "symbols": ["SPY"]},
        {"created_at": late.isoformat(), "headline": "SPY plunges", "symbols": ["SPY"]},
    ]
    source = _source(news=news, news_lookback_hours=24)
    seen: list[str] = []
    for moment, contexts in source:
        if moment.date() == dt.date(2026, 6, 15):
            seen = [a["headline"] for a in contexts["SPY"].news]
    assert seen == ["SPY beats"]


def test_headline_polarity_is_signed_and_bounded():
    assert headline_sentiment([{"headline": "Acme beats and surges"}]) > 0
    assert headline_sentiment([{"headline": "Acme misses, plunges"}]) < 0
    assert headline_sentiment([]) == 0.0
    assert -1.0 <= headline_sentiment([{"headline": "beats " * 40}]) <= 1.0


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
def test_a_replay_runs_end_to_end_and_leaves_the_clock_alone():
    result = _run()

    assert not clock.is_frozen()
    assert result.equity_curve
    assert result.provenance["synthetic"] is True
    metrics = result.metrics()
    assert metrics["sessions"] == len(result.equity_curve)
    # A gate log with nothing in it means the gates are not being reached.
    assert result.rejections


def test_nothing_is_left_open_when_the_window_ends():
    result = _run()
    assert all(t.status == "closed" for t in result.trades)
    assert result.metrics()["open_at_end"] == 0


def test_every_trade_carries_the_evidence_that_justified_it():
    result = _run()
    assert result.trades, "the smoke universe should approve at least one trade"
    for trade in result.trades:
        assert trade.thesis
        assert trade.gates.get("checked")
        assert trade.risk_checks
        assert trade.market_state["iv_rank"] is not None
        assert trade.exit_reason != "open"


def test_the_fill_model_is_adverse_on_both_sides():
    """Buying pays up from mid, selling gets hit down from it - always."""
    settings = load_settings()
    engine = BacktestEngine(settings)
    engine.fraction = 0.5
    mark = {"mid": 2.00, "bid": 1.90, "ask": 2.10}
    assert engine._execution_price(mark, buying=True) > mark["mid"]
    assert engine._execution_price(mark, buying=False) < mark["mid"]

    engine.fraction = 0.0
    assert engine._execution_price(mark, buying=True) == pytest.approx(mark["mid"])
    engine.fraction = 1.0
    assert engine._execution_price(mark, buying=True) == pytest.approx(mark["ask"])
    assert engine._execution_price(mark, buying=False) == pytest.approx(mark["bid"])


def test_crossing_more_spread_costs_more_spread():
    """The knob is monotonic in what it charges.

    Net P&L is NOT monotonic in it and a test that asserted so would be wrong:
    a different entry price changes the credit, which changes when the profit
    target trips, which changes the exit date. That path dependence is real.
    What must hold is that the modelled spread bill rises with the fraction.
    """
    costs = []
    for fraction in (0.0, 0.25, 0.5, 1.0):
        result = _run(slippage_spread_fraction=fraction)
        costs.append(result.metrics()["spread_cost"])
    assert costs[0] == 0.0
    assert costs == sorted(costs)
    assert costs[-1] > costs[1]


def test_costs_are_charged_and_reported_separately_from_gross():
    result = _run()
    metrics = result.metrics()
    assert metrics["fees_paid"] > 0
    assert metrics["spread_cost"] > 0
    assert metrics["net_pnl"] <= metrics["gross_pnl"]


def test_the_engine_refuses_a_universe_with_no_strategy():
    settings = load_settings()
    with pytest.raises(ValueError):
        run_backtest(
            settings,
            BacktestRequest(
                symbols=["SPY"], start=START, end=END,
                strategies=["not_a_strategy"], source="synthetic",
            ),
        )


def test_provenance_records_what_was_modelled():
    result = _run()
    provenance = result.provenance
    assert provenance["synthetic"] is True
    assert "SYNTHETIC" in provenance["data_source"]
    assert provenance["source"]["chain_model"]["skew"] < 0        # put skew present
    assert provenance["source"]["iv_model"]["vrp_multiple"] > 1.0  # a real premium
    assert provenance["risk"]["allow_undefined_risk"] is False


# --------------------------------------------------------------------------- #
# the profile guard
# --------------------------------------------------------------------------- #
def test_an_explicit_profile_beats_the_environment(monkeypatch):
    """OAA_PROFILE in .env silently winning over --profile is how a dev run
    ends up on the judged account."""
    monkeypatch.setenv("OAA_PROFILE", "dev")
    assert load_config(profile="judged").profile == "judged"
    assert load_config().profile == "dev"


def test_the_engine_builds_without_a_source():
    settings = load_settings()
    engine = BacktestEngine(settings)
    assert engine.costs.enabled
    assert engine.broker.account().equity == settings.config.backtest.initial_cash


# --------------------------------------------------------------------------- #
# the critic, in replay
# --------------------------------------------------------------------------- #
def _run(settings=None, **kwargs):
    """A replay that is guaranteed to open positions.

    These tests check ENGINE MECHANICS - are costs charged, does the critic's
    verdict reach the trade record, does the replay memory only ever hold
    closed trades. They need trades to exist; they are not evidence about the
    strategy. Rather than tune a synthetic price path until it happens to
    produce a rich-vol regime - which makes the fixture fragile and says
    nothing real - the premium gate is relaxed here explicitly. The gate's own
    behaviour is tested against hand-built regimes in test_strategies.py.
    """
    settings = settings or load_settings()
    for ref in settings.config.strategies:
        if ref.name == "vol_carry":
            ref.params.setdefault("premium_gate", {})["iv_rank_min"] = 0.0
            ref.params["premium_gate"]["iv_rv_spread_min"] = -1.0
    # The critic is off unless a test asks for it. With the premium gate
    # relaxed the ideas are genuinely mediocre, so a live critic declines them
    # all - which is the critic working, and would make every mechanics test
    # depend on it.
    kwargs.setdefault("critic_mode", "off")
    # One scan a session. The config now runs four, which is right for finding
    # trades and quadruples the cost of every test that drives the engine.
    kwargs.setdefault("session_times_et", ["10:00"])
    return run_backtest(
        settings,
        BacktestRequest(
            symbols=["SPY", "QQQ"], start=START, end=END, strategies=["vol_carry"],
            source="synthetic", use_news=False, **kwargs,
        ),
    )


def test_the_replay_runs_the_live_decision_path_in_the_live_order():
    result = _run(critic_mode="heuristic")
    pipeline = result.provenance["decision_pipeline"]
    assert pipeline.index("critic") < pipeline.index("risk_engine")
    assert pipeline[0] == "modelled_cost"
    assert pipeline[-1] == "execute"


def test_the_heuristic_critic_scores_every_candidate_deterministically():
    def once():
        settings = load_settings()
        settings.config.agents.critic.min_score_to_trade = 0.0
        return _run(settings=settings, critic_mode="heuristic")

    first, second = once(), once()
    assert first.provenance["critic"]["mode"] == "heuristic"
    assert first.provenance["critic"]["scored"] > 0
    assert first.provenance["critic"]["llm_calls"] == 0
    assert first.metrics()["net_pnl"] == second.metrics()["net_pnl"]


def test_turning_the_critic_off_scores_nothing():
    result = _run(critic_mode="off")
    assert result.provenance["critic"]["mode"] == "off"
    assert result.provenance["critic"]["scored"] == 0
    for trade in result.trades:
        assert trade.critic["source"] == "critic_off"


def test_every_trade_carries_the_critics_verdict():
    settings = load_settings()
    settings.config.agents.critic.min_score_to_trade = 0.0   # score, never refuse
    result = _run(settings=settings, critic_mode="heuristic")
    assert result.trades
    for trade in result.trades:
        assert trade.critic["verdict"] == "trade"
        assert trade.critic["reasoning"]
        assert 0.0 <= trade.critic["score"] <= 1.0


def test_the_critic_can_decline_and_the_decline_is_logged():
    """A critic that can never refuse is decoration. Raise the floor to 1.01
    and every candidate must be declined before it ever reaches risk."""
    settings = load_settings()
    settings.config.agents.critic.min_score_to_trade = 1.01
    result = _run(settings=settings, critic_mode="heuristic")
    assert result.trades == []
    assert result.provenance["critic"]["declined"] > 0
    assert "critic" in result.rejection_funnel()
    declined = [r for r in result.rejections if r.stage == "critic"]
    assert declined and "critic passed" in declined[0].reason


def test_an_unknown_critic_mode_is_refused():
    from oaa.backtest.critic import ReplayCritic

    with pytest.raises(ValueError):
        ReplayCritic(load_config(), mode="vibes")


def test_llm_mode_degrades_to_the_heuristic_when_the_provider_is_unreachable():
    """Downtime must cost reasoning, never trading - the live rule."""
    from oaa.agents.llm import NullClient
    from oaa.backtest.critic import ReplayCritic

    cfg = load_config()
    critic = ReplayCritic(cfg, mode="llm")
    assert isinstance(critic.critic.llm, NullClient) or critic.mode == "llm"
    if critic.mode != "llm":
        assert critic.stats.mode == "heuristic"


def test_a_failed_model_call_is_never_cached(tmp_path):
    """Caching an outage would freeze it into the run and make it look like a
    verdict the model actually gave."""
    from oaa.backtest.critic import ReplayCritic

    cfg = load_config()
    critic = ReplayCritic(cfg, mode="llm", cache_dir=tmp_path)
    if critic.mode != "llm":
        pytest.skip("no LLM provider configured in this environment")
    critic.critic.llm.complete = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    settings = load_settings()
    engine = BacktestEngine(settings, critic=critic)
    assert engine.critic is critic
    assert critic._cache == {}


def test_the_replay_memory_only_ever_holds_closed_trades():
    """The critic is fed outcomes, and a backtest that feeds it the future is
    not a backtest."""
    settings = load_settings()
    settings.config.agents.critic.min_score_to_trade = 0.0
    result = _run(settings=settings, critic_mode="heuristic")
    assert result.provenance["memory"] is True
    closes = [t for t in result.trades if t.closed_at]
    assert closes  # something must have closed for the memory to be exercised


def test_the_anthropic_client_only_sends_parameters_its_sdk_accepts():
    """`temperature` left `messages.create` in the 1.x SDK, and pyproject pins
    `anthropic>=0.34`, so a fresh install broke every critic call - silently,
    because json_complete swallows the TypeError."""
    from oaa.agents.llm import _accepted_params

    def new_sdk(*, model, max_tokens, messages, system=None, tools=None):
        return None

    accepted = _accepted_params(new_sdk)
    assert "temperature" not in accepted
    assert {"model", "max_tokens", "messages"} <= accepted

    def old_sdk(*, model, max_tokens, messages, temperature=None, **kwargs):
        return None

    assert _accepted_params(old_sdk) == set()  # **kwargs: send everything


def test_a_null_backtest_llm_block_falls_back_to_the_live_provider():
    from oaa.backtest.critic import ReplayCritic

    cfg = load_config()
    cfg.backtest.critic.llm = None
    critic = ReplayCritic(cfg, mode="heuristic")
    assert critic.llm_cfg is cfg.agents.llm
    assert critic.describe()["provider_config"]["shared_with_live"] is True


def test_the_critic_cache_key_separates_models():
    """Two models must not share a cache entry, or switching model silently
    replays the old one's verdicts."""
    import datetime as _dt

    from oaa.backtest.critic import _fingerprint
    from oaa.core.types import Leg, Side, StructureType, TradeIdea

    market = MarketContext(symbol="SPY", asof=_dt.datetime(2026, 6, 4, tzinfo=_dt.timezone.utc),
                           spot=560.0, iv_rank=0.8)
    idea = TradeIdea(
        symbol="SPY", strategy="vol_carry", structure=StructureType.VERTICAL_CREDIT,
        legs=[Leg(symbol="SPY260619P00550000", side=Side.SELL)], net_price=-1.0,
    )
    a = _fingerprint(idea, market, "", "gemini-2.5-flash")
    b = _fingerprint(idea, market, "", "gemini-2.5-flash-lite")
    assert a != b
    assert a == _fingerprint(idea, market, "", "gemini-2.5-flash")


def _fake_contracts(symbol="SPY", spot=500.0, expiries=(14, 35)):
    from oaa.backtest.chain import strike_ladder

    rows = []
    for dte in expiries:
        expiry = (START + dt.timedelta(days=dte)).isoformat()
        for strike in strike_ladder(spot, 0.10, step=5.0, max_per_side=8):
            for kind in ("call", "put"):
                rows.append({
                    "symbol": f"{symbol}{dte:03d}{kind[0].upper()}{int(strike * 1000):08d}",
                    "underlying": symbol, "expiry": expiry, "strike": strike,
                    "type": kind, "style": "american", "size": 100,
                    "open_interest": 5_000,
                })
    return {symbol: rows}


def _fake_option_bars(contracts, spot=500.0, vol=0.22, days=(0, 1, 2), volume=250):
    """Bars priced at a KNOWN vol, so inversion can be checked against truth."""
    from oaa.backtest.pricing import bs_price

    out = {}
    for row in contracts["SPY"]:
        expiry = dt.date.fromisoformat(row["expiry"])
        rows = []
        for offset in days:
            day = START + dt.timedelta(days=offset)
            years = max((expiry - day).days, 1) / 365
            price = bs_price(spot, row["strike"], years, vol, row["type"] == "call")
            if price < 0.05:
                continue
            rows.append({
                "timestamp": dt.datetime.combine(day, dt.time(21, 0), tzinfo=dt.timezone.utc),
                "open": price, "high": price, "low": price, "close": price,
                "volume": volume, "trade_count": 10, "vwap": price,
            })
        if rows:
            out[row["symbol"]] = rows
    return out


def _builder(**kwargs):
    from oaa.backtest.realchain import RealChainBuilder

    contracts = _fake_contracts()
    return RealChainBuilder.from_payload(
        contracts, _fake_option_bars(contracts, **kwargs), ChainModel()
    )


def test_the_real_chain_only_offers_contracts_that_were_actually_listed():
    """No synthetic ladder: the strategy cannot pick a strike that never
    existed, which a modelled chain will happily hand it."""
    builder = _builder()
    listed = {c.symbol for c in builder.contracts["SPY"]}
    chain = builder.build(
        "SPY", 500.0, dt.datetime.combine(START, dt.time(14, 0), tzinfo=dt.timezone.utc),
        0.20, min_dte=3, max_dte=45,
    )
    assert chain
    assert {q.symbol for q in chain} <= listed
    assert {q.strike for q in chain} <= {c.strike for c in builder.contracts["SPY"]}


def test_implied_vol_is_recovered_from_the_traded_price():
    """The whole point of the rebuild: IV is measured, not assumed."""
    builder = _builder(vol=0.27)
    chain = builder.build(
        "SPY", 500.0, dt.datetime.combine(START, dt.time(14, 0), tzinfo=dt.timezone.utc),
        0.99,                       # a deliberately wrong fallback
        min_dte=3, max_dte=45,
    )
    near = [q for q in chain if abs(q.strike - 500.0) <= 10]
    assert near
    for quote in near:
        assert quote.implied_volatility == pytest.approx(0.27, abs=0.01)
    assert builder.coverage.iv_recovered > 0
    assert builder.coverage.real_fraction > 0.9


def test_a_contract_that_never_traded_is_modelled_and_counted():
    """Coverage has to be visible. A harness that silently mixes measured and
    invented marks is worse than one that only invents."""
    from oaa.backtest.realchain import RealChainBuilder

    contracts = _fake_contracts()
    builder = RealChainBuilder.from_payload(contracts, {}, ChainModel())
    chain = builder.build(
        "SPY", 500.0, dt.datetime.combine(START, dt.time(14, 0), tzinfo=dt.timezone.utc),
        0.20, min_dte=3, max_dte=45,
    )
    assert chain                                   # still quotable
    assert builder.coverage.marks_from_bars == 0
    assert builder.coverage.marks_modelled == len(chain)
    assert builder.coverage.real_fraction == 0.0


def test_a_low_volume_print_is_not_treated_as_a_market():
    """One lot crossing at 15:59 is a print, not liquidity."""
    builder = _builder(volume=0)
    builder.build(
        "SPY", 500.0, dt.datetime.combine(START, dt.time(14, 0), tzinfo=dt.timezone.utc),
        0.20, min_dte=3, max_dte=45,
    )
    assert builder.coverage.marks_from_bars == 0


def test_repricing_prefers_the_real_bar_and_flags_when_it_cannot():
    builder = _builder()
    expiry = START + dt.timedelta(days=14)
    contract = next(
        c for c in builder.contracts["SPY"] if c.expiry == expiry and c.strike == 500.0
    )
    real = builder.reprice(
        contract.symbol, 500.0, START, 0.20, 500.0, expiry, contract.is_call, "SPY"
    )
    assert real["real"] == 1.0
    missing = builder.reprice(
        "SPY_NOT_A_CONTRACT", 500.0, START, 0.20, 500.0, expiry, True, "SPY"
    )
    assert missing["real"] == 0.0
    assert missing["mid"] > 0


def test_atm_iv_comes_back_at_the_vol_the_prints_were_made_at():
    builder = _builder(vol=0.31)
    assert builder.atm_iv("SPY", 500.0, START) == pytest.approx(0.31, abs=0.01)
    assert builder.atm_iv("SPY", 500.0, START + dt.timedelta(days=30)) is None


def test_a_real_chain_source_drives_the_context_and_records_provenance():
    from oaa.backtest.source import (
        HistoricalContextSource,
    )

    builder = _builder()
    bars = synthetic_bars("SPY", dt.date(2024, 1, 1), END)
    source = HistoricalContextSource(
        {"SPY": bars}, start=START, end=START + dt.timedelta(days=3),
        real_chain=builder, min_history=10,
    )
    described = source.describe()
    assert described["chain_source"] == "real"
    assert described["coverage"]["contracts_listed"] > 0
    for _, contexts in source:
        market = contexts["SPY"]
        assert "real Alpaca option bars" in market.enrichment["chain_source"]
        break


def test_an_empty_real_chain_is_not_quietly_replaced_by_a_modelled_one():
    """Falling through to the model when nothing was listed would manufacture
    a chain that did not exist on that date."""
    from oaa.backtest.realchain import RealChainBuilder
    from oaa.backtest.source import (
        HistoricalContextSource,
    )

    empty = RealChainBuilder.from_payload({}, {}, ChainModel())
    source = HistoricalContextSource(
        {"SPY": synthetic_bars("SPY", dt.date(2024, 1, 1), END)},
        start=START, end=START + dt.timedelta(days=3),
        real_chain=empty, min_history=10,
    )
    for _, contexts in source:
        assert contexts["SPY"].chain == []
        break


def test_iv_rank_falls_back_to_the_model_when_prints_are_too_thin():
    """Ranking a percentile over three observations is not a percentile."""
    from oaa.backtest.source import (
        HistoricalContextSource,
    )

    source = HistoricalContextSource(
        {"SPY": synthetic_bars("SPY", dt.date(2024, 1, 1), END)},
        start=START, end=END, real_chain=_builder(), min_history=10,
        min_iv_observations=20,
    )
    assert "modelled" in source.iv_provenance["SPY"]


def test_the_probe_script_imports_and_documents_itself():
    """It is the only way to check the API assumptions on a real account, so it
    must at least be runnable."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "probe_option_data.py"
    assert path.exists()
    spec = importlib.util.spec_from_file_location("probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.__doc__ and "historical option bars" in module.__doc__


# --------------------------------------------------------------------------- #
# what the first real run broke on
# --------------------------------------------------------------------------- #
def test_adjusted_contracts_are_dropped_before_the_bars_request():
    """`1MSFT251219C00369000` - a contract adjusted by a corporate action - is
    listed by the contracts endpoint and REJECTED by the bars endpoint's symbol
    regex. One in a batch used to fail the whole fetch, silently sending the
    entire run back to the modelled chain."""
    from oaa.backtest.feed import _standard_occ

    good, bad = _standard_occ([
        "MSFT251219C00369000", "1MSFT251219C00369000",
        "SPY260626P00275000", "AAPL2251219C00100000",
    ])
    assert good == ["MSFT251219C00369000", "SPY260626P00275000"]
    assert bad == ["1MSFT251219C00369000", "AAPL2251219C00100000"]


def test_only_contracts_within_reach_of_the_underlying_are_fetched():
    """Listing every strike of every weekly over eight months of SPY is ~60k
    contracts and ~600 bar requests for a chain the strategy never reads."""
    from oaa.backtest.runner import _relevant

    by_day = {START + dt.timedelta(days=i): 500.0 for i in range(0, 60)}
    expiry = (START + dt.timedelta(days=30)).isoformat()
    listed = [
        {"symbol": f"X{int(k)}", "strike": float(k), "expiry": expiry}
        for k in (300, 450, 495, 500, 505, 550, 900)
    ]
    kept = {c["strike"] for c in _relevant(
        listed, by_day=by_day, replay_from=START, dte_range=(3, 45),
        band=0.10, history_band=0.05,
    )}
    assert kept == {450.0, 495.0, 500.0, 505.0, 550.0}
    assert 300.0 not in kept and 900.0 not in kept


def test_pre_window_contracts_use_the_tighter_history_band():
    """Contracts before the replay exist only to recover an ATM implied-vol
    series, so they need near-the-money strikes, not the full ladder."""
    from oaa.backtest.runner import _relevant

    history_day = START - dt.timedelta(days=30)
    by_day = {history_day + dt.timedelta(days=i): 500.0 for i in range(0, 40)}
    expiry = (history_day + dt.timedelta(days=20)).isoformat()
    listed = [
        {"symbol": f"X{int(k)}", "strike": float(k), "expiry": expiry}
        for k in (450, 480, 500, 520, 550)
    ]
    kept = {c["strike"] for c in _relevant(
        listed, by_day=by_day, replay_from=START, dte_range=(3, 45),
        band=0.10, history_band=0.05, history_stride=1,
    )}
    assert kept == {480.0, 500.0, 520.0}      # +/-5%, not +/-10%


def test_a_run_records_the_chain_source_it_asked_for_and_the_one_it_got():
    """Asking for real prices and silently getting modelled ones is the exact
    failure that makes a backtest untrustworthy."""
    settings = load_settings()
    result = _run(critic_mode="off")           # synthetic: never uses real bars
    assert result.provenance["chain_source_used"] == "modelled"
    assert "chain_source_requested" in result.provenance
    _ = settings


def test_the_listing_dte_range_comes_from_the_strategies_not_the_envelope():
    """`options.max_days_to_expiry` is 45, but vol_carry trades 7-14 and
    event_premium 1-5. Listing real contracts across the envelope instead of
    what anything trades is a threefold overcount - and against a name that
    lists an expiration every trading day it is the difference between three
    thousand contracts and forty thousand."""
    from oaa.backtest.runner import tradable_dte_range

    cfg = load_config()
    low, high = tradable_dte_range(cfg)
    assert (low, high) != (cfg.options.min_days_to_expiry, cfg.options.max_days_to_expiry)
    assert low <= 7 and high >= 14           # covers vol_carry's 7-14
    assert high < cfg.options.max_days_to_expiry


def test_the_chain_window_reaches_the_intraday_books_expiries():
    """The window is what strategies must SEE, not what the global options
    envelope allows. Built at options.min_days_to_expiry (3) while the intraday
    book filters for 0-2 DTE, it handed that book a chain with zero qualifying
    contracts on every session of every symbol."""
    from oaa.backtest.runner import tradable_dte_range

    cfg = load_config()
    low, _high = tradable_dte_range(cfg)
    assert low == 0, "the intraday book buys the front expiry - 0 DTE must be visible"
    assert low < cfg.options.min_days_to_expiry


def test_a_declared_window_widens_the_chain_but_a_silent_one_does_not():
    """`chain_dte_window` returning None means 'the inferred window covers me'.
    If the base class returned the global envelope instead, every strategy
    would widen the chain to 45 days and the narrowing would be pointless."""
    from oaa.backtest.runner import tradable_dte_range
    from oaa.strategies.base import load_strategies

    cfg = load_config()
    strategies = load_strategies(cfg)
    assert any(s.chain_dte_window() is not None for s in strategies)
    assert tradable_dte_range(cfg, strategies) == tradable_dte_range(cfg)


def test_the_modelled_chain_actually_contains_a_front_expiry():
    """The end-to-end version of the two tests above: build the chain the way
    the runner does and assert a 0-2 DTE contract exists on a Monday, the day
    a Fridays-only calendar was emptiest."""
    from oaa.backtest.chain import DEFAULT_TIER_MAP
    from oaa.backtest.runner import tradable_dte_range

    cfg = load_config()
    low, high = tradable_dte_range(cfg)
    model = ChainModel(tier_map=DEFAULT_TIER_MAP, min_dte=low, max_dte=high)
    monday = dt.datetime(2026, 6, 1, 14, 40, tzinfo=dt.timezone.utc)
    quotes = model.build("SPY", 640.0, monday, 0.14)
    front = [q for q in quotes if (q.expiry - monday.date()).days <= 2]
    assert front, "no 0-2 DTE contract on a Monday - the intraday book cannot trade"


def test_history_expiries_are_thinned_to_one_per_stride():
    """SPY lists an expiration almost every trading day. One ATM implied-vol
    reading per session needs one expiry per week, not thirty-five."""
    from oaa.backtest.runner import _relevant

    first = START - dt.timedelta(days=120)
    by_day = {
        first + dt.timedelta(days=i): 500.0
        for i in range(0, 200)
        if (first + dt.timedelta(days=i)).weekday() < 5
    }
    listed = []
    for offset in range(0, 90):                    # an expiry every single day
        expiry = first + dt.timedelta(days=offset)
        listed.append({"symbol": f"X{offset}", "strike": 500.0,
                       "expiry": expiry.isoformat()})

    dense = _relevant(listed, by_day, START, dte_range=(6, 16),
                      band=0.06, history_band=0.03, history_stride=1)
    thinned = _relevant(listed, by_day, START, dte_range=(6, 16),
                        band=0.06, history_band=0.03, history_stride=7)
    assert len(thinned) < len(dense) / 4
    assert thinned                                  # but not thinned to nothing


def test_the_narrowed_listing_is_a_large_reduction_on_a_daily_expiry_name():
    """The regression that produced 44,596 'relevant' SPY contracts."""
    from oaa.backtest.runner import _relevant

    start = dt.date(2026, 8, 10)
    hist = start - dt.timedelta(days=180)
    by_day, day = {}, hist
    while day <= dt.date(2026, 8, 26):
        if day.weekday() < 5:
            by_day[day] = 760.0
        day += dt.timedelta(days=1)

    listed, day = [], hist
    while day <= dt.date(2026, 8, 26) + dt.timedelta(days=52):
        if day.weekday() < 5:
            for strike in range(646, 874, 4):       # coarse grid keeps the test quick
                listed.append({"symbol": f"S{day:%y%m%d}{strike}", "strike": float(strike),
                               "expiry": day.isoformat()})
        day += dt.timedelta(days=1)

    wide = _relevant(listed, by_day, start, dte_range=(3, 45), band=0.14,
                     history_band=0.05, history_stride=1)
    narrow = _relevant(listed, by_day, start, dte_range=(6, 16), band=0.06,
                       history_band=0.03, history_stride=7)
    assert len(narrow) < len(wide) * 0.35
    assert narrow


def test_the_contract_cap_keeps_the_strikes_the_replay_window_needs():
    """The 0%-coverage bug. A name that drifted from 700 to 765 had its cap
    rank by distance from the MEAN close (~700), so it kept strikes around 700
    and discarded everything near the money in the window being tested. The
    chain came back empty every session while every contract looked accounted
    for."""
    from oaa.backtest.runner import _rank_for_cap

    bars = []
    for i in range(120):
        day = START - dt.timedelta(days=120 - i)
        bars.append({"timestamp": dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc),
                     "close": 700.0 + i * 0.55})          # drifts 700 -> 765
    spot = bars[-1]["close"]
    expiry = (START + dt.timedelta(days=10)).isoformat()
    listed = [
        {"symbol": f"W{k}", "strike": float(k), "expiry": expiry}
        for k in (690, 700, 710, 755, 765, 775)
    ]
    ranked = _rank_for_cap(listed, bars, START)
    top = [c["strike"] for c in ranked[:3]]
    assert abs(top[0] - spot) < 12          # nearest the money in the WINDOW
    assert 765.0 in top and 700.0 not in top


def test_history_contracts_are_capped_away_before_window_contracts():
    from oaa.backtest.runner import _rank_for_cap

    bars = [{"timestamp": dt.datetime.combine(START - dt.timedelta(days=30 - i),
                                              dt.time(), tzinfo=dt.timezone.utc),
             "close": 500.0} for i in range(30)]
    listed = [
        {"symbol": "past", "strike": 500.0,
         "expiry": (START - dt.timedelta(days=5)).isoformat()},
        {"symbol": "future-far", "strike": 560.0,
         "expiry": (START + dt.timedelta(days=10)).isoformat()},
    ]
    ranked = [c["symbol"] for c in _rank_for_cap(listed, bars, START)]
    assert ranked[0] == "future-far"       # even a far strike in the window wins


def test_a_run_that_prices_nothing_says_so_rather_than_reporting_zero_percent():
    """0 real and 0 modelled is not '0% real' - it is 'nothing was measured',
    and the two must not render the same."""
    from oaa.backtest.realchain import RealChainBuilder
    from oaa.backtest.source import (
        HistoricalContextSource,
    )

    empty = RealChainBuilder.from_payload({}, {}, ChainModel())
    source = HistoricalContextSource(
        {"SPY": synthetic_bars("SPY", dt.date(2024, 1, 1), END)},
        start=START, end=START + dt.timedelta(days=5),
        real_chain=empty, min_history=10,
    )
    for _ in source:
        pass
    described = source.describe()
    assert described["chain_requests"] > 0
    assert described["empty_chain_sessions"] == described["chain_requests"]
    assert described["coverage"]["marks_from_real_bars"] == 0
    assert described["coverage"]["marks_modelled"] == 0


# --------------------------------------------------------------------------- #
# the volatility estimator - the IEX closing-print problem
# --------------------------------------------------------------------------- #
def test_garman_klass_ignores_a_corrupted_closing_print():
    """The free feed is IEX - ~2% of the tape - and its daily close is the last
    IEX trade, not the closing auction. `vol_carry` gates on IV - RV, so an RV
    inflated by one bad print vetoes trades that were never marginal."""
    from oaa.data.indicators import garman_klass_vol, realised_vol

    clean = [
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
        for _ in range(40)
    ]
    noisy = [dict(bar) for bar in clean]
    for i in range(0, 40, 2):                      # alternate closes off by 2%
        noisy[i]["close"] = 102.0

    noisy_cc = realised_vol(noisy, 20)
    clean_cc = realised_vol(clean, 20)
    gk_noisy = garman_klass_vol(noisy, 20)

    # close-to-close is wrecked by the bad prints...
    assert noisy_cc > clean_cc * 3
    # ...while Garman-Klass, reading the range, stays far closer to the truth
    assert gk_noisy < noisy_cc


def test_the_estimator_resolves_from_config_for_backtest_and_live_alike():
    """One resolver, so the replay and the live agent cannot disagree about
    what 'realised vol' means - which would make every IV-RV comparison
    between them meaningless."""
    from oaa.data.indicators import garman_klass_vol, realised_vol, vol_estimator

    assert vol_estimator("garman_klass") is garman_klass_vol
    assert vol_estimator("close_to_close") is realised_vol
    assert load_config().data.volatility_estimator == "garman_klass"


def test_garman_klass_falls_back_rather_than_returning_nonsense():
    """It can go negative on a bar that closed outside its own range."""
    from oaa.data.indicators import garman_klass_vol

    broken = [{"open": 100.0, "high": 100.0, "low": 100.0, "close": 130.0}
              for _ in range(30)]
    value = garman_klass_vol(broken, 20)
    assert value is None or value >= 0


def test_the_exit_rules_have_a_breakeven_below_the_observed_hit_rate():
    """A win is +target x credit and a loss is -stop x credit, so breakeven is
    stop / (target + stop). At 30%/2.0x that was 87% against an observed 88% -
    one point of margin before paying any spread at all."""
    exits = {r.name: r.params for r in load_config().enabled_strategies()}["vol_carry"]["exits"]
    target = exits["profit_target_pct"]
    stop = exits["loss_multiple_of_credit"]
    breakeven = stop / (target + stop)
    assert breakeven <= 0.80, f"breakeven {breakeven:.1%} leaves no room for costs"


def test_the_cost_gate_admits_only_spreads_the_exits_can_survive():
    """A cost ceiling above what the exit rules can afford passes trades the
    arithmetic says lose."""
    params = {r.name: r.params for r in load_config().enabled_strategies()}["vol_carry"]
    target = params["exits"]["profit_target_pct"]
    stop = params["exits"]["loss_multiple_of_credit"]
    ceiling = params["cost"]["max_spread_cost_vs_credit"]

    win, loss = target - ceiling, stop + ceiling
    assert win > 0, "the cost ceiling swallows the entire profit target"
    breakeven_at_ceiling = loss / (win + loss)
    assert breakeven_at_ceiling <= 0.88, (
        f"a trade at the cost ceiling needs a {breakeven_at_ceiling:.1%} hit "
        "rate, above the 88% observed"
    )


def test_coverage_is_read_after_the_replay_not_before_it():
    """Provenance is snapshotted at the start of a run, when every counter is
    zero. Reading coverage from that snapshot reported '0% of marks came from
    real bars' on runs whose every leg was marked from a real bar - an alarm
    that sent two debugging sessions after a bug that was not there."""
    result = _run()
    source_meta = result.provenance["source"]
    assert source_meta["chain_requests"] > 0, "the source description is stale"
    coverage = source_meta["coverage"]
    if coverage is not None:
        assert coverage["marks_from_real_bars"] + coverage["marks_modelled"] > 0


def test_a_trades_own_mark_provenance_agrees_with_the_run_total():
    """The per-trade record said 100% real while the run total said 0%. Those
    two numbers come from the same counters and must never disagree."""
    result = _run()
    marked = [t for t in result.trades if t.real_mark_fraction is not None]
    if not marked:
        pytest.skip("no trade closed with mark provenance recorded")
    coverage = result.provenance["source"]["coverage"]
    if coverage is None:
        pytest.skip("modelled chain: no per-mark provenance to reconcile")
    any_real = any(t.real_mark_fraction > 0 for t in marked)
    assert any_real == (coverage["marks_from_real_bars"] > 0)


# --------------------------------------------------------------------------- #
# Defined risk has to survive the marks
#
# `reprice` decides per CONTRACT whether to use a real bar or the model. In a
# condor the short strikes trade and the far wings often do not, so on a
# stressed session the short is marked at a real elevated print and its own wing
# on a calm modelled vol. The vertical's value is then not bounded by its strike
# width and the structure stops being defined-risk. Losses of 170% of max_loss
# appeared in a real run because of this.
# --------------------------------------------------------------------------- #
def test_a_mixed_surface_breaks_the_width_bound_which_is_why_we_reprice():
    """The arithmetic behind `_leg_marks` re-pricing onto one surface."""
    from oaa.backtest.pricing import bs_price

    spot, years, rate, width = 93.0, 7 / 365, 0.04, 5.0
    calm, stressed = 0.20, 0.55

    short_real = bs_price(spot, 97, years, stressed, False, rate)   # traded close
    wing_calm = bs_price(spot, 92, years, calm, False, rate)        # modelled
    wing_same = bs_price(spot, 92, years, stressed, False, rate)    # one surface

    mixed = short_real - wing_calm
    single = short_real - wing_same
    assert mixed > single * 1.4          # the mix is materially worse
    assert single < width                # one surface respects the bound
    assert mixed > single                # and the mix inflates the loss


def test_the_engine_clamps_a_loss_that_exceeds_the_structures_defined_risk():
    """`_bounded_gross` holds a position to the risk it was approved on."""
    import datetime as dt

    from oaa.backtest.engine import BacktestEngine, OpenStructure, TradeRecord
    from oaa.core.types import StructureType, TradeIdea

    engine = BacktestEngine(load_settings())
    idea = TradeIdea(
        symbol="SPY", strategy="vol_carry", structure=StructureType.IRON_CONDOR,
        legs=[], quantity=1, net_price=-1.5, max_loss=350.0, max_profit=150.0,
        thesis="t",
    )
    position = OpenStructure(
        record=TradeRecord(
            trade_id="BT9999", symbol="SPY", strategy="vol_carry", book="carry",
            structure="iron_condor", quantity=2, opened_at="2026-07-01T14:00:00+00:00",
        ),
        idea=idea, strategy=None, quantity=2, entry_net=-1.5,
        opened_at=dt.datetime(2026, 7, 1, 14, tzinfo=dt.timezone.utc), legs=[],
    )

    assert engine._bounded_gross(-200.0, position) == -200.0      # inside the bound
    assert engine._bounded_gross(-5_000.0, position) == -700.0    # 350 x 2, clamped
    assert engine._bounded_gross(9_999.0, position) == 300.0      # 150 x 2, clamped
    assert engine._risk_bound_clamps == 2


def test_the_hard_dollar_stop_fires_before_the_credit_relative_one():
    """`exits.max_loss_usd` caps the trade in the units the daily loss limit is
    written in. The credit-relative stop scales with the credit taken, which is
    backwards when what matters is the account."""
    from oaa.core.types import StructureType, TradeIdea
    from oaa.strategies import strategy_registry

    cfg = load_config()
    ref = next(s for s in cfg.strategies if s.name == "vol_carry")
    ref.params.setdefault("exits", {}).update(
        {"max_loss_usd": 900, "loss_multiple_of_credit": 1.5,
         "profit_target_pct": 0.50, "dte_floor": 3}
    )
    strategy = strategy_registry.get("vol_carry")(ref, cfg)
    idea = TradeIdea(
        symbol="SPY", strategy="vol_carry", structure=StructureType.IRON_CONDOR,
        legs=[], quantity=1, net_price=-8.0, max_loss=2_000.0, max_profit=800.0,
        thesis="t", meta={},
    )

    class _Ctx:
        contexts: dict = {}

        def macro_flagged(self, _symbol):
            return False

    # -1.2x credit on an $800 credit is -$960: past the hard stop, but the
    # credit-relative stop at 1.5x would not have fired until -$1,200.
    reason = strategy.should_exit(_Ctx(), idea, -1.2)
    assert reason is not None
    assert "hard stop" in reason

    assert strategy.should_exit(_Ctx(), idea, -1.0) is None   # -$800, inside both


def test_a_zero_dte_option_is_not_expired_on_the_morning_of_its_expiry():
    """The single most expensive line in the replay.

    `expired = any(leg["expiry"] <= moment.date())` marked a 0 DTE long as
    expired on the very next scan after it was opened - fifteen minutes later,
    hours before the close - and settled it at INTRINSIC. For an at-the-money
    long that is the whole position: measured over 11-22 Aug, twelve trades
    settled this way for -$4,602 of a -$4,272 result, one of them at +254%.
    The book looked like a lottery because the harness was running one.
    """
    import datetime as _dt
    import inspect

    from oaa.backtest.engine import BacktestEngine

    src = inspect.getsource(BacktestEngine._manage)
    assert 'leg["expiry"] < moment.date()' in src
    assert 'leg["expiry"] <= moment.date()' not in src

    # and the rule itself, stated plainly
    today = _dt.date(2026, 8, 21)
    assert not (today < today), "a contract expiring today has not expired yet"
    assert _dt.date(2026, 8, 20) < today


def test_the_intraday_book_is_forced_flat_before_the_firewall_cutoff():
    """`time_gate.no_entry_after` stops new entries; it closes nothing. Without
    an exit rule of its own, a position opened at the last scan of the day had
    no later scan and ran into settlement."""
    from oaa.config.loader import load_config
    from oaa.strategies.base import load_strategies

    cfg = load_config()
    book = next(s for s in load_strategies(cfg) if s.name == "intraday_momentum")
    flat_by = str(book.p("exits.flat_by", ""))
    assert flat_by, "the intraday book must declare a flat-by time"

    cutoff = [c for c in cfg.schedule.cycles if c.action == "intraday_cutoff"]
    assert cutoff, "live has a hard cutoff cycle - the backtest must match it"
    assert flat_by < cutoff[0].at, "the book must be flat BEFORE the firewall fires"

    # and the replay needs a moment on which that rule can actually run
    assert any(t > book.p("time_gate.no_entry_after", "14:45")
               for t in cfg.backtest.session_times_et), (
        "no session moment after the last entry time - nothing can close a "
        "position opened at the final scan"
    )


def test_the_replay_spot_moves_through_the_session():
    """The single line that made an intraday book unable to win.

    `spot = bar["open"]` is the correct no-lookahead price at 09:30 and only at
    09:30. Held for the whole day it freezes the underlying: every context from
    10:00 to 15:10 saw one price, so an option could only decay. Measured over
    17-21 Aug that produced 38 trades and 38 losers, each between -0.3% and
    -1.5% - the round trip plus theta, which is what "the underlying never
    moved" costs.

    The intraday bars are already truncated at the moment, so their last close
    is this moment's price with no lookahead.
    """
    import datetime as _dt

    from oaa.backtest.source import (
        HistoricalContextSource,
        synthetic_intraday_bars,
    )

    start, end = _dt.date(2026, 6, 1), _dt.date(2026, 6, 5)
    bars = {"SPY": synthetic_bars("SPY", _dt.date(2026, 1, 1), end)}
    intraday = {"SPY": synthetic_intraday_bars("SPY", bars["SPY"], interval_minutes=5)}
    source = HistoricalContextSource(
        bars, start=start, end=end,
        session_times_et=("10:00", "11:00", "14:00"),
        intraday_by_symbol=intraday,
        market_symbol="SPY",
    )
    spots: dict = collections.defaultdict(set)
    for moment, contexts in source:
        market = contexts.get("SPY")
        if market is not None:
            spots[moment.date()].add(round(market.spot, 4))

    assert spots, "no sessions replayed"
    moving = [day for day, seen in spots.items() if len(seen) > 1]
    assert moving, (
        "spot never changed within any session - the underlying is frozen and "
        "no intraday strategy can be evaluated"
    )


def test_a_strategy_that_throws_is_recorded_not_swallowed():
    """An error is not a decision.

    `_scan` caught every exception from generate() at DEBUG level and moved on,
    with no rejection record. A book failing on every candidate of a run was
    reported as simply quiet - which is how a month-long replay showed zero
    intraday trades AND zero intraday rejections while the same code traded 40
    times over five days. Silence has to mean "declined", never "crashed".
    """
    import inspect

    from oaa.backtest.engine import BacktestEngine

    src = inspect.getsource(BacktestEngine._scan)
    marker = src.index("except Exception")
    handler = src[marker:marker + 1600]
    assert "strategy_error" in handler, "a raising strategy must leave a record"
    assert "log.warning" in handler, "and must not be logged at DEBUG"


# --------------------------------------------------------------------------- #
# the mark path
#
# Entry and exit marks alone cannot separate "the move never developed" from
# "we exited before it developed" - the two produce identical records and
# opposite conclusions about the strategy. The excursion is what tells them
# apart, so every closed trade has to carry it.
# --------------------------------------------------------------------------- #
def test_every_closed_trade_carries_its_excursion():
    result = _run()
    assert result.trades
    for trade in result.trades:
        assert trade.marks_observed > 0, "a trade marked zero times cannot be diagnosed"
        assert trade.mae_usd <= 0 <= trade.mfe_usd
        assert trade.mfe_usd >= trade.mae_usd
        if trade.mfe_pct_of_premium is not None:
            assert trade.mae_pct_of_premium is not None


def test_the_excursion_is_not_uniformly_zero():
    """A recorder that never records is worse than none - it looks like data."""
    result = _run()
    moved = [t for t in result.trades if t.mfe_usd > 0 or t.mae_usd < 0]
    assert moved, "no position ever moved away from its entry mark"


def test_the_excursion_brackets_the_realised_result():
    """Fills are adverse to mid on both sides, so the realised gross can never
    be better than the best mid the position was ever marked at."""
    result = _run()
    for trade in result.trades:
        if trade.marks_observed > 1:
            assert trade.gross_pnl <= trade.mfe_usd + 1e-6


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #
def test_a_run_records_whether_the_tree_it_ran_from_was_dirty():
    """A commit hash alone is not a fingerprint: runs days apart with different
    gates all stamped the same hash and were not comparable."""
    from oaa.backtest.runner import _git_worktree

    settings = load_settings()
    worktree = _git_worktree(settings.root)
    assert set(worktree) == {"commit", "dirty", "diff_sha"}
    if worktree["dirty"]:
        assert worktree["diff_sha"], "a dirty tree must be fingerprinted"
    else:
        assert worktree["diff_sha"] is None


# --------------------------------------------------------------------------- #
# intraday marking
#
# Alpaca's option bars are daily, so a book that opens and closes inside one
# session was marked at the same price all day: 53 of 53 intraday trades in
# runs/backtests/20260829-010710 had entry_mark == exit_mark and lost exactly
# the modelled round trip. These pin the fix.
# --------------------------------------------------------------------------- #
def _intraday_legs():
    return [{
        "symbol": "SPY260821C00640000", "side": "buy", "ratio": 1,
        "strike": 640.0, "expiry": dt.date(2026, 8, 21), "is_call": True,
    }]


def _spy_context(spot: float, iv: float = 0.18) -> MarketContext:
    return MarketContext(
        symbol="SPY", asof=dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.timezone.utc),
        spot=spot, bars=[], chain=[], implied_vol=iv,
    )


def test_an_intraday_mark_moves_when_the_underlying_moves():
    settings = load_settings()
    engine = BacktestEngine(settings)
    moment = dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.timezone.utc)
    legs = _intraday_legs()

    still = engine._leg_marks({"SPY": _spy_context(640.0)}, moment, legs, "SPY", intraday=True)
    moved = engine._leg_marks({"SPY": _spy_context(644.0)}, moment, legs, "SPY", intraday=True)

    a = still[legs[0]["symbol"]]["mid"]
    b = moved[legs[0]["symbol"]]["mid"]
    assert b > a, "a 4-point rally must raise the mark on a 640 call"


def test_the_intraday_model_mark_is_counted():
    settings = load_settings()
    engine = BacktestEngine(settings)
    moment = dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.timezone.utc)
    before = engine._intraday_model_marks
    engine._leg_marks({"SPY": _spy_context(640.0)}, moment, _intraday_legs(), "SPY", intraday=True)
    assert engine._intraday_model_marks == before + 1


def test_a_daily_book_still_marks_from_the_tape():
    """The fix must not reach the carry book: it holds for days, and the daily
    bar is the right granularity there."""
    settings = load_settings()
    engine = BacktestEngine(settings)
    moment = dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.timezone.utc)
    before = engine._intraday_model_marks
    engine._leg_marks({"SPY": _spy_context(640.0)}, moment, _intraday_legs(), "SPY")
    assert engine._intraday_model_marks == before


def test_only_an_intraday_book_declares_intraday_marks():
    settings = load_settings()
    engine = BacktestEngine(settings)
    flags = {s.name: s.marks_intraday for s in engine.strategies}
    assert flags, "no strategies were built"
    for name, intraday in flags.items():
        assert intraday == (name == "intraday_momentum"), name


# --------------------------------------------------------------------------- #
# the vol anchor
#
# The tests above pass on an engine with no real option tape: with no real
# print there is nothing to recover a vol from, `_leg_marks` anchors on the
# modelled `atm_iv`, and the mark moves with spot as it should. The defect
# only exists on the REAL path, which is why a year-long run on real Alpaca
# bars was still frozen while these stayed green.
#
# Measured on runs/backtests/20260830-012950: SPY 743C expiring 22 May had a
# real 21 May bar closing at 2.65 after ranging 0.95-4.00, and the book marked
# it 2.5594 at 14:15 and 2.5889 at 14:45 while spot went 742.73 -> 743.38.
# Across 434 intraday trades the marks moved a MEDIAN of 0.0x what delta says.
# --------------------------------------------------------------------------- #
def _anchored_leg():
    """A month-dated 640 call. Dated deliberately: the recovered vol only
    exists while the print carries time value, and a 0 DTE fixture lands below
    intrinsic on the second spot, falls back to the modelled vol, and passes
    these tests for the wrong reason."""
    return [{
        "symbol": "SPY260918C00640000", "side": "buy", "ratio": 1,
        "strike": 640.0, "expiry": dt.date(2026, 9, 18), "is_call": True,
    }]


def _real_pricer_with_one_print(close: float = 12.0):
    """A real-tape pricer holding one daily print for the test contract."""
    from oaa.backtest.realchain import RealChainBuilder
    return RealChainBuilder.from_payload(
        contracts_by_symbol={"SPY": [{
            "symbol": "SPY260918C00640000", "expiry": "2026-09-18",
            "strike": 640.0, "type": "call",
        }]},
        bars_by_contract={"SPY260918C00640000": [
            {"timestamp": f"2026-08-{day}T04:00:00+00:00",
             "open": close, "high": close, "low": close, "close": close,
             "volume": 10_000.0}
            for day in ("20", "21")
        ]},
        model=ChainModel(),
    )


def test_an_intraday_mark_moves_with_spot_even_against_a_real_print():
    """The regression. Recovering the vol implied by a FIXED daily print at the
    CURRENT spot, then re-pricing at that same spot, is an algebraic fixed
    point: it returns the print. The anchor has to be recovered once and held,
    or the position has no delta and can only ever lose the round trip."""
    settings = load_settings()
    engine = BacktestEngine(settings)
    engine.real_chain = _real_pricer_with_one_print()
    moment = dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.timezone.utc)
    legs = _anchored_leg()

    still = engine._leg_marks({"SPY": _spy_context(640.0)}, moment, legs, "SPY", intraday=True)
    moved = engine._leg_marks({"SPY": _spy_context(645.0)}, moment, legs, "SPY", intraday=True)
    a = still[legs[0]["symbol"]]["mid"]
    b = moved[legs[0]["symbol"]]["mid"]

    # A 5-point rally on an ATM call is worth ~$2.50 at delta ~0.5. Anything
    # under a fifth of that is the frozen mark.
    assert b - a > 0.5, (
        f"mark moved {b - a:.4f} on a 5-point rally against a real print - "
        "the vol anchor is being re-derived at the new spot and pinning the "
        "mark to the daily bar"
    )


def test_the_vol_anchor_is_recovered_once_per_contract_per_session():
    settings = load_settings()
    engine = BacktestEngine(settings)
    engine.real_chain = _real_pricer_with_one_print()
    moment = dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.timezone.utc)
    legs = _anchored_leg()

    engine._leg_marks({"SPY": _spy_context(640.0)}, moment, legs, "SPY", intraday=True)
    key = (legs[0]["symbol"], moment.date())
    assert key in engine._iv_anchor, "no anchor was cached for the contract"
    first = engine._iv_anchor[key]

    engine._leg_marks({"SPY": _spy_context(651.0)}, moment, legs, "SPY", intraday=True)
    assert engine._iv_anchor[key] == first, (
        "the anchor moved with spot - that is the fixed point this cache exists "
        "to break"
    )


def test_a_new_session_recovers_a_fresh_vol_anchor():
    """Frozen WITHIN a session, not across them: the next day's print is new
    information and must be allowed to reset the level."""
    settings = load_settings()
    engine = BacktestEngine(settings)
    engine.real_chain = _real_pricer_with_one_print()
    legs = _anchored_leg()
    day_one = dt.datetime(2026, 8, 20, 15, 0, tzinfo=dt.timezone.utc)
    day_two = dt.datetime(2026, 8, 21, 15, 0, tzinfo=dt.timezone.utc)

    engine._leg_marks({"SPY": _spy_context(640.0)}, day_one, legs, "SPY", intraday=True)
    engine._leg_marks({"SPY": _spy_context(640.0)}, day_two, legs, "SPY", intraday=True)
    assert (legs[0]["symbol"], day_one.date()) in engine._iv_anchor
    assert (legs[0]["symbol"], day_two.date()) in engine._iv_anchor


def test_the_anchor_is_stored_deskewed_so_the_smile_is_not_applied_twice():
    """`reprice` runs `iv_at` on whatever anchor it is handed, so the anchor
    must be an ATM vol, not the leg's own skewed one."""
    from oaa.backtest.chain import ChainModel as _CM
    from oaa.backtest.chain import years_to_expiry as _yte
    from oaa.backtest.engine import _deskew
    model = _CM()
    spot, strike = 640.0, 680.0          # well OTM, where the skew bites hardest
    years = _yte(dt.date(2026, 8, 21), dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.timezone.utc))
    leg_iv = model.iv_at(0.18, spot, strike, years)
    assert abs(leg_iv - 0.18) > 1e-4, "the fixture is not actually skewed"
    assert abs(_deskew(model, leg_iv, spot, strike, years) - 0.18) < 1e-3


# --------------------------------------------------------------------------- #
# management resolution
#
# Scanning and marking are different jobs and were sharing one cadence. A
# position that lives 20-90 minutes was observed 2-6 times in its whole life,
# so no exit dial - target, stop, VWAP re-cross or time stop - could fire
# within an order of magnitude of when it became true, and MFE/MAE was built
# from two samples. Entries stay on `session_times_et`; open positions are now
# marked and managed every `backtest.mark_interval_minutes`.
# --------------------------------------------------------------------------- #
def _intraday_source(**kwargs) -> HistoricalContextSource:
    from oaa.backtest.source import synthetic_intraday_bars
    bars = {"SPY": _bars("SPY")}
    return HistoricalContextSource(
        bars,
        start=START, end=END,
        intraday_by_symbol={
            "SPY": synthetic_intraday_bars("SPY", bars["SPY"], interval_minutes=1)
        },
        session_times_et=("10:00", "10:15"),
        **kwargs,
    )


def test_open_positions_are_marked_between_scans():
    source = _intraday_source()
    start, contexts = next(iter(source))
    end = start + dt.timedelta(minutes=15)

    ticks = list(source.marks_between(start, end, contexts, ["SPY"]))

    assert ticks, "the fine loop produced nothing between two scans"
    moments = [m for m, _ in ticks]
    assert all(start < m < end for m in moments), "a mark landed on or past a scan"
    assert moments == sorted(moments)
    # The point of the change: many observations of one position's life, not two.
    assert len(moments) >= 10


def test_a_fine_context_advances_the_spot_and_carries_the_chain():
    """Marking reads spot and vol; entries read the chain. Only the first is
    rebuilt, which is what makes a one-minute cadence affordable."""
    source = _intraday_source()
    start, contexts = next(iter(source))
    base = contexts["SPY"]
    ticks = list(source.marks_between(start, start + dt.timedelta(minutes=15), contexts, ["SPY"]))

    spots = [c["SPY"].spot for _, c in ticks]
    assert len(set(spots)) > 1, "the underlying was immobile between scans"
    for moment, fine in ticks:
        ctx = fine["SPY"]
        assert ctx.asof == moment
        assert ctx.chain is base.chain, "the chain was rebuilt on a mark"
        assert ctx.implied_vol == base.implied_vol
        assert len(ctx.intraday_bars) >= len(base.intraday_bars)


def test_a_fine_context_still_contains_no_lookahead():
    from oaa.backtest.source import _parse_ts
    source = _intraday_source()
    start, contexts = next(iter(source))
    for moment, fine in source.marks_between(
        start, start + dt.timedelta(minutes=15), contexts, ["SPY"]
    ):
        for bar in fine["SPY"].intraday_bars:
            assert _parse_ts(bar["timestamp"]) <= moment


def test_marks_never_run_across_a_session_boundary():
    """`end` is simply the next scan the source produced, which overnight is
    the next morning. Marking a resident condor once a minute until then is
    both meaningless and slow."""
    source = _intraday_source()
    start, contexts = next(iter(source))
    assert list(source.marks_between(
        start, start + dt.timedelta(days=1), contexts, ["SPY"]
    )) == []


def test_the_fine_cadence_can_be_switched_off():
    source = _intraday_source(mark_interval_minutes=0)
    start, contexts = next(iter(source))
    end = start + dt.timedelta(minutes=15)
    assert list(source.marks_between(start, end, contexts, ["SPY"])) == []


class _CountingSource:
    """A source that records whether the engine asked it for fine marks."""

    def __init__(self) -> None:
        self.asked: list[tuple] = []

    def __iter__(self):
        return iter(())

    def marks_between(self, start, end, contexts, symbols):
        self.asked.append((start, end, list(symbols)))
        return iter(())


def _fake_position(marks_intraday: bool, symbol: str = "SPY"):
    import types
    return types.SimpleNamespace(
        legs=[],
        quantity=1,
        entry_net=0.0,
        marks_observed=0,
        mfe_usd=0.0,
        mae_usd=0.0,
        strategy=types.SimpleNamespace(marks_intraday=marks_intraday, params={}),
        record=types.SimpleNamespace(symbol=symbol, trade_id="T1"),
        idea=types.SimpleNamespace(max_profit=1.0, max_loss=1.0),
    )


def _drive_mark_between(engine, source, positions):
    from oaa.backtest.engine import BacktestResult
    engine._open = list(positions)
    start = dt.datetime(2026, 8, 20, 14, 0, tzinfo=dt.timezone.utc)
    engine._mark_between(
        source, start, {"SPY": _spy_context(640.0)},
        start + dt.timedelta(minutes=15), BacktestResult(),
    )


def test_a_daily_marked_book_is_not_asked_for_fine_marks():
    """`vol_carry` holds for days off a DAILY option tape. Repricing the same
    close sixty times an hour buys nothing and costs the run."""
    engine = BacktestEngine(load_settings())
    source = _CountingSource()
    _drive_mark_between(engine, source, [_fake_position(marks_intraday=False)])
    assert source.asked == []
    assert engine._fine_marks == 0


def test_an_intraday_book_is_asked_for_fine_marks():
    engine = BacktestEngine(load_settings())
    source = _CountingSource()
    _drive_mark_between(engine, source, [_fake_position(marks_intraday=True)])
    assert len(source.asked) == 1
    assert source.asked[0][2] == ["SPY"]


def test_a_flat_book_costs_nothing():
    engine = BacktestEngine(load_settings())
    source = _CountingSource()
    _drive_mark_between(engine, source, [])
    assert source.asked == []


def test_the_fine_loop_marks_and_manages_at_every_tick():
    import types

    from oaa.backtest.engine import BacktestResult
    engine = BacktestEngine(load_settings())
    moments = [
        dt.datetime(2026, 8, 20, 14, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 8, 20, 14, 2, tzinfo=dt.timezone.utc),
    ]
    source = types.SimpleNamespace(
        marks_between=lambda *a, **k: iter(
            [(m, {"SPY": _spy_context(640.0 + i)}) for i, m in enumerate(moments)]
        )
    )
    engine._open = [_fake_position(marks_intraday=True)]
    try:
        engine._mark_between(
            source, moments[0] - dt.timedelta(minutes=1), {"SPY": _spy_context(640.0)},
            moments[-1] + dt.timedelta(minutes=1), BacktestResult(),
        )
    finally:
        clock.unfreeze()
    assert engine._fine_marks == len(moments)


def test_the_run_reports_how_many_fine_marks_it_took():
    """Zero on a run containing an intraday book means the loop never fired
    and every exit dial is again being sampled on the scan grid."""
    result = _run()
    assert "fine_marks" in result.metrics()
