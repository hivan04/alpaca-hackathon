"""`oaa` - the command line surface.

    oaa doctor            check every dependency and credential before you need them
    oaa account           account, options level, positions
    oaa chain SPY         inspect a filtered option chain
    oaa scan              one dry scan: what would the agent do right now?
    oaa trade             one live cycle
    oaa run               the autonomous loop (this is the deliverable)
    oaa firewall          the capital boundary: phase, reservations, lease
    oaa discover          what the market is watching + today's regime read
    oaa pool              the accumulated candidate pool
    oaa gates             the gate-by-gate rejection log
    oaa agent <cycle>     one AI-assistant-driven cycle over MCP
    oaa manage            close positions that hit their exit rules
    oaa flatten           close everything
    oaa report            performance report -> JSON + HTML
    oaa partners          list technology-partner adapters and their stages
    oaa mcp-tools         list the tools the Alpaca MCP server exposes
    oaa serve             the public read-only dashboard (FastAPI)
    oaa backtest          replay the strategies over Alpaca history
    oaa dashboard         the Streamlit operator dashboard (backtest + live)
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from oaa import __version__

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Options Alpha Agents - autonomous options trading agents on Alpaca.",
)
console = Console()

_PROFILE = typer.Option(None, "--profile", "-p", help="dev | judged")
_CONFIG = typer.Option(None, "--config", "-c", help="Path to a config YAML")


# --------------------------------------------------------------------------- #
def _boot(profile: str | None, config: str | None, backend: str | None = None):
    """Load settings, wire logging, and build the broker + data provider."""
    from oaa.brokers.factory import get_broker
    from oaa.config.loader import load_settings
    from oaa.core.logging import setup_logging
    from oaa.data.factory import get_data_provider

    settings = load_settings(config_path=config, profile=profile)
    cfg = settings.config
    setup_logging(cfg.telemetry.log_level, cfg.telemetry.log_format)
    broker = get_broker(cfg, settings.credentials, backend=backend)
    data = get_data_provider(cfg, settings.credentials)
    return settings, broker, data


def _settings_only(profile: str | None, config: str | None):
    from oaa.config.loader import load_settings
    from oaa.core.logging import setup_logging

    settings = load_settings(config_path=config, profile=profile)
    setup_logging(settings.config.telemetry.log_level, settings.config.telemetry.log_format)
    return settings


def _journal(settings):
    from oaa.telemetry.journal import Journal

    t = settings.config.telemetry
    return Journal(settings.path(t.journal), settings.path(t.db), settings.path(t.equity_curve))


# --------------------------------------------------------------------------- #
@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"oaa {__version__}")


@app.command()
def doctor(profile: str | None = _PROFILE, config: str | None = _CONFIG) -> None:
    """Check every prerequisite. Run this before you need it to work."""
    import importlib
    import os
    import shutil

    from oaa.config.loader import load_settings

    table = Table("Check", "Status", "Detail", title="oaa doctor", title_style="bold")
    ok = True

    def row(name: str, passed: bool, detail: str = "", warn: bool = False) -> None:
        nonlocal ok
        if not passed and not warn:
            ok = False
        mark = "[green]PASS[/]" if passed else ("[yellow]WARN[/]" if warn else "[red]FAIL[/]")
        table.add_row(name, mark, detail)

    # config
    try:
        settings = load_settings(config_path=config, profile=profile)
        cfg = settings.config
        row("config", True, f"profile={cfg.profile}, {len(cfg.strategies)} strategies")
    except Exception as exc:  # noqa: BLE001
        row("config", False, str(exc)[:120])
        console.print(table)
        raise typer.Exit(1) from exc

    # packages
    for module, required in [("alpaca", True), ("yaml", True), ("pydantic", True),
                             ("mcp", False), ("anthropic", False), ("google.genai", False),
                             ("fastapi", False), ("streamlit", False), ("plotly", False)]:
        try:
            importlib.import_module(module)
            row(f"python: {module}", True)
        except ImportError:
            row(f"python: {module}", False,
                "optional - pip install -e '.[all]'" if not required else "missing",
                warn=not required)

    # cli binary
    binary = cfg.broker.cli.binary
    path = shutil.which(binary)
    row(f"binary: {binary}", bool(path), path or "brew install alpacahq/tap/cli", warn=not path)
    uvx = shutil.which("uvx")
    row("binary: uvx", bool(uvx), uvx or "needed for the MCP server", warn=not uvx)

    # credentials
    creds = settings.credentials
    row("credentials", creds.configured,
        f"{creds.masked()} (profile={creds.profile}, paper={creds.paper})")
    row("judged account id", bool(creds.account_id),
        creds.account_id or "set ALPACA_JUDGED_ACCOUNT_ID in .env - required at submission",
        warn=True)

    # LLM providers - live and backtest are deliberately different
    def _llm_row(label: str, llm_cfg) -> None:
        env_names = {
            "anthropic": ["ANTHROPIC_API_KEY"],
            "openai": ["OPENAI_API_KEY"],
            "gemini": [llm_cfg.api_key_env or "GEMINI_API_KEY", "GOOGLE_API_KEY"],
        }.get(llm_cfg.provider or "", [])
        found = next((n for n in env_names if os.getenv(n)), None)
        detail = f"{llm_cfg.provider or 'none'} / {llm_cfg.model}"
        if not env_names:
            row(label, True, detail + " (rules-only)")
            return
        row(
            label, bool(found),
            f"{detail} - key from {found}" if found
            else f"{detail} - set {env_names[0]} in .env",
            warn=True,
        )

    _llm_row("llm: live agent", cfg.agents.llm)
    backtest_llm = cfg.backtest.critic.llm
    if backtest_llm is None:
        row("llm: backtest critic", True, "shares the live provider")
    else:
        _llm_row("llm: backtest critic", backtest_llm)

    # Paper vs live is decided by broker.paper in YAML and forced onto every
    # subprocess from there. A stray ALPACA_PAPER_TRADE in .env looks like a
    # switch and is not one, which is the dangerous kind of dead config.
    stray = os.getenv("ALPACA_PAPER_TRADE")
    row("paper/live switch", stray is None,
        f"broker.paper={cfg.broker.paper} (YAML is authoritative)" if stray is None
        else f"ALPACA_PAPER_TRADE={stray} in .env has NO effect - remove it, "
             f"broker.paper={cfg.broker.paper} is what applies",
        warn=True)

    # live connection
    if creds.configured:
        try:
            from oaa.brokers.factory import get_broker

            broker = get_broker(cfg, creds, allow_fallback=False)
            account = broker.account()
            row("alpaca connection", True,
                f"equity ${account.equity:,.2f}, account {account.account_id}")
            level = account.options_trading_level
            row("options level", bool(level and level >= 3),
                f"level {level} (multi-leg spreads need 3)" if level else "unknown",
                warn=True)
            row("market", True, "open" if broker.is_market_open() else "closed")
        except Exception as exc:  # noqa: BLE001
            row("alpaca connection", False, str(exc)[:120])

    console.print(table)
    if cfg.execution.dry_run:
        console.print("[yellow]dry_run is ON - no orders will reach Alpaca.[/]")
    if cfg.profile == "judged":
        console.print("[bold red]PROFILE=judged - this account is the one judges see.[/]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def account(profile: str | None = _PROFILE, config: str | None = _CONFIG) -> None:
    """Account snapshot and open positions."""
    _, broker, _ = _boot(profile, config)
    snap = broker.account()
    console.print(Panel.fit(
        f"[bold]{snap.account_id}[/]\n"
        f"equity      ${snap.equity:,.2f}\n"
        f"cash        ${snap.cash:,.2f}\n"
        f"buying pwr  ${snap.buying_power:,.2f}\n"
        f"options bp  ${(snap.options_buying_power or 0):,.2f}\n"
        f"opt level   {snap.options_trading_level}\n"
        f"day P&L     {snap.day_pl:+,.2f} ({snap.day_pl_pct:+.2%})",
        title="account",
    ))
    if snap.positions:
        table = Table("Symbol", "Qty", "Entry", "Value", "P&L", "P&L %")
        for position in snap.positions:
            table.add_row(
                position.symbol, f"{position.qty:g}", f"{position.avg_entry_price:.2f}",
                f"{position.market_value:,.2f}", f"{position.unrealized_pl:+,.2f}",
                f"{position.unrealized_plpc:+.2%}",
            )
        console.print(table)
    else:
        console.print("[dim]no open positions[/]")


@app.command()
def chain(
    symbol: str,
    dte: int = typer.Option(30, help="Target days to expiry"),
    limit: int = typer.Option(20, help="Rows to show"),
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Inspect a filtered option chain - the view a strategy actually sees."""
    settings, _, data = _boot(profile, config)
    from oaa.options.chain import ChainView

    symbol = symbol.upper()
    spot = data.spot(symbol)
    quotes = data.option_chain(symbol)
    view = ChainView.from_quotes(symbol, spot, quotes)

    console.print(f"[bold]{symbol}[/] spot {spot:.2f} | "
                  f"{len(quotes)} contracts, {len(view)} after liquidity filter")
    if view.is_empty:
        console.print("[red]nothing survived the filter - loosen config/default.yaml options.*[/]")
        raise typer.Exit(1)

    expiry = view.nearest_expiry(dte)
    table = Table("Symbol", "Right", "Strike", "Bid", "Ask", "Mid", "IV", "Delta", "OI",
                  title=f"{symbol} {expiry} ({(expiry - view.asof).days}d)")
    rows = sorted(view.for_expiry(expiry), key=lambda q: abs(q.strike - spot))[:limit]
    for q in sorted(rows, key=lambda q: (q.right.value, q.strike)):
        table.add_row(
            q.symbol, q.right.value, f"{q.strike:.2f}",
            f"{q.bid:.2f}" if q.bid else "-", f"{q.ask:.2f}" if q.ask else "-",
            f"{q.mid:.2f}" if q.mid else "-",
            f"{q.implied_volatility:.1%}" if q.implied_volatility else "-",
            f"{q.greeks.delta:+.3f}" if q.greeks.delta is not None else "-",
            str(q.open_interest or "-"),
        )
    console.print(table)


@app.command()
def scan(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    cycle: str = typer.Option("carry_scan", help="carry_scan | intraday_scan | discover"),
    live: bool = typer.Option(False, "--live", help="Actually place orders (overrides dry_run)"),
) -> None:
    """One scan cycle across every enabled book. Dry by default."""
    from oaa.agents.orchestrator import Orchestrator

    settings, broker, data = _boot(profile, config)
    if not live:
        settings.config.execution.dry_run = True

    orch = Orchestrator(settings, broker, data)
    try:
        result = orch.run_cycle(cycle, f"cli-{cycle}")
        console.print(Panel.fit(result.summary(), title=cycle))
        for error in result.errors:
            console.print(f"[red]{error}[/]")
    finally:
        orch.close()


@app.command()
def trade(profile: str | None = _PROFILE, config: str | None = _CONFIG) -> None:
    """One live cycle. Respects the profile's dry_run setting."""
    from oaa.agents.orchestrator import Orchestrator

    settings, broker, data = _boot(profile, config)
    orch = Orchestrator(settings, broker, data)
    try:
        console.print(Panel.fit(orch.run_cycle("scan_and_trade", "cli-trade").summary()))
    finally:
        orch.close()


@app.command()
def manage(profile: str | None = _PROFILE, config: str | None = _CONFIG) -> None:
    """Apply exit rules to open positions."""
    from oaa.agents.orchestrator import Orchestrator

    settings, broker, data = _boot(profile, config)
    orch = Orchestrator(settings, broker, data)
    try:
        console.print(Panel.fit(orch.run_cycle("manage_positions", "cli-manage").summary()))
    finally:
        orch.close()


@app.command()
def flatten(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation"),
) -> None:
    """Close every position. Run before the submission deadline."""
    from oaa.agents.orchestrator import Orchestrator

    settings, broker, data = _boot(profile, config)
    if not yes:
        typer.confirm(
            f"Close ALL positions in the '{settings.config.profile}' account?", abort=True
        )
    orch = Orchestrator(settings, broker, data)
    try:
        console.print(Panel.fit(orch.run_cycle("flatten", "cli-flatten").summary()))
    finally:
        orch.close()


@app.command()
def run(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    once: bool = typer.Option(False, "--once", help="Run due cycles once, then exit"),
) -> None:
    """Start the autonomous loop. This is the deliverable."""
    from oaa.agents.orchestrator import Orchestrator
    from oaa.agents.runner import Runner

    settings, broker, data = _boot(profile, config)
    orch = Orchestrator(settings, broker, data)
    console.print(Panel.fit(
        f"profile [bold]{settings.config.profile}[/] | broker {broker.name} | "
        f"dry_run {settings.config.execution.dry_run}",
        title="autonomous loop starting",
    ))
    try:
        Runner(orch).run(once=once)
    finally:
        orch.close()


@app.command()
def report(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    out: str | None = typer.Option(None, help="Output directory"),
) -> None:
    """Performance report from the journal -> JSON + self-contained HTML."""
    from oaa.telemetry.metrics import compute_metrics
    from oaa.telemetry.report import write_report

    settings = _settings_only(profile, config)
    journal = _journal(settings)
    equity = journal.equity_series()
    metrics = compute_metrics(equity, journal.fills(2000), journal.decisions(2000))

    console.print(Panel.fit("\n".join(metrics.summary_lines()), title="performance"))
    if metrics.rejection_reasons:
        table = Table("Rule", "Rejections", title="why trades were declined")
        for rule, count in sorted(metrics.rejection_reasons.items(), key=lambda kv: -kv[1]):
            table.add_row(rule, str(count))
        console.print(table)

    out_dir = settings.path(out or f"{settings.config.telemetry.run_dir}/report")
    paths = write_report(metrics, equity, out_dir, title=settings.config.meta.project)
    console.print(f"[green]wrote[/] {paths['html']}")
    console.print(f"[green]wrote[/] {paths['json']}")


@app.command()
def journal(
    limit: int = typer.Option(20),
    action: str | None = typer.Option(None, help="open | close | skip"),
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Recent decisions, including the trades that were declined."""
    settings = _settings_only(profile, config)
    rows = _journal(settings).decisions(limit, action)
    if not rows:
        console.print("[dim]no decisions recorded yet[/]")
        return
    table = Table("When", "Cycle", "Action", "Symbol", "Strategy", "OK", "Reason")
    for row in rows:
        approved = row.get("approved")
        table.add_row(
            str(row["ts"])[11:19], str(row.get("cycle") or ""), str(row.get("action") or ""),
            str(row.get("symbol") or ""), str(row.get("strategy") or ""),
            "-" if approved is None else ("[green]y[/]" if approved else "[red]n[/]"),
            (str(row.get("reason") or "")[:70]),
        )
    console.print(table)


@app.command()
def partners(profile: str | None = _PROFILE, config: str | None = _CONFIG) -> None:
    """Technology-partner adapters and the pipeline stages they attach to."""
    from oaa.partners.base import PartnerHub

    settings = _settings_only(profile, config)
    hub = PartnerHub(settings.config)
    stages = hub.stages()
    if not stages:
        console.print(
            "[yellow]No partner adapters enabled.[/]\n"
            "Sponsor technologies are announced at kickoff - see docs/PARTNERS.md, "
            "then copy src/oaa/partners/example_partner.py."
        )
        return
    table = Table("Stage", "Adapter", "Partner", "Ready", "Contributes")
    for stage, adapters in stages.items():
        for adapter in adapters:
            table.add_row(
                stage, adapter["name"], adapter["partner"],
                "[green]yes[/]" if adapter["available"] else "[yellow]no[/]",
                adapter["contribution"],
            )
    console.print(table)


@app.command("mcp-tools")
def mcp_tools(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    filter: str | None = typer.Option(None, help="Substring filter, e.g. 'option'"),
) -> None:
    """List the tools the Alpaca MCP server exposes."""
    from oaa.brokers.alpaca_mcp import McpBridge
    from oaa.config.loader import load_settings

    settings = load_settings(config_path=config, profile=profile)
    bridge = McpBridge(settings.config, settings.credentials)
    bridge.start()
    try:
        names = sorted(bridge.tools)
        if filter:
            names = [n for n in names if filter.lower() in n.lower()]
        table = Table("Tool", "Description", title=f"{len(names)} MCP tools")
        for name in names:
            description = (bridge.tools[name].description or "").split("\n")[0][:88]
            table.add_row(name, description)
        console.print(table)
    finally:
        bridge.stop()


@app.command()
def strategies(profile: str | None = _PROFILE, config: str | None = _CONFIG) -> None:
    """Registered strategies and whether they are enabled."""
    from oaa.strategies.base import strategy_registry

    settings = _settings_only(profile, config)
    strategy_registry.autoload("oaa.strategies")
    enabled = {s.name: s for s in settings.config.strategies}
    table = Table("Name", "Enabled", "Weight", "Description")
    for name, cls in strategy_registry:
        ref = enabled.get(name)
        table.add_row(
            name,
            "[green]yes[/]" if ref and ref.enabled else "[dim]no[/]",
            f"{ref.weight:.2f}" if ref else "-",
            getattr(cls, "description", "")[:70],
        )
    console.print(table)


@app.command()
def config_dump(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    section: str | None = typer.Argument(None, help="e.g. risk, execution, partners"),
) -> None:
    """Print the fully merged configuration as JSON."""
    settings = _settings_only(profile, config)
    payload = settings.config.model_dump(mode="json")
    if section:
        payload = payload.get(section, {})
    console.print_json(json.dumps(payload, default=str))


@app.command()
def serve(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    port: int | None = typer.Option(None),
) -> None:
    """Run the public dashboard - the submission's Application URL."""
    import uvicorn

    from oaa.app.server import create_app

    settings = _settings_only(profile, config)
    api = create_app(settings)
    uvicorn.run(
        api,
        host=settings.config.app.host,
        port=port or settings.config.app.port,
        log_level="info",
    )




# --------------------------------------------------------------------------- #
# Backtesting
# --------------------------------------------------------------------------- #
def _normalise_reason(reason: str) -> str:
    """Collapse a rejection reason to its shape by blanking the numbers.

    "IV rank 19% is below the 70% floor" and "IV rank 4% is below the 70%
    floor" are one finding, not two. Without this the breakdown is a list of
    every distinct percentage the run happened to observe.
    """
    import re

    return re.sub(r"[-+]?\d*\.?\d+", "N", reason or "").strip()


def _reason_table(rejections: list[object], limit: int) -> Table:
    """The reasons candidates were declined, most common first."""
    import collections

    counts = collections.Counter(
        (r.strategy, r.vetoed_by, _normalise_reason(r.reason)) for r in rejections
    )
    table = Table(
        title=f"why candidates were declined (top {limit} of {len(counts)} distinct)",
        show_lines=False,
    )
    table.add_column("n", justify="right", style="bold")
    table.add_column("strategy")
    table.add_column("gate")
    table.add_column("reason", overflow="fold")
    for (strategy, gate, reason), count in counts.most_common(limit):
        share = count / len(rejections)
        table.add_row(
            f"{count}", strategy, gate,
            f"{reason}  [dim]({share:.0%})[/dim]",
        )
    return table


def _trade_table(trades: list[object]) -> Table:
    """Every trade, with what opened it and what closed it.

    Column widths are pinned rather than left to Rich: an 80-column terminal
    wraps every cell onto four lines otherwise, which turns a 20-trade run into
    a page of unreadable fragments.
    """
    table = Table(title="trades", show_lines=False, box=None, pad_edge=False)
    table.add_column("id", width=6, no_wrap=True)
    table.add_column("symbol", width=6, no_wrap=True)
    table.add_column("strategy", width=9, no_wrap=True)
    table.add_column("opened", width=11, no_wrap=True)
    table.add_column("held", width=5, justify="right", no_wrap=True)
    table.add_column("net", width=9, justify="right", no_wrap=True)
    table.add_column("RoR", width=7, justify="right", no_wrap=True)
    table.add_column("exit", width=22, no_wrap=True)
    for t in trades:
        net = t.net_pnl
        table.add_row(
            t.trade_id, t.symbol, t.strategy[:9],
            str(t.opened_at)[5:16].replace("T", " "),
            f"{t.held_days:.1f}d",
            f"[green]{net:+,.2f}[/green]" if net >= 0 else f"[red]{net:+,.2f}[/red]",
            f"{t.return_on_risk:.1%}" if t.return_on_risk else "-",
            (getattr(t, "exit_reason", "") or "")[:22],
        )
    return table


@app.command()
def backtest(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    symbols: str | None = typer.Option(None, "--symbols", "-s", help="Comma separated; defaults to the configured universe"),
    start: str | None = typer.Option(None, help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD"),
    strategies: str | None = typer.Option(None, help="Comma separated; defaults to every enabled strategy"),
    cash: float | None = typer.Option(None, help="Initial capital"),
    slippage: float | None = typer.Option(None, help="0.0 fills at mid, 1.0 pays the full quoted side"),
    source: str = typer.Option("alpaca", help="alpaca | synthetic (synthetic is a wiring test, not a backtest)"),
    news: bool = typer.Option(True, help="Fetch Alpaca headlines for the catalyst read"),
    offline: bool = typer.Option(False, help="Use only bars already cached on disk"),
    critic: str | None = typer.Option(
        None, "--critic",
        help="off | heuristic | llm. Default heuristic: the real Critic class "
             "with the null-LLM fallback, deterministic and free. 'llm' calls "
             "the model - inspect reasoning with it, do not quote P&L from it.",
    ),
    critic_model: str | None = typer.Option(
        None, "--critic-model",
        help="Override backtest.critic.llm.model for this run, e.g. "
             "gemini-2.5-flash-lite. Model IDs move; the config default is a "
             "safe one, not necessarily the cheapest.",
    ),
    label: str = typer.Option("", help="Name for this run"),
    save: bool = typer.Option(True, help="Write the run to runs/backtests/"),
    why: int = typer.Option(
        12, "--why", "-w",
        help="How many distinct rejection REASONS to print. The gate funnel "
             "names which gate declined a candidate; this names why it did. "
             "0 hides the breakdown.",
    ),
    trades: bool = typer.Option(
        False, "--trades", "-t",
        help="Print every closed trade with the gates that let it through.",
    ),
) -> None:
    """Replay the strategies over Alpaca history with a modelled option chain.

    The underlying prices and the headlines are real. The chain is modelled -
    Alpaca's free tier serves no historical chain - so this proves the logic
    fires when intended and sizes correctly. It is not evidence of edge.
    """
    import datetime as dtm

    from oaa.app.identity import print_banner, resolve
    from oaa.backtest.runner import BacktestRequest, run_backtest, save_run

    settings = _settings_only(profile, config)
    print_banner(resolve(settings, "Backtest"))
    cfg = settings.config
    if critic_model:
        if cfg.backtest.critic.llm is None:
            console.print(
                "[red]backtest.critic.llm is null (the replay shares the live "
                "provider), so --critic-model has nothing to override.[/red]"
            )
            raise typer.Exit(1)
        cfg.backtest.critic.llm.model = critic_model

    universe = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if symbols else cfg.universe.active()
    )
    request = BacktestRequest(
        symbols=universe,
        start=dtm.date.fromisoformat(start or cfg.backtest.start),
        end=dtm.date.fromisoformat(end or cfg.backtest.end),
        strategies=[s.strip() for s in (strategies or "").split(",") if s.strip()],
        initial_cash=cash,
        slippage_spread_fraction=slippage,
        source=source,
        use_news=news,
        offline=offline,
        critic_mode=critic,
        label=label,
    )
    from oaa.core.errors import DataError

    try:
        result = run_backtest(settings, request)
    except DataError as exc:
        console.print(Panel(
            f"[red]{exc}[/red]\n\n"
            "The replay needs Alpaca historical bars. Check that the keys for "
            f"profile [bold]{settings.config.profile}[/bold] are valid and that "
            "this machine can reach data.alpaca.markets. Once a window has been "
            "fetched it is cached under "
            f"[bold]{settings.config.backtest.cache_dir}[/bold] and "
            "[bold]--offline[/bold] will replay it with no network at all.",
            title="no historical data", expand=False,
        ))
        raise typer.Exit(1) from None
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    metrics = result.metrics()

    table = Table(title=f"Backtest {request.start} -> {request.end}", show_header=False)
    table.add_column("metric", style="bold")
    table.add_column("value", justify="right")
    for key in (
        "sessions", "trades", "closed_trades", "net_pnl", "total_return", "sharpe",
        "max_drawdown", "volatility_annual", "win_rate", "worst_trade", "best_trade",
        "profit_factor", "avg_hold_days", "gross_pnl", "total_modelled_cost",
        "ideas_generated", "ideas_approved", "rejections",
    ):
        table.add_row(key.replace("_", " "), str(metrics.get(key)))
    console.print(table)

    risk = result.provenance.get("risk") or {}
    if risk.get("profile") != "judged":
        console.print(Panel(
            f"These results use the [bold]{risk.get('profile')}[/bold] risk "
            f"limits: {risk.get('max_risk_per_trade_pct', 0):.2%} per trade, "
            f"{risk.get('max_new_positions_per_day')} new positions/day.\n"
            "[yellow]The judged account runs different limits.[/yellow] Add "
            "[bold]--profile judged[/bold] to calibrate against the "
            "configuration that will actually trade.",
            title="risk profile", expand=False,
        ))

    critic_stats = result.provenance.get("critic") or {}
    if critic_stats.get("mode") != "off":
        line = (
            f"mode {critic_stats.get('mode')}  scored {critic_stats.get('scored')}  "
            f"declined {critic_stats.get('declined')}  "
            f"llm calls {critic_stats.get('llm_calls')}  "
            f"cache hits {critic_stats.get('cache_hits')}"
        )
        provider = critic_stats.get("provider_config") or {}
        if critic_stats.get("mode") == "llm":
            line += f"\nprovider {provider.get('provider')} / {provider.get('model')}"
        if critic_stats.get("degraded_to_heuristic"):
            line += (
                f"\n[yellow]{critic_stats['degraded_to_heuristic']} call(s) fell back "
                "to the heuristic[/yellow]"
            )
        if critic_stats.get("lookahead_warning"):
            line += f"\n[red]{critic_stats['lookahead_warning']}[/red]"
        console.print(Panel(line, title="critic", expand=False))

    funnel = result.rejection_funnel()
    if funnel:
        console.print(Panel(
            "\n".join(f"{gate:<16} {count:>5}" for gate, count in funnel.items()),
            title="candidates declined, by gate", expand=False,
        ))

    if why and result.rejections:
        console.print(_reason_table(result.rejections, why))

    if trades and result.trades:
        console.print(_trade_table(result.trades))

    if not result.trades:
        console.print(Panel(
            "No trade was approved in this window. The gate funnel above names "
            "which gate declined each candidate and [bold]--why[/bold] names "
            "the reason it gave. A gate holding ~100% of candidates is usually "
            "a threshold set past what the data ever reaches, or a gate that "
            "cannot be evaluated at all - not a market with no opportunities.",
            title="zero trades", expand=False,
        ))

    if save:
        directory = save_run(settings, request, result)
        console.print(f"[dim]run saved to {directory}[/dim]")


@app.command()
def dashboard(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    port: int = typer.Option(8501, help="Port Streamlit listens on"),
    headless: bool = typer.Option(True, help="Do not open a browser automatically"),
) -> None:
    """The Streamlit operator dashboard: backtesting and live trading."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from oaa.app.identity import print_banner, resolve

    settings = _settings_only(profile, config)
    print_banner(resolve(settings, "Dashboard launch"))

    script = Path(__file__).resolve().parent / "app" / "dashboard.py"
    env = dict(os.environ)
    if profile:
        env["OAA_PROFILE"] = profile
    if config:
        env["OAA_CONFIG"] = config

    command = [
        sys.executable, "-m", "streamlit", "run", str(script),
        "--server.port", str(port),
        "--server.headless", "true" if headless else "false",
        "--browser.gatherUsageStats", "false",
    ]
    console.print(f"[bold]dashboard[/bold] -> http://localhost:{port}")
    try:
        subprocess.run(command, env=env, check=False)
    except FileNotFoundError:
        console.print(
            "[red]streamlit is not installed.[/red] "
            "Install the dashboard extra:  pip install -e '.[dashboard]'"
        )
        raise typer.Exit(1) from None


# --------------------------------------------------------------------------- #
# The two-book firewall
# --------------------------------------------------------------------------- #
@app.command()
def firewall(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    at: str | None = typer.Option(None, help="Simulate a time, e.g. '15:15' or '2026-09-04T15:15'"),
) -> None:
    """Show the capital firewall state: phase, reservations, lease, ledger."""
    import datetime as dtm

    from oaa.firewall.lock import Book, TemporalFirewall

    settings = _settings_only(profile, config)
    fw = TemporalFirewall(settings.config)

    if at:
        moment = (
            dtm.datetime.fromisoformat(at)
            if "T" in at or "-" in at
            else dtm.datetime.combine(dtm.date.today(), dtm.time.fromisoformat(at))
        )
        fw.clock.freeze(moment)

    status = fw.status()
    console.print(Panel.fit(
        f"[bold]{status['now_et']}[/]\n"
        f"phase                [bold cyan]{status['phase']}[/]\n"
        f"carry reserved       ${status['carry_reserved']:,.2f}\n"
        f"transient lease      {status['transient_owner'] or '[dim]free[/]'} "
        f"(${status['transient_budget']:,.2f})\n"
        f"transient locked to  {status['transient_disabled_until'] or '[dim]-[/]'}\n"
        f"ledger               {status['ledger']}",
        title="capital firewall",
    ))

    table = Table("Book", "May open?", "Why")
    for book in Book:
        allowed, why = fw.may_open(book)
        table.add_row(book.value, "[green]yes[/]" if allowed else "[red]no[/]", why)
    console.print(table)

    times = Table("Boundary", "ET", "What happens")
    what = {
        "market_open": "bell",
        "intraday_start": "intraday book may lease the transient headroom",
        "carry_entry_start": "carry book may open resident structures",
        "intraday_last_entry": "no new intraday entries - runway before the cutoff",
        "carry_entry_end": "no new carry entries",
        "intraday_cutoff": "HARD CUTOFF: cancel, liquidate TRANSIENT ONLY, confirm flat",
        "carry_verification": "THE SIGN-OFF: zero transient exposure, fresh Reg T, carry covered",
        "market_close": "bell - the carry book is HELD, not flattened",
    }
    for boundary, name in fw.clock.times.ordered():
        times.add_row(name, boundary.strftime("%H:%M"), what.get(name, ""))
    console.print(times)


@app.command()
def agent(
    cycle: str = typer.Argument("carry_scan", help="carry_scan | intraday_scan | intraday_cutoff | carry_verify"),
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Run one AI-assistant-driven cycle. The agent talks to Alpaca via MCP."""
    from oaa.agents.orchestrator import Orchestrator
    from oaa.agents.trading_agent import TradingAgent

    settings, broker, data = _boot(profile, config)
    orch = Orchestrator(settings, broker, data)
    try:
        assistant = TradingAgent(orch)
        if not assistant.available:
            console.print(
                "[yellow]No LLM configured — running the deterministic cycle instead.\n"
                "Set ANTHROPIC_API_KEY in .env for the agent path.[/]"
            )
        run = assistant.run_cycle(cycle)
        console.print(Panel.fit(run.summary(), title=f"agent: {cycle}"))
        if run.tool_calls:
            table = Table("Tool", "Mutating", "OK")
            for call in run.tool_calls:
                table.add_row(
                    call["tool"],
                    "[red]yes[/]" if call["mutating"] else "no",
                    "[red]no[/]" if call["error"] else "[green]yes[/]",
                )
            console.print(table)
        if run.narrative:
            console.print(Panel(run.narrative, title="what the agent decided"))
    finally:
        orch.close()


# --------------------------------------------------------------------------- #
# Discovery and the macro lens
# --------------------------------------------------------------------------- #
@app.command()
def discover(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    no_filters: bool = typer.Option(False, "--no-filters", help="Skip tradability checks"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Rules-only macro read (free)"),
    top: int = typer.Option(15, help="Rows to show"),
) -> None:
    """What the market is paying attention to, and tonight's regime read."""
    from oaa.agents.llm import get_llm
    from oaa.discovery.engine import DiscoveryEngine

    settings = _settings_only(profile, config)
    cfg = settings.config
    if no_llm:
        cfg.discovery.macro.use_llm = False

    journal = _journal(settings)
    llm = None if no_llm else get_llm(cfg.agents.llm)
    engine = DiscoveryEngine(settings, llm=llm, journal=journal)
    if not engine.enabled:
        console.print("[yellow]discovery is disabled in config[/]")
        raise typer.Exit(1)

    strategies = [s.name for s in cfg.enabled_strategies()]

    with console.status("querying Alpaca..."):
        result = engine.run(strategies=strategies, apply_filters=not no_filters)

    snap = result.snapshot
    if snap.source_errors:
        for name, error in snap.source_errors.items():
            console.print(f"[yellow]source '{name}' failed:[/] {error[:110]}")

    table = Table("Symbol", "Score", "Move", "News", "Sources", "Headline",
                  title=f"attention — {len(snap.symbols)} symbols")
    for entry in snap.top(top):
        move = (
            f"{'+' if entry.direction == 'up' else '-'}{entry.percent_change:.1f}%"
            if entry.percent_change is not None else "-"
        )
        table.add_row(
            entry.symbol,
            f"{entry.score:.3f}",
            move,
            f"x{entry.news_velocity:.1f}" if entry.news_velocity else "-",
            ",".join(sorted(entry.components)),
            (entry.headlines[0][:52] if entry.headlines else ""),
        )
    console.print(table)

    macro = result.macro
    console.print(Panel.fit(
        f"regime         [bold cyan]{macro.regime}[/]\n"
        f"vol            {macro.vol_expectation}\n"
        f"overnight risk {macro.overnight_risk:.2f}\n"
        f"collar         x{macro.collar_widening:.2f}\n"
        f"source         {macro.source}\n\n"
        f"{macro.rationale}",
        title="macro lens",
    ))

    if macro.guidance:
        guide = Table("Strategy", "Stance")
        for name, stance in sorted(macro.guidance.items()):
            colour = {"trade": "green", "reduce": "yellow", "stand_down": "red"}[stance]
            guide.add_row(name, f"[{colour}]{stance}[/]")
        console.print(guide)

    if macro.flagged_symbols:
        flags = Table("Flagged symbol", "Why its premium should not be sold")
        for symbol, reason in macro.flagged_symbols.items():
            flags.add_row(f"[red]{symbol}[/]", reason[:88])
        console.print(flags)
    if macro.shared_themes:
        console.print("[dim]Sector-wide moves deliberately NOT flagged:[/]")
        for theme in macro.shared_themes:
            console.print(f"  [dim]· {theme}[/]")

    if result.rejected:
        rej = Table("Rejected", "Reason")
        for verdict in result.rejected[:10]:
            rej.add_row(verdict.symbol, "; ".join(verdict.reasons)[:80])
        console.print(rej)

    console.print(
        f"\n[green]pool[/] {len(result.pool.entries)} symbols"
        + (f", {len(result.new_symbols)} new: {', '.join(result.new_symbols[:8])}"
           if result.new_symbols else "")
    )
    console.print("[dim]Next: `oaa scan --cycle carry_scan` to run the four premium gates.[/]")


@app.command()
def pool(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    limit: int = typer.Option(30),
) -> None:
    """The accumulated candidate pool feeding the cointegration screen."""
    from oaa.discovery.universe import CandidatePool

    settings = _settings_only(profile, config)
    cfg = settings.config.discovery.pool
    candidate_pool = CandidatePool.load(
        settings.path(cfg.path), cfg.accumulate_days, cfg.max_symbols, cfg.seeds
    )
    if not candidate_pool.entries:
        console.print(
            "[yellow]The pool is empty.[/] Run `oaa discover` first — it accumulates "
            "across days, so it gets more useful the longer it has been running."
        )
        return

    table = Table("Symbol", "Days seen", "Best", "Last seen", "Screened", "Approved",
                  title=f"candidate pool ({len(candidate_pool.entries)} symbols)")
    for row in candidate_pool.table(limit):
        table.add_row(
            row["symbol"], str(row["days_seen"]), f"{row['best_score']:.3f}",
            row["last_seen"],
            "[green]y[/]" if row["screened"] else "[dim]n[/]",
            "[green]y[/]" if row["approved"] else "[dim]n[/]",
        )
    console.print(table)
    console.print(
        f"[dim]Persistence beats intensity — a name seen on four days outranks one "
        f"that spiked once.[/]\n[dim]Screen order: {', '.join(candidate_pool.candidates(12))}[/]"
    )


@app.command()
def gates(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    book: str | None = typer.Option(None, help="carry | intraday | opportunistic"),
    limit: int = typer.Option(25),
) -> None:
    """The gate-by-gate rejection log.

    The highest-value artefact for judging: it shows the agent DECLINING trades
    and the exact measurement that stopped each one. Expect the spread gate to
    dominate on the intraday book - that is the finding, not a bug.
    """
    settings = _settings_only(profile, config)
    journal = _journal(settings)
    rows = journal.events("gate_rejection", limit * 4)
    if book:
        rows = [r for r in rows if r.get("book") == book]
    if not rows:
        console.print("[yellow]No rejections logged yet.[/] Run a scan cycle first.")
        return

    table = Table("When", "Book", "Symbol", "Vetoed by", "Reason",
                  title=f"gate rejections ({len(rows)})")
    for row in rows[:limit]:
        table.add_row(
            str(row.get("ts", ""))[11:19],
            str(row.get("book", "")),
            str(row.get("symbol", "")),
            f"[red]{row.get('vetoed_by', '')}[/]",
            str(row.get("reason", ""))[:90],
        )
    console.print(table)

    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("vetoed_by") or "-")
        counts[key] = counts.get(key, 0) + 1
    console.print(
        "[dim]by gate: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        + "[/]"
    )


if __name__ == "__main__":  # pragma: no cover
    app()
