"""`oaa` - the command line surface.

    oaa doctor            check every dependency and credential before you need them
    oaa account           account, options level, positions
    oaa chain SPY         inspect a filtered option chain
    oaa scan              one dry scan: what would the agent do right now?
    oaa trade             one live cycle
    oaa run               the autonomous loop (this is the deliverable)
    oaa firewall          the two-book capital lock: phase, owner, budget
    oaa discover          what the market is watching + tonight's regime read
    oaa pool              the accumulated candidate pool
    oaa pairs             the approved cointegrated universe
    oaa signal KO/PEP     tonight's Kalman state and gap forecast
    oaa backtest-overnight   walk-forward backtest, bars via the Alpaca CLI
    oaa agent <cycle>     one AI-assistant-driven cycle over MCP
    oaa manage            close positions that hit their exit rules
    oaa flatten           close everything
    oaa report            performance report -> JSON + HTML
    oaa partners          list technology-partner adapters and their stages
    oaa mcp-tools         list the tools the Alpaca MCP server exposes
    oaa serve             the public dashboard
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
                             ("mcp", False), ("anthropic", False), ("fastapi", False)]:
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
    live: bool = typer.Option(False, "--live", help="Actually place orders (overrides dry_run)"),
) -> None:
    """One scan cycle. Dry by default: shows what the agent would do."""
    from oaa.agents.orchestrator import Orchestrator

    settings, broker, data = _boot(profile, config)
    if not live:
        settings.config.execution.dry_run = True

    orch = Orchestrator(settings, broker, data)
    try:
        result = orch.run_cycle("scan_and_trade", "cli-scan")
        console.print(Panel.fit(result.summary(), title="scan"))
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
# The two-book firewall
# --------------------------------------------------------------------------- #
@app.command()
def firewall(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    at: str | None = typer.Option(None, help="Simulate a time, e.g. '15:54' or '2026-09-04T15:54'"),
) -> None:
    """Show the temporal firewall state: phase, lock owner, verified budget."""
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
        f"phase        [bold cyan]{status['phase']}[/]\n"
        f"lock owner   {status['lock_owner'] or '[dim]free[/]'}\n"
        f"budget       ${status['budget']:,.2f}\n"
        f"intraday locked for  {status['intraday_locked_for'] or '[dim]-[/]'}",
        title="temporal firewall",
    ))

    table = Table("Book", "May open?", "Why")
    for book in (Book.INTRADAY, Book.OVERNIGHT):
        allowed, why = fw.may_open(book)
        table.add_row(
            book.value,
            "[green]yes[/]" if allowed else "[red]no[/]",
            why,
        )
    console.print(table)

    times = Table("Boundary", "ET", "What happens")
    what = {
        "market_open": "bell",
        "overnight_exit": "liquidate the overnight book, release the lock",
        "intraday_start": "intraday book may acquire the lock",
        "intraday_last_entry": "no new intraday entries",
        "intraday_cutoff": "HARD CUTOFF: cancel, liquidate, confirm flat",
        "overnight_signal": "Kalman + ML compute; nothing routed",
        "overnight_verify": "THE GATE: prove flat, read fresh Reg T, take the lock",
        "overnight_entry": "dispatch the pairs combo",
        "market_close": "bell",
    }
    for boundary, name in fw.clock.times.ordered():
        times.add_row(name, boundary.strftime("%H:%M"), what.get(name, ""))
    console.print(times)


@app.command()
def pairs(profile: str | None = _PROFILE, config: str | None = _CONFIG) -> None:
    """The approved cointegrated pair universe for the overnight book."""
    settings = _settings_only(profile, config)
    ref = next(
        (s for s in settings.config.strategies if s.name == "overnight_pairs"), None
    )
    if ref is None:
        console.print("[yellow]overnight_pairs is not configured[/]")
        raise typer.Exit(1)

    meta = ref.params.get("pairs_meta", {})
    screened = meta.get("screened_at")
    console.print(
        f"screened: [bold]{screened or '[red]NEVER[/] - run `python scripts/find_pairs.py --write`'}[/]"
    )
    table = Table("Pair", "On", "Hedge", "p-value", "Half-life", "Notes")
    for raw in ref.params.get("pairs", []):
        hl = raw.get("half_life_days")
        table.add_row(
            f"{raw['left']}/{raw['right']}",
            "[green]yes[/]" if raw.get("enabled") else "[dim]no[/]",
            f"{raw.get('hedge_ratio', 1.0):.3f}",
            f"{raw['pvalue']:.4f}" if raw.get("pvalue") else "-",
            f"{hl:.1f}d" if hl else "-",
            (raw.get("notes") or "")[:46],
        )
    console.print(table)


@app.command()
def signal(
    pair: str = typer.Argument(..., help="Pair as 'LEFT/RIGHT', e.g. KO/PEP"),
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Tonight's Kalman state and gap forecast for one pair. Reads only."""
    from oaa.agents.orchestrator import Orchestrator
    from oaa.agents.tools import ToolBelt

    settings, broker, data = _boot(profile, config)
    orch = Orchestrator(settings, broker, data)
    try:
        payload = ToolBelt(orch).call("compute_pair_signal", {"pair": pair})
        if "error" in payload:
            console.print(f"[red]{payload['error']}[/]")
            raise typer.Exit(1)

        kalman = payload["kalman"]
        forecast = payload.get("forecast") or {}
        console.print(Panel.fit(
            f"[bold]{payload['pair']}[/]\n"
            f"hedge ratio (beta)  {kalman['beta']:.4f}\n"
            f"spread z-score      {kalman['zscore']:+.3f}\n"
            f"observations        {kalman['observations']}\n"
            f"filter ready        {payload['filter_ready']}",
            title="Kalman state",
        ))
        if forecast:
            console.print(Panel.fit(
                f"direction     [bold]{forecast['direction']}[/]\n"
                f"E[r] (q50)    {forecast['expected']:+.4%}\n"
                f"q05 (bad)     {forecast['lower_q05']:+.4%}   -> protective put strike\n"
                f"q95 (good)    {forecast['upper_q95']:+.4%}   -> protective call strike\n"
                f"edge / risk   {forecast['edge_to_risk']:.3f}\n"
                f"confidence    {forecast['confidence']:.3f}\n"
                f"model         {forecast['model']} ({forecast['train_rows']} rows)",
                title="overnight gap forecast",
            ))
        gate = payload.get("gate")
        console.print(
            "[green]entry gates: PASS[/]" if not gate else f"[yellow]would skip: {gate}[/]"
        )
    finally:
        orch.close()


@app.command("backtest-overnight")
def backtest_overnight(
    pair: str | None = typer.Argument(None, help="Pair as 'LEFT/RIGHT'; omit for all enabled"),
    start: str | None = typer.Option(None, help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD"),
    equity: float = typer.Option(100_000.0, help="Starting equity"),
    out: str | None = typer.Option(None, help="Write per-night CSV here"),
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Walk-forward backtest of the overnight strategy. Bars come from the CLI."""
    import csv
    import datetime as dtm

    from oaa.backtest.overnight import OvernightBacktest
    from oaa.data.factory import get_data_provider

    settings = _settings_only(profile, config)
    provider = get_data_provider(settings.config, settings.credentials)
    ref = next((s for s in settings.config.strategies if s.name == "overnight_pairs"), None)
    if ref is None:
        console.print("[red]overnight_pairs is not configured[/]")
        raise typer.Exit(1)

    if pair:
        left, right = [p.strip().upper() for p in pair.split("/")]
        targets = [(left, right)]
    else:
        targets = [
            (p["left"], p["right"]) for p in ref.params.get("pairs", []) if p.get("enabled")
        ]
    if not targets:
        console.print("[yellow]no enabled pairs to backtest[/]")
        raise typer.Exit(1)

    begin = dtm.date.fromisoformat(start) if start else None
    finish = dtm.date.fromisoformat(end) if end else None
    engine = OvernightBacktest(settings, provider, ref.params)

    rows: list[dict] = []
    for left, right in targets:
        console.print(f"\n[bold]{left}/{right}[/] — fetching bars via {provider.name}...")
        try:
            result = engine.run(left, right, start=begin, end=finish, initial_equity=equity)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{left}/{right}: {exc}[/]")
            continue
        console.print(Panel.fit("\n".join(result.summary_lines()), title=f"{left}/{right}"))
        skips = result.metrics()["skip_reasons"]
        if skips:
            table = Table("Why a night was skipped", "Count")
            for reason, count in list(skips.items())[:8]:
                table.add_row(reason, str(count))
            console.print(table)
        rows.extend(n.as_row() for n in result.nights)

    if out and rows:
        path = settings.path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        console.print(f"\n[green]wrote[/] {path} ({len(rows)} nights)")

    console.print(
        "\n[dim]The options overlay is modelled, not replayed - no historical option\n"
        "chain with greeks exists on the free tier. Assumptions are deliberately\n"
        "pessimistic; see src/oaa/backtest/pricing.py.[/]"
    )


@app.command()
def agent(
    cycle: str = typer.Argument("overnight_signal", help="overnight_signal | overnight_entry | scan_and_trade | intraday_cutoff"),
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

    pairs = [
        (p["left"], p["right"])
        for ref in cfg.strategies if ref.name == "overnight_pairs"
        for p in ref.params.get("pairs", []) if p.get("enabled")
    ]
    strategies = [s.name for s in cfg.enabled_strategies()]

    with console.status("querying Alpaca..."):
        result = engine.run(
            strategies=strategies, pairs=pairs, apply_filters=not no_filters
        )

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
        flags = Table("Flagged leg", "Why it should not be held overnight")
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
    console.print("[dim]Next: python scripts/find_pairs.py --from-pool --write[/]")


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


if __name__ == "__main__":  # pragma: no cover
    app()
