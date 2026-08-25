from __future__ import annotations

import datetime as dt

from oaa.core.types import AccountSnapshot, Decision, DecisionAction, Fill, RiskVerdict
from oaa.telemetry.journal import Journal
from oaa.telemetry.metrics import compute_metrics
from oaa.telemetry.report import render_html


def journal_at(tmp_path) -> Journal:
    return Journal(tmp_path / "j.jsonl", tmp_path / "j.sqlite", tmp_path / "e.csv")


def test_journal_records_decisions_and_reads_them_back(tmp_path):
    journal = journal_at(tmp_path)
    journal.record(Decision(
        cycle="test", action=DecisionAction.OPEN, symbol="SPY", strategy="s",
        verdict=RiskVerdict.approve(2), rationale="because",
        fill=Fill(order_id="o1", symbol="SPY", status="filled", filled_qty=2),
    ))
    rows = journal.decisions()
    assert len(rows) == 1 and rows[0]["symbol"] == "SPY"
    assert journal.counts()["fills"] == 1
    assert (tmp_path / "j.jsonl").read_text().strip()


def test_rejected_decisions_are_recorded_too(tmp_path):
    journal = journal_at(tmp_path)
    verdict = RiskVerdict.reject("too big")
    verdict.reasons.append("rule=sizing")
    journal.record(Decision(action=DecisionAction.SKIP, symbol="QQQ", verdict=verdict))
    metrics = compute_metrics([], [], journal.decisions())
    assert metrics.rejected == 1
    assert metrics.rejection_reasons == {"sizing": 1}


def test_equity_snapshots_build_a_curve(tmp_path):
    journal = journal_at(tmp_path)
    for i, equity in enumerate([100_000, 101_000, 99_500, 103_000]):
        journal.snapshot(AccountSnapshot(
            equity=equity, last_equity=100_000, cash=equity,
            asof=dt.datetime(2026, 9, 1, 10 + i, tzinfo=dt.timezone.utc),
        ))
    rows = journal.equity_series()
    assert len(rows) == 4
    metrics = compute_metrics(rows)
    assert metrics.start_equity == 100_000
    assert metrics.end_equity == 103_000
    assert metrics.absolute_pl == 3_000
    assert metrics.max_drawdown_pct < 0


def test_report_html_is_self_contained(tmp_path):
    journal = journal_at(tmp_path)
    journal.snapshot(AccountSnapshot(equity=100_000, last_equity=100_000))
    journal.snapshot(AccountSnapshot(equity=104_000, last_equity=100_000,
                                     asof=dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)))
    rows = journal.equity_series()
    html = render_html(compute_metrics(rows), rows)
    assert "<svg" in html
    assert "http://" not in html and "https://" not in html  # no external assets
