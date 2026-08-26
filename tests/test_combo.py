"""Combo execution: ordering, rollback, and the property they protect."""

from __future__ import annotations

from oaa.brokers.sim import SimBroker
from oaa.core.errors import BrokerError
from oaa.core.types import AssetKind, Intent, Leg, Side, StructureType, TradeIdea
from oaa.execution.combo import ComboExecutor, StepStatus, plan_from_idea


def condor_idea() -> TradeIdea:
    """A four-leg carry structure, legged rather than routed as one combo.

    Legging is the fallback for venues that cannot submit a combo atomically.
    It doubles spread exposure, so it is never the default - but when it is
    used, the ORDERING is the safety property.
    """
    return TradeIdea(
        symbol="AAA",
        strategy="vol_carry",
        structure=StructureType.IRON_CONDOR,
        book="carry",
        legs=[
            Leg(symbol="AAA260911P00090000", side=Side.BUY, kind=AssetKind.OPTION,
                intent=Intent.BUY_TO_OPEN, limit_price=0.40),
            Leg(symbol="AAA260911P00095000", side=Side.SELL, kind=AssetKind.OPTION,
                intent=Intent.SELL_TO_OPEN, limit_price=0.95),
            Leg(symbol="AAA260911C00105000", side=Side.SELL, kind=AssetKind.OPTION,
                intent=Intent.SELL_TO_OPEN, limit_price=0.90),
            Leg(symbol="AAA260911C00110000", side=Side.BUY, kind=AssetKind.OPTION,
                intent=Intent.BUY_TO_OPEN, limit_price=0.35),
        ],
        quantity=1,
        net_price=-1.10,
        max_loss=390.0,
        max_profit=110.0,
    )


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #
def test_long_legs_are_sequenced_before_short_legs():
    """The ordering IS the safety property.

    Longs first means a partial failure leaves a cheaper structure than
    intended. Shorts first means a partial failure can leave an UNCOVERED
    short, which is the one state this system must never reach.
    """
    plan = plan_from_idea(condor_idea())
    sides = [step.legs[0].side for step in plan.ordered()]
    assert sides == [Side.BUY, Side.BUY, Side.SELL, Side.SELL]


def test_every_step_is_critical_by_default():
    assert all(step.critical for step in plan_from_idea(condor_idea()).ordered())


def test_step_labels_name_the_side_and_the_contract():
    labels = [s.label for s in plan_from_idea(condor_idea()).ordered()]
    assert labels[0].startswith("long-")
    assert labels[-1].startswith("short-")


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def test_dry_run_sends_nothing(cfg):
    cfg.execution.dry_run = True
    broker = SimBroker(cfg)
    before = broker.cash
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(condor_idea()))
    assert result.dry_run
    assert broker.cash == before
    assert all(s.status is StepStatus.SKIPPED for s in result.plan.ordered())


def test_all_steps_fill_on_a_healthy_broker(cfg):
    cfg.execution.dry_run = False
    broker = SimBroker(cfg, starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(condor_idea()), risk_stamp="ok")
    assert result.ok
    assert len(result.filled_steps) == 4
    assert result.failed_step is None
    assert {p.symbol for p in broker.account().positions} == {
        "AAA260911P00090000", "AAA260911P00095000",
        "AAA260911C00105000", "AAA260911C00110000",
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

    Both long wings and the short put fill; the short call is rejected. Without
    a rollback the account holds two long wings and a naked short put. With
    one, it holds nothing.
    """
    cfg.execution.dry_run = False
    broker = FailAt(cfg, "AAA260911C00105000", starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(condor_idea()), risk_stamp="ok")

    assert not result.ok
    assert result.failed_step is not None
    assert result.failed_step.label == "short-AAA260911C00105000"
    assert len(result.unwound) == 3
    assert result.clean_rollback
    assert broker.account().positions == []      # genuinely flat


def test_a_failed_long_wing_stops_before_any_short_is_sold(cfg):
    cfg.execution.dry_run = False
    broker = FailAt(cfg, "AAA260911C00110000", starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(condor_idea()), risk_stamp="ok")

    assert not result.ok
    # Only the first long wing had filled, and it was unwound. No short
    # exposure was ever created.
    assert broker.account().positions == []
    assert not any(
        s.legs[0].side is Side.SELL and s.status is StepStatus.FILLED
        for s in result.plan.ordered()
    )


def test_first_step_failing_leaves_nothing_to_unwind(cfg):
    cfg.execution.dry_run = False
    broker = FailAt(cfg, "AAA260911P00090000", starting_cash=200_000)
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(condor_idea()), risk_stamp="ok")
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
    result = ComboExecutor(cfg, broker).execute(plan_from_idea(condor_idea()), risk_stamp="ok")

    assert not result.ok
    assert not result.clean_rollback
    assert result.unwind_errors
    assert "UNWIND ERRORS" in result.summary()


def test_journal_records_the_combo_outcome(cfg, tmp_path):
    from oaa.telemetry.journal import Journal

    cfg.execution.dry_run = False
    journal = Journal(tmp_path / "j.jsonl", tmp_path / "j.db", tmp_path / "e.csv")
    broker = FailAt(cfg, "AAA260911C00105000", starting_cash=200_000)
    ComboExecutor(cfg, broker, journal=journal).execute(plan_from_idea(condor_idea()))

    text = (tmp_path / "j.jsonl").read_text()
    assert '"kind": "combo"' in text
    assert '"outcome": "aborted"' in text
