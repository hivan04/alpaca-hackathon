"""Performance metrics from the equity curve and fill log.

These numbers are the P&L Performance evidence: judges see the account, but
this is what turns raw account history into a chart and a claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oaa.data.indicators import max_drawdown, sharpe


@dataclass
class PerformanceReport:
    start_equity: float = 0.0
    end_equity: float = 0.0
    total_return_pct: float = 0.0
    absolute_pl: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float | None = None
    trading_days: int = 0
    snapshots: int = 0
    total_orders: int = 0
    filled_orders: int = 0
    fill_rate: float = 0.0
    decisions: int = 0
    approved: int = 0
    rejected: int = 0
    approval_rate: float = 0.0
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_equity": self.start_equity,
            "end_equity": self.end_equity,
            "total_return_pct": self.total_return_pct,
            "absolute_pl": self.absolute_pl,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe": self.sharpe,
            "trading_days": self.trading_days,
            "snapshots": self.snapshots,
            "total_orders": self.total_orders,
            "filled_orders": self.filled_orders,
            "fill_rate": self.fill_rate,
            "decisions": self.decisions,
            "approved": self.approved,
            "rejected": self.rejected,
            "approval_rate": self.approval_rate,
            "by_strategy": self.by_strategy,
            "rejection_reasons": self.rejection_reasons,
        }

    def summary_lines(self) -> list[str]:
        arrow = "+" if self.absolute_pl >= 0 else ""
        return [
            f"Equity      {self.start_equity:>12,.2f}  ->  {self.end_equity:,.2f}",
            f"P&L         {arrow}{self.absolute_pl:>12,.2f}  ({self.total_return_pct:+.2%})",
            f"Max DD      {self.max_drawdown_pct:>12.2%}",
            f"Sharpe      {('n/a' if self.sharpe is None else f'{self.sharpe:.2f}'):>12}",
            f"Orders      {self.filled_orders:>12} filled / {self.total_orders} sent"
            f"  ({self.fill_rate:.0%})",
            f"Decisions   {self.approved:>12} approved / {self.decisions}"
            f"  ({self.approval_rate:.0%})",
        ]


def compute_metrics(
    equity_rows: list[dict[str, Any]],
    fills: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
) -> PerformanceReport:
    report = PerformanceReport()
    fills = fills or []
    decisions = decisions or []

    equity = [float(r["equity"]) for r in equity_rows if r.get("equity") is not None]
    if equity:
        report.start_equity = round(equity[0], 2)
        report.end_equity = round(equity[-1], 2)
        report.absolute_pl = round(equity[-1] - equity[0], 2)
        if equity[0] > 0:
            report.total_return_pct = round((equity[-1] - equity[0]) / equity[0], 5)
        report.max_drawdown_pct = max_drawdown(equity)
        report.snapshots = len(equity)
        report.trading_days = len({str(r["ts"])[:10] for r in equity_rows})
        returns = [
            (equity[i] - equity[i - 1]) / equity[i - 1]
            for i in range(1, len(equity))
            if equity[i - 1] > 0
        ]
        # Snapshots are intraday, so annualise on the observed cadence rather
        # than assuming daily bars.
        per_day = max(1, report.snapshots // max(1, report.trading_days))
        report.sharpe = sharpe(returns, periods_per_year=252 * per_day)

    report.total_orders = len(fills)
    report.filled_orders = sum(1 for f in fills if str(f.get("status")) == "filled")
    if report.total_orders:
        report.fill_rate = round(report.filled_orders / report.total_orders, 4)

    report.decisions = len(decisions)
    report.approved = sum(1 for d in decisions if d.get("approved") == 1)
    report.rejected = sum(1 for d in decisions if d.get("approved") == 0)
    if report.decisions:
        report.approval_rate = round(report.approved / report.decisions, 4)

    for decision in decisions:
        name = decision.get("strategy") or "unknown"
        bucket = report.by_strategy.setdefault(
            name, {"decisions": 0, "approved": 0, "symbols": set()}
        )
        bucket["decisions"] += 1
        bucket["approved"] += 1 if decision.get("approved") == 1 else 0
        if decision.get("symbol"):
            bucket["symbols"].add(decision["symbol"])

        if decision.get("approved") == 0:
            reason = str(decision.get("reason") or "")
            rule = next(
                (part.split("=", 1)[1] for part in reason.split("; ") if part.startswith("rule=")),
                "other",
            )
            report.rejection_reasons[rule] = report.rejection_reasons.get(rule, 0) + 1

    for bucket in report.by_strategy.values():
        bucket["symbols"] = sorted(bucket["symbols"])

    return report
