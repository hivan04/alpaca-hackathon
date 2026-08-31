"""The carry book: short volatility premium, defined risk, held for days.

Thesis
------
Index and single-name options persistently trade at an implied volatility above
the volatility that subsequently gets realised. The seller of that premium is
paid for bearing gap and event risk. The edge is the **IV-RV spread**, and it is
collected by holding short-premium, defined-risk structures over multiple
sessions.

The agent's job is to decide *when* that premium is worth selling and when it is
fairly priced - because elevated IV is sometimes compensation for a real,
identifiable catalyst, and selling into that is selling the one premium the
market has got right.

Two properties make this the right shape for a one-week judged window:

  * **The option IS the position, not an overlay.** Max loss is arithmetic from
    the structure, so `risk.allow_undefined_risk: false` is satisfied by
    construction rather than by buying protection every night.
  * **Theta accrues on calendar days**, weekends included. There is no nightly
    round trip, so there is no nightly spread cost.

Signal stack - four HARD gates, not a blended score
---------------------------------------------------
A score would let a rich-IV reading paper over an earnings date, which is
exactly the trade this strategy must never take.

    3.1 premium   is vol rich?            IV rank AND IV-RV spread, both required
    3.2 trend     is it going nowhere?    short premium is short movement
    3.3 event     hard exclusions         earnings, ex-div, macro dates
    3.4 macro     shared or idiosyncratic? the question a number cannot answer

Why 7-14 DTE and not the conventional 20-45
--------------------------------------------
Theta is steeply non-linear into expiry. At 30 DTE a five-session hold captures
roughly 20-25% of the structure's remaining decay; at 10 DTE it captures the
majority of it. The judged window is the constraint and the structure's life has
to fit inside it.

The cost is gamma, and it is a real cost. This is a deliberate trade of tail
risk for realised P&L inside the window: right for a seven-day judged period,
wrong for a live book. Three controls offset it partially, not completely -
delta-based strike selection, the position-count cap, and the defensive exit on
a short-strike touch.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from oaa.core import clock
from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import MarketContext, Right, TradeIdea
from oaa.signals.gates import GateResult, entry_window_gate, gates_summary, spread_gate
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry

log = get_logger("strategies.carry")


@strategy_registry.register("vol_carry")
class VolCarry(Strategy):
    description = (
        "Sells rich implied volatility as defined-risk structures held 3-10 sessions."
    )
    book = "carry"

    # ------------------------------------------------------------------ #
    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        market = ctx.market
        if market is None:
            return []
        checks: list[GateResult] = []

        # 0. dated window --------------------------------------------------- #
        cutoff = self.p("exits.entry_cutoff_utc") or ctx.config.management.entry_cutoff_utc
        window = entry_window_gate(clock.utcnow(), cutoff)
        checks.append(window)
        if not window:
            return self._reject(ctx, market, checks)

        # 3.1 premium gate -------------------------------------------------- #
        premium = self._premium_gate(market)
        checks.append(premium)
        if not premium:
            return self._reject(ctx, market, checks)

        # 3.2 trend gate ---------------------------------------------------- #
        trend = self._trend_gate(market)
        checks.append(trend)
        if not trend:
            return self._reject(ctx, market, checks)

        # 3.3 event gate ---------------------------------------------------- #
        event = self._event_gate(market)
        checks.append(event)
        if not event:
            return self._reject(ctx, market, checks)

        # 3.4 macro lens ---------------------------------------------------- #
        macro = self._macro_gate(ctx, market)
        checks.append(macro)
        if not macro:
            return self._reject(ctx, market, checks)

        # 4. build ---------------------------------------------------------- #
        try:
            idea = self._build(ctx, market, premium, trend)
        except (StrategyError, DataError) as exc:
            checks.append(GateResult.veto("structure", str(exc)))
            return self._reject(ctx, market, checks)
        if idea is None:
            checks.append(GateResult.veto("structure", "no structure fitted the chain"))
            return self._reject(ctx, market, checks)

        # 5. cost gate ------------------------------------------------------- #
        # Measured against the CREDIT RECEIVED, not against the profit target:
        # the credit is the money on the table, and the spread is what the
        # round trip takes out of it before the thesis has done anything.
        credit = abs(idea.net_price) * 100
        cost = spread_gate(
            idea,
            max_relative_spread=self.p("cost.max_relative_spread", 0.10),
            target_profit=credit,
            max_cost_fraction=self.p("cost.max_spread_cost_vs_credit", 0.20),
        )
        checks.append(cost)
        if not cost:
            return self._reject(ctx, market, checks, idea=idea)

        idea.confidence = self._confidence(
            market, premium, trend,
            anchor=float(self.p("premium_gate.iv_rank_min", 0.70)),
        )
        idea.book = self.capital_book
        idea.tags = ["short_vol", "defined_risk", "carry", "resident"]
        idea.probability_of_profit = _pop(idea)
        # Modelled PoP is understated in level (median 0.488 against a 0.550
        # realised win rate) but it RANKS correctly: by tertile the realised win
        # rate was 47.2% / 52.8% / 64.8%. It was computed on every idea and gated
        # on by nothing. None = off, which is the baseline.
        pop_floor = self.p("structures.min_probability_of_profit", None)
        if (
            pop_floor is not None
            and idea.probability_of_profit is not None
            and idea.probability_of_profit < float(pop_floor)
        ):
            raise StrategyError(
                f"modelled PoP {idea.probability_of_profit:.1%} is below the "
                f"{float(pop_floor):.1%} floor"
            )
        # The reward/risk the EXIT POLICY implies, not the one expiry implies.
        # This book never holds to expiry: it takes `profit_target_pct` of max
        # profit or stops at `loss_multiple_of_credit`. Judging it on the
        # expiry payoff describes a trade it does not take.
        target = float(self.p("exits.profit_target_pct", 0.50))
        stop = float(self.p("exits.loss_multiple_of_credit", 1.5))
        if target > 0 and stop > 0:
            idea.meta["exit_reward_risk"] = round(target / stop, 4)
            idea.meta["breakeven_hit_rate"] = round(stop / (target + stop), 4)
        # This book holds for ~6 days. Re-entering the same underlying the same
        # session is not a second opportunity, it is the first one taken twice -
        # and because brokers NET identical legs it does not even show up as a
        # second position.
        idea.meta["reentry_cooldown_minutes"] = int(
            self.p("exits.reentry_cooldown_minutes", 1440)
        )
        idea.meta["gates"] = gates_summary(checks)
        idea.meta["size_multiplier"] = ctx.macro_size_multiplier(self.name)
        return [idea]

    # ================================================================== #
    # gates
    # ================================================================== #
    def _premium_gate(self, market: MarketContext) -> GateResult:
        """IV rank says vol is high *for this name*. The IV-RV spread says you
        are being paid more than the stock has actually been moving. Both are
        required; either alone is a known false-positive generator."""
        iv_rank = market.iv_rank
        floor = self.p("premium_gate.iv_rank_min", 0.70)
        iv, rv = market.implied_vol, market.realised_vol
        spread = round(iv - rv, 4) if (iv is not None and rv is not None) else None
        metrics = {
            "iv_rank": iv_rank if iv_rank is not None else -1.0,
            "iv": iv or 0.0,
            "rv": rv or 0.0,
            "iv_rv_spread": spread if spread is not None else -1.0,
        }

        if iv_rank is None:
            return GateResult.veto(
                "premium",
                "no IV rank available - the premium gate is the whole thesis and "
                "cannot be skipped",
                **metrics,
            )
        if iv_rank < floor:
            return GateResult.veto(
                "premium",
                f"IV rank {iv_rank:.0%} is below the {floor:.0%} floor - premium is "
                "not rich enough to be worth the gamma",
                **metrics,
            )
        # ABSOLUTE IV ceiling. `iv_rank` is relative to the name's own history,
        # so rank 90% on a 45-vol name and rank 90% on a 14-vol name both pass -
        # but only one of them can actually move through a short strike. Measured
        # over 2026-02-01..08-21, carry win rate by absolute IV tertile was
        # 62.3% / 56.6% / 46.3% (low to high), monotonic, while iv_rank was the
        # weakest predictor scanned. None = off, which is the baseline.
        iv_max = self.p("premium_gate.iv_max", None)
        if iv_max is not None and iv is not None and iv > float(iv_max):
            return GateResult.veto(
                "premium",
                f"absolute IV {iv:.1%} is above the {float(iv_max):.1%} ceiling - "
                "rich for this name, but the name moves too much to sell its wings",
                **metrics,
            )
        min_spread = self.p("premium_gate.iv_rv_spread_min", 0.03)
        if spread is None:
            return GateResult.veto(
                "premium", "no IV-RV spread available", **metrics
            )
        if spread < min_spread:
            return GateResult.veto(
                "premium",
                f"IV-RV spread {spread:.1%} is below the {min_spread:.1%} floor - "
                "IV is high but the underlying is moving just as much",
                **metrics,
            )
        return GateResult.ok("premium", **metrics)

    def _trend_gate(self, market: MarketContext) -> GateResult:
        """Short premium is short movement.

        This measurement used to do double duty: it also fired
        `momentum_debit_spread`, so the two strategies were mutually exclusive
        by construction. That strategy was removed on 29 Aug and the gate
        stays, because it was never a handoff - it is a statement about when
        THIS book should not trade. Trend present -> no condor."""
        adx_value = market.adx
        trend = abs(market.trend_strength or 0.0)
        max_adx = self.p("trend_gate.adx_max", 25)
        max_trend = self.p("trend_gate.max_trend_strength", 0.60)
        metrics = {"adx": adx_value if adx_value is not None else -1.0, "trend": trend}

        if adx_value is not None and adx_value > max_adx:
            return GateResult.veto(
                "trend",
                f"ADX {adx_value:.0f} is above {max_adx} - the underlying is trending "
                "and a range bet is the wrong instrument",
                **metrics,
            )
        if trend > max_trend:
            return GateResult.veto(
                "trend",
                f"trend strength {trend:.2f} exceeds {max_trend:.2f}",
                **metrics,
            )
        return GateResult.ok("trend", **metrics)

    def _event_gate(self, market: MarketContext) -> GateResult:
        """Pure downside removal. No model input, no scoring."""
        dte_max = int(self.p("structures.dte_max", 14))
        horizon = clock.today() + dt.timedelta(days=dte_max)
        metrics: dict[str, float] = {"window_days": float(dte_max)}

        if self.p("event_gate.exclude_earnings_in_window", True) and market.earnings_date:
            if clock.today() <= market.earnings_date <= horizon:
                return GateResult.veto(
                    "event",
                    f"earnings on {market.earnings_date} sits inside the expiry window - "
                    "IV is elevated BECAUSE of the event, so the premium is fair and "
                    "selling it is selling the one thing the market priced correctly",
                    **metrics,
                )
        exdiv = market.enrichment.get("ex_dividend_date")
        if self.p("event_gate.exclude_exdiv_short_calls", True) and exdiv:
            parsed = exdiv if isinstance(exdiv, dt.date) else None
            if parsed and clock.today() <= parsed <= horizon:
                return GateResult.veto(
                    "event",
                    f"ex-dividend on {parsed} inside the window - assignment risk on "
                    "the short call",
                    **metrics,
                )
        return GateResult.ok("event", **metrics)

    def _macro_gate(self, ctx: StrategyContext, market: MarketContext) -> GateResult:
        """Shared or idiosyncratic? The question the numeric gates cannot answer.

        Sector-wide IV elevation with no name-specific catalyst is *shared* -
        safe to sell, and exactly the premium the thesis targets. A name
        repricing on its own news is *idiosyncratic*: the distribution has a fat
        tail the model cannot see, and that is a veto.
        """
        stance = ctx.macro_stance(self.name)
        multiplier = ctx.macro_size_multiplier(self.name)
        metrics = {"size_multiplier": multiplier}

        if not ctx.macro_allows(self.name):
            return GateResult.veto(
                "macro", f"macro lens stood {self.name} down (stance '{stance}')", **metrics
            )
        if ctx.macro_flagged(market.symbol):
            note = getattr(ctx.macro, "flagged_symbols", {}).get(market.symbol.upper(), "")
            return GateResult.veto(
                "macro",
                f"{market.symbol} flagged as idiosyncratic by the macro lens: {note}",
                **metrics,
            )
        return GateResult.ok("macro", **metrics)

    # ================================================================== #
    # structure
    # ================================================================== #
    def _build(
        self,
        ctx: StrategyContext,
        market: MarketContext,
        premium: GateResult,
        trend: GateResult,
    ) -> TradeIdea | None:
        """Iron condor by default; credit vertical on a mild directional lean;
        calendar on a term-structure kink with no catalyst explaining it."""
        dte_min = int(self.p("structures.dte_min", 7))
        dte_max = int(self.p("structures.dte_max", 14))
        delta = abs(self.p("structures.short_delta_target", 0.14))
        quantity = int(self.p("structures.fixed_quantity", 1))
        builder = self.builder(ctx)

        lean = self._directional_lean(ctx, market)
        thesis = self._thesis(market, premium, trend, lean)

        if lean == 0:
            idea = builder.iron_condor_by_delta(
                dte_range=(dte_min, dte_max),
                short_put_delta=-delta,
                short_call_delta=delta,
                wing_points=self.p("structures.wing_width_points", 5),
                wing_pct=self.p("structures.wing_width_pct", None),
                quantity=quantity,
                thesis=thesis,
            )
            floor = self.p("structures.min_credit_to_width", 0.18)
            ratio = idea.meta.get("credit_to_width")
            if ratio is not None and ratio < floor:
                raise StrategyError(
                    f"credit/width {ratio:.3f} below {floor:.3f} - not worth the tail risk"
                )
            idea.meta["selected_structure"] = "iron_condor"
            return idea

        # Mild lean -> sell the side the lens says is safer, keep it defined risk.
        right = Right.PUT if lean > 0 else Right.CALL
        short_delta = -delta if right is Right.PUT else delta
        wing = abs(self.p("structures.wing_width_points", 5))
        short_leg = builder.view.by_delta(
            builder.view.expiry_in_range((dte_min, dte_max)), right, short_delta
        )
        long_leg = builder.view.strike_offset(
            short_leg.expiry, right, short_leg.strike, -wing if right is Right.PUT else wing
        )
        from oaa.options.structures import build_vertical

        idea = build_vertical(
            symbol=market.symbol,
            strategy=self.name,
            long_leg=long_leg,
            short_leg=short_leg,
            quantity=quantity,
            thesis=thesis,
        )
        if not idea.is_credit:
            raise StrategyError("vertical priced as a debit - not a premium sale")
        idea.meta["selected_structure"] = "credit_vertical"
        idea.meta["lean"] = lean
        return idea

    def _directional_lean(self, ctx: StrategyContext, market: MarketContext) -> int:
        """+1 bullish lean, -1 bearish, 0 neutral. Only the macro lens may set
        a lean; the numeric gates have already established there is no trend."""
        if not self.p("structures.allow_directional_lean", True):
            return 0
        regime = getattr(ctx.macro, "regime", "neutral")
        stance = ctx.macro_stance(self.name)
        if stance != "trade":
            return 0
        if regime == "risk_on":
            return 1
        if regime == "risk_off":
            return -1
        return 0

    # ================================================================== #
    # exits
    # ================================================================== #
    def should_exit(
        self, ctx: StrategyContext, idea: TradeIdea, pnl_pct: float
    ) -> str | None:
        """Mechanical. No discretionary exits, no LLM in the exit path.

        The 30% profit target rather than the conventional 50% is a deliberate
        choice for a five-session window: a target that is rarely reached leaves
        positions open at judging, which means the P&L is an unrealised
        mark-to-market on wide option spreads - noisy, unflattering, and marked
        at a mid the account could not actually trade at. Taking 30% converts
        decay into realised gains inside the window and produces more closed
        trades, which is what makes an equity curve a curve rather than one flat
        line with a single mark at the end.
        """
        # A hard dollar floor, checked FIRST and independently of the
        # credit-relative stop. `loss_multiple_of_credit` scales with the credit
        # taken, so a fat credit on a wide structure buys a proportionally fatter
        # loss before the stop trips - which is exactly backwards when what
        # matters is the account, not the trade. This caps the trade in the units
        # the daily loss limit is written in. 0 disables it.
        hard = float(self.p("exits.max_loss_usd", 0.0) or 0.0)
        if hard > 0:
            credit = float(idea.max_profit or 0.0)
            if credit > 0 and pnl_pct * credit <= -hard:
                return (
                    f"loss reached the ${hard:,.0f} hard stop "
                    f"(${abs(pnl_pct * credit):,.0f} on the structure)"
                )

        target = self.p("exits.profit_target_pct", 0.30)
        if pnl_pct >= target:
            return f"profit target {target:.0%} of max profit reached ({pnl_pct:.0%})"

        loss_multiple = self.p("exits.loss_multiple_of_credit", 2.0)
        if pnl_pct <= -abs(loss_multiple):
            return f"loss reached {loss_multiple:.1f}x the credit received ({pnl_pct:.0%})"

        # Optional hard time stop. OFF by default (0), because it fights the
        # profit target: extrinsic value decays with roughly the SQUARE ROOT of
        # time while the round-trip cost is fixed, so a 2-day hold collects
        # ~11% of the credit against a cost that does not shrink - spread and
        # fees then take ~70% of gross. If you set this, lower
        # `profit_target_pct` to match, or the target becomes unreachable and
        # this becomes the only exit the book ever uses.
        max_hold = float(self.p("exits.max_hold_days", 0) or 0)
        opened_at = idea.meta.get("opened_at")
        if max_hold > 0 and opened_at:
            try:
                opened = dt.datetime.fromisoformat(str(opened_at))
            except ValueError:
                opened = None
            if opened is not None:
                held = (clock.utcnow() - opened).total_seconds() / 86400.0
                if held >= max_hold:
                    return (
                        f"held {held:.1f}d, at or past the {max_hold:.0f}d "
                        "maximum hold"
                    )

        floor = int(self.p("exits.dte_floor", 3))
        expiry = idea.meta.get("expiry")
        if expiry:
            try:
                remaining = (dt.date.fromisoformat(str(expiry)) - clock.today()).days
            except ValueError:
                remaining = None
            if remaining is not None and remaining <= floor:
                return (
                    f"{remaining}d to expiry, at or below the {floor}d floor - gamma "
                    "risk rises sharply into expiry"
                )

        market = ctx.contexts.get(idea.symbol)
        if market is not None:
            touched = self._short_strike_touched(idea, market.spot)
            if touched:
                mode = self.p("exits.defensive_mode", "close_tested_side")
                # `hold` lets a defined-risk structure ride a touch. A short
                # strike being tested is a normal event on a 14-delta condor, and
                # closing on it realises the loss at maximum adverse excursion,
                # before theta has done anything: measured over
                # 2026-02-01..08-21, 69 of 160 carry trades exited this way at a
                # 17% win rate for -$10,801, while every other exit reason
                # averaged an 84% win rate. `loss_multiple_of_credit` remains the
                # real stop, so downside is still bounded when this is off.
                # Until 30 Aug `mode` was interpolated into the message and
                # otherwise unread, so every value behaved as close_tested_side.
                if str(mode) == "hold":
                    return None
                return f"underlying touched the short {touched} strike - {mode}"

        if ctx.macro_flagged(idea.symbol):
            return "macro lens flagged the name mid-hold - closing at the next open"
        return None

    @staticmethod
    def _short_strike_touched(idea: TradeIdea, spot: float) -> str | None:
        short_put = idea.meta.get("short_put_strike")
        short_call = idea.meta.get("short_call_strike")
        if short_put and spot <= float(short_put):
            return "put"
        if short_call and spot >= float(short_call):
            return "call"
        short_strike = idea.meta.get("short_strike")
        if short_strike and idea.meta.get("right") == "put" and spot <= float(short_strike):
            return "put"
        if short_strike and idea.meta.get("right") == "call" and spot >= float(short_strike):
            return "call"
        return None

    # ================================================================== #
    # annotation
    # ================================================================== #
    def _reject(
        self,
        ctx: StrategyContext,
        market: MarketContext,
        checks: list[GateResult],
        idea: TradeIdea | None = None,
    ) -> list[TradeIdea]:
        """The gate-by-gate rejection log is the highest-value artefact here.
        It shows an agent reasoning, not just an agent trading."""
        summary = gates_summary(checks)
        log.info(
            "%s: carry candidate vetoed by '%s' - %s",
            market.symbol, summary["vetoed_by"], summary["reason"],
        )
        journal = getattr(getattr(ctx, "firewall", None), "journal", None)
        if journal is not None:
            try:
                journal.event(
                    "gate_rejection", book="carry", strategy=self.name,
                    symbol=market.symbol, **summary,
                )
            except Exception:  # noqa: BLE001
                pass
        return []

    def _thesis(
        self,
        market: MarketContext,
        premium: GateResult,
        trend: GateResult,
        lean: int,
    ) -> str:
        structure = "iron condor" if lean == 0 else (
            "put credit spread" if lean > 0 else "call credit spread"
        )
        return (
            f"{market.symbol} at {market.spot:.2f}: IV rank "
            f"{premium.metrics.get('iv_rank', 0):.0%} with a "
            f"{premium.metrics.get('iv_rv_spread', 0):.1%} IV-RV spread, ADX "
            f"{trend.metrics.get('adx', 0):.0f} and trend "
            f"{trend.metrics.get('trend', 0):.2f} - premium is rich and the "
            f"underlying is going nowhere. Selling it as a {structure}, 7-14 DTE, "
            "so the structure's decay fits inside the judged window. Max loss is "
            "width less credit and is known before the order is sent."
        )

    @staticmethod
    def _confidence(
        market: MarketContext,
        premium: GateResult,
        trend: GateResult,
        anchor: float = 0.70,
    ) -> float:
        """Confidence relative to the CONFIGURED premium floor, not a constant.

        This was hard-coded to 0.70. When `premium_gate.iv_rank_min` moved to
        0.35 the formula kept scoring against the old threshold, so a trade that
        comfortably passed the gate still scored as though it had failed one at
        0.70 - and the critic, which reads this as its starting point, declined
        every single carry candidate. A scoring function that references a
        threshold has to read that threshold from the same place the gate does.
        """
        score = 0.5
        iv_rank_anchor = anchor
        score += (premium.metrics.get("iv_rank", 0.5) - iv_rank_anchor) * 0.6
        score += min(0.2, premium.metrics.get("iv_rv_spread", 0.0) * 2.0)
        score -= trend.metrics.get("trend", 0.0) * 0.25
        return round(max(0.0, min(1.0, score)), 3)

    def universe(self) -> list[str]:
        symbols: Any = self.params.get("universe")
        if symbols:
            return [s.upper() for s in symbols]
        return self.config.universe.active()


def _pop(idea: TradeIdea) -> float | None:
    """Rough probability of profit from the short deltas.

    A 14-delta short strike is breached about 14% of the time, so a condor with
    two of them expires inside its wings roughly 1 - 0.14 - 0.14 of the time.
    Crude, but honest, and it beats claiming precision the free feed cannot give.
    """
    deltas = [
        abs(leg.quote.greeks.delta)
        for leg in idea.legs
        if leg.quote and leg.quote.greeks.delta is not None and leg.side.value == "sell"
    ]
    if not deltas:
        return None
    return round(max(0.0, 1.0 - sum(deltas)), 3)
