"""Builders that turn chain selections into priced, risk-bounded TradeIdeas.

Every builder returns max_loss and max_profit computed from the actual
selected strikes, so the risk engine never has to guess.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from oaa.core.errors import StrategyError
from oaa.core.types import Intent, Leg, OptionQuote, Right, Side, StructureType, TradeIdea
from oaa.options.chain import ChainView

MULTIPLIER = 100


def _mid_or_fail(q: OptionQuote) -> float:
    mid = q.mid
    if mid is None or mid <= 0:
        raise StrategyError(f"no usable price for {q.symbol}")
    return mid


def _leg(q: OptionQuote, side: Side, ratio: int = 1) -> Leg:
    return Leg(
        symbol=q.symbol,
        side=side,
        ratio=ratio,
        intent=Intent.opening(side),
        quote=q,
        limit_price=q.mid,
    )


# --------------------------------------------------------------------------- #
# single long option
# --------------------------------------------------------------------------- #
def build_single_long(
    *,
    symbol: str,
    strategy: str,
    leg: OptionQuote,
    quantity: int = 1,
    thesis: str = "",
) -> TradeIdea:
    """One long option. Max loss is the premium paid, always, by construction.

    The intraday book uses this when implied vol is cheap enough that paying
    for the whole distribution is better than capping it with a short wing.
    There is no `build_single_short` and there never will be: an uncovered
    short fails `risk.allow_undefined_risk: false` at the engine.
    """
    debit = _mid_or_fail(leg)
    return TradeIdea(
        symbol=symbol,
        strategy=strategy,
        structure=StructureType.SINGLE_LONG,
        legs=[_leg(leg, Side.BUY)],
        quantity=quantity,
        net_price=round(debit, 4),
        max_loss=round(debit * MULTIPLIER, 2),
        # Upside on a long option is unbounded; claiming a max profit we cannot
        # compute would put a fictional number into the reward/risk ratio.
        max_profit=None,
        thesis=thesis,
        meta={
            "strike": leg.strike,
            "right": leg.right.value,
            "expiry": leg.expiry.isoformat(),
            "iv": leg.implied_volatility,
            "delta": leg.greeks.delta,
            "premium": debit,
        },
    )


# --------------------------------------------------------------------------- #
# vertical spreads
# --------------------------------------------------------------------------- #
def build_vertical(
    *,
    symbol: str,
    strategy: str,
    long_leg: OptionQuote,
    short_leg: OptionQuote,
    quantity: int = 1,
    thesis: str = "",
) -> TradeIdea:
    """Two-leg vertical. Debit or credit is inferred from the prices."""
    if long_leg.expiry != short_leg.expiry:
        raise StrategyError("vertical legs must share an expiry")
    if long_leg.right is not short_leg.right:
        raise StrategyError("vertical legs must share a right")
    if long_leg.strike == short_leg.strike:
        raise StrategyError("vertical legs must have different strikes")

    long_px, short_px = _mid_or_fail(long_leg), _mid_or_fail(short_leg)
    net = round(long_px - short_px, 4)          # >0 debit, <0 credit
    width = abs(long_leg.strike - short_leg.strike)

    if net > 0:  # debit spread
        structure = StructureType.VERTICAL_DEBIT
        max_loss = round(net * MULTIPLIER, 2)
        max_profit = round((width - net) * MULTIPLIER, 2)
    else:        # credit spread
        structure = StructureType.VERTICAL_CREDIT
        credit = abs(net)
        max_profit = round(credit * MULTIPLIER, 2)
        max_loss = round((width - credit) * MULTIPLIER, 2)

    return TradeIdea(
        symbol=symbol,
        strategy=strategy,
        structure=structure,
        legs=[_leg(long_leg, Side.BUY), _leg(short_leg, Side.SELL)],
        quantity=quantity,
        net_price=net,
        max_loss=max_loss,
        max_profit=max_profit,
        thesis=thesis,
        meta={
            "width": width,
            "expiry": long_leg.expiry.isoformat(),
            "right": long_leg.right.value,
            "long_strike": long_leg.strike,
            "short_strike": short_leg.strike,
        },
    )


# --------------------------------------------------------------------------- #
# iron condor
# --------------------------------------------------------------------------- #
def build_iron_condor(
    *,
    symbol: str,
    strategy: str,
    long_put: OptionQuote,
    short_put: OptionQuote,
    short_call: OptionQuote,
    long_call: OptionQuote,
    quantity: int = 1,
    thesis: str = "",
) -> TradeIdea:
    """Short put spread + short call spread, same expiry. Net credit."""
    strikes = [long_put.strike, short_put.strike, short_call.strike, long_call.strike]
    if strikes != sorted(strikes):
        raise StrategyError(f"iron condor strikes out of order: {strikes}")
    if len({q.expiry for q in (long_put, short_put, short_call, long_call)}) != 1:
        raise StrategyError("all iron condor legs must share an expiry")

    credit = round(
        (_mid_or_fail(short_put) + _mid_or_fail(short_call))
        - (_mid_or_fail(long_put) + _mid_or_fail(long_call)),
        4,
    )
    if credit <= 0:
        raise StrategyError(f"{symbol}: condor would be a net debit ({credit})")

    put_width = short_put.strike - long_put.strike
    call_width = long_call.strike - short_call.strike
    widest = max(put_width, call_width)

    max_profit = round(credit * MULTIPLIER, 2)
    max_loss = round((widest - credit) * MULTIPLIER, 2)

    return TradeIdea(
        symbol=symbol,
        strategy=strategy,
        structure=StructureType.IRON_CONDOR,
        legs=[
            _leg(long_put, Side.BUY),
            _leg(short_put, Side.SELL),
            _leg(short_call, Side.SELL),
            _leg(long_call, Side.BUY),
        ],
        quantity=quantity,
        net_price=round(-credit, 4),   # negative = credit, per Alpaca
        max_loss=max_loss,
        max_profit=max_profit,
        thesis=thesis,
        meta={
            "credit": credit,
            "put_width": put_width,
            "call_width": call_width,
            "credit_to_width": round(credit / widest, 4) if widest else None,
            "expiry": short_put.expiry.isoformat(),
            "short_put_strike": short_put.strike,
            "short_call_strike": short_call.strike,
            "breakeven_low": round(short_put.strike - credit, 2),
            "breakeven_high": round(short_call.strike + credit, 2),
        },
    )


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
def build_calendar(
    *,
    symbol: str,
    strategy: str,
    short_leg: OptionQuote,
    long_leg: OptionQuote,
    quantity: int = 1,
    thesis: str = "",
) -> TradeIdea:
    """Sell the near expiry, buy the far expiry, same strike and right."""
    if short_leg.strike != long_leg.strike or short_leg.right is not long_leg.right:
        raise StrategyError("calendar legs must share strike and right")
    if long_leg.expiry <= short_leg.expiry:
        raise StrategyError("calendar long leg must expire later")

    debit = round(_mid_or_fail(long_leg) - _mid_or_fail(short_leg), 4)
    if debit <= 0:
        raise StrategyError(f"{symbol}: calendar priced as a credit - check the chain")

    return TradeIdea(
        symbol=symbol,
        strategy=strategy,
        structure=StructureType.CALENDAR,
        legs=[_leg(short_leg, Side.SELL), _leg(long_leg, Side.BUY)],
        quantity=quantity,
        net_price=debit,
        # Debit paid is the true floor. Upside depends on terminal vol, so we
        # do not claim a max profit we cannot compute without a model.
        max_loss=round(debit * MULTIPLIER, 2),
        max_profit=None,
        thesis=thesis,
        meta={
            "strike": short_leg.strike,
            "right": short_leg.right.value,
            "front_expiry": short_leg.expiry.isoformat(),
            "back_expiry": long_leg.expiry.isoformat(),
            "front_iv": short_leg.implied_volatility,
            "back_iv": long_leg.implied_volatility,
        },
    )


# --------------------------------------------------------------------------- #
# convenience builder bound to a ChainView
# --------------------------------------------------------------------------- #
@dataclass
class StructureBuilder:
    """Wraps a ChainView so strategies express intent, not strike arithmetic."""

    view: ChainView
    strategy: str

    def single_long(
        self,
        *,
        right: Right,
        dte_range: tuple[int, int],
        target_delta: float,
        quantity: int = 1,
        thesis: str = "",
        expiry: dt.date | None = None,
    ) -> TradeIdea:
        exp = expiry or self.view.expiry_in_range(dte_range)
        leg = self.view.by_delta(exp, right, target_delta)
        return build_single_long(
            symbol=self.view.symbol,
            strategy=self.strategy,
            leg=leg,
            quantity=quantity,
            thesis=thesis,
        )

    def vertical_by_delta(
        self,
        *,
        right: Right,
        dte_range: tuple[int, int],
        long_delta: float,
        short_delta: float,
        quantity: int = 1,
        thesis: str = "",
        expiry: dt.date | None = None,
    ) -> TradeIdea:
        exp = expiry or self.view.expiry_in_range(dte_range)
        long_leg = self.view.by_delta(exp, right, long_delta)
        short_leg = self.view.by_delta(exp, right, short_delta)
        if long_leg.symbol == short_leg.symbol:
            raise StrategyError(
                f"{self.view.symbol}: {long_delta:.2f}/{short_delta:.2f} deltas "
                "resolved to the same strike - widen the deltas"
            )
        return build_vertical(
            symbol=self.view.symbol,
            strategy=self.strategy,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=quantity,
            thesis=thesis,
        )

    def iron_condor_by_delta(
        self,
        *,
        dte_range: tuple[int, int],
        short_put_delta: float,
        short_call_delta: float,
        wing_points: float,
        quantity: int = 1,
        thesis: str = "",
        expiry: dt.date | None = None,
    ) -> TradeIdea:
        exp = expiry or self.view.expiry_in_range(dte_range)
        short_put = self.view.by_delta(exp, Right.PUT, short_put_delta)
        short_call = self.view.by_delta(exp, Right.CALL, short_call_delta)
        long_put = self.view.strike_offset(exp, Right.PUT, short_put.strike, -abs(wing_points))
        long_call = self.view.strike_offset(exp, Right.CALL, short_call.strike, abs(wing_points))

        if long_put.strike >= short_put.strike or long_call.strike <= short_call.strike:
            raise StrategyError(
                f"{self.view.symbol}: wing width {wing_points} does not fit the strike grid"
            )
        return build_iron_condor(
            symbol=self.view.symbol,
            strategy=self.strategy,
            long_put=long_put,
            short_put=short_put,
            short_call=short_call,
            long_call=long_call,
            quantity=quantity,
            thesis=thesis,
        )

    def calendar_atm(
        self,
        *,
        front_dte: tuple[int, int],
        back_dte: tuple[int, int],
        right: Right = Right.CALL,
        quantity: int = 1,
        thesis: str = "",
    ) -> TradeIdea:
        front = self.view.expiry_in_range(front_dte)
        back = self.view.expiry_in_range(back_dte)
        atm = self.view.atm(front, right)
        long_leg = self.view.by_strike(back, right, atm.strike)
        return build_calendar(
            symbol=self.view.symbol,
            strategy=self.strategy,
            short_leg=atm,
            long_leg=long_leg,
            quantity=quantity,
            thesis=thesis,
        )


def closing_idea(idea: TradeIdea, exit_price: float) -> TradeIdea:
    """Mirror an open structure into the order that flattens it."""
    legs = [
        Leg(
            symbol=leg.symbol,
            side=leg.side.opposite,
            ratio=leg.ratio,
            intent=Intent.closing(leg.side.opposite),
        )
        for leg in idea.legs
    ]
    return TradeIdea(
        symbol=idea.symbol,
        strategy=idea.strategy,
        structure=idea.structure,
        legs=legs,
        quantity=idea.quantity,
        net_price=round(-exit_price, 4),
        thesis=f"close {idea.id}",
        tags=["close", *idea.tags],
        meta={"closes": idea.id},
    )
