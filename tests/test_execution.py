from __future__ import annotations

import pytest

from oaa.brokers.sim import SimBroker
from oaa.core.errors import RiskRejection
from oaa.core.types import Intent, Leg, RiskVerdict, Side, StructureType, TradeIdea
from oaa.execution.pricer import limit_price_for, mid_price, structure_bid_ask
from oaa.execution.router import ExecutionRouter
from tests.conftest import make_quote


def spread_idea() -> TradeIdea:
    long_leg = make_quote(strike=500, bid=9.90, ask=10.10)
    short_leg = make_quote(strike=510, bid=5.90, ask=6.10)
    return TradeIdea(
        symbol="SPY",
        strategy="test",
        structure=StructureType.VERTICAL_DEBIT,
        legs=[
            Leg(symbol=long_leg.symbol, side=Side.BUY, intent=Intent.BUY_TO_OPEN,
                quote=long_leg, limit_price=long_leg.mid),
            Leg(symbol=short_leg.symbol, side=Side.SELL, intent=Intent.SELL_TO_OPEN,
                quote=short_leg, limit_price=short_leg.mid),
        ],
        net_price=4.0,
        max_loss=400.0,
        max_profit=600.0,
    )


def test_structure_bid_ask_signs():
    best, worst = structure_bid_ask(spread_idea())
    # Buying the 500 and selling the 510: best case pays less than worst case.
    assert best < worst
    assert mid_price(spread_idea()) == pytest.approx(4.0)


def test_limit_price_walks_toward_the_touch():
    idea = spread_idea()
    prices = [limit_price_for(idea, 0.5, step=s) for s in range(3)]
    assert prices == sorted(prices)  # a debit gets more expensive as we chase


def test_execution_refuses_an_unstamped_idea(cfg):
    broker = SimBroker(cfg)
    router = ExecutionRouter(cfg, broker)
    with pytest.raises(RiskRejection):
        router.build_ticket(spread_idea(), RiskVerdict.reject("nope"))


def test_client_order_id_is_deterministic(cfg):
    broker = SimBroker(cfg)
    idea = spread_idea()
    assert broker.client_order_id(idea) == broker.client_order_id(idea)
    assert broker.client_order_id(idea) != broker.client_order_id(idea, suffix="1")


def test_duplicate_submission_does_not_double_fill(cfg):
    cfg.execution.dry_run = False
    cfg.execution.chase.enabled = False
    broker = SimBroker(cfg)
    router = ExecutionRouter(cfg, broker)
    idea = spread_idea()
    verdict = RiskVerdict.approve(1)

    first = router.execute(idea, verdict)
    cash_after_first = broker.cash
    second = router.execute(idea, verdict)

    assert first.ok and second.ok
    assert second.fill.order_id == first.fill.order_id
    assert broker.cash == cash_after_first  # the retry cost nothing


def test_dry_run_never_moves_cash(cfg):
    cfg.execution.dry_run = True
    broker = SimBroker(cfg)
    router = ExecutionRouter(cfg, broker)
    before = broker.cash
    result = router.execute(spread_idea(), RiskVerdict.approve(1))
    assert result.fill.status == "dry_run"
    assert broker.cash == before


def test_sim_broker_tracks_legs_and_cash(cfg):
    cfg.execution.dry_run = False
    cfg.execution.chase.enabled = False
    broker = SimBroker(cfg, starting_cash=50_000.0)
    router = ExecutionRouter(cfg, broker)
    router.execute(spread_idea(), RiskVerdict.approve(2))

    account = broker.account()
    assert len(account.positions) == 2
    assert account.cash < 50_000.0  # a debit spread costs money
