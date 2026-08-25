"""Combo execution: ordering, rollback, and the property they protect."""

from __future__ import annotations

from oaa.brokers.sim import SimBroker
from oaa.core.errors import BrokerError
from oaa.core.types import AssetKind, Intent, Leg, Side, StructureType, TradeIdea
from oaa.execution.combo import ComboExecutor, StepStatus, plan_from_idea


def pairs_idea() -> TradeIdea:
    return TradeIdea(
        symbol="AAA/BBB",
        strategy="overnight_pairs",
        structure=StructureType.PAIRS_COLLAR,
        book="overnight",
        legs=[
            Leg(symbol="AAA260902P00095000", side=Side.BUY, kind=AssetKind.OPTION,
                intent=Intent.BUY_TO_OPEN, qty=5, limit_price=0.40),
            Leg(symbol="BBB260902C00105000", side=Side.BUY, kind=AssetKind.OPTION,
                intent=Intent.BUY_TO_OPEN, qty=7, limit_price=0.35),
            Leg(symbol="AAA", side=Side.BUY, kind=AssetKind.EQUITY, qty=500, limit_price=100.0),
            Leg(symbol="BBB", side=Side.SELL, kind=AssetKind.EQUITY, qty=700, limit_price=71.0),
        ],
        quantity=1,
        net_price=0.45,
        max_loss=4_000.0,
        max_profit=900.0,
    )


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #
def test_protective_options_are_sequenced_before_the_equity_legs():
    """The ordering IS the safety property.

    Options first means a partial failure leaves cheap long options on the
    books. Equity first means a partial failure can leave an unhedged
    overnight short, which is the one state this system must never reach.
    """
    plan = plan_from_idea(pairs_idea())
    kinds = [step.kind for step in plan.ordered()]
    assert kinds == [
        AssetKind.OPTION, AssetKind.OPTION, AssetKind.EQUITY, AssetKind.EQUITY
    ]


def test_the_long_equity_leg_precedes_the_short():
    plan = plan_from_idea(pairs_idea())
    equity = [s for s in plan.ordered() if s.kind is AssetKind.EQUITY]
    assert equity[0].legs[0].side is Side.BUY
    assert equity[1].legs[0].side is Side.SELL


def test_every_step_is_critical_by_default():
    assert all(step.critical for step in plan_from_idea(pairs_idea()).ordered())


def test_equity_step_quantity_comes_from_share_count():
    plan = plan_from_idea(pairs_idea())
    equity = {s.legs[0].symbol: s.quantity for s in plan.ordered() if s.kind is AssetKind.EQUITY}
    assert equity == {"AAA": 500, "BBB": 700}


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def test_dry_run_sends_nothing(cfg):
    cfg.execution.dry_run = True
    broker = SimBroker(cfg)
    before = broker.cash
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(pairs_idea()))
    assert result.dry_run
    assert broker.cash == before
    assert all(s.status is StepStatus.SKIPPED for s in result.plan.ordered())


def test_all_steps_fill_on_a_healthy_broker(cfg):
    cfg.execution.dry_run = False
    broker = SimBroker(cfg, starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(pairs_idea()), risk_stamp="ok")
    assert result.ok
    assert len(result.filled_steps) == 4
    assert result.failed_step is None
    assert {p.symbol for p in broker.account().positions} == {
        "AAA260902P00095000", "BBB260902C00105000", "AAA", "BBB"
    }


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #
class FailAt(SimBroker):
    """Rejects the first order whose symbol matches, fills everything else."""

    def __init__(self, cfg, symbol: str, **kwargs):
        super().__init__(cfg, **kwargs)
        self.fail_symbol = symbol

    def submit(self, ticket):
        if ticket.legs and ticket.legs[0].symbol == self.fail_symbol:
            raise BrokerError(f"simulated rejection for {self.fail_symbol}")
        return super().submit(ticket)


def test_a_failed_short_leg_unwinds_everything_that_filled(cfg):
    """The scenario this whole design exists for.

    The put, the call and the long equity leg fill; the short leg is rejected
    (hard-to-borrow, say). Without a rollback the account holds a long stock
    position and two options it did not want. With one, it holds nothing.
    """
    cfg.execution.dry_run = False
    broker = FailAt(cfg, "BBB", starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(pairs_idea()), risk_stamp="ok")

    assert not result.ok
    assert result.failed_step is not None
    assert result.failed_step.label == "equity-BBB"
    assert len(result.unwound) == 3
    assert result.clean_rollback
    assert broker.account().positions == []      # genuinely flat


def test_a_failed_protective_option_stops_before_any_equity_is_bought(cfg):
    cfg.execution.dry_run = False
    broker = FailAt(cfg, "BBB260902C00105000", starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(pairs_idea()), risk_stamp="ok")

    assert not result.ok
    # Only the first put had filled, and it was unwound. No equity exposure
    # was ever created.
    assert broker.account().positions == []
    assert not any(s.kind is AssetKind.EQUITY and s.status is StepStatus.FILLED
                   for s in result.plan.ordered())


def test_first_step_failing_leaves_nothing_to_unwind(cfg):
    cfg.execution.dry_run = False
    broker = FailAt(cfg, "AAA260902P00095000", starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(pairs_idea()), risk_stamp="ok")
    assert not result.ok
    assert result.unwound == []
    assert broker.account().positions == []


def test_unwind_failure_is_surfaced_not_swallowed(cfg):
    """If the unwind itself fails, that must be loud - it needs a human."""

    class UnwindBreaks(SimBroker):
        """Fills the first three steps, then rejects everything - including the
        unwind attempts. The broker going down mid-combo is the real version
        of this."""

        submissions = 0

        def submit(self, ticket):
            UnwindBreaks.submissions += 1
            if UnwindBreaks.submissions > 3:
                raise BrokerError("broker unavailable")
            return super().submit(ticket)

    UnwindBreaks.submissions = 0

    cfg.execution.dry_run = False
    broker = UnwindBreaks(cfg, starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(pairs_idea()), risk_stamp="ok")

    assert not result.ok
    assert not result.clean_rollback
    assert result.unwind_errors
    assert "UNWIND ERRORS" in result.summary()


def test_journal_records_the_combo_outcome(cfg, tmp_path):
    from oaa.telemetry.journal import Journal

    cfg.execution.dry_run = False
    journal = Journal(tmp_path / "j.jsonl", tmp_path / "j.db", tmp_path / "e.csv")
    broker = FailAt(cfg, "BBB", starting_cash=200_000)
    ComboExecutor(cfg, broker, journal=journal).execute(plan_from_idea(pairs_idea()))

    text = (tmp_path / "j.jsonl").read_text()
    assert '"kind": "combo"' in text
    assert '"outcome": "aborted"' in text
