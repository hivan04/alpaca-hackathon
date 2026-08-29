"""Strategy contract.

A strategy is a pure function of a MarketContext: it sees a snapshot and
returns zero or more TradeIdeas. It does not size them (risk does that), does
not price the order (execution does that), and never calls the broker.

That separation is what lets the same strategy code run live and in backtest.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from oaa.config.schema import Config, StrategyRef
from oaa.core.errors import ConfigError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.registry import Registry
from oaa.core.types import AccountSnapshot, MarketContext, TradeIdea
from oaa.options.chain import ChainFilter, ChainView
from oaa.options.structures import StructureBuilder

log = get_logger("strategies")


@dataclass
class StrategyContext:
    """Everything a strategy is allowed to see.

    Single-symbol strategies read `market`. Portfolio strategies (a pairs
    trade needs two legs at once) read `contexts`, keyed by symbol.
    """

    account: AccountSnapshot
    config: Config
    market: MarketContext | None = None
    contexts: dict[str, MarketContext] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    #: Verified capital budget handed down by the temporal firewall. Zero means
    #: this book does not currently hold the capital lock.
    budget: float = 0.0
    firewall: Any = None
    #: The macro lens's regime read for this session. An OVERLAY: it may stand a
    #: strategy down or reduce size, never approve a trade or increase one.
    macro: Any = None
    #: CatalystEngine for the intraday book's §3.2 gate. Deterministic by design:
    #: an LLM call inside a loop whose signal decays in minutes is a latency risk.
    catalyst: Any = None
    #: This cycle's AttentionSnapshot (movers breadth, most-actives, news velocity).
    attention: Any = None

    def __post_init__(self) -> None:
        if self.market is not None:
            self.contexts.setdefault(self.market.symbol, self.market)

    def require(self, symbol: str) -> MarketContext:
        context = self.contexts.get(symbol.upper())
        if context is None:
            raise StrategyError(f"no market context available for {symbol}")
        return context

    def default_filter(self, **overrides: Any) -> ChainFilter:
        opts = self.config.options
        base = {
            "min_dte": opts.min_days_to_expiry,
            "max_dte": opts.max_days_to_expiry,
            "min_open_interest": opts.min_open_interest,
            "min_volume": opts.min_volume,
            "max_spread_pct": opts.max_bid_ask_spread_pct,
            "min_price": opts.min_option_price,
            "max_price": opts.max_option_price,
        }
        base.update(overrides)
        return ChainFilter(**base)

    def chain_view(
        self,
        chain_filter: ChainFilter | None = None,
        symbol: str | None = None,
    ) -> ChainView:
        market = self.require(symbol) if symbol else self.market
        if market is None:
            raise StrategyError("chain_view needs a symbol when there is no primary market")
        return ChainView.from_quotes(
            symbol=market.symbol,
            spot=market.spot,
            quotes=market.chain,
            chain_filter=chain_filter or self.default_filter(),
            asof=market.asof.date(),
        )

    def has_position_in(self, symbol: str) -> bool:
        return bool(self.account.by_underlying(symbol))

    # -- macro overlay ---------------------------------------------------- #
    def macro_stance(self, strategy: str) -> str:
        return self.macro.stance_for(strategy) if self.macro is not None else "trade"

    def macro_allows(self, strategy: str) -> bool:
        return self.macro.may_trade(strategy) if self.macro is not None else True

    def macro_flagged(self, symbol: str) -> bool:
        return self.macro.is_flagged(symbol) if self.macro is not None else False

    def macro_size_multiplier(self, strategy: str) -> float:
        return self.macro.size_multiplier(strategy) if self.macro is not None else 1.0

    def collar_widening(self) -> float:
        return float(getattr(self.macro, "collar_widening", 1.0) or 1.0)


class Strategy(abc.ABC):
    """Base class. Subclasses implement `generate`."""

    registry_name: str = "base"
    description: str = ""
    #: "per_symbol" -> called once per symbol with `ctx.market` set.
    #: "portfolio"  -> called ONCE per cycle with every context in `ctx.contexts`.
    #: A pairs strategy needs both legs simultaneously, so it is a portfolio one.
    mode: str = "per_symbol"
    #: Which capital book this trades from. The firewall gates on it.
    #: "carry" is resident; "intraday" and "opportunistic" are transient tenants.
    book: str = "intraday"

    def __init__(self, ref: StrategyRef, config: Config) -> None:
        self.ref = ref
        self.config = config
        self.params: dict[str, Any] = ref.params or {}
        self.weight = ref.weight

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def capital_book(self) -> str:
        """Config wins over the class default, so a strategy can be re-homed."""
        return getattr(self.ref, "book", None) or self.book

    @property
    def marks_intraday(self) -> bool:
        """Does this book open and close inside one session?

        It decides how the replay MARKS the position, not how it trades. The
        option tape is daily, so a book that is flat by the close has to be
        re-priced from the model against the intraday spot or it can only ever
        realise the spread - see `BacktestEngine._leg_marks`.
        """
        return self.capital_book == "intraday"

    def chain_dte_window(self) -> tuple[int, int] | None:
        """The expiry window this strategy needs to SEE, not the one it trades.

        The replay builds one chain per context and every strategy filters it.
        That chain used to be built from `options.min_days_to_expiry` alone, so
        a strategy whose window sat outside the global one was handed a chain
        that could not contain a single qualifying contract - it then reported
        "no contracts survived the liquidity filter" forever, which reads as a
        liquidity problem and is actually an empty shelf. Strategies that trade
        outside the global window override this; the runner unions what they
        declare with the window it infers from the YAML.

        None means "the window inferred from the YAML already covers me",
        which is true of every strategy trading inside the global envelope.
        """
        return None

    # -- the one method that matters ---------------------------------------- #
    @abc.abstractmethod
    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        """Return candidate structures. Empty list is a perfectly good answer."""

    # -- optional hooks ------------------------------------------------------ #
    def should_exit(self, ctx: StrategyContext, idea: TradeIdea, pnl_pct: float) -> str | None:
        """Return an exit reason, or None to hold.

        Default: the profit target and stop loss from `management` in the YAML.
        """
        mgmt = self.config.management
        exit_cfg = self.params.get("exit", {})
        target = exit_cfg.get("profit_target_pct", mgmt.profit_target_pct)
        stop = exit_cfg.get("stop_loss_pct", mgmt.stop_loss_pct)
        if pnl_pct >= target:
            return f"profit target {target:.0%} reached ({pnl_pct:.0%})"
        if pnl_pct <= -abs(stop):
            return f"stop loss {stop:.0%} hit ({pnl_pct:.0%})"
        return None

    def universe(self) -> list[str]:
        """Symbols this strategy wants. Defaults to the global universe."""
        symbols = self.params.get("universe")
        if symbols:
            return [s.upper() for s in symbols]
        return self.config.universe.active()

    # -- convenience --------------------------------------------------------- #
    def builder(
        self,
        ctx: StrategyContext,
        symbol: str | None = None,
        chain_filter: ChainFilter | None = None,
    ) -> StructureBuilder:
        view = ctx.chain_view(chain_filter=chain_filter, symbol=symbol)
        if view.is_empty:
            raise StrategyError(
                f"{view.symbol}: no contracts survived the liquidity filter"
            )
        return StructureBuilder(view=view, strategy=self.name)

    def p(self, path: str, default: Any = None) -> Any:
        """Dotted lookup into the strategy's YAML params. p('entry.min_iv_rank')"""
        cursor: Any = self.params
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor


strategy_registry: Registry[Strategy] = Registry("strategy")


def load_strategies(
    config: Config, book: str | None = None, include: set[str] | None = None,
) -> list[Strategy]:
    """Instantiate every enabled strategy named in the config.

    `include` names strategies to build even though config has them disabled -
    used by the runtime switchboard, which can turn a configured-off book ON
    without a config edit. A strategy built this way is still only TRADED if
    the switchboard says so; building it early is what makes the switch take
    effect without a restart.
    """
    strategy_registry.autoload("oaa.strategies")
    loaded: list[Strategy] = []
    wanted = set(include or ())
    refs = list(config.enabled_strategies(book))
    seen = {ref.name for ref in refs}
    refs += [
        ref for ref in config.strategies
        if ref.name in wanted and ref.name not in seen
        and (book is None or ref.book == book)
    ]
    for ref in refs:
        # A broken module is tolerated only while nothing asks for it. If the
        # config enables a strategy whose module failed to import, silently
        # running without it would mean trading a book the operator believes
        # is on - so that becomes a hard error naming the real cause, rather
        # than the "unknown strategy" the registry would otherwise report.
        failure = strategy_registry.import_errors.get(ref.name)
        if failure and ref.name not in strategy_registry and not ref.enabled:
            # Asked for by the switchboard, not by the config. A broken module
            # here is a message to the operator, not a reason to stop a loop
            # that was trading perfectly well without it.
            log.error("strategy '%s' was switched on but its module fails to "
                      "import: %s", ref.name, failure)
            continue
        if failure and ref.name not in strategy_registry:
            raise ConfigError(
                f"strategy '{ref.name}' is enabled but its module failed to "
                f"import: {failure}"
            )
        cls = strategy_registry.get(ref.name)
        loaded.append(cls(ref, config))
        log.debug("loaded strategy %s (weight %.2f)", ref.name, ref.weight)
    if not loaded:
        log.warning("no strategies enabled - the agent will not open anything")
    return loaded
