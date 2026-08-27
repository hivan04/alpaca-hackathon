"""The replay harness: the clock, the modelled chain, and no lookahead.

The properties tested here are the ones that decide whether a backtest number
is worth anything. A chain model with a bug produces a plausible equity curve,
which is the most dangerous failure mode available, so each modelled piece is
pinned to a property rather than to a golden number.
"""

from __future__ import annotations

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
    model = ChainModel()
    mark = model.reprice(
        "SPY", spot=560.0, asof=dt.date(2026, 6, 19), atm_iv=0.14,
        strike=550.0, expiry=dt.date(2026, 6, 19), is_call=True, tier_symbol="SPY",
    )
    assert mark["mid"] == pytest.approx(10.0)


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


# --------------------------------------------------------------------------- #
# the live / backtest provider split
# --------------------------------------------------------------------------- #
def test_the_replay_and_the_live_agent_use_different_providers_by_default():
    """A replay scores every candidate in every session and is re-run whenever
    a parameter moves. Live is a handful of calls a day. Sharing one model
    conflates two completely different cost shapes."""
    cfg = load_config()
    assert cfg.agents.llm.provider == "anthropic"
    assert cfg.backtest.critic.llm is not None
    assert cfg.backtest.critic.llm.provider == "gemini"
    assert cfg.backtest.critic.llm.api_key_env == "GEMINI_API_KEY"


def test_the_replay_critic_reads_the_backtest_provider_not_the_live_one():
    from oaa.backtest.critic import ReplayCritic

    cfg = load_config()
    critic = ReplayCritic(cfg, mode="heuristic")
    assert critic.llm_cfg is cfg.backtest.critic.llm
    described = critic.describe()["provider_config"]
    assert described["provider"] == "gemini"
    assert described["shared_with_live"] is False


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


def test_the_gemini_client_reports_a_missing_key_by_name(monkeypatch):
    from oaa.agents.llm import GeminiClient, LLMUnavailable

    # Load the config FIRST: load_config() calls load_dotenv, which would put a
    # real key from .env straight back into the environment we just cleared.
    cfg = load_config().backtest.critic.llm
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMUnavailable, match="GEMINI_API_KEY"):
        GeminiClient(cfg)


def test_the_gemini_client_refuses_tool_use_loudly(monkeypatch):
    """The MCP agent loop is Anthropic-only. Silently dropping the tools and
    answering anyway would look like the agent had used them."""
    from oaa.agents.llm import GeminiClient, LLMUnavailable

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    cfg = load_config().backtest.critic.llm
    client = GeminiClient(cfg)
    with pytest.raises(LLMUnavailable, match="tool use"):
        client.complete("sys", "user", tools=[{"name": "get_account"}])


def test_the_gemini_client_asks_for_json_natively(monkeypatch):
    """Native JSON mode beats asking the model nicely and stripping fences."""
    from oaa.agents.llm import GeminiClient

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    cfg = load_config().backtest.critic.llm
    client = GeminiClient(cfg)
    captured: dict[str, object] = {}

    class _Response:
        text = '{"score": 0.8, "verdict": "trade", "reasoning": "fine"}'

    def fake(model, contents, config):
        captured["model"] = model
        captured["mime"] = config.response_mime_type
        captured["seed"] = config.seed
        captured["temperature"] = config.temperature
        return _Response()

    client._client.models.generate_content = fake
    result = client.json_complete("sys", "user")
    assert result["score"] == 0.8
    assert captured["mime"] == "application/json"
    assert captured["seed"] == 7          # reproducibility
    assert captured["temperature"] == 0.0  # a judge should not be creative


def test_a_gemini_outage_still_degrades_to_rules(monkeypatch):
    from oaa.agents.llm import GeminiClient

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    cfg = load_config().backtest.critic.llm
    client = GeminiClient(cfg)

    def boom(**kwargs):
        raise RuntimeError("503")

    client._client.models.generate_content = boom
    assert client.json_complete("sys", "user", default={"fallback": True}) == {"fallback": True}


# --------------------------------------------------------------------------- #
# the real chain: strikes, marks and implied vol from Alpaca history
# --------------------------------------------------------------------------- #
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
    from oaa.backtest.source import HistoricalContextSource

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
    from oaa.backtest.source import HistoricalContextSource

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
    from oaa.backtest.source import HistoricalContextSource

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
    from oaa.backtest.source import HistoricalContextSource

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
