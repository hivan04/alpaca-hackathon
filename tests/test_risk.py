from __future__ import annotations

import datetime as dt

from oaa.core.types import (
    Intent,
    Leg,
    PositionSnapshot,
    Side,
    StructureType,
    TradeIdea,
)
from oaa.risk.engine import RiskEngine
from oaa.risk.sizing import kelly_fraction, size_by_risk

# 14:00 UTC = 10:00 New York - inside the trading window.
MIDDAY = dt.datetime(2026, 9, 2, 14, 0, tzinfo=dt.timezone.utc)


def idea(max_loss: float = 300.0, structure: StructureType = StructureType.IRON_CONDOR) -> TradeIdea:
    return TradeIdea(
        symbol="SPY",
        strategy="test",
        structure=structure,
        legs=[
            Leg(symbol="SPY260918P00475000", side=Side.SELL, intent=Intent.SELL_TO_OPEN),
            Leg(symbol="SPY260918P00470000", side=Side.BUY, intent=Intent.BUY_TO_OPEN),
        ],
        quantity=1,
        net_price=-2.0,
        max_loss=max_loss,
        max_profit=200.0,
        thesis="test",
    )


def test_approves_a_sane_defined_risk_trade(cfg, account):
    verdict = RiskEngine(cfg).evaluate(idea(), account, now=MIDDAY)
    assert verdict.approved
    assert verdict.stamp
    assert verdict.adjusted_quantity >= 1


def test_rejects_undefined_risk_structures(cfg, account):
    verdict = RiskEngine(cfg).evaluate(
        idea(structure=StructureType.STRANGLE), account, now=MIDDAY
    )
    assert not verdict.approved
    assert "rule=undefined_risk" in verdict.reasons


def test_rejects_when_max_loss_is_unknown(cfg, account):
    verdict = RiskEngine(cfg).evaluate(idea(max_loss=0.0), account, now=MIDDAY)
    assert not verdict.approved


def test_position_size_respects_the_risk_cap(cfg, account):
    # 2% of 100k = 2000 budget; a 300 max-loss condor => 6 structures.
    verdict = RiskEngine(cfg).evaluate(idea(max_loss=300.0), account, now=MIDDAY)
    assert verdict.adjusted_quantity == 6


def test_trade_too_large_for_the_cap_is_rejected(cfg, account):
    verdict = RiskEngine(cfg).evaluate(idea(max_loss=50_000.0), account, now=MIDDAY)
    assert not verdict.approved
    assert "rule=sizing" in verdict.reasons


def test_daily_loss_limit_halts_trading(cfg, account):
    engine = RiskEngine(cfg)
    engine.observe(account, MIDDAY)
    down = account.model_copy(update={"equity": account.equity * 0.94})
    engine.observe(down, MIDDAY)
    assert engine.state.halted
    assert not engine.evaluate(idea(), down, now=MIDDAY).approved


def test_daily_new_position_cap(cfg, account):
    engine = RiskEngine(cfg)
    for _ in range(cfg.risk.max_new_positions_per_day):
        assert engine.evaluate(idea(), account, now=MIDDAY).approved
        engine.record_open()
    verdict = engine.evaluate(idea(), account, now=MIDDAY)
    assert not verdict.approved
    assert "rule=max_new_per_day" in verdict.reasons


def test_market_closed_blocks_everything(cfg, account):
    verdict = RiskEngine(cfg).evaluate(idea(), account, now=MIDDAY, market_open=False)
    assert not verdict.approved


def test_no_trade_window_at_the_open(cfg, account):
    just_open = dt.datetime(2026, 9, 2, 13, 31, tzinfo=dt.timezone.utc)  # 09:31 NY
    verdict = RiskEngine(cfg).evaluate(idea(), account, now=just_open)
    assert not verdict.approved
    assert "rule=time_window" in verdict.reasons


def test_concentration_limit_per_underlying(cfg, account):
    positions = [
        PositionSnapshot(symbol=f"SPY260918C0050{i}000", qty=-1, avg_entry_price=1.0,
                         underlying="SPY", market_value=-100)
        for i in range(8)
    ]
    loaded = account.model_copy(update={"positions": positions})
    verdict = RiskEngine(cfg).evaluate(idea(), loaded, now=MIDDAY)
    assert not verdict.approved


def test_duplicate_legs_are_rejected(cfg, account):
    bad = idea()
    bad.legs[1] = Leg(symbol=bad.legs[0].symbol, side=Side.BUY)
    verdict = RiskEngine(cfg).evaluate(bad, account, now=MIDDAY)
    assert not verdict.approved
    assert "rule=duplicate_legs" in verdict.reasons


def test_size_by_risk_is_floor_not_round():
    assert size_by_risk(idea(max_loss=350.0), 100_000, 0.02) == 5   # 2000/350 = 5.7
    assert size_by_risk(idea(max_loss=3000.0), 100_000, 0.02) == 0


def test_kelly_is_capped():
    assert kelly_fraction(0.9, 5.0) <= 0.25
    assert kelly_fraction(0.1, 0.5) == 0.0
