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

The expression is chosen by the SIGN of the vol divergence, not fixed (30 Aug):

  * Options rich against the name's own last four reactions - SELL premium as a
    defined-risk iron condor with both shorts outside the implied move.
  * Options cheap - BUY the move as a vertical debit spread in the called
    direction. Not a naked long: the print is followed by an implied-vol
    collapse, and long premium can be right about direction and still lose to
    the crush. The short leg gives back some upside and takes back some vega.
  * Neither - no trade.

The reason for the change is arithmetic. The screen measures a VOLATILITY
mispricing and the book used to express every one of them directionally, so the
payoff was orthogonal to the quantity measured. A directional structure bought
at a fair-to-rich implied move, on a direction call no better than a coin flip,
returns minus the round trip in expectation - regardless of sample size, gate
tuning or how good the signal turns out to be. See `StructureParams`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import MarketContext, Right, TradeIdea
from oaa.signals.gates import GateResult, gates_summary
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry
from oaa.strategies.events.calendar import EarningsEvent, load_calendar
from oaa.strategies.events.direction import DirectionCall
from oaa.strategies.events.params import DEFAULT_PARAMS_PATH, EventsParams, load_params
from oaa.strategies.events.sizing import size
from oaa.strategies.events.technicals import (
    TechnicalRead,
    stop_breached,
)
from oaa.strategies.events.technicals import (
    evaluate as read_tape,
)
from oaa.strategies.events.volscreen import VolRead, screen_one

log = get_logger("strategies.events")


@strategy_registry.register("earnings_event_directional")
class EarningsEventDirectional(Strategy):
    description = (
        "Trades a confirmed earnings print in the structure its own vol "
        "divergence justifies - short premium when the options are rich, a "
        "debit vertical when they are cheap - and closes the next morning."
    )
    book = "events"
    mode = "per_symbol"

    def __init__(self, ref: Any, config: Any) -> None:
        super().__init__(ref, config)
        self.events: EventsParams = load_params(self.p("params_path", DEFAULT_PARAMS_PATH))
        # A per-run override, so a replay can be pointed at a different week's
        # calendar without editing the params file. Live this is never set:
        # `oaa events arm` reads the calendar the params file names.
        self.calendar_path = self.p("calendar_path") or self.events.calendar_path
        self.calendar = load_calendar(self.calendar_path)

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
        #
        # This used to log at DEBUG and return, which made the most common way
        # to misuse this book completely silent: point it at a universe with no
        # calendar rows - `--symbols NVDA,CRM,...` - and every symbol is
        # refused here, the funnel records nothing, and the run reports zero
        # trades with no reason. That is indistinguishable from a broken
        # strategy, and it is the exact failure this whole book exists to avoid.
        event = self.event_for(market.symbol)
        if event is None:
            known = self.calendar.get(market.symbol.upper())
            reason = (
                f"{market.symbol} is in the calendar but not confirmed "
                f"({known.source})" if known else
                f"{market.symbol} has no row in {self.calendar_path} - "
                "this book only trades names whose print is confirmed, so a "
                "symbol that is not on the calendar can never produce a trade"
            )
            return self._reject(ctx, market, [GateResult.veto("scheduled_event", reason)])

        # Interlock 2: today is the session we arm into.
        today = market.asof.date()
        if today != event.entry_date:
            return self._reject(ctx, market, [GateResult.veto(
                "event_window",
                f"{today} is not the entry date {event.entry_date} for the "
                f"{event.report_date} print",
            )])

        # Interlock 3: the engine supplies the direction call. No LLM traffic
        # from inside a generation loop.
        call: DirectionCall | None = (ctx.params or {}).get("direction_call")
        if call is None and self.events.direction.derive_from_tape_when_no_call:
            # Replay. The live engine ALWAYS supplies a call - an abstention is
            # still a call - so arriving here with None means no engine, which
            # means a backtest. Deriving the direction from the tape is what
            # makes the technical layer measurable without an LLM in the loop.
            call = self._derived_call(market)
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

        # The tape gets a vote. The LLM says which way; this says whether the
        # setup supports expressing it, and how large - so a confident call on
        # a name with no coiled volatility, or one already at an RSI extreme,
        # does not become a position on sentiment alone.
        tape = read_tape(
            symbol=market.symbol,
            bars=market.bars,
            spot=market.spot,
            bullish=call.bullish,
            params=self.events.technicals,
        )
        if not tape.ok:
            log.info(tape.summary())
            return []

        idea = self.build_idea(ctx, read, call, view_expiry=read.expiry, tape=tape)
        return [idea] if idea else []

    def _derived_call(self, market: Any) -> DirectionCall | None:
        """Direction from price alone, for replay. Never reached live."""
        from oaa.data.indicators import bollinger, closes

        bars = list(market.bars or [])
        ta = self.events.technicals
        if len(bars) < ta.bollinger_period:
            return None
        middle, _, _ = bollinger(closes(bars), ta.bollinger_period, ta.bollinger_std)
        if middle is None:
            return None
        bullish = market.spot >= middle
        return DirectionCall(
            symbol=market.symbol,
            direction="bullish" if bullish else "bearish",
            confidence=self.events.direction.derived_confidence,
            rationale=(
                "derived from the tape, not from a model: spot sits "
                f"{'above' if bullish else 'below'} the {ta.bollinger_period}-day "
                "Bollinger midline. No LLM ran in this replay."
            ),
            evidence=["bollinger midline"],
            degraded=True,
        )

    def _reject(
        self,
        ctx: StrategyContext,
        market: MarketContext,
        checks: list[GateResult],
    ) -> list[TradeIdea]:
        """Record a refusal in the funnel rather than returning a silent [].

        The rejection log is the artefact that distinguishes a book standing
        down from a book that cannot fire - and `--why` reads it.
        """
        summary = gates_summary(checks)
        log.info(
            "%s: events candidate vetoed by '%s' - %s",
            market.symbol, summary["vetoed_by"], summary["reason"],
        )
        journal = getattr(getattr(ctx, "firewall", None), "journal", None)
        if journal is not None:
            try:
                journal.event(
                    "gate_rejection", book=self.events.book, strategy=self.name,
                    symbol=market.symbol, **summary,
                )
            except Exception:  # noqa: BLE001
                pass
        return []

    # ------------------------------------------------------------------ #
    def build_idea(
        self,
        ctx: StrategyContext,
        read: VolRead,
        call: DirectionCall,
        view_expiry: dt.date | None = None,
        tape: TechnicalRead | None = None,
    ) -> TradeIdea | None:
        """Choose the expression from the divergence, price it, size it."""
        expression = self._expression(read)
        if expression is None:
            return None
        if expression == "sell_premium":
            idea = self._short_premium(ctx, read, call, view_expiry)
        else:
            idea = self._debit_vertical(ctx, read, call, view_expiry)
        if idea is None:
            return None

        decision = size(
            confidence=call.confidence,
            confidence_floor=self.events.direction.min_confidence,
            max_loss_per_contract=float(idea.max_loss or 0),
            equity=float(ctx.account.equity or 0),
            params=self.events.sizing,
            budget_remaining=(ctx.params or {}).get("budget_remaining"),
            extra_multiple=tape.size_multiple if tape else 1.0,
        )
        if not decision.ok:
            log.info("%s: not sized - %s", read.symbol, decision.reason)
            return None

        idea.quantity = decision.contracts
        idea.book = self.events.book
        idea.confidence = call.confidence
        expression = str(idea.meta.get("expression"))
        idea.tags = ["earnings", "event", "defined_risk", expression]
        # Only a directional structure carries a directional tag. The ATR stop
        # in `should_exit` reads this, and a condor has no side for it to be
        # right or wrong about.
        if expression == "buy_direction":
            idea.tags.append("bullish" if call.bullish else "bearish")
        if call.degraded:
            idea.tags.append("derived")
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
            **(tape.as_meta() if tape else {}),
        })
        return idea

    # ------------------------------------------------------------------ #
    # expression selection
    # ------------------------------------------------------------------ #
    def _expression(self, read: VolRead) -> str | None:
        """Which structure the measured divergence actually justifies.

        Returning None is a real answer, and on most names it is the right
        one. The screen ranks by |implied - realised|; a name sitting near 1.0
        has no measured mispricing in either direction, and a structure opened
        there is a bet on the direction call alone, paid for with four
        half-spreads. That was the whole book until 30 Aug.
        """
        structure = self.events.structure
        if not structure.expression_follows_divergence:
            return "buy_direction"
        if read.ratio is None:
            log.info(
                "%s: no realised reaction history - the divergence is unmeasured, "
                "so there is no edge to express", read.symbol,
            )
            return None
        if read.ratio >= structure.rich_ratio_threshold:
            return "sell_premium"
        if read.ratio <= structure.cheap_ratio_threshold:
            return "buy_direction"
        log.info(
            "%s: implied/realised %.2f sits between the %.2f cheap and %.2f rich "
            "thresholds - no measured mispricing to trade",
            read.symbol, read.ratio,
            structure.cheap_ratio_threshold, structure.rich_ratio_threshold,
        )
        return None

    # ------------------------------------------------------------------ #
    def _debit_vertical(
        self,
        ctx: StrategyContext,
        read: VolRead,
        call: DirectionCall,
        view_expiry: dt.date | None,
    ) -> TradeIdea | None:
        """Buy the move, in the called direction, when the options are cheap."""
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
        idea.meta["expression"] = "buy_direction"
        return idea

    # ------------------------------------------------------------------ #
    def _short_premium(
        self,
        ctx: StrategyContext,
        read: VolRead,
        call: DirectionCall,
        view_expiry: dt.date | None,
    ) -> TradeIdea | None:
        """Sell the overpricing, defined risk, shorts outside the implied move.

        This is the expression that collects the quantity the screen measures.
        The direction call tilts the short strikes and nothing more: on a
        structure that profits from the move being SMALLER than priced, a
        strong view is worth a few points of delta, not the thesis.
        """
        structure = self.events.structure
        move = read.spot * (read.implied_move_pct or 0.0) / 100.0
        base = structure.shorts_at_implied_move
        tilt = abs(structure.condor_direction_tilt)
        # The tilt pushes the THREATENED side out and leaves the other where
        # it was. A bullish call means the call side is the one at risk.
        put_multiple = base if call.bullish else base + tilt
        call_multiple = base + tilt if call.bullish else base

        try:
            idea = self.builder(
                ctx,
                symbol=read.symbol,
                chain_filter=self._filter(ctx),
            ).iron_condor_outside_move(
                dte_range=structure.dte_window,
                move_dollars=move,
                put_multiple=put_multiple,
                call_multiple=call_multiple,
                wing_pct=structure.condor_wing_pct,
                quantity=1,
                expiry=view_expiry,
                thesis=self._thesis_short(read, call),
            )
        except (StrategyError, DataError) as exc:
            log.info("%s: could not build the condor - %s", read.symbol, exc)
            return None

        credit_to_width = float(idea.meta.get("credit_to_width") or 0.0)
        if credit_to_width < structure.min_credit_to_width:
            log.info(
                "%s: credit is %.0f%% of the wing, below the %.0f%% floor - "
                "risking the width to collect very little",
                read.symbol, credit_to_width * 100,
                structure.min_credit_to_width * 100,
            )
            return None

        # What the listed ladder actually delivered. The shorts were ASKED for
        # at the implied move; a coarse grid can snap one back inside it, and
        # that is a different trade from the one the screen justified.
        clearance = min(
            float(idea.meta.get("put_clearance") or 0.0),
            float(idea.meta.get("call_clearance") or 0.0),
        )
        if clearance < structure.min_shorts_clearance:
            log.info(
                "%s: the listed strikes put the nearest short at %.2fx the implied "
                "move, below the %.2fx floor - the grid is too coarse for this trade",
                read.symbol, clearance, structure.min_shorts_clearance,
            )
            return None

        idea.meta["shorts_clearance"] = round(clearance, 4)
        idea.meta["expression"] = "sell_premium"
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
        reached = exit_date and market.asof.date() >= dt.date.fromisoformat(str(exit_date))

        # The ATR stop cannot be watched overnight - no cycle runs between the
        # arm and the exit, and the gap through it is the risk the position was
        # opened to take. What it does is govern the morning: through the level
        # means close now rather than wait for a target.
        stop = idea.meta.get("ta_stop_underlying")
        directional = idea.meta.get("expression") == "buy_direction"
        bullish = "bullish" in (idea.tags or [])
        if reached and directional and stop_breached(stop, market.spot, bullish):
            return (
                f"{market.symbol} through its {idea.meta.get('ta_atr_multiple', 2)}x ATR "
                f"stop at {float(stop):.2f} - closing without waiting for the target"
            )
        if reached:
            return f"reaction session {exit_date} - closing into the vol crush"
        return super().should_exit(ctx, idea, pnl_pct)

    @staticmethod
    def _thesis_short(read: VolRead, call: DirectionCall) -> str:
        ratio = f"{read.ratio:.2f}x" if read.ratio else "above"
        tilt = "upside" if call.bullish else "downside"
        return (
            f"{read.symbol} reports {read.event.report_date:%d %b} "
            f"({'after the close' if read.event.timing == 'amc' else 'before the open'}). "
            f"The {read.expiry:%d %b} straddle prices a {read.implied_move_pct:.2f}% move, "
            f"{ratio} what this name has actually paid on its last four prints. "
            f"Sold as a defined-risk iron condor with both shorts outside the implied "
            f"move, so the position profits from the move being smaller than priced "
            f"rather than from picking a side. Evidence leans {tilt}, which skews the "
            f"short strikes and nothing else."
        )

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
