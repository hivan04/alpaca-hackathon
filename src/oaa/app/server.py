"""Public read-only dashboard.

This is the submission's "Application URL": equity curve, open positions and
the decision log, live. Read-only by design - nobody should be able to make
this account trade from a browser.

Deploy: `oaa serve`, or the Dockerfile, behind any host that gives you a URL.
"""

from __future__ import annotations

from typing import Any

from oaa.config.loader import Settings
from oaa.core.logging import get_logger

log = get_logger("app")


def create_app(settings: Settings) -> Any:
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install the app extra: pip install -e '.[app]'") from exc

    from oaa.telemetry.journal import Journal
    from oaa.telemetry.metrics import compute_metrics
    from oaa.telemetry.report import render_html

    cfg = settings.config
    t = cfg.telemetry
    journal = Journal(settings.path(t.journal), settings.path(t.db), settings.path(t.equity_curve))

    api = FastAPI(title=cfg.app.title, docs_url="/api/docs", redoc_url=None)

    def _metrics():
        return compute_metrics(
            journal.equity_series(), journal.fills(2000), journal.decisions(2000)
        )

    @api.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "profile": cfg.profile, "project": cfg.meta.project}

    @api.get("/api/metrics")
    def metrics() -> Any:
        return JSONResponse(_metrics().as_dict())

    @api.get("/api/equity")
    def equity() -> Any:
        return JSONResponse(journal.equity_series())

    @api.get("/api/decisions")
    def decisions(limit: int = 100, action: str | None = None) -> Any:
        return JSONResponse(journal.decisions(limit, action))

    @api.get("/api/fills")
    def fills(limit: int = 100) -> Any:
        return JSONResponse(journal.fills(limit))

    @api.get("/api/status")
    def status() -> Any:
        return JSONResponse({
            "project": cfg.meta.project,
            "profile": cfg.profile,
            "strategies": [s.name for s in cfg.enabled_strategies()],
            "partners": [
                {"name": a.name, "stage": a.stage}
                for a in cfg.partners.adapters if a.enabled
            ],
            "counts": journal.counts(),
        })

    @api.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        report = _metrics()
        rows = journal.equity_series()
        page = render_html(
            report,
            rows,
            title=cfg.app.title,
            subtitle=(
                f"profile: {cfg.profile} | strategies: "
                f"{', '.join(s.name for s in cfg.enabled_strategies()) or 'none'}"
            ),
        )
        recent = journal.decisions(25)
        if recent:
            body = "".join(
                "<tr>"
                f"<td>{str(r['ts'])[5:19].replace('T', ' ')}</td>"
                f"<td>{r.get('symbol') or ''}</td>"
                f"<td>{r.get('strategy') or ''}</td>"
                f"<td>{r.get('action') or ''}</td>"
                f"<td>{'yes' if r.get('approved') else ('no' if r.get('approved') == 0 else '-')}</td>"
                f"<td>{(r.get('reason') or r.get('thesis') or '')[:110]}</td>"
                "</tr>"
                for r in recent
            )
            table = (
                '<div class="section"><h2>Decision log</h2><table><thead><tr>'
                "<th>Time</th><th>Symbol</th><th>Strategy</th><th>Action</th>"
                "<th>Approved</th><th>Reasoning</th></tr></thead>"
                f"<tbody>{body}</tbody></table></div>"
            )
            page = page.replace("</body>", table + "</body>")
        refresh = (
            f'<meta http-equiv="refresh" content="{cfg.app.refresh_seconds}">'
            if cfg.app.refresh_seconds else ""
        )
        return page.replace("</head>", refresh + "</head>")

    log.info("dashboard ready on %s:%s", cfg.app.host, cfg.app.port)
    return api
