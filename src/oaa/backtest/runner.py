"""One entry point for a backtest, and the store it writes to.

The CLI (`oaa backtest`) and the dashboard both come through here, so a run
launched from a button and a run launched from a terminal are the same run with
the same provenance. Every run is written to `runs/backtests/<id>/` as JSON so
it can be reopened, compared, and put in the deck without re-running anything -
which also means a judge can be handed the artefact rather than a screenshot.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import platform
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from oaa.backtest.chain import DEFAULT_TIER_MAP, ChainModel
from oaa.backtest.critic import ReplayCritic
from oaa.backtest.engine import BacktestEngine, BacktestResult
from oaa.backtest.feed import HistoricalFeed
from oaa.backtest.ivmodel import IVModel
from oaa.backtest.realchain import RealChainBuilder
from oaa.backtest.source import (
    HistoricalContextSource,
    synthetic_bars,
    synthetic_intraday_bars,
)
from oaa.config.loader import Settings
from oaa.core.errors import DataError
from oaa.core.logging import get_logger
from oaa.strategies.base import load_strategies

log = get_logger("backtest.runner")

SOURCE_ALPACA = "alpaca"
SOURCE_SYNTHETIC = "synthetic"


@dataclass
class BacktestRequest:
    symbols: list[str]
    start: dt.date
    end: dt.date
    strategies: list[str] = field(default_factory=list)
    #: Params merged into the ref of every strategy built for this run. Used to
    #: point the events book at a different week's calendar without editing its
    #: params file.
    strategy_params: dict[str, Any] = field(default_factory=dict)
    initial_cash: float | None = None
    slippage_spread_fraction: float | None = None
    session_times_et: list[str] | None = None
    #: "alpaca" replays real bars and real headlines. "synthetic" invents a
    #: price path and is a WIRING TEST ONLY - it is labelled as such everywhere.
    source: str = SOURCE_ALPACA
    use_news: bool = True
    offline: bool = False
    label: str = ""
    #: "off" | "heuristic" | "llm" - see backtest/critic.py. None takes the
    #: configured default, which is "heuristic".
    critic_mode: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start"] = self.start.isoformat()
        payload["end"] = self.end.isoformat()
        return payload


# --------------------------------------------------------------------------- #
def tradable_dte_range(cfg: Any, strategies: list[Any] | None = None) -> tuple[int, int]:
    """The DTE band an ENABLED strategy could actually trade.

    `options.min/max_days_to_expiry` is the outer envelope the chain filter
    uses, not what anything trades: `vol_carry` is 7-14 DTE and
    `event_premium` is 1-5. Listing real contracts across 3-45 DTE instead of
    1-14 is roughly a threefold overcount, and against a name that lists an
    expiration nearly every trading day it is the difference between three
    thousand contracts and forty thousand.
    """
    lows: list[int] = []
    highs: list[int] = []
    for ref in cfg.enabled_strategies():
        params = ref.params or {}
        structures = params.get("structures") or {}
        low = structures.get("dte_min")
        high = structures.get("dte_max")
        target = params.get("structure", {}).get("target_dte") or params.get("target_dte")
        if isinstance(target, (list, tuple)) and len(target) == 2:
            low = target[0] if low is None else min(low, target[0])
            high = target[1] if high is None else max(high, target[1])
        # a calendar's back month is the widest thing any strategy reaches for
        back = (params.get("structure") or {}).get("back_dte")
        if isinstance(back, (list, tuple)) and len(back) == 2:
            high = back[1] if high is None else max(high, back[1])
        # The intraday book selects by `selection.dte_max` and buys 0-2 DTE.
        # Without this its contracts are never listed and it cannot trade.
        selection_max = (params.get("selection") or {}).get("dte_max")
        if selection_max is not None:
            low = 0 if low is None else min(low, 0)
            high = int(selection_max) if high is None else max(high, int(selection_max))
        if low is not None:
            lows.append(int(low))
        if high is not None:
            highs.append(int(high))
    # Instantiated strategies state their own window and are authoritative;
    # the YAML sniffing above is the fallback for callers that have none.
    for strategy in strategies or []:
        window = getattr(strategy, "chain_dte_window", None)
        declared = window() if callable(window) else None
        if declared:
            lows.append(int(declared[0]))
            highs.append(int(declared[1]))
    # The BACK ANCHOR of the term structure has to be listed or the slope is
    # unmeasurable in every replay, silently. No strategy trades that expiry -
    # it is read, not bought - but the chain has to contain it. Without this the
    # window stops at ~16 DTE (vol_carry's 14 plus slack) while the live chain
    # runs to 45, so the two paths would anchor the same signal at different
    # maturities and only the live one would match its own config.
    if highs:
        highs.append(int(cfg.data.term_back_dte))

    if not lows or not highs:
        return cfg.options.min_days_to_expiry, cfg.options.max_days_to_expiry
    return (
        max(0, min(lows) - 1),                       # one day of slack each side
        min(cfg.options.max_days_to_expiry, max(highs) + 2),
    )


def build_source(
    settings: Settings, request: BacktestRequest
) -> HistoricalContextSource:
    cfg = settings.config
    bt = cfg.backtest
    symbols = [s.upper() for s in request.symbols]
    market_symbol = bt.iv_model.market_symbol.upper()
    wanted = sorted(set(symbols) | {market_symbol})

    warmup_start = request.start - dt.timedelta(days=bt.warmup_days)
    bars: dict[str, list[dict[str, Any]]] = {}
    news: list[dict[str, Any]] = []

    feed_for_intraday: Any = None
    synthetic_intraday: dict[str, list[dict[str, Any]]] = {}
    if request.source == SOURCE_SYNTHETIC:
        for symbol in wanted:
            bars[symbol] = synthetic_bars(symbol, warmup_start, request.end)
            # The intraday book refuses a session with fewer than 30 intraday
            # bars. Without these the synthetic path reports the book as never
            # trading when what it has measured is an empty fixture.
            synthetic_intraday[symbol] = synthetic_intraday_bars(
                symbol, bars[symbol], interval_minutes=_interval_minutes(cfg)
            )
    else:
        feed = HistoricalFeed(
            api_key=settings.credentials.api_key,
            secret_key=settings.credentials.secret_key,
            cache_dir=settings.path(bt.cache_dir),
            stock_feed=cfg.data.stock_feed,
            offline=request.offline,
        )
        for symbol in wanted:
            rows = feed.bars(symbol, warmup_start, request.end, "1Day")
            if rows:
                bars[symbol] = rows
            else:
                log.warning("%s: no bars returned - dropped from the universe", symbol)
        feed_for_intraday = feed
        if request.use_news and bt.fetch_news and symbols:
            news = feed.news(symbols, request.start - dt.timedelta(days=2), request.end)

    chain_cfg = bt.chain
    chain_dte = tradable_dte_range(cfg)
    log.info("chain DTE window: %d-%d days", chain_dte[0], chain_dte[1])
    tier_map = {**DEFAULT_TIER_MAP, **{k.upper(): v for k, v in chain_cfg.tier_map.items()}}
    chain_model = ChainModel(
        skew=chain_cfg.skew,
        smile=chain_cfg.smile,
        term_slope=chain_cfg.term_slope,
        rate=chain_cfg.rate,
        strike_window_pct=chain_cfg.strike_window_pct,
        max_strikes_per_side=chain_cfg.max_strikes_per_side,
        min_quotable_mid=chain_cfg.min_quotable_mid,
        tier_map=tier_map,
        default_tier=chain_cfg.default_tier,
        # The window every ENABLED strategy needs to see, not the global
        # options envelope. Building it at options.min_days_to_expiry (3) while
        # the intraday book filters for 0-2 DTE handed that book a chain with
        # zero qualifying contracts on every session of every symbol - it read
        # as "no contracts survived the liquidity filter" and was in fact an
        # empty shelf.
        min_dte=chain_dte[0],
        max_dte=chain_dte[1],
    )
    iv_cfg = bt.iv_model
    iv_model = IVModel(
        vrp_multiple=iv_cfg.vrp_multiple,
        anchor_halflife=iv_cfg.anchor_halflife_days,
        rv_lookback=iv_cfg.rv_lookback,
        market_beta=iv_cfg.market_beta,
        rank_lookback=iv_cfg.rank_lookback,
        estimator=cfg.data.volatility_estimator,
    )

    real_chain = None
    if request.source == SOURCE_ALPACA and chain_cfg.source == "real":
        real_chain = _real_chain(settings, request, symbols, bars, chain_model)
        if real_chain is None:
            log.warning(
                "backtest.chain.source is 'real' but no real option data could "
                "be assembled - this run is MODELLED. The dashboard says so."
            )

    intraday = (
        synthetic_intraday
        if request.source == SOURCE_SYNTHETIC
        else _intraday_bars(settings, request, symbols, feed_for_intraday)
    )

    source = HistoricalContextSource(
        {k: v for k, v in bars.items() if k in symbols or k == market_symbol},
        start=request.start,
        end=request.end,
        chain_model=chain_model,
        iv_model=iv_model,
        news=news,
        news_lookback_hours=bt.news_lookback_hours,
        session_times_et=tuple(request.session_times_et or bt.session_times_et),
        mark_interval_minutes=bt.mark_interval_minutes,
        market_symbol=market_symbol,
        min_history=bt.min_history_days,
        intraday_history_sessions=cfg.data.intraday_lookback_days,
        real_chain=real_chain,
        intraday_by_symbol=intraday,
        min_iv_observations=chain_cfg.min_iv_observations,
        options_dte=chain_dte,
        term_anchors=(
            cfg.data.term_front_dte,
            cfg.data.term_back_dte,
            cfg.data.term_min_separation_days,
        ),
        term_max_abs_slope_pct=cfg.data.term_max_abs_slope_pct,
    )
    source.chain_source_requested = chain_cfg.source
    return source



def _build_unconfigured(
    cfg: Any, missing: set[str], params: dict[str, Any] | None = None
) -> list[Any]:
    """Instantiate registered strategies that `config.strategies` does not list.

    They get a synthetic ref with empty params, so each falls back to its own
    default params file - which is what a strategy that owns its config (the
    events book) already expects.
    """
    if not missing:
        return []
    from oaa.config.schema import StrategyRef
    from oaa.strategies.base import strategy_registry

    strategy_registry.autoload("oaa.strategies")
    built: list[Any] = []
    for name in sorted(missing):
        try:
            cls = strategy_registry.get(name)
        except Exception:  # noqa: BLE001 - an unknown name is reported below
            log.warning("strategy '%s' is not registered - ignoring", name)
            continue
        ref = StrategyRef(
            name=name, enabled=True, book=getattr(cls, "book", "intraday"),
            params=dict(params or {}),
        )
        built.append(cls(ref, cfg))
        log.info("built '%s' from the registry - not listed in config", name)
    return built

# --------------------------------------------------------------------------- #
def run_backtest(
    settings: Settings,
    request: BacktestRequest,
    progress: Any = None,
) -> BacktestResult:
    """Build the source, drive the engine, stamp the provenance."""
    cfg = settings.config
    if request.initial_cash is not None:
        cfg.backtest.initial_cash = float(request.initial_cash)
    if request.slippage_spread_fraction is not None:
        cfg.backtest.slippage_spread_fraction = float(request.slippage_spread_fraction)

    strategies = load_strategies(cfg)
    if request.strategies:
        wanted = {s.lower() for s in request.strategies}
        strategies = [s for s in strategies if s.name.lower() in wanted]
        # Naming a strategy explicitly is a request to run THAT strategy, even
        # when config has it switched off or does not list it at all. The
        # events book is deliberately absent from `config.strategies` - it runs
        # in its own process - so without this, `--strategy
        # earnings_event_directional` failed with "no strategies selected",
        # which reads as a config error rather than as the design.
        strategies += _build_unconfigured(
            cfg, wanted - {s.name.lower() for s in strategies}, request.strategy_params
        )
        for built in strategies:
            if request.strategy_params:
                built.params = {**(built.params or {}), **request.strategy_params}
    if not strategies:
        raise ValueError(
            "no strategies selected - enable one in config/default.yaml or pick "
            "one in the sidebar"
        )

    source = build_source(settings, request)

    critic_cfg = cfg.backtest.critic
    critic = ReplayCritic(
        cfg,
        mode=request.critic_mode or critic_cfg.mode,
        cache_dir=settings.path(cfg.backtest.cache_dir) / "critic",
        max_llm_calls=critic_cfg.max_llm_calls,
    )
    memory = _replay_memory(settings) if critic_cfg.memory and critic.active else None

    engine = BacktestEngine(
        settings, strategies=strategies, critic=critic, memory=memory,
        partners=_partners(cfg),
    )
    engine.chain = source.chain_model
    engine.real_chain = source.real_chain
    engine.catalyst = _catalyst_engine(settings)
    try:
        result = engine.run(source, progress=progress)
    finally:
        if memory is not None:
            import shutil

            shutil.rmtree(memory.path.parent, ignore_errors=True)

    if source.real_chain is not None and source.chain_requests:
        empty = source.empty_chain_sessions
        if empty == source.chain_requests:
            log.error(
                "REAL CHAIN PRODUCED NOTHING: all %d chain requests came back "
                "empty, so no option was priced from real data and no trade "
                "could be built. The contracts listed do not cover the strikes "
                "and expiries this window needs - check listing_band, the "
                "strategies' DTE range, and whether max_contracts_per_symbol "
                "capped away the near-the-money strikes.",
                source.chain_requests,
            )
        elif empty:
            log.warning(
                "%d of %d sessions had no real contracts in range and were "
                "skipped entirely", empty, source.chain_requests,
            )

    result.provenance.update(
        {
            "request": request.as_dict(),
            "universe": sorted(source.histories),
            # Daily closes of every underlying the replay offered, so the
            # dashboard can show pairwise correlation for a SAVED run without
            # re-fetching - and so the correlations a reader sees are the ones
            # that held during the replayed window, not the ones holding today.
            "underlying_closes": _underlying_closes(source, request),
            "chain_source_requested": getattr(source, "chain_source_requested", "modelled"),
            "chain_source_used": "real" if source.real_chain is not None else "modelled",
            "data_source": _describe_source(request, source),
            "synthetic": request.source == SOURCE_SYNTHETIC,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "git_commit": _git_commit(settings.root),
            # The commit alone does not identify what ran - see _git_worktree.
            "git_worktree": _git_worktree(settings.root),
            "python": platform.python_version(),
        }
    )
    return result


def _underlying_closes(
    source: Any, request: BacktestRequest
) -> dict[str, list[tuple[str, float]]]:
    """`{symbol: [(iso date, close), ...]}` over the replayed window.

    Warmup bars are dropped: the correlation on screen should describe the
    window the equity curve covers, not the 60 sessions of history the vol
    model needed before it.
    """
    out: dict[str, list[tuple[str, float]]] = {}
    start, end = request.start.isoformat(), request.end.isoformat()
    for symbol, history in getattr(source, "histories", {}).items():
        rows: list[tuple[str, float]] = []
        for bar in getattr(history, "bars", []) or []:
            close = bar.get("close")
            if close is None:
                continue
            day = str(bar.get("timestamp"))[:10]
            if start <= day <= end:
                rows.append((day, float(close)))
        if rows:
            out[symbol.upper()] = rows
    return out


# --------------------------------------------------------------------------- #
# the run store
# --------------------------------------------------------------------------- #
def run_id(request: BacktestRequest, when: dt.datetime | None = None) -> str:
    stamp = (when or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    tag = request.label or "-".join(sorted(s.upper() for s in request.symbols))[:32]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in tag)
    return f"{stamp}__{safe}" if safe else stamp


def save_run(settings: Settings, request: BacktestRequest, result: BacktestResult) -> Path:
    root = settings.path(settings.config.backtest.output_dir)
    directory = root / run_id(request)
    directory.mkdir(parents=True, exist_ok=True)

    payload = result.as_dict()
    (directory / "result.json").write_text(json.dumps(payload, indent=2, default=str))
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": directory.name,
                "label": request.label,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "symbols": sorted(s.upper() for s in request.symbols),
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
                "source": request.source,
                "strategies": result.provenance.get("strategies", []),
                "metrics": payload["metrics"],
            },
            indent=2,
            default=str,
        )
    )
    lines = ["timestamp,equity"]
    lines += [f"{ts},{value}" for ts, value in payload["equity_curve"]]
    (directory / "equity.csv").write_text("\n".join(lines))
    log.info("run saved to %s", directory)
    return directory


def list_runs(settings: Settings) -> list[dict[str, Any]]:
    root = settings.path(settings.config.backtest.output_dir)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for manifest in sorted(root.glob("*/manifest.json"), reverse=True):
        try:
            data = json.loads(manifest.read_text())
        except json.JSONDecodeError:
            continue
        data["path"] = str(manifest.parent)
        out.append(data)
    return out


def load_run(path: str | Path) -> dict[str, Any]:
    """Read a saved run, gzipped or not.

    `result.json` is where a local run writes itself. `result.json.gz` is what
    `scripts/publish_runs.py` writes into `public/runs/`, because the raw file
    runs to 50MB on a wide universe and 333MB across the whole store - far too
    much for a repo a deploy host has to clone. It compresses about 28x, which
    is the difference between publishing every backtest and publishing three.

    Plain wins if both are present: a locally re-run result should never be
    shadowed by an older published copy.
    """
    directory = Path(path)
    plain = directory / "result.json"
    if plain.exists():
        return json.loads(plain.read_text())
    packed = directory / "result.json.gz"
    if packed.exists():
        with gzip.open(packed, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    raise FileNotFoundError(f"no result.json or result.json.gz in {directory}")


def _real_chain(
    settings: Settings,
    request: BacktestRequest,
    symbols: list[str],
    bars: dict[str, list[dict[str, Any]]],
    chain_model: ChainModel,
) -> RealChainBuilder | None:
    """List the real contracts and pull their real bars.

    Two windows matter and they are different. Contracts must cover every
    expiry the strategy could trade from the first replayed session, which
    reaches `options.max_days_to_expiry` PAST the end of the window. Bars must
    cover the replay itself plus `iv_history_days` before it, because implied
    vol has to be ranked against its own history and a rank computed over three
    weeks is not a rank.

    The strike band is taken from the underlying's actual range over that whole
    period rather than from one day's spot, because a name that moved 20% would
    otherwise have half its traded strikes missing from the listing.
    """
    cfg = settings.config
    bt = cfg.backtest
    chain_cfg = bt.chain

    feed = HistoricalFeed(
        api_key=settings.credentials.api_key,
        secret_key=settings.credentials.secret_key,
        cache_dir=settings.path(bt.cache_dir),
        stock_feed=cfg.data.stock_feed,
        offline=request.offline,
    )

    history_start = request.start - dt.timedelta(days=chain_cfg.iv_history_days)
    expiry_to = request.end + dt.timedelta(days=cfg.options.max_days_to_expiry + 7)

    contracts: dict[str, list[dict[str, Any]]] = {}
    wanted_bars: list[str] = []

    for symbol in symbols:
        rows = [
            b for b in bars.get(symbol, [])
            if history_start <= _bar_date(b) <= request.end
        ]
        if not rows:
            log.warning("%s: no underlying bars in the window - no option chain", symbol)
            continue
        closes = [float(b["close"]) for b in rows]
        window = chain_cfg.strike_window_pct
        low = round(min(closes) * (1 - window), 2)
        high = round(max(closes) * (1 + window), 2)

        try:
            listed = feed.option_contracts(symbol, history_start, expiry_to, low, high)
        except DataError as exc:
            log.warning("%s: contract listing failed (%s) - falling back to the "
                        "modelled chain for this symbol", symbol, exc)
            continue

        raw = len(listed)
        dte_low, dte_high = tradable_dte_range(cfg)
        listed = _relevant(
            listed,
            by_day={_bar_date(b): float(b["close"]) for b in rows},
            replay_from=request.start,
            dte_range=(dte_low, dte_high),
            band=chain_cfg.listing_band,
            history_band=chain_cfg.iv_history_band,
            history_stride=chain_cfg.iv_history_expiry_stride_days,
        )
        log.info(
            "%s: %d contracts listed -> %d relevant (%d-%d DTE, strikes within "
            "%.0f%% of the underlying while tradable)",
            symbol, raw, len(listed), dte_low, dte_high, chain_cfg.listing_band * 100,
        )

        if len(listed) > chain_cfg.max_contracts_per_symbol:
            log.warning(
                "%s: %d relevant contracts, capped at %d - the widest strikes "
                "were DROPPED and will be modelled, not measured. If this "
                "fires, the LISTING is too wide before the cap is: narrow "
                "backtest.chain.listing_band or the strategies' DTE range "
                "rather than raising the cap.",
                symbol, len(listed), chain_cfg.max_contracts_per_symbol,
            )
            listed = _rank_for_cap(listed, rows, request.start)
            listed = listed[: chain_cfg.max_contracts_per_symbol]

        contracts[symbol] = listed
        wanted_bars.extend(c["symbol"] for c in listed)

    if not wanted_bars:
        log.warning("no option contracts resolved - using the modelled chain")
        return None

    log.info(
        "option data: %d contracts across %d underlyings, bars %s..%s",
        len(wanted_bars), len(contracts), history_start, request.end,
    )
    try:
        option_bars = feed.option_bars(wanted_bars, history_start, request.end, "1Day")
    except DataError as exc:
        log.warning("option bar fetch failed (%s) - using the modelled chain", exc)
        return None

    builder = RealChainBuilder.from_payload(contracts, option_bars, chain_model)
    log.info(
        "option coverage: %d contracts listed, %d with at least one print",
        builder.coverage.contracts_listed, len(option_bars),
    )
    return builder


def _interval_minutes(cfg: Any) -> int:
    """Minutes per intraday bar, parsed from `data.intraday_timeframe`."""
    text = str(getattr(cfg.data, "intraday_timeframe", "5Min")).lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    minutes = int(digits) if digits else 5
    if "hour" in text or text.endswith("h"):
        minutes *= 60
    return max(1, minutes)


def _intraday_bars(
    settings: Settings,
    request: BacktestRequest,
    symbols: list[str],
    feed: Any,
) -> dict[str, list[dict[str, Any]]]:
    """5-minute bars, but only if an enabled strategy actually reads them.

    The intraday book refuses a session with fewer than 30 intraday bars -
    VWAP and Bollinger width are meaningless on fewer - so a replay without
    these produces a book that silently never trades. Fetching them for a
    carry-only run would be tens of thousands of bars nobody reads.
    """
    cfg = settings.config
    needs = any(
        (ref.params or {}).get("momentum") or (ref.params or {}).get("time_gate")
        for ref in cfg.enabled_strategies()
    )
    if not needs or feed is None or not cfg.data.fetch_intraday:
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    timeframe = cfg.data.intraday_timeframe
    for symbol in symbols:
        try:
            rows = feed.bars(symbol, request.start, request.end, timeframe)
        except DataError as exc:
            log.warning("%s: no %s bars (%s) - the intraday book cannot trade it",
                        symbol, timeframe, exc)
            continue
        if rows:
            out[symbol] = rows
    log.info("intraday: %s bars fetched for %d symbols", timeframe, len(out))
    return out


def _relevant(
    listed: list[dict[str, Any]],
    by_day: dict[dt.date, float],
    replay_from: dt.date,
    dte_range: tuple[int, int],
    band: float,
    history_band: float,
    history_stride: int = 7,
) -> list[dict[str, Any]]:
    """Keep only contracts the replay could plausibly look at.

    Listing every strike of every weekly expiry over eight months of SPY is
    ~60,000 contracts, which is ~600 bar requests for a chain the strategy
    never touches: a 14-delta condor reads a handful of strikes near the money.

    Two bands, because the contract set serves two jobs. Inside the replay
    window a contract matters if its strike came within `band` of the
    underlying at any point while it was tradable - that is the chain the
    strategy actually sees. Before the window the contracts exist only to
    recover a historical ATM implied-vol series for IV rank, and that needs
    near-the-money strikes only, so a much tighter band applies.
    """
    if not by_day:
        return listed
    sessions = sorted(by_day)
    dte_low, dte_high = dte_range

    # In the history period keep one expiry per stride. SPY lists an expiration
    # almost every trading day; one ATM implied-vol reading per session needs
    # one expiry per week, not thirty-five of them.
    history_expiries = sorted(
        {
            str(c["expiry"])[:10] for c in listed
            if str(c["expiry"])[:10] < replay_from.isoformat()
        }
    )
    keep_history: set[str] = set()
    last: dt.date | None = None
    for text in history_expiries:
        try:
            day = dt.date.fromisoformat(text)
        except ValueError:
            continue
        if last is None or (day - last).days >= history_stride:
            keep_history.add(text)
            last = day

    kept: list[dict[str, Any]] = []
    for contract in listed:
        try:
            text = str(contract["expiry"])[:10]
            expiry = dt.date.fromisoformat(text)
            strike = float(contract["strike"])
        except (KeyError, ValueError):
            continue

        in_replay = expiry >= replay_from
        if not in_replay and text not in keep_history:
            continue

        # Only the sessions on which this contract sits inside a tradable DTE
        # band matter - that is when the strategy could actually see it.
        window_start = expiry - dt.timedelta(days=dte_high)
        window_end = expiry - dt.timedelta(days=dte_low)
        prices = [by_day[d] for d in sessions if window_start <= d <= window_end]
        if not prices:
            continue

        width = band if in_replay else history_band
        low, high = min(prices) * (1 - width), max(prices) * (1 + width)
        if low <= strike <= high:
            kept.append(contract)
    return kept


def _rank_for_cap(
    listed: list[dict[str, Any]], bars: list[dict[str, Any]], replay_from: dt.date
) -> list[dict[str, Any]]:
    """Order contracts so the cap drops the least useful ones first.

    The previous ordering measured distance from the MEAN close over the whole
    history window, which is not where the underlying is during the replay. A
    name that drifted from 700 to 765 had its cap keep twelve thousand strikes
    around 700 and discard everything near the money in the window being
    tested - so the chain came back empty and coverage was 0/0 while every
    contract looked accounted for.

    Rank by what the replay actually reads: contracts inside the window first,
    then by distance from the spot at the time each is tradable.
    """
    in_window = [b for b in bars if _bar_date(b) >= replay_from] or bars
    spot = float(in_window[-1]["close"])
    first_close = float(bars[0]["close"])

    def key(contract: dict[str, Any]) -> tuple[int, float]:
        try:
            expiry = dt.date.fromisoformat(str(contract["expiry"])[:10])
            strike = float(contract["strike"])
        except (KeyError, ValueError):
            return (2, 0.0)
        if expiry >= replay_from:
            return (0, abs(strike / spot - 1.0))
        # history contracts serve only the ATM implied-vol series
        return (1, abs(strike / first_close - 1.0))

    return sorted(listed, key=key)


def _bar_date(bar: dict[str, Any]) -> dt.date:
    stamp = bar["timestamp"]
    if hasattr(stamp, "date"):
        return stamp.date()
    return dt.date.fromisoformat(str(stamp)[:10])


def _replay_memory(settings: Settings) -> Any:
    """A throwaway outcome store, seeded only by this replay's own closes.

    The live critic is handed recent outcomes; a replay that withholds them is
    not running the same critic. It is scoped to one run and deleted afterwards,
    so it can never leak into the live agent's memory or into the next run.

    It lives in the system temp directory rather than under the repo for two
    reasons. It is scratch, and scratch does not belong in a user's project
    folder. And SQLite needs POSIX locking, which network shares, mounted
    volumes and some sync clients (iCloud Drive, Dropbox) do not provide - a
    repo on one of those fails with a bare "disk I/O error" that points at the
    schema statement and says nothing about the filesystem.
    """
    import tempfile

    from oaa.agents.memory import Memory

    directory = Path(tempfile.mkdtemp(prefix="oaa-replay-memory-"))
    return Memory(
        directory / "memory.sqlite",
        lookback_days=settings.config.agents.memory.lookback_days,
    )


def _catalyst_engine(settings: Settings) -> Any:
    """The intraday book's catalyst gate, reading REAL Alpaca headlines.

    Deterministic by design - the same engine the live loop runs. Without one
    the gate vetoes every candidate with "no catalyst engine wired into this
    cycle", which is correct behaviour and a silent zero-trade backtest.
    """
    cfg = settings.config
    params: dict[str, Any] = {}
    for ref in cfg.enabled_strategies():
        block = (ref.params or {}).get("catalyst_gate")
        if block:
            params = block
            break
    if not params:
        return None

    from oaa.signals.catalyst import CatalystEngine, MacroCalendar

    calendar_path = params.get("macro_calendar")
    calendar = MacroCalendar.load(
        settings.path(calendar_path) if calendar_path else None
    )
    return CatalystEngine(
        weights=params.get("factor_weights"),
        lookback_minutes=int(params.get("lookback_minutes", 30)),
        calendar=calendar,
    )


def _partners(cfg: Any) -> Any:
    """The partner hub, only when an adapter is actually enabled.

    Loading it otherwise costs a pointless import per run and the veto stage
    would be a no-op anyway.
    """
    if not cfg.partners.enabled or not any(a.enabled for a in cfg.partners.adapters):
        return None
    from oaa.partners.base import PartnerHub

    return PartnerHub(cfg)


def _describe_source(request: BacktestRequest, source: HistoricalContextSource) -> str:
    if request.source != SOURCE_ALPACA:
        return "SYNTHETIC price path - wiring test only, not a backtest"
    if source.real_chain is None:
        return (
            "REAL Alpaca daily bars + REAL Alpaca news; option chain MODELLED"
        )
    coverage = source.real_chain.coverage
    return (
        "REAL Alpaca daily bars + REAL Alpaca news + REAL Alpaca option bars "
        f"({coverage.real_fraction:.0%} of marks from actual prints); implied "
        "vol RECOVERED by inverting Black-Scholes on the traded price; "
        "bid-ask spread MODELLED"
    )


def _git_commit(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip()
    except Exception:  # noqa: BLE001
        return None


def _git_worktree(root: Path) -> dict[str, Any]:
    """Fingerprint the code that ACTUALLY ran, not just the last commit.

    A commit hash alone is a lie whenever the tree is dirty: runs days apart,
    with different gates and a different engine, all stamped the same hash and
    were therefore not comparable to each other. `dirty` says whether anything
    was uncommitted; `diff_sha` is a hash OF the uncommitted diff, so two runs
    made from the same edits match and two runs made from different edits do
    not - without writing the diff itself into every result file.
    """
    out: dict[str, Any] = {"commit": _git_commit(root), "dirty": None, "diff_sha": None}
    try:
        diff = subprocess.check_output(
            ["git", "-C", str(root), "diff", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=15,
        )
    except Exception:  # noqa: BLE001
        return out
    out["dirty"] = bool(diff.strip())
    if diff.strip():
        out["diff_sha"] = hashlib.sha256(diff).hexdigest()[:12]
    return out
