"""The events book, in the repo's Strategy shape.

Registered so `oaa strategies` lists it and the gate log speaks the same
language as the other books - but not enabled in `config/default.yaml`, and the
options runner will never load it, because nothing in `strategies:` names it.
Its real entry point is `oaa events arm`, which drives `events.engine`.

Three interlocks make that structural rather than conventional:

  * `generate` refuses any symbol without a CONFIRMED row in the earnings
    calendar, so it cannot fire on a name the model merely proposed.
  * `generate` refuses unless today is the event's entry date, so it cannot
    open a position days early and sit through unrelated sessions.
  * `generate` refuses without a direction call supplied by the engine. The
    strategy builds structures; it never calls an LLM from inside the
    generation loop.

The expression is a vertical debit spread in the called direction. Not a naked
long: the print is followed by an implied-vol collapse, and long premium can be
right about direction and still lose to the crush. The short leg gives back
some upside and takes back some of that vega.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import Right, TradeIdea
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry
from oaa.strategies.events.calendar import EarningsEvent, load_calendar
from oaa.strategies.events.direction import DirectionCall
from oaa.strategies.events.params import DEFAULT_PARAMS_PATH, EventsParams, load_params
from oaa.strategies.events.sizing import size
from oaa.strategies.events.volscreen import VolRead, screen_one

log = get_logger("strategies.events")


@strategy_registry.register("earnings_event_directional")
class EarningsEventDirectional(Strategy):
    description = (
        "Buys a vertical debit spread into a confirmed earnings print, sized on "
        "an LLM's confidence in the direction, and closes it the next morning."
    )
    book = "events"
    mode = "per_symbol"

    def __init__(self, ref: Any, config: Any) -> None:
        super().__init__(ref, config)
        self.events: EventsParams = load_params(self.p("params_path", DEFAULT_PARAMS_PATH))
        self.calendar = load_calendar(self.events.calendar_path)

    # ------------------------------------------------------------------ #
    def universe(self) -> list[str]:
        """Only names with a confirmed print. Not the global universe."""
        return sorted(s for s, e in self.calendar.items() if e.confirmed)

    def chain_dte_window(self) -> tuple[int, int] | None:
        """This book trades the front weekly, which sits well inside the global
        minimum DTE the other books are built around. Declaring it is what
        stops the replay handing this strategy a chain that cannot contain a
        single qualifying contract."""
        return self.events.structure.dte_window

    def _filter(self, ctx: StrategyContext) -> Any:
        """The chain filter for this book.

        Two overrides on the global one, and both matter. The DTE window is the
        front weekly, well inside the global minimum. The price ceiling is
        raised because the global $25 cap is calibrated for 30-45 day options on
        mid-priced names; an ATM weekly on a $450 stock pricing a 16% move is
        worth several times that. Left alone it would not block the trade - it
        would remove the near-the-money contracts and leave the cheap far-OTM
        ones, so the screen would price an OTM strike as "ATM" and understate
        the implied move, and the 45-delta long leg would resolve to whatever
        delta survived. A distorted structure, not a refused one.
        """
        return ctx.default_filter(
            min_dte=self.events.structure.dte_window[0],
            max_dte=self.events.structure.dte_window[1],
            min_price=self.events.screen.min_option_price,
            max_price=self.events.screen.max_option_price,
        )

    def event_for(self, symbol: str) -> EarningsEvent | None:
        event = self.calendar.get(symbol.upper())
        return event if event and event.confirmed else None

    # ------------------------------------------------------------------ #
    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        market = ctx.market
        if market is None:
            return []

        # Interlock 1: a confirmed event, not a proposal.
        event = self.event_for(market.symbol)
        if event is None:
            log.debug("%s: no confirmed earnings row - refusing", market.symbol)
            return []

        # Interlock 2: today is the session we arm into.
        today = market.asof.date()
        if today != event.entry_date:
            log.debug(
                "%s: %s is not the entry date %s for a %s print",
                market.symbol, today, event.entry_date, event.report_date,
            )
            return []

        # Interlock 3: the engine supplies the direction call. No LLM traffic
        # from inside a generation loop.
        call: DirectionCall | None = (ctx.params or {}).get("direction_call")
        if call is None or not call.actionable:
            reason = call.skip_reason if call else "no direction call supplied"
            log.info("%s: no trade - %s", market.symbol, reason)
            return []

        try:
            view = ctx.chain_view(chain_filter=self._filter(ctx))
        except (StrategyError, DataError) as exc:
            log.info("%s: no usable chain - %s", market.symbol, exc)
            return []

        read = screen_one(event, market, view, self.events.screen)
        if not read.ok:
            log.info(read.summary())
            return []

        idea = self.build_idea(ctx, read, call, view_expiry=read.expiry)
        return [idea] if idea else []

    # ------------------------------------------------------------------ #
    def build_idea(
        self,
        ctx: StrategyContext,
        read: VolRead,
        call: DirectionCall,
        view_expiry: dt.date | None = None,
    ) -> TradeIdea | None:
        """Price the vertical, check it, size it on confidence."""
        structure = self.events.structure
        long_delta, short_delta = structure.long_delta, structure.short_delta
        right = Right.CALL if call.bullish else Right.PUT
        if not call.bullish:  # put deltas are negative
            long_delta, short_delta = -abs(long_delta), -abs(short_delta)

        try:
            idea = self.builder(
                ctx,
                symbol=read.symbol,
                chain_filter=self._filter(ctx),
            ).vertical_by_delta(
                right=right,
                dte_range=structure.dte_window,
                long_delta=long_delta,
                short_delta=short_delta,
                quantity=1,
                expiry=view_expiry,
                thesis=self._thesis(read, call),
            )
        except (StrategyError, DataError) as exc:
            log.info("%s: could not build the spread - %s", read.symbol, exc)
            return None

        width = float(idea.meta.get("width") or 0)
        if width and idea.net_price / width > structure.max_debit_to_width:
            log.info(
                "%s: debit %.2f is %.0f%% of a %.2f-wide spread - above the %.0f%% ceiling",
                read.symbol, idea.net_price, idea.net_price / width * 100, width,
                structure.max_debit_to_width * 100,
            )
            return None
        if idea.max_loss and idea.max_profit:
            reward_risk = idea.max_profit / idea.max_loss
            if reward_risk < structure.min_reward_risk:
                log.info(
                    "%s: reward:risk %.2f below the %.2f floor",
                    read.symbol, reward_risk, structure.min_reward_risk,
                )
                return None

        decision = size(
            confidence=call.confidence,
            confidence_floor=self.events.direction.min_confidence,
            max_loss_per_contract=float(idea.max_loss or 0),
            equity=float(ctx.account.equity or 0),
            params=self.events.sizing,
            budget_remaining=(ctx.params or {}).get("budget_remaining"),
        )
        if not decision.ok:
            log.info("%s: not sized - %s", read.symbol, decision.reason)
            return None

        idea.quantity = decision.contracts
        idea.book = self.events.book
        idea.confidence = call.confidence
        idea.tags = [
            "earnings", "event", "defined_risk",
            "bullish" if call.bullish else "bearish",
        ]
        idea.meta.update({
            "event_date": read.event.report_date.isoformat(),
            "event_timing": read.event.timing,
            "exit_date": read.event.exit_date.isoformat(),
            "implied_move_pct": read.implied_move_pct,
            "realised_mean_abs_pct": read.realised_mean_abs_pct,
            "implied_realised_ratio": read.ratio,
            "relative_spread": read.relative_spread,
            "size_multiple": decision.multiple,
            "risk_dollars": decision.risk_dollars,
            **call.as_meta(),
        })
        return idea

    # ------------------------------------------------------------------ #
    def should_exit(self, ctx: StrategyContext, idea: TradeIdea, pnl_pct: float) -> str | None:
        """The exit is a date, not a percentage.

        The whole position exists to be open across one print. Holding it past
        the reaction session turns an event bet into a directional one on a
        decaying option, which is not the trade that was approved.
        """
        market = ctx.market
        if market is None:
            return None
        exit_date = idea.meta.get("exit_date")
        if exit_date and market.asof.date() >= dt.date.fromisoformat(str(exit_date)):
            return f"reaction session {exit_date} - closing into the vol crush"
        return super().should_exit(ctx, idea, pnl_pct)

    @staticmethod
    def _thesis(read: VolRead, call: DirectionCall) -> str:
        side = "upside" if call.bullish else "downside"
        ratio = f", {read.ratio:.2f}x its last four prints" if read.ratio else ""
        return (
            f"{read.symbol} reports {read.event.report_date:%d %b} "
            f"({'after the close' if read.event.timing == 'amc' else 'before the open'}). "
            f"The {read.expiry:%d %b} straddle prices a {read.implied_move_pct:.2f}% move"
            f"{ratio}. Evidence points {side} at {call.confidence:.0%} confidence: "
            f"{call.rationale} Expressed as a vertical debit spread, so the loss is "
            f"capped at the debit and the short leg offsets part of the post-print "
            f"IV collapse."
        )
