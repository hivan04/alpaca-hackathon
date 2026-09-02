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


def test_a_live_profile_refuses_to_fall_back_to_the_simulator():
    """The most dangerous failure in the system: a thirty-second broker outage
    at start-up silently downgraded a live run to an in-process simulator with
    its own imaginary $100k. The agent would trade all day, journal the fills,
    write an equity curve and report success - while the judged account sat
    empty and the reported P&L was fabricated."""
    import pytest

    from oaa.brokers.factory import get_broker
    from oaa.config.loader import load_config
    from oaa.core.errors import BrokerError

    cfg = load_config()
    cfg.broker.primary = "rest"
    cfg.broker.fallback = "sim"
    cfg.execution.dry_run = False

    with pytest.raises(BrokerError, match="traded against a simulator"):
        get_broker(cfg, credentials=None)     # no credentials -> connect fails


def test_the_simulator_closes_a_short_instead_of_doubling_it():
    """`close_position` negated the caller's quantity unconditionally, but the
    orchestrator passes a MAGNITUDE. On a -5 holding, close(5) applied another
    -5: every exit doubled the short and reported it closed."""
    from oaa.brokers.sim import SimBroker
    from oaa.config.loader import load_config

    broker = SimBroker(load_config(), None)
    broker.connect()
    broker._apply("SPY260918P00470000", -5, 2.00)
    broker.close_position("SPY260918P00470000", 5)
    assert broker._positions.get("SPY260918P00470000") is None

    broker._apply("SPY260918C00500000", 3, 1.00)
    broker.close_position("SPY260918C00500000", 3)
    assert broker._positions.get("SPY260918C00500000") is None


# --------------------------------------------------------------------------- #
# the order that was cancelled before it could fill - 31 Aug
# --------------------------------------------------------------------------- #
class _AsyncFillBroker:
    """A broker that behaves like a real one: `submit` acknowledges, it does not
    fill. The fill shows up on the next `order_status`, which is how every
    venue works and how Alpaca works."""

    def __init__(self, fills_on_poll: bool = True) -> None:
        self.fills_on_poll = fills_on_poll
        self.submitted = 0
        self.cancelled: list[str] = []
        self.polls = 0

    def client_order_id(self, idea, suffix: str = "") -> str:
        return f"{idea.id}-{suffix}"

    def submit(self, ticket):
        from oaa.core.types import Fill

        self.submitted += 1
        return Fill(symbol=ticket.symbol, order_id=f"ord-{self.submitted}",
                    status="accepted", filled_qty=0, filled_avg_price=None)

    def order_status(self, order_id: str):
        from oaa.core.types import Fill

        self.polls += 1
        if not self.fills_on_poll:
            return Fill(symbol="SPY", order_id=order_id, status="accepted",
                        filled_qty=0, filled_avg_price=None)
        return Fill(symbol="SPY", order_id=order_id, status="filled",
                    filled_qty=1, filled_avg_price=4.05)

    def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)


def _router_with(broker, monkeypatch, chase_enabled: bool):
    from oaa.config.loader import load_config

    cfg = load_config()
    cfg.execution.chase.enabled = chase_enabled
    # no real sleeping in tests; guarded so this file also RUNS against a tree
    # without the field, where it fails on the behaviour rather than an
    # AttributeError - which is the point of a regression test
    if hasattr(cfg.execution, "fill_settle_seconds"):
        cfg.execution.fill_settle_seconds = 0
    cfg.execution.chase.interval_seconds = 0
    cfg.execution.dry_run = False
    cfg.broker.require_risk_approval = False
    return ExecutionRouter(cfg, broker)


def test_an_order_is_given_time_to_fill_before_it_is_cancelled(monkeypatch):
    """QQQ 31 Aug: a marketable limit on a one-cent-wide 0-DTE call, approved by
    every gate, came back `unfilled after chase`. With the chase disabled the
    router checked the SUBMIT ACKNOWLEDGEMENT - which is never 'filled' - and
    cancelled immediately. No order could ever fill on the judged account."""
    broker = _AsyncFillBroker(fills_on_poll=True)
    router = _router_with(broker, monkeypatch, chase_enabled=False)

    result = router.execute(spread_idea(), RiskVerdict(approved=True, stamp="ok"))

    assert result.ok, "the order filled on the re-poll and must be reported as filled"
    assert result.fill is not None and result.fill.is_filled
    assert broker.polls == 1, "the router must ASK before giving up"
    assert broker.cancelled == [], "a filled order must not be cancelled"
    assert broker.submitted == 1


def test_a_genuinely_unfilled_order_is_still_cancelled(monkeypatch):
    """The fix must not leave orders resting. If it is still unfilled after the
    settle, cancel it - an abandoned limit is worse than a missed trade."""
    broker = _AsyncFillBroker(fills_on_poll=False)
    router = _router_with(broker, monkeypatch, chase_enabled=False)

    result = router.execute(spread_idea(), RiskVerdict(approved=True, stamp="ok"))

    assert not result.ok
    assert broker.cancelled == ["ord-1"], "an unfilled order must be cancelled"
    assert broker.submitted == 1, "chase is off - exactly one order, never two"
    assert "unfilled" in (result.error or "")


def test_the_chase_still_sends_one_resting_order_per_step(monkeypatch):
    """The 28 Aug reason for disabling the chase was a doubled position: cancel
    without checking, then resubmit under a new client_order_id. Whatever the
    settle does, it must never leave two live orders."""
    broker = _AsyncFillBroker(fills_on_poll=False)
    router = _router_with(broker, monkeypatch, chase_enabled=True)

    router.execute(spread_idea(), RiskVerdict(approved=True, stamp="ok"))

    assert broker.submitted == len(broker.cancelled), (
        "every submitted order must be cancelled before the next is sent"
    )


# --- the stale-quote pad, 2 Sep ------------------------------------------- #
# Three orders posted at exactly the quoted ask and none filled, because the
# free `indicative` feed quotes up to fifteen minutes late. The pad prices
# past the stale touch; the quantity guard stops it spending unapproved risk.
# See `claude/limits-priced-off-a-delayed-feed.md`.


def test_the_pad_prices_past_the_far_touch_only_at_full_aggression():
    idea = spread_idea()
    _, worst = structure_bid_ask(idea)

    at_touch = limit_price_for(idea, 1.0, step=0)
    padded = limit_price_for(idea, 1.0, step=0, pad_pct=0.10, pad_min=0.02)
    halfway = limit_price_for(idea, 0.5, step=0, pad_pct=0.10, pad_min=0.02)

    assert at_touch == pytest.approx(worst, abs=0.01)
    assert padded > at_touch
    assert padded == pytest.approx(worst * 1.10, abs=0.01)
    # Below the touch there is still room to walk; the pad must not front-run it.
    assert halfway == limit_price_for(idea, 0.5, step=0)


def test_the_pad_floor_clears_a_tick_on_a_cheap_contract():
    cheap = make_quote(strike=500, bid=0.10, ask=0.12)
    idea = TradeIdea(
        symbol="SPY",
        strategy="test",
        structure=StructureType.SINGLE_LONG,
        legs=[Leg(symbol=cheap.symbol, side=Side.BUY, intent=Intent.BUY_TO_OPEN,
                  quote=cheap, limit_price=cheap.mid)],
        net_price=0.11,
        max_loss=11.0,
    )
    # 10% of 0.12 is 1.2c, which rounds back onto the touch. The floor is why
    # a cheap contract still gets a real pad.
    assert limit_price_for(idea, 1.0, pad_pct=0.10, pad_min=0.02) == pytest.approx(0.14)


def test_the_pad_shrinks_the_quantity_rather_than_the_risk_budget(cfg):
    cfg.execution.dry_run = False
    cfg.execution.chase.enabled = False
    cfg.execution.limit_price_ratio = 1.0
    cfg.execution.stale_quote_pad_pct = 0.10
    cfg.execution.stale_quote_pad_min = 0.02
    router = ExecutionRouter(cfg, SimBroker(cfg))
    idea = spread_idea()          # max_loss 400 per unit at a net price of 4.00

    ticket = router.build_ticket(idea, RiskVerdict.approve(6), step=0)

    approved = 6 * idea.max_loss
    padded_unit_risk = idea.max_loss + (ticket.limit_price - idea.net_price) * 100
    assert ticket.quantity < 6
    assert ticket.quantity * padded_unit_risk <= approved


def test_the_pad_never_sizes_a_trade_out_of_existence(cfg):
    cfg.execution.dry_run = False
    cfg.execution.chase.enabled = False
    cfg.execution.limit_price_ratio = 1.0
    cfg.execution.stale_quote_pad_pct = 0.10
    cfg.execution.stale_quote_pad_min = 0.02
    router = ExecutionRouter(cfg, SimBroker(cfg))

    ticket = router.build_ticket(spread_idea(), RiskVerdict.approve(1), step=0)

    assert ticket.quantity == 1
