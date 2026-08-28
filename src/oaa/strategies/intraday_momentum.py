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
import statistics
from typing import Any

from oaa.core import clock as wallclock
from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import MarketContext, Right, TradeIdea
from oaa.data.indicators import (
    atr,
    bollinger_width_series,
    closes,
    crossed,
    persistence,
    resample,
    rsi,
    volume_zscore_by_bucket,
    vwap_series,
    width_is_rising,
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
    def chain_dte_window(self) -> tuple[int, int]:
        """0-2 DTE. This book buys the front expiry and is flat by 15:15, so
        the global 3-45 window contains nothing it can trade."""
        return 0, max(int(self.p("selection.dte_max", 2)), 0)

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

        # 3.2 catalyst - a CONFIRMATION unless explicitly made mandatory ------ #
        catalyst = self._catalyst_gate(ctx, market, bullish, now_et)
        checks.append(catalyst)
        catalyst_required = bool(self.p("catalyst_gate.required", True))
        if catalyst_required and not catalyst:
            return self._reject(ctx, market, checks)

        # 3.2b the confirmation tally ----------------------------------------
        #
        # The trigger (VWAP) decides DIRECTION and stays hard. Everything else
        # is evidence, and evidence is counted rather than required. Requiring
        # all of it was a conjunction of five to eight conditions - which fires
        # at a rate no five-session window can use, and which meant loosening
        # any one gate simply promoted the next one to being the wall.
        confirmations = int(momentum.metrics.get("confirmations", 0))
        possible = int(momentum.metrics.get("confirmations_possible", 0))
        if not catalyst_required:
            # `catalyst.passed` is not the vote: when the gate is not mandatory
            # it passes by construction and reports what it MEASURED in
            # metrics["confirmed"]. Reading the pass bit here handed the book a
            # free fifth vote on every candidate.
            possible += 1
            if float(catalyst.metrics.get("confirmed", 0.0)) >= 1.0:
                confirmations += 1
        needed = int(self.p("momentum.confirmations_required", 3))
        needed = min(needed, possible)
        if confirmations < needed:
            failed = momentum.metrics.get("confirmations_failed") or ""
            checks.append(GateResult.veto(
                "confirmation",
                f"{confirmations}/{possible} confirmations, needs {needed}"
                + (f" - {failed}" if failed else ""),
                confirmations=float(confirmations),
                required=float(needed),
            ))
            return self._reject(ctx, market, checks)
        checks.append(GateResult.ok(
            "confirmation",
            confirmations=float(confirmations), required=float(needed),
        ))

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
        idea.meta["opened_at"] = wallclock.utcnow().isoformat()
        idea.meta["size_multiplier"] = ctx.macro_size_multiplier(self.name)
        return [idea]

    # ================================================================== #
    # 3.1 momentum
    # ================================================================== #
    @staticmethod
    def _latest_session_bars(bars: list[dict]) -> list[dict]:
        return _session_slice(bars)

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
        atr_value = atr(bars, self.p("momentum.atr_period", 14)) or 0.0
        distance = px[-1] - anchor[-1]

        # The band is a multiple of how far price NORMALLY sits from VWAP this
        # session, not a multiple of one bar's range.
        #
        # Measuring it in 1-minute ATR made it meaningless: on SPY that is a
        # band of +/-$0.08, or 0.013% of spot - it asks price to be within eight
        # cents of the session average. Session dispersion is typically 0.1-0.3%
        # of spot, so the test was 10-20x too tight and "price is not on the
        # band" became 75% of every rejection this book produced. Dispersion is
        # also scale-free, which matters now the universe runs from a $50 ETF to
        # a $600 one; ATR in dollars never was.
        band = 0.0
        dispersion_mult = float(self.p("momentum.band_dispersion_mult", 0) or 0)
        if dispersion_mult > 0:
            session = _session_slice(bars)
            if len(session) >= 20:
                offset = len(px) - len(session)
                gaps = [
                    p - a for p, a in
                    zip(px[offset:], anchor[offset:], strict=False)
                ]
                if len(gaps) >= 20:
                    band = dispersion_mult * statistics.pstdev(gaps)
        if band <= 0:                       # fallback: the old ATR measure
            band = float(self.p("momentum.vwap_band_atr_mult", 0.25)) * atr_value

        bounce = 0
        if band > 0 and abs(distance) <= band:
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

        # ------------------------------------------------------------------
        # CONFIRMATIONS - scored, not vetoes.
        #
        # Volume, persistence, band width and RSI used to be four consecutive
        # hard vetoes stacked behind the VWAP trigger and the catalyst gate.
        # Eight vetoes in a row, each passing perhaps 70% of candidates, is
        # 0.7^8 = 6% - the book was ARITHMETICALLY designed almost never to
        # fire, and every time one gate was loosened the next became the wall.
        # Measured over 864 candidates: 424 died on volume alone and none
        # survived the chain.
        #
        # They ask genuinely different questions, so requiring all of them to
        # agree is a tickbox exercise, not a confluence. Each now votes;
        # `momentum.confirmations_required` decides how many must agree. The
        # DIRECTION signal (VWAP) stays hard, because without it there is no
        # trade to make.
        # ------------------------------------------------------------------
        votes: dict[str, bool] = {}
        notes: list[str] = []

        volume_z = volume_zscore_by_bucket(
            bars, bucket_minutes=int(self.p("momentum.volume_bucket_minutes", 30))
        )
        metrics["volume_z"] = volume_z if volume_z is not None else -99.0
        floor = self.p("momentum.volume_zscore_min", 1.0)
        if volume_z is None:
            # Unmeasurable is not the same as failed. It used to veto.
            notes.append("volume: no time-of-day baseline yet")
        else:
            votes["volume"] = volume_z >= floor
            if not votes["volume"]:
                notes.append(f"volume z {volume_z:.2f} < {floor:.2f}")

        need = int(self.p("momentum.persistence_bars", 2))
        held = persistence(px, anchor, need)
        metrics["persistence"] = float(held)
        votes["persistence"] = not (
            (direction > 0 and held < need) or (direction < 0 and held > -need)
        )
        if not votes["persistence"]:
            notes.append(f"only {abs(held)}/{need} bars held the direction")

        widths = bollinger_width_series(
            px,
            period=int(self.p("momentum.bb_period", 20)),
            num_std=float(self.p("momentum.bb_std", 2.0)),
        )
        lookback = int(self.p("momentum.width_lookback_bars", 6))
        metrics["band_width"] = widths[-1] if widths else -1.0
        votes["band_width"] = width_is_rising(widths, lookback)
        if not votes["band_width"]:
            notes.append("Bollinger width not expanding")

        # HIGHER TIMEFRAME. A 1-minute VWAP cross says nothing about whether it
        # is running with the hour or into it. Resampling the same bars costs
        # no extra data and answers a question none of the other confirmations
        # ask: is this move with the larger trend, or a retrace inside a move
        # going the other way? A vote, not a veto - the hour can be flat while
        # a perfectly good 30-minute move sets up underneath it.
        htf_minutes = int(self.p("momentum.higher_timeframe_minutes", 0) or 0)
        if htf_minutes > 0:
            hourly = resample(bars, htf_minutes)
            need_bars = int(self.p("momentum.higher_timeframe_bars", 3))
            hourly_px = closes(hourly)
            if len(hourly_px) > need_bars:
                reference = hourly_px[-(need_bars + 1)]
                drift = hourly_px[-1] - reference
                metrics["htf_drift_pct"] = round(
                    drift / reference * 100 if reference else 0.0, 4
                )
                votes["higher_timeframe"] = (drift > 0) if direction > 0 else (drift < 0)
                if not votes["higher_timeframe"]:
                    notes.append(
                        f"the last {need_bars} x {htf_minutes}m bars are moving "
                        "against the signal"
                    )
            else:
                notes.append(f"htf: fewer than {need_bars + 1} x {htf_minutes}m bars yet")

        rsi_value = rsi(px, int(self.p("momentum.rsi_period", 14)))
        metrics["rsi"] = rsi_value if rsi_value is not None else -1.0
        upper = float(self.p("momentum.rsi_veto_upper", 80))
        lower = float(self.p("momentum.rsi_veto_lower", 20))
        if rsi_value is None:
            notes.append("rsi: unavailable")
        else:
            votes["rsi"] = not (
                (direction > 0 and rsi_value > upper)
                or (direction < 0 and rsi_value < lower)
            )
            if not votes["rsi"]:
                notes.append(f"RSI {rsi_value:.0f} at the exhaustion veto")

        metrics["confirmations"] = float(sum(1 for v in votes.values() if v))
        metrics["confirmations_possible"] = float(len(votes))
        metrics["confirmations_failed"] = "; ".join(notes)
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
            now=wallclock.utcnow(),
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
        result.metrics["required"] = 1.0 if self.p("catalyst_gate.required", True) else 0.0
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
                held = (wallclock.utcnow() - started).total_seconds() / 60
                if held >= limit:
                    return f"time stop: {held:.0f} minutes in trade, the signal has decayed"

        # The firewall cutoff, enforced by the BOOK and not merely assumed.
        # `time_gate.no_entry_after` stops new entries at 14:45, but nothing
        # closed what was already on: a position opened at 14:45 had no later
        # scan, so a 0 DTE long ran to settlement and paid out at intrinsic.
        # An intraday book that can hold into expiry is not an intraday book.
        flat_by = str(self.p("exits.flat_by", "") or "")
        if flat_by:
            now_et = self._now_et(ctx)
            hour, _, minute = flat_by.partition(":")
            try:
                cutoff = now_et.replace(
                    hour=int(hour), minute=int(minute or 0), second=0, microsecond=0
                )
            except ValueError:
                cutoff = None
            if cutoff is not None and now_et >= cutoff:
                return (
                    f"cutoff: {flat_by} ET - this book does not hold a position "
                    "into the close or into expiry"
                )

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

        return wallclock.now(ZoneInfo("America/New_York"))

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
        """The rationale a judge reads. It must describe THIS trade.

        The previous version was written for the veto design, where reaching
        this line meant every gate had passed - so it could safely assert
        "Bollinger width expanding", "RSI clear of the exhaustion veto",
        "breadth confirming". Under a confirmation SCORE none of that follows:
        a trade opens on three votes of six, and the other three may have
        failed. Measured over 624 candidates, the catalyst confirmed in three
        of them - so "breadth confirming" would have been false on essentially
        every trade written into the journal the judges read.

        A rationale that reports what was measured, including what did NOT
        confirm, is both honest and a better argument.
        """
        direction = "above" if bullish else "below"
        confirmed = int(momentum.metrics.get("confirmations", 0))
        possible = int(momentum.metrics.get("confirmations_possible", 0))
        if not bool(catalyst.metrics.get("required", 0.0)):
            possible += 1
            confirmed += int(float(catalyst.metrics.get("confirmed", 0.0)) >= 1.0)
        missed = str(momentum.metrics.get("confirmations_failed") or "").strip()
        caveat = f" Not confirming: {missed}." if missed else ""
        return (
            f"{market.symbol} crossed {direction} session VWAP "
            f"({momentum.metrics.get('close', 0):.2f} vs "
            f"{momentum.metrics.get('vwap', 0):.2f}), the directional trigger. "
            f"{confirmed} of {possible} confirmations agree - volume "
            f"{momentum.metrics.get('volume_z', 0):.1f} sigma for this "
            f"time-of-day bucket, RSI {momentum.metrics.get('rsi', 0):.0f}, "
            f"catalyst score {catalyst.metrics.get('score', 0):.2f} on "
            f"{int(catalyst.metrics.get('news_count', 0))} headline(s)."
            f"{caveat} {selection.reason}. Loss is capped at the premium paid; "
            "the position is flat by 15:10, ahead of the 15:15 cutoff."
        )

    def universe(self) -> list[str]:
        symbols: Any = self.params.get("universe")
        # Index only. See the module docstring - this is arithmetic on the
        # spread, not a preference.
        return [s.upper() for s in (symbols or ["SPY", "QQQ"])]


def _session_slice(bars: list[dict]) -> list[dict]:
    """Only the most recent trading day's bars.

    Contexts carry several sessions - live always did, and replay now matches -
    so anything reasoning about "this session" has to slice or it measures a
    week. VWAP itself restarts on the day boundary, so the two must agree.
    """
    if not bars:
        return []

    def _day(bar: dict) -> Any:
        stamp = bar.get("timestamp")
        if isinstance(stamp, str):
            stamp = dt.datetime.fromisoformat(stamp)
        return stamp.date() if hasattr(stamp, "date") else stamp

    last = _day(bars[-1])
    return [b for b in bars if _day(b) == last]
