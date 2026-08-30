"""`oaa` - the command line surface.

    oaa status            is the agent live, and what has it decided today?
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
    oaa events screen     which confirmed prints land this week
    oaa events watch      read the names whose prints are coming
    oaa events arm        open tonight's earnings spreads
    oaa events flatten    close everything that has now reported
"""

from __future__ import annotations

import json
from typing import Any

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
    setup_logging(cfg.telemetry.log_level, cfg.telemetry.log_format,
                  console=cfg.telemetry.console)
    broker = get_broker(cfg, settings.credentials, backend=backend)
    data = get_data_provider(cfg, settings.credentials)
    return settings, broker, data


def _settings_only(profile: str | None, config: str | None):
    from oaa.config.loader import load_settings
    from oaa.core.logging import setup_logging

    settings = load_settings(config_path=config, profile=profile)
    setup_logging(
        settings.config.telemetry.log_level,
        settings.config.telemetry.log_format,
        console=settings.config.telemetry.console,
    )
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
def status(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    as_json: bool = typer.Option(False, "--json", help="Machine-readable, for scripts."),
    watch: int = typer.Option(0, "--watch", "-w", metavar="SECONDS",
                              help="Re-render every N seconds until interrupted."),
) -> None:
    """Is the agent live, and what has it decided and screened today?

    The one command to run in a fresh terminal. Read-only: it reads the journal
    and the process table, and cannot place, size or cancel anything.
    """
    import time

    from oaa.app import status as status_mod

    settings = _settings_only(profile, config)
    resolved = settings.config.profile or "dev"
    journal_obj = _journal(settings)

    def once() -> None:
        snap = status_mod.collect(settings, journal_obj, resolved)
        if as_json:
            payload = dict(snap)
            age = payload.pop("journal_age", None)
            payload["journal_age_seconds"] = age.total_seconds() if age else None
            console.print_json(json.dumps(payload, default=str))
        else:
            _render_status(snap)

    if not watch:
        once()
        return
    try:
        while True:
            console.clear()
            once()
            console.print(f"[dim]refreshing every {watch}s - ctrl-c to stop[/]")
            time.sleep(watch)
    except KeyboardInterrupt:
        console.print("[dim]stopped[/]")


def _render_status(snap: dict) -> None:
    from oaa.app.status import human_age

    state = snap["state"]
    colour, headline = {
        "live": ("green", "LIVE"),
        "idle": ("green", "UP - MARKET CLOSED"),
        "stale": ("yellow", "UP BUT STALE"),
        "unknown": ("yellow", "NO PROCESS VISIBLE"),
        "offline": ("red", "NOT RUNNING"),
    }[state]
    age = human_age(snap["journal_age"])
    market = snap.get("session") or {}
    # Silence outside a session is the schedule working, so an idle process is
    # described by what it is waiting for rather than by how long it has been
    # quiet - "last entry 12h ago" reads as a fault on a Saturday.
    detail = f"last journal entry {age}"
    if state == "idle" and market.get("next_open"):
        detail = f"waiting for the {market['next_open']} ET open | quiet since {age}"
    clock_line = (
        f"\n[dim]{market['now_et']} - phase {market['phase']}[/]" if market else ""
    )
    ident = snap.get("identity") or {}
    account_line = ""
    if ident:
        account_line = (
            f"\n[dim]account {ident['key']} from {ident['key_source']}"
            + (f" | {ident['account_id']}" if ident.get("account_id") else "")
            + (" | paper" if ident.get("paper") else " | [red]LIVE MONEY[/]")
            + "[/]"
        )
    console.print(Panel(
        f"[{colour}][bold]{headline}[/bold][/]  profile [bold]{snap['profile']}[/bold]"
        f"  |  {detail}{clock_line}{account_line}",
        title="oaa status", border_style=colour,
    ))

    # Everything below belongs to the profile named above. If a process is
    # trading a DIFFERENT account, say so before the numbers, not after.
    for other in snap.get("other_profiles") or []:
        console.print(
            f"[yellow]Note:[/] an [bold]oaa-{other}[/bold] process is also running. "
            f"These numbers are the [bold]{snap['profile']}[/bold] account's - "
            f"run [bold]oaa status --profile {other}[/bold] for that one."
        )

    if snap["processes"]:
        table = Table("Process", "Account", "PID", "Status", "Uptime", "Restarts",
                      title="processes")
        for proc in snap["processes"]:
            account = proc.get("profile") or "-"
            if account == snap["profile"]:
                account = f"[bold]{account}[/]"
            table.add_row(
                str(proc["name"])[:40], account, str(proc.get("pid") or "-"),
                str(proc.get("status") or "-"), str(proc.get("uptime") or "-"),
                str(proc.get("restarts") if proc.get("restarts") is not None else "-"),
            )
        console.print(table)
    else:
        console.print(
            "[red]No agent process found.[/] Start it with "
            "[bold]pm2 start ecosystem.config.js --only oaa-judged[/] "
            "(or `oaa run --profile judged`)."
        )

    # -- the reasoning layer, and whether it actually ran -------------------- #
    agent = snap["agent"]
    if agent:
        if agent["degraded"]:
            console.print(Panel(
                f"[red]The reasoning layer did not run.[/] Last cycle "
                f"'{agent['cycle']}' fell back to deterministic rules.\n"
                f"[dim]{agent['error']}[/]",
                title="AI layer", border_style="red",
            ))
        else:
            console.print(
                f"[green]AI layer OK[/] - last cycle '{agent['cycle']}': "
                f"{agent['turns']} turn(s), {agent['tool_calls']} tool call(s) "
                f"({agent['mutating']} mutating)"
            )

    # -- what it saw in the market ------------------------------------------ #
    disc = snap["discovery"]
    if disc:
        console.print(
            f"\n[bold]screening[/] [dim]({disc['ts'][11:19] if disc.get('ts') else '?'} UTC)[/]  "
            f"scanned [bold]{disc['scanned']}[/] | tradable [bold]{len(disc['tradable'])}[/] | "
            f"new {len(disc['new_symbols'])} | pool {disc['pool'].get('symbols', 0)}"
        )
        if disc["top"]:
            console.print("[dim]highest attention: " + ", ".join(
                f"{sym} {score:.2f}" for sym, score in disc["top"] if sym
            ) + "[/]")
        if disc["reasons"]:
            table = Table("Rejected because", "Count", title="why candidates were dropped")
            for reason, count in disc["reasons"]:
                table.add_row(str(reason), str(count))
            console.print(table)
        for source, error in (disc["source_errors"] or {}).items():
            console.print(f"[yellow]source '{source}' failed:[/] {str(error).splitlines()[0][:120]}")

    macro = snap["macro"]
    if macro:
        stood = ", ".join(macro["stood_down"]) or "none"
        console.print(
            f"[bold]regime[/] {macro['regime']} / vol {macro['vol_expectation']} / "
            f"overnight risk {macro['overnight_risk']} | stood down: {stood}"
        )

    # -- money -------------------------------------------------------------- #
    report = snap["report"]
    if report:
        console.print(
            f"[bold]account[/] equity ${report.get('equity', 0):,.2f} | "
            f"day P&L {report.get('day_pl', 0):+,.2f} | "
            f"{report.get('positions', 0)} position(s)"
        )

    decisions = snap["decisions"]
    if decisions:
        table = Table("When", "Cycle", "Action", "Symbol", "OK", "Reason",
                      title="recent decisions")
        for row in decisions:
            approved = row.get("approved")
            table.add_row(
                str(row["ts"])[11:19], str(row.get("cycle") or ""),
                str(row.get("action") or ""), str(row.get("symbol") or ""),
                "-" if approved is None else ("[green]y[/]" if approved else "[red]n[/]"),
                str(row.get("reason") or "")[:60],
            )
        console.print(table)
    else:
        console.print("[dim]no trade decisions recorded yet[/]")

    today = snap["events_today"]
    if today:
        console.print("[dim]today: " + ", ".join(
            f"{k} x{v}" for k, v in sorted(today.items(), key=lambda kv: -kv[1])
        ) + "[/]")
    console.print(f"[dim]journal: {snap['journal_path']}[/]")


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
            # What the environment SAYS against what the broker actually opens.
            # A key can be well formed, resolved from exactly the right
            # variable, and still belong to the other account - and nothing
            # upstream of this line can see that.
            expected = (creds.expected_account_id or "").strip().upper()
            actual = (account.account_id or "").strip().upper()
            row("account identity", bool(expected) and expected == actual,
                f"keys open {actual or '?'}, profile expects {expected or '(none recorded)'}"
                + ("" if expected else " - set it in .env"))
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
    # A book that runs in its own process is not "disabled" - `oaa run` was
    # never going to load it. Rendering it as `no` alongside a book that is
    # switched off reads as the same state, and it is not.
    #
    # The events book left this set on 30 Aug: it is now a scheduled cycle
    # inside `oaa run` (events_flatten 09:45, events_arm 15:50), so `yes` is
    # the honest answer for it. `oaa events arm` still works and is still the
    # way to arm off-schedule or with --dry-run.
    own_process = {"weekend"}
    table = Table("Name", "Book", "In `oaa run`", "Weight", "Description")
    for name, cls in strategy_registry:
        ref = enabled.get(name)
        book = (getattr(ref, "book", None) or getattr(cls, "book", "")) or "-"
        if book in own_process:
            state = "[cyan]own process[/]"
        elif ref and ref.enabled:
            state = "[green]yes[/]"
        else:
            state = "[dim]no[/]"
        table.add_row(
            name, book, state,
            f"{ref.weight:.2f}" if ref else "-",
            getattr(cls, "description", "")[:70],
        )
    console.print(table)
    console.print(
        "[dim]own process = armed by its own command on its own schedule; "
        "`oaa run` cannot open a position for it.[/]"
    )
    console.print(
        "[dim]events = in `oaa run` (09:45 flatten, 15:50 arm) but NOT a "
        "firewall tenant - it holds overnight and leases no capital. "
        "`oaa events --help` for the manual verbs.[/]"
    )


@app.command()
def switchboard(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    on: str | None = typer.Option(None, "--on", help="Comma separated: switch these books ON"),
    off: str | None = typer.Option(None, "--off", help="Comma separated: switch these books OFF"),
) -> None:
    """Show or change which books are switched on for THIS account.

    The switch is per profile and lives in `<run_dir>/switchboard.json`. A
    running agent picks a change up at its next cycle - no restart. A book with
    no entry falls back to the config's own `enabled` flag.
    """
    from oaa.core.switchboard import Switchboard

    settings = _settings_only(profile, config)
    board = Switchboard.open(settings.config.telemetry.run_dir)
    changes: dict[str, bool] = {}
    for names, value in ((on, True), (off, False)):
        for name in (n.strip() for n in (names or "").split(",") if n.strip()):
            changes[name] = value
    if changes:
        board.update(changes, actor="cli")

    configured = {ref.name: ref.enabled for ref in settings.config.strategies}
    state = board.state()
    table = Table("Book", "Config", "Switch", "Trading now")
    for name in sorted(set(configured) | set(state)):
        default = configured.get(name, False)
        live = board.enabled(name, default)
        switch = "-" if name not in state else ("on" if state[name] else "off")
        table.add_row(
            name,
            "on" if default else "[dim]off[/]",
            switch if switch == "-" else (f"[green]{switch}[/]" if state[name] else f"[red]{switch}[/]"),
            "[green]yes[/]" if live else "[dim]no[/]",
        )
    console.print(f"[bold]{settings.config.profile}[/] - {board.path}")
    console.print(table)
    if board.updated.get("at"):
        console.print(f"[dim]last changed {board.updated['at']} by {board.updated.get('by')}[/]")


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
def runs(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    show: str | None = typer.Option(
        None, "--show", help="Run id (or a unique prefix) to open in full"
    ),
    limit: int = typer.Option(15, "--limit", "-n", help="How many runs to list"),
    trades: bool = typer.Option(False, "--trades", help="With --show, list every trade"),
) -> None:
    """Saved backtests: list them, or open one without re-running it.

    A replay costs minutes and its result is already on disk. Comparing today's
    settings against this morning's should not mean running this morning's
    again - and re-running it is not even a comparison, because the code has
    moved underneath it.

    Runs written with --no-save are not here. That is the usual reason the
    newest entry is older than the backtest you just watched finish.
    """
    settings = _settings_only(profile, config)
    root = settings.path(settings.config.backtest.output_dir)
    saved = sorted(
        (p for p in root.glob("*") if (p / "result.json").exists()),
        reverse=True,
    )
    if not saved:
        console.print(Panel(
            f"No saved runs under {root}.\n\n"
            "`make bt` passes --no-save, which writes nothing to disk. Run\n"
            "  oaa backtest --start YYYY-MM-DD --end YYYY-MM-DD\n"
            "without --no-save to keep one.",
            title="no runs", border_style="yellow",
        ))
        return

    if show:
        matches = [p for p in saved if p.name.startswith(show) or show in p.name]
        if not matches:
            console.print(f"[red]no run matching {show!r}[/red]")
            raise typer.Exit(1)
        if len(matches) > 1:
            console.print(f"[yellow]{show!r} matches {len(matches)} runs:[/yellow]")
            for m in matches[:10]:
                console.print(f"  {m.name}")
            raise typer.Exit(1)
        run = matches[0]
        result = json.loads((run / "result.json").read_text())
        metrics = result.get("metrics") or {}
        request = (result.get("provenance") or {}).get("request") or {}

        table = Table(box=None, pad_edge=False)
        table.add_column("", style="dim", width=20)
        table.add_column("", justify="right")
        for key in (
            "trades", "closed_trades", "net_pnl", "total_return", "sharpe",
            "max_drawdown", "win_rate", "profit_factor", "avg_hold_days",
            "total_modelled_cost", "ideas_generated", "ideas_approved",
        ):
            if key in metrics and metrics[key] is not None:
                value = metrics[key]
                table.add_row(
                    key.replace("_", " "),
                    f"{value:,.4f}" if isinstance(value, float) else str(value),
                )
        console.print(Panel(
            table,
            title=f"{run.name}  |  {request.get('start')} -> {request.get('end')}"
                  f"  |  source {request.get('source', '?')}",
        ))

        funnel = result.get("rejection_funnel") or {}
        if funnel:
            gates = Table(title="declined by gate", box=None, pad_edge=False)
            gates.add_column("gate", width=20)
            gates.add_column("n", justify="right", width=6)
            for gate, count in sorted(funnel.items(), key=lambda kv: -kv[1])[:10]:
                gates.add_row(gate, str(count))
            console.print(gates)

        if trades and result.get("trades"):
            from types import SimpleNamespace
            console.print(_trade_table(
                [SimpleNamespace(**t) for t in result["trades"]]
            ))
        console.print(f"\n[dim]chart:[/dim] python scripts/plot_trades.py "
                      f"--run {run}")
        return

    listing = Table(title=f"saved backtests in {root}", box=None, pad_edge=False)
    # Widths pinned to fit an 80-column terminal. Left to Rich, every row wraps
    # onto four lines and a listing becomes unreadable.
    listing.add_column("id", width=15, no_wrap=True)
    listing.add_column("window", width=13, no_wrap=True)
    listing.add_column("src", width=4, no_wrap=True)
    listing.add_column("n", justify="right", width=4)
    listing.add_column("net", justify="right", width=10)
    listing.add_column("win", justify="right", width=4)
    listing.add_column("symbols", width=17, no_wrap=True)
    for run in saved[:limit]:
        try:
            payload = json.loads((run / "result.json").read_text())
        except (OSError, ValueError):
            continue
        metrics = payload.get("metrics") or {}
        request = (payload.get("provenance") or {}).get("request") or {}
        net = float(metrics.get("net_pnl") or 0.0)
        symbols = request.get("symbols") or []
        listing.add_row(
            run.name.split("__")[0],
            f"{str(request.get('start', '?'))[5:]}>{str(request.get('end', '?'))[5:]}",
            "synt" if request.get("source") == "synthetic" else "real",
            str(metrics.get("trades", 0)),
            f"[green]{net:+,.2f}[/green]" if net >= 0 else f"[red]{net:+,.2f}[/red]",
            f"{float(metrics.get('win_rate') or 0):.0%}",
            ",".join(symbols)[:26],
        )
    console.print(listing)
    console.print("\n[dim]open one:[/dim] oaa runs --show <id> --trades")


def _strategy_universe(
    cfg: Any, picked: list[str], overrides: dict[str, Any] | None = None
) -> list[str]:
    """The universe a single named strategy asks for, or [] when it has none.

    Only applied to a ONE-strategy run: with several strategies selected there
    is no single right answer and the configured universe stays in charge.
    """
    if len(picked) != 1:
        return []
    from oaa.strategies.base import strategy_registry

    strategy_registry.autoload("oaa.strategies")
    try:
        cls = strategy_registry.get(picked[0])
    except Exception:  # noqa: BLE001 - run_backtest reports an unknown name
        return []
    ref = next(
        (r for r in cfg.strategies if r.name == picked[0]),
        _SyntheticRef(picked[0], getattr(cls, "book", "intraday")),
    )
    if overrides:
        ref.params = {**(ref.params or {}), **overrides}
    try:
        return list(cls(ref, cfg).universe())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]could not read {picked[0]}'s universe: {exc}[/yellow]")
        return []


class _SyntheticRef:
    """A config entry for a strategy config does not list. Empty params, so the
    strategy falls back to its own default params file."""

    def __init__(self, name: str, book: str) -> None:
        self.name = name
        self.enabled = True
        self.weight = 1.0
        self.book = book
        self.params: dict[str, Any] = {}
        self.params_file = None


@app.command()
def backtest(
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
    symbols: str | None = typer.Option(None, "--symbols", "-s", help="Comma separated; defaults to the configured universe"),
    start: str | None = typer.Option(None, help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD"),
    strategies: str | None = typer.Option(None, help="Comma separated; defaults to every enabled strategy"),
    events_calendar: str | None = typer.Option(
        None, "--events-calendar",
        help="Point the events book at a different calendar file for this run "
             "- how you backtest names that are not in the live universe. "
             "Ships with config/events/earnings_calendar_2026-08-24.json "
             "(last week's ten prints, out of sample).",
    ),
    strategy: str | None = typer.Option(
        None, "--strategy", "-S",
        help="Backtest ONE strategy in isolation, by name. Works for a "
             "strategy config has switched off, or does not list at all - "
             "`earnings_event_directional` is not in `strategies:` because it "
             "runs in its own process. `oaa strategies` lists the names.",
    ),
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

    picked = [s.strip() for s in (strategies or "").split(",") if s.strip()]
    if strategy:
        if picked:
            console.print("[red]use --strategy or --strategies, not both.[/red]")
            raise typer.Exit(1)
        picked = [strategy.strip()]

    # `--symbols earnings-week` expands to whatever reports this week, read
    # from the confirmed earnings calendar rather than a second hardcoded list.
    from oaa.strategies.events.universe import ALIAS
    from oaa.strategies.events.universe import resolve as resolve_universe

    universe = resolve_universe(symbols)
    if symbols and symbols.strip().lower() in {ALIAS, "earnings", "earnings_week"}:
        console.print(
            f"[dim]{ALIAS}: {len(universe)} confirmed reporter(s) - "
            f"{', '.join(universe)}[/dim]"
        )
    # The replay skips any symbol outside a strategy's own universe, silently:
    # `if symbol not in strategy.universe(): continue`. So asking one strategy
    # for symbols it does not cover produced a clean run, zero ideas, zero
    # rejections and no reason - which is indistinguishable from a strategy
    # that is broken. Say it here, before the run, where it can be acted on.
    overrides: dict[str, Any] = {}
    if events_calendar:
        path = settings.path(events_calendar)
        if not path.exists():
            console.print(f"[red]no calendar file at {path}[/red]")
            raise typer.Exit(1)
        overrides["calendar_path"] = str(path)

    own = set(_strategy_universe(cfg, picked, overrides))
    if universe is None:
        # A single-strategy run uses that strategy's own universe when none is
        # given: replaying the events book against the configured SPY/QQQ
        # universe would measure nothing, since neither has an earnings date.
        universe = sorted(own) or cfg.universe.active()
    if not universe:
        console.print("[red]empty universe - nothing to replay.[/red]")
        raise typer.Exit(1)
    if own:
        outside = [s for s in universe if s not in own]
        if outside:
            console.print(
                f"[yellow]{picked[0]} does not cover {', '.join(outside)}[/yellow] - "
                "the replay skips symbols outside a strategy's own universe."
            )
        if not set(universe) & own:
            console.print(
                f"[red]None of the requested symbols are in {picked[0]}'s "
                f"universe, so the run would produce nothing.[/red]"
            )
            covered = ", ".join(sorted(own)[:12]) + ("…" if len(own) > 12 else "")
            console.print(f"[dim]it covers: {covered}[/dim]")
            if picked[0] == "earnings_event_directional":
                console.print(
                    "[dim]this book only trades names with a CONFIRMED row in "
                    "config/events/earnings_calendar.json. Use --symbols "
                    "earnings-week, or add the names you want to that file "
                    "with a report date, session and source.[/dim]"
                )
            raise typer.Exit(1)
    request = BacktestRequest(
        symbols=universe,
        start=dtm.date.fromisoformat(start or cfg.backtest.start),
        end=dtm.date.fromisoformat(end or cfg.backtest.end),
        strategies=picked,
        strategy_params=overrides,
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
        "mixed_surface_marks", "risk_bound_clamps", "fine_marks",
    ):
        table.add_row(key.replace("_", " "), str(metrics.get(key)))
    console.print(table)

    if metrics.get("risk_bound_clamps"):
        console.print(Panel(
            f"[red]{metrics['risk_bound_clamps']} mark(s) broke the structure's own "
            "arithmetic bound and were clamped to its defined risk.[/red]\n"
            "A defined-risk structure cannot lose more than max_loss before costs, "
            "so this is a PRICING fault, not a market outcome. Usually a stale "
            "print on one leg. The clamped P&L is the honest one; the count is a "
            "bug to chase.",
            title="risk bound violated", expand=False,
        ))
    if metrics.get("mixed_surface_marks"):
        console.print(Panel(
            f"{metrics['mixed_surface_marks']} structure mark(s) had legs "
            "disagreeing about provenance - some real, some modelled - and were "
            "re-priced onto a single surface anchored on the real prints.\n"
            "This is expected where the real option tape is thin. It is also why "
            "`real_mark_fraction` on those trades is lower than the raw coverage "
            "figure: mixing surfaces breaks the width bound that makes a condor "
            "defined-risk, so one surface is chosen over more real marks.",
            title="mixed-surface marks", expand=False,
        ))

    intraday_held = any(
        t.strategy == "intraday_momentum" for t in result.trades
    )
    if intraday_held and not metrics.get("fine_marks"):
        console.print(Panel(
            "[yellow]This run held intraday positions and took ZERO marks "
            "between scans.[/yellow]\n"
            "Every exit dial - target, stop, VWAP re-cross, time stop - was "
            "therefore sampled on the scan grid, which for a position that "
            "lives 20-90 minutes is 2-6 observations of its whole life. Trades "
            "will appear to overshoot their own settings, and MFE/MAE is built "
            "from too few samples to read. Check "
            "`backtest.mark_interval_minutes`.",
            title="management resolution", expand=False,
        ))

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


# --------------------------------------------------------------------------- #
# events book - one overnight hold across a scheduled earnings print.
#
# Since 30 Aug the book is ALSO driven by `oaa run` (events_flatten 09:45,
# events_arm 15:50) - a book that only trades when a human remembers to start
# a second process is not an autonomous submission. These verbs remain, and
# remain the only way to screen the week, arm off-schedule, or dry-run against
# the live chain. Both paths build the same engine with the same firewall
# bypass; see Orchestrator._events_engine.
# --------------------------------------------------------------------------- #
events_app = typer.Typer(
    no_args_is_help=True,
    help="The events book: earnings prints, armed on a date rather than a signal.",
)
app.add_typer(events_app, name="events")

_EVENTS_PARAMS = typer.Option(
    "config/strategies/earnings_event.yaml", "--params", help="Events params YAML"
)


def _events(profile: str | None, config: str | None, params_path: str, with_broker: bool = True):
    """Build the events engine and everything it is injected with."""
    from oaa.agents.llm import get_llm
    from oaa.execution.router import ExecutionRouter
    from oaa.risk.engine import RiskEngine
    from oaa.strategies.events import EventsEngine, load_params
    from oaa.strategies.events.strategy import EarningsEventDirectional

    settings, broker, data = _boot(profile, config)
    events_params = load_params(settings.path(params_path))
    ref = type("Ref", (), {"name": "earnings_event_directional", "params":
                           {"params_path": str(settings.path(params_path))},
                           "weight": 1.0, "book": "events", "enabled": True})()
    strategy = EarningsEventDirectional(ref, settings.config)
    llm = get_llm(settings.config.agents.llm)
    engine = EventsEngine(
        settings=settings,
        broker=broker,
        data=data,
        llm=llm,
        params=events_params,
        strategy=strategy,
        # firewall=None on purpose: this book runs in its own process and never
        # holds the intraday/carry capital lease.
        risk=RiskEngine(settings.config, firewall=None),
        router=ExecutionRouter(settings.config, broker),
        journal=_journal(settings),
    )
    return settings, engine, llm


@events_app.command("screen")
def events_screen(
    params: str = _EVENTS_PARAMS,
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Which confirmed prints land this week, and what the model proposed."""
    import datetime as dtm

    _, engine, llm = _events(profile, config, params)
    today = dtm.date.today()
    start, end = engine.week_window(today)
    result = engine.screen(today)

    console.print(Panel.fit(
        f"[bold]{start:%d %b} - {end:%d %b %Y}[/]\n"
        f"provider    {getattr(llm, 'provider', 'null')}\n"
        f"confirmed   {len(result.events)}\n"
        f"unverified  {len(result.unverified)}",
        title="earnings screen",
    ))
    if result.events:
        table = Table("Symbol", "Report", "Session", "Arms", "Exits", "Avg past move")
        for event in result.events:
            history = event.mean_abs_history
            table.add_row(
                event.symbol, f"{event.report_date:%a %d %b}",
                "after close" if event.timing == "amc" else "before open",
                str(event.entry_date), str(event.exit_date),
                f"{history:.2f}%" if history else "-",
            )
        console.print(table)
    if result.unverified:
        console.print(
            "[yellow]proposed by the model with no confirmed calendar row "
            "(not armed):[/] " + ", ".join(result.unverified)
        )


@events_app.command("watch")
def events_watch(
    date: str | None = typer.Option(None, "--date", help="Override today, YYYY-MM-DD"),
    show: str | None = typer.Option(
        None, "--show", help="Print one name's accumulated dossier and exit"
    ),
    params: str = _EVENTS_PARAMS,
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Read the names whose prints are coming, and retire the ones that reported.

    `oaa run` does this three times a session. Run it by hand to see what the
    book has been reading, or to force a poll after adding a calendar row.
    """
    import datetime as dtm

    _, engine, _ = _events(profile, config, params)
    asof = dtm.date.fromisoformat(date) if date else dtm.date.today()

    if show:
        dossier = engine.watcher.load(show.upper())
        lean, score = dossier.lean()
        console.print(Panel.fit(
            f"[bold]{dossier.symbol}[/] reports {dossier.report_date or '?'}\n"
            f"notes       {len(dossier.notes)}\n"
            f"items read  {len(dossier.seen)}\n"
            f"dossier lean {lean} ({score:+.2f})",
            title="watch dossier",
        ))
        if not dossier.notes:
            console.print("[dim]nothing logged yet for this name[/]")
            return
        table = Table("Date", "Salience", "Lean", "Items", "Summary")
        for note in dossier.notes:
            table.add_row(
                note.asof, f"{note.salience:.2f}", note.lean,
                f"{note.headlines}h/{note.messages}m", note.summary[:80],
            )
        console.print(table)
        return

    report = engine.watch(asof)
    console.print(Panel.fit(report.summary(), title="events watch"))
    if report.watching:
        table = Table("Symbol", "New items", "Outcome")
        for symbol in report.watching:
            new = report.new_items.get(symbol, 0)
            if symbol in report.noted:
                outcome = "[green]note written[/]"
            elif symbol in report.quiet:
                outcome = "[dim]quiet - nothing new[/]"
            elif new:
                outcome = "[yellow]read, judged immaterial[/]"
            else:
                outcome = "[red]not polled[/]"
            table.add_row(symbol, str(new), outcome)
        console.print(table)
    else:
        console.print(
            "[dim]no confirmed print falls inside the watch window - "
            "check the calendar and `watch.lookahead_days`[/]"
        )
    if report.retired:
        console.print(
            "[cyan]stopped watching (already reported):[/] " + ", ".join(report.retired)
        )
    for error in report.errors:
        console.print(f"[red]{error}[/]")


@events_app.command("arm")
def events_arm(
    dry_run: bool = typer.Option(True, "--dry-run/--live", help="Route orders?"),
    date: str | None = typer.Option(None, "--date", help="Override today, YYYY-MM-DD"),
    params: str = _EVENTS_PARAMS,
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Open a spread into each of tonight's prints. Dry run unless --live."""
    import datetime as dtm

    _, engine, _ = _events(profile, config, params)
    asof = dtm.date.fromisoformat(date) if date else dtm.date.today()
    report = engine.arm(asof=asof, dry_run=dry_run)

    console.print(Panel.fit(report.summary(), title="events arm"
                            + ("" if not dry_run else " (dry run)")))
    if report.opened:
        table = Table("Symbol", "Side", "Qty", "Debit", "Max loss", "Conf", "Implied", "Ratio")
        for idea in report.opened:
            meta = idea.meta
            table.add_row(
                idea.symbol,
                "call" if "bullish" in idea.tags else "put",
                str(idea.quantity), f"{idea.net_price:.2f}",
                f"${(idea.max_loss or 0) * idea.quantity:,.0f}",
                f"{idea.confidence:.2f}",
                f"{meta.get('implied_move_pct', 0):.2f}%",
                f"{meta.get('implied_realised_ratio') or 0:.2f}x",
            )
        console.print(table)
    for symbol, reason in sorted(report.declined.items()):
        console.print(f"[dim]{symbol}: {reason}[/]")
    for error in report.errors:
        console.print(f"[red]{error}[/]")


@events_app.command("flatten")
def events_flatten(
    date: str | None = typer.Option(None, "--date", help="Override today, YYYY-MM-DD"),
    params: str = _EVENTS_PARAMS,
    profile: str | None = _PROFILE,
    config: str | None = _CONFIG,
) -> None:
    """Close every events position whose print has now happened."""
    import datetime as dtm

    _, engine, _ = _events(profile, config, params)
    asof = dtm.date.fromisoformat(date) if date else dtm.date.today()
    closed = engine.flatten(asof=asof)
    console.print(f"closed {len(closed)} position(s): {', '.join(closed) or '-'}")


if __name__ == "__main__":  # pragma: no cover
    app()
