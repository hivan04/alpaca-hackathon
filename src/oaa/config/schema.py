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
    project: str = "Options Alpha Agents"
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
    provider: str = "alpaca"
    stock_feed: Literal["iex", "sip", "delayed_sip", "otc"] = "iex"
    option_feed: Literal["indicative", "opra"] = "indicative"
    delayed_minutes: int = 15
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


class OptionsConfig(Base):
    min_days_to_expiry: int = 3
    max_days_to_expiry: int = 45
    min_open_interest: int = 250
    min_volume: int = 10
    max_bid_ask_spread_pct: float = 0.12
    min_option_price: float = 0.10
    max_option_price: float = 25.0
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
    max_notional_per_trade_pct: float = 0.10
    min_cash_buffer_pct: float = 0.15
    allow_undefined_risk: bool = False
    max_net_delta: float = 0.35
    max_net_vega: float = 50.0
    daily_loss_limit_pct: float = 0.04
    max_drawdown_halt_pct: float = 0.15
    no_trade_open_minutes: int = 5
    no_trade_close_minutes: int = 10


class ChaseConfig(Base):
    enabled: bool = True
    steps: int = 3
    interval_seconds: int = 20
    max_slippage_pct: float = 0.15


class ExecutionConfig(Base):
    order_type: Literal["limit", "market"] = "limit"
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


# --------------------------------------------------------------------------- #
# strategies / agents / schedule
# --------------------------------------------------------------------------- #
class FirewallTimesConfig(Base):
    """Every boundary in the trading day, US/Eastern, HH:MM."""

    market_open: str = "09:30"
    overnight_exit: str = "09:35"
    intraday_start: str = "10:00"
    intraday_last_entry: str = "15:00"
    intraday_cutoff: str = "15:15"
    overnight_signal: str = "15:45"
    overnight_verify: str = "15:54"
    overnight_entry: str = "15:55"
    market_close: str = "16:00"


class FirewallConfig(Base):
    """The dual-layer capital lock between the intraday and overnight books.

    Layer 1 is temporal (a book trades only inside its own window); layer 2 is
    capital (size is scaled against buying power measured *after* the other
    book is proven flat).
    """

    enabled: bool = True
    times: FirewallTimesConfig = Field(default_factory=FirewallTimesConfig)
    #: How many liquidate-then-poll rounds the 15:15 cutoff will run.
    liquidation_confirm_attempts: int = 4
    liquidation_confirm_delay_seconds: float = 5.0
    #: If positions are still open at 15:54, liquidate them before aborting.
    emergency_liquidate: bool = True
    #: Fraction of *verified* Reg T buying power the overnight book may use.
    overnight_regt_utilisation: float = 0.95
    #: Hard ceiling on overnight gross exposure as a fraction of equity.
    overnight_max_equity_pct: float = 0.50
    #: Fraction of day-trading buying power the intraday book may use.
    intraday_dtbp_utilisation: float = 0.50
    min_trade_value: float = 500.0

    @field_validator("overnight_regt_utilisation", "intraday_dtbp_utilisation")
    @classmethod
    def _sane_utilisation(cls, v: float) -> float:
        if not 0 < v <= 1.0:
            raise ValueError("utilisation must be in (0, 1] - never borrow above the limit")
        return v


class StrategyRef(Base):
    name: str
    enabled: bool = True
    weight: float = 1.0
    #: Which capital book this strategy trades from. Gated by the firewall.
    book: Literal["intraday", "overnight"] = "intraday"
    params_file: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class LLMConfig(Base):
    provider: Literal["anthropic", "openai"] | None = "anthropic"
    model: str = "claude-sonnet-4-5"
    temperature: float = 0.2
    max_tokens: int = 4000
    timeout_seconds: int = 90
    fallback_to_rules: bool = True


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
        default_factory=lambda: ["overnight_signal", "overnight_entry"]
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
    # mechanism by which the two books never hold capital at the same time.
    "intraday_cutoff",
    "discover",
    "overnight_signal",
    "overnight_verify",
    "overnight_entry",
    "overnight_exit",
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
class TelemetryConfig(Base):
    run_dir: str = "runs"
    journal: str = "runs/journal.jsonl"
    db: str = "runs/oaa.sqlite"
    equity_curve: str = "runs/equity.csv"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    snapshot_interval_seconds: int = 300
    capture_screenshots: bool = False


class AppConfig(Base):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    title: str = "Options Alpha Agents"
    public: bool = True
    refresh_seconds: int = 15


class BacktestConfig(Base):
    start: str = "2026-06-01"
    end: str = "2026-08-22"
    initial_cash: float = 100_000.0
    slippage_spread_fraction: float = 0.5
    commission_per_contract: float = 0.0
    output_dir: str = "runs/backtests"


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

    def enabled_strategies(self, book: str | None = None) -> list[StrategyRef]:
        found = [s for s in self.strategies if s.enabled]
        return [s for s in found if s.book == book] if book else found
