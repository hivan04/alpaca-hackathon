"""The opportunistic book: event premium mispricing.

Dormant by default. It fires only on a specific, dated condition and it may not
fire at all inside the judged window. **That is an acceptable outcome and is
reported as such** - an agent that correctly stands down is demonstrating
judgement, and a module presented as designed-and-dormant reads far better than
one quietly deleted because it never triggered.

The trade
---------
For a known, scheduled catalyst inside the window (a macro print, a scheduled
release), compare the implied overnight move priced into the front expiry
against the distribution of *realised* moves for that same recurring event
historically.

    implied materially above historical realised   sell the event premium via a
                                                   defined-risk structure on an
                                                   index proxy (tight spreads)
    implied in line or below                       STAND DOWN - and this is the
                                                   expected outcome most of the time

Constraints, all hard:

  * index products only (SPY / QQQ) - single-name spreads are too wide to pay
    for a short hold
  * capped at `opportunistic.max_risk` (default 2% of equity)
  * never opened while the carry book is at its aggregate risk cap
  * requires the macro lens to return `guidance: trade`

The historical realised distribution is a committed config table rather than a
live fetch, for the same reason the macro calendar is: a live dependency that
fails on the morning of the print fails exactly when it was needed.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import MarketContext, Right, TradeIdea
from oaa.signals.gates import GateResult, gates_summary, spread_gate
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry

log = get_logger("strategies.event")


@strategy_registry.register("event_premium")
class EventPremium(Strategy):
    description = (
        "Sells scheduled-event premium on index proxies when implied is materially "
        "above the historical realised move. Dormant unless a print is due."
    )
    book = "opportunistic"

    # ------------------------------------------------------------------ #
    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        market = ctx.market
        if market is None:
            return []
        checks: list[GateResult] = []
        now = dt.datetime.now(dt.timezone.utc)

        # 1. is a qualifying print actually due? ---------------------------- #
        event = self._due_event(ctx, now)
        if event is None:
            checks.append(GateResult.veto(
                "scheduled_event",
                "no qualifying scheduled catalyst inside the horizon - the "
                "opportunistic book stands down, which is the expected outcome",
            ))
            return self._reject(ctx, market, checks)
        checks.append(GateResult.ok("scheduled_event", hours_out=event["hours_out"]))

        # 2. macro lens must actively say trade ------------------------------ #
        stance = ctx.macro_stance(self.name)
        if stance != "trade":
            checks.append(GateResult.veto(
                "macro", f"macro lens returned '{stance}', not 'trade'"
            ))
            return self._reject(ctx, market, checks)
        checks.append(GateResult.ok("macro"))

        # 3. the mispricing test --------------------------------------------- #
        mispricing = self._mispricing_gate(market, event)
        checks.append(mispricing)
        if not mispricing:
            return self._reject(ctx, market, checks)

        # 4. build ------------------------------------------------------------ #
        try:
            idea = self._build(ctx, market, event, mispricing)
        except (StrategyError, DataError) as exc:
            checks.append(GateResult.veto("structure", str(exc)))
            return self._reject(ctx, market, checks)

        # Against the CREDIT RECEIVED, as in the carry book: the credit is the
        # money on the table and the round trip takes its cut before the event
        # has even happened.
        credit = abs(idea.net_price) * 100
        cost = spread_gate(
            idea,
            max_relative_spread=self.p("cost.max_relative_spread", 0.06),
            target_profit=credit,
            max_cost_fraction=self.p("cost.max_spread_cost_vs_credit", 0.20),
        )
        checks.append(cost)
        if not cost:
            return self._reject(ctx, market, checks, idea=idea)

        idea.book = self.capital_book
        idea.tags = ["short_vol", "defined_risk", "event", "opportunistic"]
        idea.meta["gates"] = gates_summary(checks)
        idea.meta["event"] = event
        idea.confidence = round(
            min(1.0, 0.45 + mispricing.metrics.get("premium_ratio", 1.0) - 1.0), 3
        )
        return [idea]

    # ================================================================== #
    def _due_event(self, ctx: StrategyContext, now: dt.datetime) -> dict[str, Any] | None:
        engine = getattr(ctx, "catalyst", None)
        calendar = getattr(engine, "calendar", None)
        if calendar is None:
            return None
        horizon_hours = float(self.p("event.horizon_hours", 30))
        for candidate in calendar.events:
            hours_out = candidate.minutes_from(now) / 60.0
            if 0 <= hours_out <= horizon_hours and candidate.importance == "high":
                return {
                    "name": candidate.name,
                    "kind": candidate.kind,
                    "when": candidate.when.isoformat(),
                    "hours_out": round(hours_out, 2),
                }
        return None

    def _mispricing_gate(
        self, market: MarketContext, event: dict[str, Any]
    ) -> GateResult:
        """Implied move from the front-expiry ATM straddle vs the historical
        realised distribution for this recurring event."""
        implied = self._implied_move(market)
        history = [
            abs(float(v))
            for v in (self.p(f"event.realised_moves.{event['kind']}") or [])
        ]
        metrics = {
            "implied_move": implied if implied is not None else -1.0,
            "samples": float(len(history)),
        }

        if implied is None:
            return GateResult.veto(
                "mispricing", "could not price the front-expiry ATM straddle", **metrics
            )
        if len(history) < int(self.p("event.min_samples", 6)):
            return GateResult.veto(
                "mispricing",
                f"only {len(history)} historical realised moves for '{event['kind']}' - "
                "not enough to claim the premium is mispriced",
                **metrics,
            )

        realised = statistics.median(history)
        ratio = round(implied / realised, 4) if realised > 0 else 0.0
        metrics.update({"realised_median": round(realised, 5), "premium_ratio": ratio})
        floor = float(self.p("event.min_premium_ratio", 1.25))
        if ratio < floor:
            return GateResult.veto(
                "mispricing",
                f"implied {implied:.2%} vs historical realised median {realised:.2%} "
                f"is a ratio of {ratio:.2f}, below the {floor:.2f} floor - the event "
                "premium is in line, so there is nothing to sell",
                **metrics,
            )
        return GateResult.ok("mispricing", **metrics)

    @staticmethod
    def _implied_move(market: MarketContext) -> float | None:
        if not market.chain or market.spot <= 0:
            return None
        front = min(q.expiry for q in market.chain)
        legs = [q for q in market.chain if q.expiry == front]
        calls = [q for q in legs if q.right is Right.CALL]
        puts = [q for q in legs if q.right is Right.PUT]
        if not calls or not puts:
            return None
        call = min(calls, key=lambda q: abs(q.strike - market.spot))
        put = min(puts, key=lambda q: abs(q.strike - market.spot))
        if call.mid is None or put.mid is None:
            return None
        return round((call.mid + put.mid) / market.spot, 6)

    def _build(
        self,
        ctx: StrategyContext,
        market: MarketContext,
        event: dict[str, Any],
        mispricing: GateResult,
    ) -> TradeIdea:
        dte = tuple(self.p("structures.target_dte", [1, 5]))
        idea = self.builder(ctx).iron_condor_by_delta(
            dte_range=(int(dte[0]), int(dte[1])),
            short_put_delta=-abs(self.p("structures.short_delta_target", 0.20)),
            short_call_delta=abs(self.p("structures.short_delta_target", 0.20)),
            wing_points=self.p("structures.wing_width_points", 5),
            quantity=int(self.p("structures.fixed_quantity", 1)),
            thesis=(
                f"{event['name']} lands in {event['hours_out']:.1f}h. The front expiry "
                f"prices a {mispricing.metrics['implied_move']:.2%} move; the median "
                f"realised move across {int(mispricing.metrics['samples'])} prior "
                f"{event['kind']} events is "
                f"{mispricing.metrics['realised_median']:.2%} - a "
                f"{mispricing.metrics['premium_ratio']:.2f}x premium. Selling it as a "
                "defined-risk condor on an index proxy, where the quote is tight "
                "enough for a short hold to pay."
            ),
        )
        idea.meta["selected_structure"] = "event_iron_condor"
        return idea

    # ================================================================== #
    def _reject(
        self,
        ctx: StrategyContext,
        market: MarketContext,
        checks: list[GateResult],
        idea: TradeIdea | None = None,
    ) -> list[TradeIdea]:
        summary = gates_summary(checks)
        log.info(
            "%s: opportunistic candidate stood down at '%s' - %s",
            market.symbol, summary["vetoed_by"], summary["reason"],
        )
        journal = getattr(getattr(ctx, "firewall", None), "journal", None)
        if journal is not None:
            try:
                journal.event(
                    "gate_rejection", book="opportunistic", strategy=self.name,
                    symbol=market.symbol, **summary,
                )
            except Exception:  # noqa: BLE001
                pass
        return []

    def universe(self) -> list[str]:
        symbols: Any = self.params.get("universe")
        return [s.upper() for s in (symbols or ["SPY", "QQQ"])]
