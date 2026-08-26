"""The intraday book: momentum expressed through options.

Thesis, stated honestly
-----------------------
This is a **momentum strategy expressed through options**. The option is
leverage and defined risk; it is not the source of edge. The edge, such as it
is, comes from short-horizon directional continuation.

That framing is deliberate. Presenting this as a vol strategy invites the
question "which vol premium are you harvesting?", and there isn't one. An
honest "directional, expressed through options for convex payoff and bounded
loss" is a stronger position than a mislabelled one.

    How this differs from the carry book
    ------------------------------------------------------------------
    edge source       IV-RV premium          directional continuation
    direction         neutral                directional by construction
    option role       the position           leverage + loss bound
    holding period    3-10 sessions          minutes to hours, flat by 15:15
    primary risk      short gamma            spread cost and signal decay

The two are uncorrelated in signal and opposite in vol exposure, which is the
genuine portfolio argument for running both.

The hard constraint: spread cost
--------------------------------
Target profit is 5-15% of premium. On a $2.00 ATM option that is $10-30 per
contract. A $0.10-wide single-name quote costs $20 round trip - the entire
target. **Index products only**, and that is arithmetic, not preference. This
is not a universe-wide momentum system; it is a two-symbol system because those
are the only two symbols where the sums work.

Signal stack
------------
    3.1 momentum   VWAP trigger / Bollinger WIDTH filter / RSI veto  (see
                   oaa.data.indicators for why each gets a different question)
    3.2 catalyst   news, breadth, volume participation - the non-generic layer
    3.3 spread     mandatory, and expected to reject more than anything else
    3.4 time       09:45-14:45, lunch skipped
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import MarketContext, Right, TradeIdea
from oaa.data.indicators import (
    atr,
    bollinger_width_series,
    closes,
    crossed,
    persistence,
    rsi,
    volume_zscore_by_bucket,
    vwap_series,
    width_is_rising,
    width_percentile,
)
from oaa.options.selection import Selection, select
from oaa.signals.gates import GateResult, gates_summary, spread_gate, time_gate
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry

log = get_logger("strategies.intraday")


@strategy_registry.register("intraday_momentum")
class IntradayMomentum(Strategy):
    description = (
        "Catalyst-confirmed VWAP momentum on index options, flat by the 15:15 cutoff."
    )
    book = "intraday"

    # ------------------------------------------------------------------ #
    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        market = ctx.market
        if market is None:
            return []
        checks: list[GateResult] = []
        now_et = self._now_et(ctx)

        # 3.4 time-of-day gate (cheapest, so it runs first) ------------------ #
        clock = time_gate(
            now_et,
            no_entry_before=self.p("time_gate.no_entry_before", "09:45"),
            no_entry_after=self.p("time_gate.no_entry_after", "14:45"),
            skip_lunch=bool(self.p("time_gate.skip_lunch", True)),
            lunch_window=self.p("time_gate.lunch_window", ["11:30", "13:30"]),
        )
        checks.append(clock)
        if not clock:
            return self._reject(ctx, market, checks)

        bars = market.intraday_bars or []
        if len(bars) < self.p("momentum.min_bars", 30):
            checks.append(GateResult.veto(
                "data", f"only {len(bars)} intraday bars - not enough to compute VWAP "
                "and band width reliably"
            ))
            return self._reject(ctx, market, checks)

        # 3.1 momentum trigger ---------------------------------------------- #
        momentum = self._momentum_gate(market, bars)
        checks.append(momentum)
        if not momentum:
            return self._reject(ctx, market, checks)
        bullish = momentum.metrics.get("direction", 0) > 0

        # 3.2 catalyst confirmation ------------------------------------------ #
        catalyst = self._catalyst_gate(ctx, market, bullish, now_et)
        checks.append(catalyst)
        if not catalyst:
            return self._reject(ctx, market, checks)

        # macro overlay: reduce, or stand down entirely ----------------------- #
        if not ctx.macro_allows(self.name):
            checks.append(GateResult.veto(
                "macro", f"macro lens stood {self.name} down for this session"
            ))
            return self._reject(ctx, market, checks)

        # 4. option selection ------------------------------------------------- #
        selection = self._select(market, bars, now_et)
        checks.append(
            GateResult.ok("selection", expected_move=selection.expected_move_pct)
            if selection.tradable
            else GateResult.veto("selection", selection.reason)
        )
        if not selection.tradable:
            return self._reject(ctx, market, checks)

        try:
            idea = self._build(ctx, market, selection, bullish, momentum, catalyst)
        except (StrategyError, DataError) as exc:
            checks.append(GateResult.veto("structure", str(exc)))
            return self._reject(ctx, market, checks)

        # 3.3 spread gate - mandatory ------------------------------------------ #
        premium = abs(idea.net_price) * 100
        target = premium * self.p("exits.target_pct_of_premium", 0.10)
        cost = spread_gate(
            idea,
            max_relative_spread=self.p("spread_gate.max_relative_spread", 0.02),
            target_profit=target,
            max_cost_fraction=self.p("spread_gate.spread_cost_fraction_of_target", 0.30),
        )
        checks.append(cost)
        if not cost:
            return self._reject(ctx, market, checks, idea=idea)

        idea.confidence = round(
            min(1.0, 0.40 + 0.35 * catalyst.metrics.get("score", 0.0)
                + 0.25 * min(1.0, abs(momentum.metrics.get("volume_z", 0.0)) / 2)), 3
        )
        idea.book = self.capital_book
        idea.tags = [
            "momentum", "defined_risk", "intraday",
            "bullish" if bullish else "bearish",
        ]
        idea.meta["gates"] = gates_summary(checks)
        idea.meta["selection"] = selection.as_dict()
        idea.meta["target_profit"] = round(target, 2)
        idea.meta["breakeven_hit_rate"] = round(
            abs(self.p("exits.stop_pct_of_premium", 0.15))
            / (abs(self.p("exits.stop_pct_of_premium", 0.15))
               + self.p("exits.target_pct_of_premium", 0.10)), 4
        )
        idea.meta["opened_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        idea.meta["size_multiplier"] = ctx.macro_size_multiplier(self.name)
        return [idea]

    # ================================================================== #
    # 3.1 momentum
    # ================================================================== #
    def _momentum_gate(self, market: MarketContext, bars: list[dict]) -> GateResult:
        """VWAP decides direction. Band width decides whether the move is real.
        RSI can only veto. Because the three are non-overlapping, this fires at
        a usable rate rather than almost never - which is the failure mode of a
        naive three-way agreement gate."""
        px = closes(bars)
        anchor = vwap_series(bars)
        if not px or not anchor:
            return GateResult.veto("momentum", "could not compute session VWAP")

        cross = crossed(px, anchor, int(self.p("momentum.cross_lookback_bars", 3)))
        band_mult = self.p("momentum.vwap_band_atr_mult", 0.25)
        atr_value = atr(bars, self.p("momentum.atr_period", 14)) or 0.0
        distance = px[-1] - anchor[-1]
        bounce = 0
        if atr_value > 0 and abs(distance) <= band_mult * atr_value:
            # Sitting on the VWAP band: a bounce, direction taken from the last
            # persistent side rather than from a single bar.
            bounce = 1 if persistence(px, anchor, 3) > 0 else -1

        direction = cross or bounce
        metrics = {
            "direction": float(direction),
            "vwap": anchor[-1],
            "close": px[-1],
            "atr": atr_value,
        }
        if direction == 0:
            return GateResult.veto(
                "momentum", "no VWAP cross and price is not on the band", **metrics
            )

        # volume confirmation, bucketed by time of day
        volume_z = volume_zscore_by_bucket(
            bars, bucket_minutes=int(self.p("momentum.volume_bucket_minutes", 30))
        )
        metrics["volume_z"] = volume_z if volume_z is not None else -99.0
        floor = self.p("momentum.volume_zscore_min", 1.0)
        if volume_z is None:
            if self.p("momentum.require_volume", True):
                return GateResult.veto(
                    "momentum",
                    "no time-of-day volume baseline yet - the volume gate cannot be "
                    "evaluated (it accumulates from live data)",
                    **metrics,
                )
        elif volume_z < floor:
            return GateResult.veto(
                "momentum",
                f"volume z-score {volume_z:.2f} for this time-of-day bucket is below "
                f"{floor:.2f} - the move is not being participated in",
                **metrics,
            )

        # persistence: reject the single-bar spike that immediately reverts
        need = int(self.p("momentum.persistence_bars", 2))
        held = persistence(px, anchor, need)
        metrics["persistence"] = float(held)
        if (direction > 0 and held < need) or (direction < 0 and held > -need):
            return GateResult.veto(
                "momentum",
                f"only {abs(held)} of the last {need} bars held the direction - "
                "single-bar spikes revert",
                **metrics,
            )

        # Bollinger WIDTH as the regime filter. Width, not position: position is
        # a mean-reversion signal and would fight the VWAP trigger on every
        # single candidate.
        widths = bollinger_width_series(
            px,
            period=int(self.p("momentum.bb_period", 20)),
            num_std=float(self.p("momentum.bb_std", 2.0)),
        )
        lookback = int(self.p("momentum.width_lookback_bars", 6))
        metrics["band_width"] = widths[-1] if widths else -1.0
        if self.p("momentum.require_width_rising", True) and not width_is_rising(widths, lookback):
            return GateResult.veto(
                "momentum",
                "Bollinger band width is not expanding - contracting bands during a "
                "VWAP cross is chop, and chop pays the spread for nothing",
                **metrics,
            )
        if self.p("momentum.require_prior_squeeze", False):
            pct = width_percentile(widths)
            metrics["width_percentile"] = pct if pct is not None else -1.0
            floor_pct = float(self.p("momentum.squeeze_percentile", 25)) / 100
            if pct is not None and pct < floor_pct:
                return GateResult.veto(
                    "momentum",
                    f"no prior squeeze: width percentile {pct:.0%} never compressed "
                    f"below {floor_pct:.0%}",
                    **metrics,
                )

        # RSI: one-sided veto at extremes only. RSI at 65 blocks nothing.
        rsi_value = rsi(px, int(self.p("momentum.rsi_period", 14)))
        metrics["rsi"] = rsi_value if rsi_value is not None else -1.0
        if rsi_value is not None:
            upper = float(self.p("momentum.rsi_veto_upper", 80))
            lower = float(self.p("momentum.rsi_veto_lower", 20))
            if direction > 0 and rsi_value > upper:
                return GateResult.veto(
                    "momentum",
                    f"RSI {rsi_value:.0f} above the {upper:.0f} exhaustion veto - the "
                    "move has already run",
                    **metrics,
                )
            if direction < 0 and rsi_value < lower:
                return GateResult.veto(
                    "momentum",
                    f"RSI {rsi_value:.0f} below the {lower:.0f} exhaustion veto",
                    **metrics,
                )
        return GateResult.ok("momentum", **metrics)

    # ================================================================== #
    # 3.2 catalyst
    # ================================================================== #
    def _catalyst_gate(
        self,
        ctx: StrategyContext,
        market: MarketContext,
        bullish: bool,
        now_et: dt.datetime,
    ) -> GateResult:
        engine = getattr(ctx, "catalyst", None)
        if engine is None:
            if self.p("catalyst_gate.required", True):
                return GateResult.veto(
                    "catalyst", "no catalyst engine wired into this cycle"
                )
            return GateResult.ok("catalyst")

        view = engine.view(
            market.symbol,
            now=dt.datetime.now(dt.timezone.utc),
            news=market.news,
            snapshot=getattr(ctx, "attention", None),
        )
        result = engine.gate(
            view,
            bullish=bullish,
            min_headlines=int(self.p("catalyst_gate.min_headlines", 1)),
            relevance_floor=float(self.p("catalyst_gate.relevance_floor", 0.5)),
            breadth_min=float(self.p("catalyst_gate.breadth_min", 0.60)),
            required=bool(self.p("catalyst_gate.required", True)),
        )
        result.metrics["score"] = view.score
        return result

    # ================================================================== #
    # 4. selection and build
    # ================================================================== #
    def _select(
        self, market: MarketContext, bars: list[dict], now_et: dt.datetime
    ) -> Selection:
        minutes_left = max(
            0.0, (15 * 60 + 15) - (now_et.hour * 60 + now_et.minute)
        )
        session_minutes = float(self.p("selection.session_minutes", 345))
        return select(
            spot=market.spot,
            atr_value=atr(market.bars, 14) if market.bars else atr(bars, 14),
            horizon_fraction=minutes_left / session_minutes if session_minutes else 1.0,
            iv=market.implied_vol,
            iv_rank=market.iv_rank,
            large_move_pct=float(self.p("selection.large_move_pct", 0.006)),
            iv_rank_no_trade_above=float(self.p("selection.iv_rank_no_trade_above", 0.85)),
            prefer_vertical_above_iv_rank=float(
                self.p("selection.prefer_vertical_above_iv_rank", 0.60)
            ),
            dte_max=int(self.p("selection.dte_max", 2)),
        )

    def _build(
        self,
        ctx: StrategyContext,
        market: MarketContext,
        selection: Selection,
        bullish: bool,
        momentum: GateResult,
        catalyst: GateResult,
    ) -> TradeIdea:
        right = Right.CALL if bullish else Right.PUT
        sign = 1.0 if bullish else -1.0
        # The global chain filter is built for the carry book's 7-45 DTE range
        # and its 12% spread ceiling. Neither fits here: this book buys 0-2 DTE
        # and its whole viability depends on a much tighter quote.
        builder = self.builder(ctx, chain_filter=ctx.default_filter(
            min_dte=0,
            max_dte=max(selection.dte_range[1], int(self.p("selection.dte_max", 2))),
            max_spread_pct=float(self.p("spread_gate.max_relative_spread", 0.02)) * 2,
            min_open_interest=int(self.p("structure.min_open_interest", 100)),
            min_volume=0,
        ))
        thesis = self._thesis(market, selection, bullish, momentum, catalyst)
        quantity = int(self.p("structure.fixed_quantity", 1))

        if selection.mode == "vertical":
            idea = builder.vertical_by_delta(
                right=right,
                dte_range=selection.dte_range,
                long_delta=sign * selection.long_delta,
                short_delta=sign * selection.short_delta,
                quantity=quantity,
                thesis=thesis,
            )
        else:
            idea = builder.single_long(
                right=right,
                dte_range=selection.dte_range,
                target_delta=sign * selection.long_delta,
                quantity=quantity,
                thesis=thesis,
            )
        if idea.is_credit:
            raise StrategyError("intraday structures are long premium by construction")
        return idea

    # ================================================================== #
    # exits
    # ================================================================== #
    def should_exit(
        self, ctx: StrategyContext, idea: TradeIdea, pnl_pct: float
    ) -> str | None:
        """Mechanical, no LLM in the exit path.

        Note the stop is WIDER than the target (15% vs 10%). Option premium is
        noisy and a tight stop gets hit by spread flicker alone. The cost is a
        demanding breakeven hit rate of ~60%, which is stated rather than hidden
        and is tracked live against the actual rate.
        """
        target = self.p("exits.target_pct_of_premium", 0.10)
        stop = abs(self.p("exits.stop_pct_of_premium", 0.15))
        if pnl_pct >= target:
            return f"target {target:.0%} of premium reached ({pnl_pct:.0%})"
        if pnl_pct <= -stop:
            return f"stop {stop:.0%} of premium hit ({pnl_pct:.0%})"

        opened = idea.meta.get("opened_at")
        limit = int(self.p("exits.time_stop_minutes", 20))
        if opened:
            try:
                started = dt.datetime.fromisoformat(str(opened))
            except ValueError:
                started = None
            if started is not None:
                held = (dt.datetime.now(dt.timezone.utc) - started).total_seconds() / 60
                if held >= limit:
                    return f"time stop: {held:.0f} minutes in trade, the signal has decayed"

        if self.p("exits.exit_on_vwap_recross", True):
            market = ctx.contexts.get(idea.symbol)
            bars = market.intraday_bars if market else []
            if bars:
                px, anchor = closes(bars), vwap_series(bars)
                if px and anchor:
                    bullish = "bullish" in idea.tags
                    if (bullish and px[-1] < anchor[-1]) or (not bullish and px[-1] > anchor[-1]):
                        return "price re-crossed VWAP against the position - thesis invalidated"
        return None

    # ================================================================== #
    def _now_et(self, ctx: StrategyContext) -> dt.datetime:
        firewall = getattr(ctx, "firewall", None)
        if firewall is not None and getattr(firewall, "clock", None) is not None:
            return firewall.clock.now()
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo("America/New_York"))

    def _reject(
        self,
        ctx: StrategyContext,
        market: MarketContext,
        checks: list[GateResult],
        idea: TradeIdea | None = None,
    ) -> list[TradeIdea]:
        summary = gates_summary(checks)
        log.info(
            "%s: intraday candidate vetoed by '%s' - %s",
            market.symbol, summary["vetoed_by"], summary["reason"],
        )
        journal = getattr(getattr(ctx, "firewall", None), "journal", None)
        if journal is not None:
            try:
                journal.event(
                    "gate_rejection", book="intraday", strategy=self.name,
                    symbol=market.symbol, **summary,
                )
            except Exception:  # noqa: BLE001
                pass
        return []

    @staticmethod
    def _thesis(
        market: MarketContext,
        selection: Selection,
        bullish: bool,
        momentum: GateResult,
        catalyst: GateResult,
    ) -> str:
        direction = "above" if bullish else "below"
        return (
            f"{market.symbol} crossed {direction} session VWAP "
            f"({momentum.metrics.get('close', 0):.2f} vs "
            f"{momentum.metrics.get('vwap', 0):.2f}) on "
            f"{momentum.metrics.get('volume_z', 0):.1f}-sigma volume for this "
            f"time-of-day bucket, with Bollinger width expanding and RSI "
            f"{momentum.metrics.get('rsi', 0):.0f} clear of the exhaustion veto. "
            f"Catalyst score {catalyst.metrics.get('score', 0):.2f}: "
            f"{int(catalyst.metrics.get('news_count', 0))} headline(s) in the window "
            f"and breadth confirming. {selection.reason}. Loss is capped at the "
            "premium paid; the position is flat by 15:15 whatever happens."
        )

    def universe(self) -> list[str]:
        symbols: Any = self.params.get("universe")
        # Index only. See the module docstring - this is arithmetic on the
        # spread, not a preference.
        return [s.upper() for s in (symbols or ["SPY", "QQQ"])]
