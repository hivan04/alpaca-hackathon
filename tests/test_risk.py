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


# --------------------------------------------------------------------------- #
# Re-entering a position you already hold
#
# Both the sim broker and Alpaca NET identical option symbols, so opening the
# same condor twice produces one position at double size, not two positions.
# Every portfolio limit keyed on a COUNT - max_positions, the per-underlying leg
# count - is therefore blind to it. Measured on a replay polled every fifteen
# minutes: eight identical IWM condors in one session, every limit reading green.
# --------------------------------------------------------------------------- #
def _held(idea_, account_):
    """The account, holding exactly the legs of `idea_` on the same sides."""
    account_.positions = [
        PositionSnapshot(
            symbol=leg.symbol,
            qty=1.0 if leg.side is Side.BUY else -1.0,
            avg_entry_price=2.0,
            market_value=200.0,
            asset_class="us_option",
            underlying="SPY",
        )
        for leg in idea_.legs
    ]
    return account_


def test_refuses_a_structure_already_held_leg_for_leg(cfg, account):
    proposal = idea()
    verdict = RiskEngine(cfg).evaluate(proposal, _held(proposal, account), now=MIDDAY)
    assert not verdict.approved
    assert any("duplicate_structure" in r for r in verdict.reasons)


def test_the_same_legs_on_the_OPPOSITE_side_are_a_close_not_a_duplicate(cfg, account):
    """Holding the mirror image is the position being closed out, and must not
    be mistaken for re-entry - otherwise the engine refuses to let a book flat
    itself."""
    proposal = idea()
    account.positions = [
        PositionSnapshot(
            symbol=leg.symbol,
            qty=-1.0 if leg.side is Side.BUY else 1.0,
            avg_entry_price=2.0, market_value=200.0,
            asset_class="us_option", underlying="SPY",
        )
        for leg in proposal.legs
    ]
    verdict = RiskEngine(cfg).evaluate(proposal, account, now=MIDDAY)
    assert verdict.checks.get("duplicate_structure") is True


def test_the_reentry_cooldown_stops_a_second_entry_within_the_window(cfg, account):
    cfg.risk.reentry_cooldown_minutes = 60
    engine = RiskEngine(cfg)
    proposal = idea()
    assert engine.evaluate(proposal, account, now=MIDDAY).approved
    engine.record_open(proposal, now=MIDDAY)

    soon = engine.evaluate(proposal, account, now=MIDDAY + dt.timedelta(minutes=15))
    assert not soon.approved
    assert any("reentry_cooldown" in r for r in soon.reasons)

    later = engine.evaluate(proposal, account, now=MIDDAY + dt.timedelta(minutes=61))
    assert later.approved


def test_the_cooldown_is_per_strategy_not_per_symbol(cfg, account):
    """Two books legitimately trade the same underlying. A cooldown keyed on the
    symbol alone would let whichever ran first mute the other."""
    cfg.risk.reentry_cooldown_minutes = 60
    engine = RiskEngine(cfg)
    first = idea()
    engine.record_open(first, now=MIDDAY)

    other = idea()
    other.strategy = "a_different_book"
    verdict = engine.evaluate(other, account, now=MIDDAY + dt.timedelta(minutes=5))
    assert verdict.approved


def test_a_zero_cooldown_disables_the_check(cfg, account):
    cfg.risk.reentry_cooldown_minutes = 0
    engine = RiskEngine(cfg)
    proposal = idea()
    engine.record_open(proposal, now=MIDDAY)
    verdict = engine.evaluate(proposal, account, now=MIDDAY + dt.timedelta(minutes=1))
    assert verdict.approved


def test_a_strategy_can_override_the_reentry_cooldown(cfg, account):
    """One global cooldown cannot serve a book that holds for days and one that
    holds for minutes.

    The 60-minute default is sized for the intraday book. On the carry book,
    which holds ~6 days, it let the same NVDA condor open at 14:00 and again at
    15:15 on a 10:00-15:15 scan grid - 75 minutes apart, so the check passed.
    That single duplicate was $784 of a $4,852 drawdown.
    """
    cfg.risk.reentry_cooldown_minutes = 60
    engine = RiskEngine(cfg)

    patient = idea()
    patient.meta["reentry_cooldown_minutes"] = 1440
    engine.record_open(patient, now=MIDDAY)

    # 75 minutes later: past the global cooldown, inside the strategy's own.
    later = engine.evaluate(patient, account, now=MIDDAY + dt.timedelta(minutes=75))
    assert not later.approved
    assert any("reentry_cooldown" in r for r in later.reasons)

    # A book that did NOT override still gets the global 60.
    brisk = idea()
    brisk.strategy = "intraday_momentum"
    engine.record_open(brisk, now=MIDDAY)
    assert engine.evaluate(
        brisk, account, now=MIDDAY + dt.timedelta(minutes=75)
    ).approved


# --------------------------------------------------------------------------- #
# Kickoff-day audit fixes
# --------------------------------------------------------------------------- #
def test_a_credit_structure_requires_collateral_not_zero(cfg, account):
    """`max(0, net_price)` is zero for every credit structure, so the cash gate
    charged $0 for short verticals, condors and butterflies - the entire carry
    book - and could not bind on the structures that most need it."""
    proposal = idea(max_loss=300.0)
    proposal.net_price = -2.0                      # a credit
    account.equity = 100_000.0
    account.options_buying_power = 1_000.0         # far too little for collateral
    account.buying_power = 1_000.0

    verdict = RiskEngine(cfg).evaluate(proposal, account, now=MIDDAY)
    assert not verdict.approved
    assert any("cash_buffer" in r for r in verdict.reasons)


def test_the_daily_loss_baseline_survives_a_restart(cfg, account):
    """A restart mid-session used to re-baseline to the already-lossy equity,
    re-arming the whole loss budget."""
    engine = RiskEngine(cfg)
    account.equity = 97_400.0
    account.last_equity = 100_000.0                # the broker's previous close
    engine.observe(account, now=MIDDAY)
    assert engine.state.start_equity == 100_000.0

    account.equity = 96_000.0                      # -4.0% on the day
    engine.observe(account, now=MIDDAY)
    assert engine.state.halted, "the 3-4% daily loss limit should have halted"
