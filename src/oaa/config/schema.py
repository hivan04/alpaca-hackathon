"""Typed schema for config/*.yaml.

Every key in the YAML has a home here. Unknown keys are rejected rather than
silently ignored - a typo in a risk limit at 3am should fail loudly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- #
# meta / broker
# --------------------------------------------------------------------------- #
class MetaConfig(Base):
    project: str = "Eventus Algorithm"
    #: The hackathon track, submitted verbatim. Not the project name.
    track: str = "Options Alpha Agents"
    version: str = "0.1.0"


class CliBrokerConfig(Base):
    binary: str = "alpaca"
    profile: str | None = None
    timeout_seconds: int = 30


class McpBrokerConfig(Base):
    command: str = "uvx"
    args: list[str] = Field(default_factory=lambda: ["alpaca-mcp-server"])
    transport: Literal["stdio", "streamable-http"] = "stdio"
    url: str = "http://127.0.0.1:8000/mcp"
    toolsets: list[str] = Field(
        default_factory=lambda: [
            "account",
            "trading",
            "assets",
            "options-data",
            "stock-data",
            "news",
        ]
    )


BrokerBackend = Literal["rest", "cli", "mcp", "sim"]


class BrokerConfig(Base):
    primary: BrokerBackend = "rest"
    fallback: BrokerBackend | None = "sim"
    paper: bool = True
    idempotency: bool = True
    require_risk_approval: bool = True
    cli: CliBrokerConfig = Field(default_factory=CliBrokerConfig)
    mcp: McpBrokerConfig = Field(default_factory=McpBrokerConfig)


# --------------------------------------------------------------------------- #
# data / universe / options
# --------------------------------------------------------------------------- #
class CacheConfig(Base):
    enabled: bool = True
    dir: str = "data/cache"
    ttl_seconds: int = 60


class RateLimitConfig(Base):
    requests_per_minute: int = 190
    burst: int = 20


class DataConfig(Base):
    #: How realised volatility is measured. `vol_carry` gates on IV - RV, so
    #: this is not a detail: the free IEX feed is ~2% of the tape and its daily
    #: "close" is the last IEX print rather than the closing auction, which
    #: inflates a close-to-close estimate. Measured on our own universe over 20
    #: sessions, close-to-close ran 1.3-2.3x the Garman-Klass estimate from the
    #: SAME bars (MSFT: 56.3% vs 24.1%). Garman-Klass reads the daily range
    #: instead, so one bad closing print barely moves it.
    volatility_estimator: Literal["garman_klass", "close_to_close"] = "garman_klass"
    provider: str = "alpaca"
    stock_feed: Literal["iex", "sip", "delayed_sip", "otc"] = "iex"
    option_feed: Literal["indicative", "opra"] = "indicative"
    delayed_minutes: int = 15
    #: The intraday book needs a session VWAP, which daily bars cannot express.
    fetch_intraday: bool = True
    intraday_timeframe: str = "5Min"
    intraday_lookback_days: int = 5
    #: Per-symbol headlines for the intraday catalyst gate. Alpaca-native.
    fetch_news: bool = True
    news_limit: int = 20
    news_lookback_hours: int = 6
    # --- ATM IV term structure ------------------------------------------- #
    # Where the two anchors sit on the ladder. These live here, not in a
    # strategy's params, because the slope must mean the SAME THING on the live
    # path and the replay path. IV rank did not, for exactly the reason of two
    # call sites with two definitions, and the gate on top of it could not tell
    # (`claude/iv-rank-divergence.md`).
    #
    # 1 rather than 0 for the front: a 0 DTE contract on expiry morning has its
    # recovered vol dominated by the bid-ask and the pin. 30 for the back is
    # the conventional 30-day surface reference, so the number is comparable to
    # something outside this repo.
    term_front_dte: int = 1
    term_back_dte: int = 30
    #: Below this gap the two expiries are the same maturity and the "slope" is
    #: quote noise over a small denominator.
    term_min_separation_days: int = 7
    #: Plausibility ceiling on |slope_pct|. Beyond it the reading is reported as
    #: unmeasurable rather than as an extreme slope - measured 30 Aug, a 0 DTE
    #: front anchor produced +621% on XLF, which is the pin and the bid-ask, not
    #: a forecast. 0 disables the check.
    term_max_abs_slope_pct: float = 1.0
    cache: CacheConfig = Field(default_factory=CacheConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)


class UniverseConfig(Base):
    symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ"])
    min_underlying_price: float = 20.0
    max_underlying_price: float = 1500.0
    exclude: list[str] = Field(default_factory=list)
    avoid_earnings_within_days: int = 2

    @field_validator("symbols", "exclude")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s.strip()]

    def active(self) -> list[str]:
        excluded = set(self.exclude)
        return [s for s in self.symbols if s not in excluded]


class _VolEstimator:
    CLOSE = "close_to_close"
    GARMAN_KLASS = "garman_klass"


class OptionsConfig(Base):
    min_days_to_expiry: int = 3
    max_days_to_expiry: int = 45
    min_open_interest: int = 250
    min_volume: int = 10
    max_bid_ask_spread_pct: float = 0.12
    min_option_price: float = 0.10
    #: None disables the per-contract price ceiling. It was 25.0, which quietly
    #: removed near-the-money contracts on any underlying above roughly $350 -
    #: distorting the structure that got built rather than refusing it.
    max_option_price: float | None = None
    strike_selection: Literal["delta", "moneyness", "strike"] = "delta"
    contract_multiplier: int = 100


# --------------------------------------------------------------------------- #
# risk / execution / management
# --------------------------------------------------------------------------- #
class RiskConfig(Base):
    max_positions: int = 12
    max_new_positions_per_day: int = 6
    max_positions_per_underlying: int = 2
    max_risk_per_trade_pct: float = 0.02
    max_portfolio_risk_pct: float = 0.20
    #: Delta-equivalent notional a SINGLE structure may control, as a fraction
    #: of equity. Enforced from 30 Aug; before that it was declared here and
    #: read by nothing.
    max_notional_per_trade_pct: float = 0.10
    min_cash_buffer_pct: float = 0.15
    allow_undefined_risk: bool = False
    # --- aggregate GREEK caps -------------------------------------------- #
    # Enforced from 30 Aug. Until then these two sat in the config, in the
    # docs and in NO code path: every portfolio limit counted structures, and a
    # count cannot see twenty-five positions that are one bet. Measured 28 Aug,
    # the intraday universe behaves like 2.4 independent bets.
    #
    # Because they were never enforced, no value here was ever calibrated
    # against a run - so the defaults are set from the measured distribution
    # (see `claude/portfolio-greek-caps.md`), not carried over. The old 0.35
    # carried a comment reading "per $1k equity"; the enforced definition is
    # the fraction-of-equity one below, which is the form that can be compared
    # across account sizes.
    #
    # <= 0 means MEASURE ONLY: the exposure is computed and journalled on every
    # verdict, and nothing is refused. That is the honest setting for a limit
    # you have not yet calibrated, and it is how these were calibrated.

    #: |net dollar delta| / equity. 0.35 = a 1% move in the underlyings moves
    #: the book by 0.35% of equity.
    max_net_delta: float = 0.35
    #: |net vega| in dollars per vol POINT, per $10k of equity.
    max_net_vega: float = 50.0
    daily_loss_limit_pct: float = 0.04
    max_drawdown_halt_pct: float = 0.15
    #: Minutes before the same strategy may open another structure on the
    #: same underlying. Without this a book re-enters at every cycle: the
    #: broker NETS identical legs into one position, so neither the
    #: position count nor the per-underlying leg count changes and every
    #: other portfolio limit stays blind to it. 0 disables the check.
    reentry_cooldown_minutes: int = 60
    no_trade_open_minutes: int = 5
    no_trade_close_minutes: int = 10


class ChaseConfig(Base):
    enabled: bool = True
    steps: int = 3
    interval_seconds: int = 20
    max_slippage_pct: float = 0.15


class ExecutionConfig(Base):
    order_type: Literal["limit", "market"] = "limit"
    #: atomic -> one multi-leg order (preferred: one spread crossed, not four).
    #: legged -> rollback-safe combo, long/protective legs first, unwound in
    #: reverse on any critical failure. Legging doubles spread exposure, so it
    #: is a fallback for venues that cannot route combos, not a default.
    multileg_mode: Literal["atomic", "legged"] = "atomic"
    limit_price_ratio: float = 0.5
    chase: ChaseConfig = Field(default_factory=ChaseConfig)
    time_in_force: Literal["day", "gtc"] = "day"
    cancel_unfilled_after_seconds: int = 180
    dry_run: bool = True


class RollConfig(Base):
    enabled: bool = False
    dte_trigger: int = 7


class ManagementConfig(Base):
    profit_target_pct: float = 0.50
    stop_loss_pct: float = 2.0
    close_at_dte: int = 1
    roll: RollConfig = Field(default_factory=RollConfig)
    flatten_before: str | None = None
    #: Stop opening carry structures once the window is shorter than one can
    #: meaningfully decay. ISO-8601 UTC.
    entry_cutoff_utc: str | None = None
    #: Close the entire book before the submission deadline so the judged P&L
    #: is realised rather than an unrealised mark on a wide quote. ISO-8601 UTC.
    submission_flatten_utc: str | None = None


# --------------------------------------------------------------------------- #
# strategies / agents / schedule
# --------------------------------------------------------------------------- #
class FirewallTimesConfig(Base):
    """Every boundary in the trading day, US/Eastern, HH:MM."""

    market_open: str = "09:30"
    #: 09:30-09:45 is skipped outright: the open is wide and unstable, and the
    #: intraday book's entire edge fits inside a few cents of spread.
    intraday_start: str = "09:45"
    carry_entry_start: str = "10:00"
    #: No intraday entry that cannot be closed calmly before the 15:15 cutoff.
    intraday_last_entry: str = "14:45"
    carry_entry_end: str = "15:00"
    intraday_cutoff: str = "15:15"
    carry_verification: str = "15:45"
    market_close: str = "16:00"


class FirewallConfig(Base):
    """The capital boundary between the resident carry book and the transient
    intraday/opportunistic books.

    Layer 1 is temporal (a book trades only inside its own window); layer 2 is
    capital (the transient lease is whatever Reg T leaves *after* the carry
    book's requirement is reserved, measured on a fresh poll).
    """

    enabled: bool = True
    times: FirewallTimesConfig = Field(default_factory=FirewallTimesConfig)
    #: Which book owns which leg. Persisted so a restart cannot cause the 15:15
    #: cutoff to liquidate a multi-session carry structure.
    ledger_path: str = "runs/position_ledger.json"
    #: How many liquidate-then-poll rounds the 15:15 cutoff will run.
    liquidation_confirm_attempts: int = 4
    liquidation_confirm_delay_seconds: float = 5.0
    #: Transient positions still open at 15:45: liquidate, then disable the
    #: transient books for the following session anyway.
    emergency_liquidate: bool = True
    #: Hard ceiling on the resident book's gross exposure, as a fraction of equity.
    carry_max_equity_pct: float = 0.50
    #: Fraction of the REMAINING Reg T buying power the transient books may lease.
    transient_utilisation: float = 0.50
    #: Second, absolute ceiling on transient exposure as a fraction of equity.
    transient_max_equity_pct: float = 0.15
    #: Reg T cushion the carry book must clear at the 15:45 verification.
    carry_margin_cushion: float = 1.25
    min_trade_value: float = 500.0

    @field_validator("transient_utilisation")
    @classmethod
    def _sane_utilisation(cls, v: float) -> float:
        if not 0 < v <= 1.0:
            raise ValueError("utilisation must be in (0, 1] - never borrow above the limit")
        return v


class StrategyRef(Base):
    name: str
    enabled: bool = True
    weight: float = 1.0
    #: Deep-merged OVER the contents of `params_file`. This exists so a variant
    #: (config/variants/<name>.yaml) can change three settings without cloning a
    #: 300-line params file that would then drift from the original.
    params_overlay: dict[str, Any] = Field(default_factory=dict)
    #: Which capital book this strategy trades from. Gated by the firewall.
    #: "weekend" is the odd one out and deliberately so: it runs in its own
    #: process (`oaa weekend run`) inside a window that cannot overlap an
    #: equity session, so it never leases capital from the firewall. It is
    #: listed here anyway because the runtime switchboard keys off the config's
    #: strategy list - an unlisted strategy shows as "not wired" and its toggle
    #: does nothing.
    #: "events" joins "weekend" as a book that runs in its own process
    #: (`oaa events arm`) and never leases capital from the firewall.
    book: Literal["carry", "intraday", "opportunistic", "weekend", "events"] = "intraday"
    params_file: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class LLMConfig(Base):
    provider: Literal["anthropic", "openai", "featherless"] | None = "featherless"
    model: str = "Qwen/Qwen3-32B"
    temperature: float = 0.2
    max_tokens: int = 4000
    timeout_seconds: int = 90
    fallback_to_rules: bool = True
    #: Which environment variable holds the key. Defaults per provider:
    #: ANTHROPIC_API_KEY / OPENAI_API_KEY / FEATHERLESS_API_KEY.
    api_key_env: str | None = None
    #: OpenAI-compatible providers only. Featherless defaults to
    #: https://api.featherless.ai/v1; override to pin a region or a proxy.
    base_url: str | None = None
    #: Featherless only. Serverless inference cold-starts and rate-limits, so a
    #: transient 429 retries rather than costing the cycle its reasoning.
    max_retries: int = 3
    retry_backoff_seconds: float = 1.5
    #: Featherless. Makes repeated calls far more reproducible, which is the
    #: difference between a backtest and a coin flip.
    seed: int | None = None


class CriticConfig(Base):
    enabled: bool = True
    min_score_to_trade: float = 0.55
    require_thesis: bool = True


class MemoryConfig(Base):
    enabled: bool = True
    path: str = "runs/memory.sqlite"
    lookback_days: int = 7


class AgentsConfig(Base):
    enabled: bool = True
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tool_backend: Literal["mcp", "rest"] = "mcp"
    proposers: list[str] = Field(default_factory=lambda: ["vol_regime", "trend", "flow"])
    critic: CriticConfig = Field(default_factory=CriticConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    #: Which cycles the assistant drives. Everything else stays deterministic.
    #: This is the main cost dial - each entry is roughly one LLM cycle per day.
    #: Set to [] to run the whole system rules-only at zero token cost.
    agent_cycles: list[str] = Field(
        default_factory=lambda: ["carry_scan"]
    )
    #: MCP read tools exposed to the model. null = the built-in allowlist.
    #: Every tool's schema is re-sent on every turn, so this is the second
    #: biggest cost lever after agent_cycles.
    mcp_read_tools: list[str] | None = None
    #: Mark the system prompt and tool schemas cacheable. They are byte-identical
    #: across turns, so this removes most of the repeated input cost.
    prompt_caching: bool = True
    #: Hard cap on tool-result size handed back to the model, in characters.
    max_tool_result_chars: int = 4000


CycleAction = Literal[
    "scan_and_trade",
    "manage_positions",
    "report",
    "flatten",
    # Firewall-driven cycles. These fire at fixed ET boundaries and are the
    # mechanism by which the resident and transient books never hold
    # conflicting claims on the same capital.
    "discover",
    "carry_scan",
    "intraday_scan",
    "intraday_cutoff",
    "carry_verify",
    "submission_flatten",
    # The events book. It arms on a DATE rather than a signal, in the last
    # minutes of the session, and holds one night across a confirmed earnings
    # print. It runs inside `oaa run` from 30 Aug, but deliberately does NOT
    # lease capital from the temporal firewall - see
    # `Orchestrator._events_engine`, which builds it a RiskEngine with
    # firewall=None, exactly as `oaa events arm` has always done.
    "events_arm",
    "events_flatten",
    # Reads the names whose prints are coming, several times a day, and stops
    # reading each one the day its print is behind us. Opens nothing.
    "events_watch",
]


class CycleConfig(Base):
    name: str
    at: str
    action: CycleAction


class ScheduleConfig(Base):
    timezone: str = "America/New_York"
    trading_days: list[str] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"]
    )
    cycles: list[CycleConfig] = Field(default_factory=list)
    monitor_interval_seconds: int = 300


# --------------------------------------------------------------------------- #
# telemetry / app / backtest / partners
# --------------------------------------------------------------------------- #
class CostModelConfig(Base):
    """Modelled transaction costs, per COST_STRUCTURE.md.

    Paper trading charges none of this and fills optimistically at mid. Reporting
    a fee- and spread-adjusted P&L line alongside the raw number is cheap,
    honest, and the difference between a judge discovering the gap and reading
    that we measured it.
    """

    enabled: bool = True
    occ_clearing: float = 0.025
    orf: float = 0.015
    cat_per_contract: float = 0.0003
    taf_sell: float = 0.00329
    sec_rate: float = 0.0000206
    #: Half-spread assumption per leg, in dollars. Tune from live quotes.
    modelled_slippage_per_leg: float = 0.02
    margin_rate_annual: float = 0.0625
    #: Index products carry exchange fees on top. symbol -> $/contract.
    index_exchange_fees: dict[str, float] = Field(
        default_factory=lambda: {"SPX": 0.66, "SPXW": 0.59, "VIX": 0.45, "XSP": 0.0}
    )


class TelemetryConfig(Base):
    run_dir: str = "runs"
    journal: str = "runs/journal.jsonl"
    db: str = "runs/oaa.sqlite"
    equity_curve: str = "runs/equity.csv"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    # What reaches the TERMINAL, not what is recorded. "focused" passes the
    # tape (research complete, position opened, position closed with P&L) plus
    # WARNING and above; "full" passes everything, including the per-gate
    # REJECT lines. The journal, the JSONL sink and `oaa gates` are identical
    # either way - this cannot hide a decision, only move where you read it.
    console: Literal["full", "focused"] = "full"
    snapshot_interval_seconds: int = 300
    capture_screenshots: bool = False


class AppConfig(Base):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    title: str = "Eventus Algorithm"
    public: bool = True
    refresh_seconds: int = 15


class BacktestIVConfig(Base):
    """How the replay harness models implied volatility. See backtest/ivmodel.py.

    There is no historical option chain on the free tier, so IV rank and the
    IV-RV spread - the two inputs the carry book actually trades on - have to
    be modelled. These knobs are the model, and they belong in config rather
    than in code so the deck can show what was assumed.
    """

    vrp_multiple: float = 1.13
    anchor_halflife_days: float = 45.0
    rv_lookback: int = 20
    market_beta: float = 0.45
    rank_lookback: int = 252
    market_symbol: str = "SPY"


class BacktestChainConfig(Base):
    """The modelled volatility surface, spread and liquidity. See backtest/chain.py."""

    skew: float = -0.11
    smile: float = 0.06
    term_slope: float = 0.02
    rate: float = 0.04
    strike_window_pct: float = 0.14
    max_strikes_per_side: int = 30
    min_quotable_mid: float = 0.03
    #: symbol -> liquidity tier name; unknown symbols fall to `default_tier`
    tier_map: dict[str, str] = Field(default_factory=dict)
    default_tier: str = "single_name"
    #: real     - list the actual contracts and mark them from real Alpaca
    #:            option bars, recovering implied vol by inverting Black-Scholes
    #:            on the traded price. Gaps (contract-days with no print) fall
    #:            back to the surface below and are counted in the run's
    #:            coverage. THE DEFAULT.
    #: modelled - the whole chain from the surface. No option API calls at all;
    #:            useful offline and for isolating what the pricing model does.
    source: Literal["real", "modelled"] = "real"
    #: calendar days of option history pulled before `start`, so implied vol has
    #: something to be ranked against. Longer is better and costs more requests.
    iv_history_days: int = 180
    #: minimum sessions with a usable print before a symbol's real IV series is
    #: trusted; below it the harness falls back to the modelled series and says so
    min_iv_observations: int = 20
    #: strike band for LISTING real contracts inside the replay window. Much
    #: tighter than `strike_window_pct`, which sizes the modelled ladder: a
    #: 14-delta short strike at 7-14 DTE sits ~3% from spot and the wings are
    #: a few points beyond it, so anything past ~6% is a contract the strategy
    #: will never look at and a bar request that buys nothing.
    listing_band: float = 0.06
    #: strike band for contracts BEFORE the replay window. Those exist only to
    #: recover a historical ATM implied-vol series, so near-the-money is all
    #: that is needed.
    iv_history_band: float = 0.03
    #: In the history period, keep at most one expiry per this many days. SPY
    #: now lists an expiration almost every trading day; recovering one ATM
    #: implied-vol reading per session does not need all of them, and listing
    #: them all is tens of thousands of contracts.
    iv_history_expiry_stride_days: int = 7
    #: safety valve on the contract listing per underlying. Exceeding it is
    #: logged, never silent.
    max_contracts_per_symbol: int = 12_000


class BacktestCriticConfig(Base):
    """The critic, in replay. See backtest/critic.py.

    `heuristic` is the default on purpose: it is the real `Critic` class with
    the documented null-LLM fallback, so it is deterministic and free. `llm`
    calls the actual model - useful for inspecting the quality of the reasoning
    on a handful of trades, NOT for producing a P&L number, because the model
    may have the replayed period in its training data.
    """

    mode: Literal["off", "heuristic", "llm"] = "heuristic"
    #: hard cap on model calls per run; past it the critic degrades to the
    #: heuristic, which is the same degradation the live system has
    max_llm_calls: int = 250
    #: feed the critic the outcomes of trades already closed in this replay,
    #: exactly as the live agent feeds it
    memory: bool = True
    #: The settings the REPLAY uses, overriding `agents.llm`. Same provider
    #: since 28 Aug - one vendor, one key - but deliberately different
    #: settings: temperature 0 and a fixed seed, because a judge should not be
    #: creative and a backtest whose numbers move on re-run is not a backtest.
    #: The smaller token budget matters too: a replay scores every candidate in
    #: every session and is re-run whenever a parameter moves, so it is by far
    #: the heavier caller. Set to null to inherit `agents.llm` wholesale.
    llm: LLMConfig | None = Field(
        default_factory=lambda: LLMConfig(
            provider="featherless",
            model="Qwen/Qwen3-32B",
            temperature=0.0,
            max_tokens=1024,
            api_key_env="FEATHERLESS_API_KEY",
            seed=7,
        )
    )


class BacktestConfig(Base):
    start: str = "2026-06-01"
    end: str = "2026-08-22"
    initial_cash: float = 100_000.0
    #: 0.0 fills at mid (what paper does); 1.0 pays the full quoted side.
    slippage_spread_fraction: float = 0.5
    commission_per_contract: float = 0.0
    output_dir: str = "runs/backtests"
    #: sessions the replay evaluates, in Eastern time
    session_times_et: list[str] = Field(default_factory=lambda: ["10:00"])
    #: Cadence, in minutes, at which OPEN positions are re-marked and their
    #: exit rules evaluated BETWEEN the scan moments above. Entries stay on
    #: `session_times_et`. A position living 20-90 minutes was previously
    #: observed 2-6 times in its whole life, which is too coarse for any exit
    #: dial - target, stop or trailing - to mean what it says. 0 disables the
    #: fine loop and restores scan-grid-only management.
    mark_interval_minutes: int = 1
    #: how far back a session looks for headlines feeding the catalyst read
    news_lookback_hours: float = 18.0
    fetch_news: bool = True
    #: complete sessions of history required before a symbol becomes tradable
    min_history_days: int = 40
    #: extra calendar days of bars pulled before `start`, to warm the indicators
    warmup_days: int = 400
    cache_dir: str = "data/cache/backtest"
    iv_model: BacktestIVConfig = Field(default_factory=BacktestIVConfig)
    chain: BacktestChainConfig = Field(default_factory=BacktestChainConfig)
    critic: BacktestCriticConfig = Field(default_factory=BacktestCriticConfig)


# --------------------------------------------------------------------------- #
# discovery / macro lens
# --------------------------------------------------------------------------- #
class SourceSpec(Base):
    enabled: bool = False
    limit: int = 30
    #: news source only
    lookback_days: int = 3
    baseline_days: int = 20
    #: external source only
    url: str | None = None
    symbol_path: str | None = None
    score_path: str | None = None
    api_key_env: str | None = None
    api_key_header: str = "Authorization"
    api_key_format: str = "Bearer {key}"
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 15.0


class DiscoverySourcesConfig(Base):
    most_actives: SourceSpec = Field(default_factory=lambda: SourceSpec(enabled=True, limit=30))
    movers: SourceSpec = Field(default_factory=lambda: SourceSpec(enabled=True, limit=20))
    news: SourceSpec = Field(default_factory=lambda: SourceSpec(enabled=True, limit=200))
    external: SourceSpec = Field(default_factory=SourceSpec)


class DiscoveryFiltersConfig(Base):
    """Tradability guards. `require_shortable` is the one that bites silently -
    the pairs trade shorts a leg, so a candidate that cannot be borrowed fails
    at 15:55 with the protective options already bought."""

    min_price: float = 10.0
    max_price: float = 1500.0
    min_dollar_volume: float = 50_000_000.0
    require_optionable: bool = True
    require_shortable: bool = True
    exclude_leveraged: bool = True
    min_history_days: int = 250
    exclude: list[str] = Field(default_factory=list)


class CandidatePoolConfig(Base):
    path: str = "runs/candidate_pool.json"
    #: How long a never-approved symbol survives without reappearing.
    accumulate_days: int = 10
    max_symbols: int = 40
    #: Hand-picked, economically-linked names that are always screened.
    seeds: list[str] = Field(default_factory=list)


class MacroLensConfig(Base):
    enabled: bool = True
    #: False runs the deterministic breadth rule instead of the model.
    use_llm: bool = True
    max_symbols: int = 15
    max_headlines: int = 25
    #: Overnight-risk level at which strategies are told to stand down.
    stand_down_threshold: float = 0.75


class DiscoveryConfig(Base):
    """Universe discovery and the macro lens.

    Attention generates candidates; cointegration still decides. Nothing here
    feeds the gap model's feature set - most-actives and movers are live
    snapshots with no history, so a feature built on them could not be replayed
    and would silently poison the walk-forward backtest.
    """

    enabled: bool = True
    sources: DiscoverySourcesConfig = Field(default_factory=DiscoverySourcesConfig)
    filters: DiscoveryFiltersConfig = Field(default_factory=DiscoveryFiltersConfig)
    pool: CandidatePoolConfig = Field(default_factory=CandidatePoolConfig)
    macro: MacroLensConfig = Field(default_factory=MacroLensConfig)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "news": 0.40, "movers": 0.35, "most_actives": 0.15, "external": 0.10
        }
    )
    #: Cap on symbols pushed through the (rate-limited) tradability filter.
    max_filter_checks: int = 25


PartnerStage = Literal[
    "data_enrichment", "signal", "reasoning", "risk", "execution", "telemetry", "ui"
]


class PartnerAdapterConfig(Base):
    """One sponsor technology, plugged into a named pipeline stage.

    The hackathon publishes its technology partners at kickoff. Adding one
    should be: write an adapter module, add a block here. Nothing in the core
    pipeline changes.
    """

    name: str
    enabled: bool = False
    module: str
    stage: PartnerStage = "data_enrichment"
    priority: int = 100
    params: dict[str, Any] = Field(default_factory=dict)


class PartnersConfig(Base):
    enabled: bool = True
    on_error: Literal["skip", "fail"] = "skip"
    timeout_seconds: int = 20
    adapters: list[PartnerAdapterConfig] = Field(default_factory=list)

    def for_stage(self, stage: str) -> list[PartnerAdapterConfig]:
        if not self.enabled:
            return []
        return sorted(
            (a for a in self.adapters if a.enabled and a.stage == stage),
            key=lambda a: a.priority,
        )


# --------------------------------------------------------------------------- #
# root
# --------------------------------------------------------------------------- #
class Config(Base):
    meta: MetaConfig = Field(default_factory=MetaConfig)
    profile: Literal["dev", "judged"] = "dev"
    #: Name of the config/variants/<name>.yaml overlay applied to this run, or
    #: None for the baseline strategy. Stamped into backtest provenance so two
    #: runs can never be compared without it being visible which is which.
    variant: str | None = None
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    options: OptionsConfig = Field(default_factory=OptionsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    firewall: FirewallConfig = Field(default_factory=FirewallConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    strategies: list[StrategyRef] = Field(default_factory=list)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    management: ManagementConfig = Field(default_factory=ManagementConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    app: AppConfig = Field(default_factory=AppConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    partners: PartnersConfig = Field(default_factory=PartnersConfig)
    cost_model: CostModelConfig = Field(default_factory=CostModelConfig)

    def enabled_strategies(self, book: str | None = None) -> list[StrategyRef]:
        found = [s for s in self.strategies if s.enabled]
        return [s for s in found if s.book == book] if book else found
