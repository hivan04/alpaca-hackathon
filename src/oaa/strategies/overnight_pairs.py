"""Overnight Anomaly Pairs — market-neutral gap capture with a defined-risk overlay.

Thesis
------
A structural premium accrues between the US close and the next open. It is not
free: the overnight session is where gap risk lives, which is precisely why the
premium exists and why most retail books cannot hold it. The strategy harvests
it while staying market-neutral, and caps the gap risk contractually rather
than hoping.

Three timeframes, run in sequence
---------------------------------
1. **Universe (daily, 1-2y, offline).** Engle-Granger cointegration produces the
   approved pair list in `config/pairs.yaml`. Never re-screened live — that is
   how you conjure a pair into existence at 15:45 and lose money on it at 09:35.
2. **State (intraday).** A Kalman filter tracks the dynamic hedge ratio and the
   standardised spread. A static OLS beta is stale the moment the relationship
   drifts, and overnight pairs live or die on the hedge being right at 15:55.
3. **Forecast (15:45).** Huber baseline + quantile ensemble. The q50 gives the
   directional edge and the size; the q05/q95 give the tails, and the option
   strikes are read straight off them.

The options overlay
-------------------
Mandatory, and genuinely load-bearing. A long/short equity pair has *unbounded*
risk on the short leg — a takeover bid overnight is unrecoverable. The default
overlay is a **collar on the pair**: a long put on the long leg and a long call
on the short leg, both at the modelled tails, both expiring at the next
practical expiry. That converts an open-ended overnight exposure into a
structure with a contractual maximum loss, which is what lets the risk engine
approve it at all.

`overlay.mode: put_only` exists for completeness but leaves the short leg naked,
so `risk.allow_undefined_risk` must be true for it to pass. It is off by default
for a reason.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from oaa.core.errors import DataError, StrategyError
from oaa.core.logging import get_logger
from oaa.core.types import (
    AssetKind,
    Intent,
    Leg,
    OptionQuote,
    Right,
    Side,
    StructureType,
    TradeIdea,
)
from oaa.options.chain import ChainFilter, ChainView
from oaa.quant.features import build_features, overnight_gap_return
from oaa.quant.forecast import GapForecast, OvernightGapModel
from oaa.quant.kalman import KalmanPairFilter
from oaa.strategies.base import Strategy, StrategyContext, strategy_registry

log = get_logger("strategies.overnight")

SHARES_PER_CONTRACT = 100


@dataclass
class PairSpec:
    """One approved pair, as produced by the offline cointegration screen."""

    left: str                       # long-side candidate (dependent variable)
    right: str                      # hedge leg (independent variable)
    hedge_ratio: float = 1.0        # seed; the Kalman filter updates it live
    pvalue: float | None = None
    half_life_days: float | None = None
    enabled: bool = True
    notes: str = ""

    @property
    def name(self) -> str:
        return f"{self.left}/{self.right}"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PairSpec:
        return cls(
            left=str(raw["left"]).upper(),
            right=str(raw["right"]).upper(),
            hedge_ratio=float(raw.get("hedge_ratio", 1.0)),
            pvalue=raw.get("pvalue"),
            half_life_days=raw.get("half_life_days"),
            enabled=bool(raw.get("enabled", True)),
            notes=str(raw.get("notes", "")),
        )


@dataclass
class PairState:
    """Per-pair working state, carried across cycles within a session."""

    spec: PairSpec
    kalman: KalmanPairFilter
    model: OvernightGapModel
    trained_on: dt.date | None = None
    recent_gaps: list[float] = field(default_factory=list)
    last_forecast: GapForecast | None = None


@strategy_registry.register("overnight_pairs")
class OvernightPairs(Strategy):
    description = (
        "Market-neutral cointegrated pairs held close-to-open, collared with "
        "short-dated options at the modelled gap tails."
    )
    mode = "portfolio"      # needs both legs of a pair simultaneously
    book = "overnight"      # gated by the temporal firewall

    def __init__(self, ref: Any, config: Any) -> None:
        super().__init__(ref, config)
        self._states: dict[str, PairState] = {}
        self._pairs: list[PairSpec] | None = None

    # ------------------------------------------------------------------ #
    # universe
    # ------------------------------------------------------------------ #
    def pairs(self) -> list[PairSpec]:
        if self._pairs is None:
            raw = self.p("pairs", []) or []
            self._pairs = [PairSpec.from_dict(item) for item in raw]
            enabled = [p for p in self._pairs if p.enabled]
            log.info("overnight universe: %d enabled pair(s)", len(enabled))
        return [p for p in self._pairs if p.enabled]

    def universe(self) -> list[str]:
        symbols: list[str] = []
        for pair in self.pairs():
            symbols.extend([pair.left, pair.right])
        return sorted(set(symbols))

    # ------------------------------------------------------------------ #
    # the entry point
    # ------------------------------------------------------------------ #
    def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        # The macro lens can stand this strategy down for the session. It is an
        # overlay, not an approver - it can only ever reduce what happens.
        if not ctx.macro_allows(self.name):
            log.info(
                "%s stood down by the macro lens: %s",
                self.name, getattr(ctx.macro, "rationale", "no reason given"),
            )
            return []

        ideas: list[TradeIdea] = []
        max_concurrent = int(self.p("risk.max_concurrent_pairs", 2))
        if ctx.macro_stance(self.name) == "reduce":
            max_concurrent = max(1, max_concurrent // 2)

        for pair in self.pairs():
            if len(ideas) >= max_concurrent:
                log.debug("hit the %d-pair concurrency cap", max_concurrent)
                break
            try:
                idea = self._evaluate_pair(pair, ctx)
            except (StrategyError, DataError) as exc:
                log.debug("%s: %s", pair.name, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("%s blew up during evaluation: %s", pair.name, exc)
                continue
            if idea is not None:
                ideas.append(idea)

        ideas.sort(key=lambda i: -(i.confidence or 0))
        return ideas

    # ------------------------------------------------------------------ #
    def _evaluate_pair(self, pair: PairSpec, ctx: StrategyContext) -> TradeIdea | None:
        left = ctx.require(pair.left)
        right = ctx.require(pair.right)

        if ctx.has_position_in(pair.left) or ctx.has_position_in(pair.right):
            log.debug("%s: already holding one of the legs", pair.name)
            return None

        # A leg on unusual news velocity is a leg that can gap again tonight on
        # a story the other leg does not share - which is exactly how a hedged
        # pair loses money. The hedge is against market moves, not headlines.
        for leg in (pair.left, pair.right):
            if ctx.macro_flagged(leg):
                reason = getattr(ctx.macro, "flagged_symbols", {}).get(leg, "flagged")
                log.info("%s: skipping - %s is flagged (%s)", pair.name, leg, reason)
                return None

        state = self._state_for(pair, left, right)
        if not state.kalman.ready:
            raise StrategyError(
                f"{pair.name}: Kalman filter not warmed up "
                f"({state.kalman.state.observations} observations)"
            )

        kalman = state.kalman.state
        forecast = state.last_forecast
        if forecast is None:
            raise StrategyError(f"{pair.name}: no forecast produced")

        # -- gates ---------------------------------------------------------- #
        reason = self._entry_gate(pair, kalman, forecast, state)
        if reason:
            log.debug("%s: %s", pair.name, reason)
            return None

        # Direction: positive expected spread return = long the spread
        # (buy the left leg, short the hedge leg).
        long_left = forecast.expected > 0
        long_symbol, short_symbol = (
            (pair.left, pair.right) if long_left else (pair.right, pair.left)
        )
        long_ctx = left if long_left else right
        short_ctx = right if long_left else left

        # -- sizing ---------------------------------------------------------- #
        sizing = self._size(pair, ctx, kalman, forecast, long_ctx, short_ctx)
        if sizing is None:
            return None
        shares_long, shares_short, contracts_long, contracts_short = sizing

        # -- options overlay -------------------------------------------------- #
        overlay = self._build_overlay(
            ctx=ctx,
            long_symbol=long_symbol, short_symbol=short_symbol,
            long_ctx=long_ctx, short_ctx=short_ctx,
            forecast=forecast,
            contracts_long=contracts_long, contracts_short=contracts_short,
        )
        if overlay is None:
            return None
        put_quote, call_quote, structure = overlay

        # -- assemble --------------------------------------------------------- #
        legs: list[Leg] = []
        if put_quote is not None:
            legs.append(Leg(
                symbol=put_quote.symbol, side=Side.BUY, ratio=1,
                intent=Intent.BUY_TO_OPEN, quote=put_quote,
                limit_price=put_quote.mid, kind=AssetKind.OPTION,
                qty=float(contracts_long),
            ))
        if call_quote is not None:
            legs.append(Leg(
                symbol=call_quote.symbol, side=Side.BUY, ratio=1,
                intent=Intent.BUY_TO_OPEN, quote=call_quote,
                limit_price=call_quote.mid, kind=AssetKind.OPTION,
                qty=float(contracts_short),
            ))
        legs.append(Leg(
            symbol=long_symbol, side=Side.BUY, kind=AssetKind.EQUITY,
            qty=float(shares_long), limit_price=long_ctx.spot,
        ))
        legs.append(Leg(
            symbol=short_symbol, side=Side.SELL, kind=AssetKind.EQUITY,
            qty=float(shares_short), limit_price=short_ctx.spot,
        ))

        economics = self._economics(
            long_ctx=long_ctx, short_ctx=short_ctx,
            shares_long=shares_long, shares_short=shares_short,
            put_quote=put_quote, call_quote=call_quote,
            contracts_long=contracts_long, contracts_short=contracts_short,
            forecast=forecast,
        )

        idea = TradeIdea(
            symbol=f"{long_symbol}/{short_symbol}",
            strategy=self.name,
            structure=structure,
            book="overnight",
            legs=legs,
            quantity=1,                       # the whole combo is one unit
            net_price=economics["net_debit_per_unit"],
            max_loss=economics["max_loss"],
            max_profit=economics["expected_profit"],
            probability_of_profit=economics["prob_profit"],
            confidence=round(min(1.0, forecast.confidence), 3),
            thesis=self._thesis(pair, kalman, forecast, economics, long_symbol, short_symbol),
            tags=["overnight", "market_neutral", "pairs", "defined_risk"],
            meta={
                "pair": pair.name,
                "long_leg": long_symbol,
                "short_leg": short_symbol,
                "shares_long": shares_long,
                "shares_short": shares_short,
                "hedge_ratio_kalman": round(kalman.beta, 6),
                "hedge_ratio_realised": economics["realised_hedge_ratio"],
                "hedge_error_pct": economics["hedge_error_pct"],
                "zscore": round(kalman.zscore, 4),
                "half_life_days": pair.half_life_days,
                "forecast": forecast.as_dict(),
                "model": state.model.backend,
                "put_strike": put_quote.strike if put_quote else None,
                "call_strike": call_quote.strike if call_quote else None,
                "expiry": (put_quote or call_quote).expiry.isoformat()
                if (put_quote or call_quote) else None,
                "gross_notional": economics["gross_notional"],
                "overlay_cost": economics["overlay_cost"],
                "macro_regime": getattr(ctx.macro, "regime", None),
                "macro_stance": ctx.macro_stance(self.name),
                "collar_widening": ctx.collar_widening(),
                "exit_at": self.p("exit.time", "09:35"),
            },
        )
        log.info("%s -> %s", pair.name, idea.describe())
        return idea

    # ------------------------------------------------------------------ #
    # state, filter and model
    # ------------------------------------------------------------------ #
    def _state_for(self, pair: PairSpec, left: Any, right: Any) -> PairState:
        state = self._states.get(pair.name)
        today = left.asof.date()

        if state is None:
            state = PairState(
                spec=pair,
                kalman=KalmanPairFilter(
                    delta=float(self.p("kalman.delta", 1e-4)),
                    obs_covariance=float(self.p("kalman.observation_covariance", 1e-3)),
                    zscore_window=int(self.p("kalman.zscore_window", 60)),
                    warmup=int(self.p("kalman.warmup", 30)),
                ),
                model=OvernightGapModel(
                    min_train_rows=int(self.p("model.min_train_rows", 120)),
                    n_estimators=int(self.p("model.n_estimators", 150)),
                    learning_rate=float(self.p("model.learning_rate", 0.05)),
                    max_depth=int(self.p("model.max_depth", 3)),
                ),
            )
            self._states[pair.name] = state

        # Refit once per session. Fitting is cheap (~1s per pair on 300 rows)
        # and a stale model on Thursday is worse than a second of latency.
        if state.trained_on != today:
            self._refit(state, left, right)
            state.trained_on = today

        return state

    def _refit(self, state: PairState, left: Any, right: Any) -> None:
        bars_left, bars_right = _aligned_bars(left.bars, right.bars)
        if len(bars_left) < int(self.p("model.min_bars", 90)):
            raise StrategyError(
                f"{state.spec.name}: only {len(bars_left)} aligned daily bars"
            )

        closes_left = [b["close"] for b in bars_left]
        closes_right = [b["close"] for b in bars_right]

        state.kalman.reset()
        state.kalman.fit(closes_left, closes_right)

        # Build the training set walk-forward: every row uses only the filter
        # state as it stood that evening, and the target is the gap that
        # actually followed. No lookahead.
        warmup = int(self.p("kalman.warmup", 30))
        rows: list[dict[str, float]] = []
        targets: list[float] = []
        gaps: list[float] = []

        for i in range(warmup, len(bars_left) - 1):
            snapshot = state.kalman.history[i]
            previous = state.kalman.history[i - 1]
            row = build_features(
                zscore=snapshot.zscore,
                prev_zscore=previous.zscore,
                beta=snapshot.beta,
                prev_beta=previous.beta,
                spread=snapshot.spread,
                spread_std=snapshot.spread_std,
                bars_y=bars_left[: i + 1],
                bars_x=bars_right[: i + 1],
                recent_gaps=gaps[-10:],
                asof=_bar_date(bars_left[i]),
            )
            target = overnight_gap_return(
                close_y=bars_left[i]["close"], open_y=bars_left[i + 1]["open"],
                close_x=bars_right[i]["close"], open_x=bars_right[i + 1]["open"],
                beta=snapshot.beta,
            )
            rows.append(row)
            targets.append(target)
            gaps.append(target)

        state.recent_gaps = gaps[-30:]
        state.model.fit(rows, targets)

        # Tonight's row: the filter is at its latest state, today's bar is the
        # last one we have, and the target is what we are trying to predict.
        latest = state.kalman.history[-1]
        prior = state.kalman.history[-2] if len(state.kalman.history) > 1 else latest
        tonight = build_features(
            zscore=latest.zscore, prev_zscore=prior.zscore,
            beta=latest.beta, prev_beta=prior.beta,
            spread=latest.spread, spread_std=latest.spread_std,
            bars_y=bars_left, bars_x=bars_right,
            recent_gaps=state.recent_gaps[-10:],
            asof=left.asof.date(),
        )
        state.last_forecast = state.model.predict(tonight)
        # Per-leg empirical gap tails, used to place the option strikes.
        state.last_forecast.features["leg_gap_q05_left"] = _leg_gap_quantile(bars_left, 0.05)
        state.last_forecast.features["leg_gap_q95_left"] = _leg_gap_quantile(bars_left, 0.95)
        state.last_forecast.features["leg_gap_q05_right"] = _leg_gap_quantile(bars_right, 0.05)
        state.last_forecast.features["leg_gap_q95_right"] = _leg_gap_quantile(bars_right, 0.95)

        log.info(
            "%s refit: %d rows, %s, z=%.2f, %s",
            state.spec.name, len(rows), state.model.backend,
            latest.zscore, state.last_forecast.describe(),
        )

    # ------------------------------------------------------------------ #
    # gates
    # ------------------------------------------------------------------ #
    def _entry_gate(
        self, pair: PairSpec, kalman: Any, forecast: GapForecast, state: PairState
    ) -> str | None:
        min_abs_z = float(self.p("entry.min_abs_zscore", 0.75))
        max_abs_z = float(self.p("entry.max_abs_zscore", 3.5))
        if abs(kalman.zscore) < min_abs_z:
            return f"|z| {abs(kalman.zscore):.2f} below {min_abs_z}"
        if abs(kalman.zscore) > max_abs_z:
            # A spread this stretched usually means the relationship broke,
            # not that it is about to snap back.
            return f"|z| {abs(kalman.zscore):.2f} above {max_abs_z} - treating as a regime break"

        min_edge = float(self.p("entry.min_expected_return", 0.0015))
        if abs(forecast.expected) < min_edge:
            return f"expected {forecast.expected:+.4%} below the {min_edge:.2%} floor"

        min_edge_to_risk = float(self.p("entry.min_edge_to_risk", 0.12))
        if forecast.edge_to_risk < min_edge_to_risk:
            return (
                f"edge/risk {forecast.edge_to_risk:.3f} below {min_edge_to_risk} - "
                "the tail is too wide for the edge"
            )

        min_conf = float(self.p("entry.min_confidence", 0.10))
        if forecast.confidence < min_conf:
            return f"confidence {forecast.confidence:.2f} below {min_conf}"

        max_tail = float(self.p("entry.max_tail_width", 0.08))
        if forecast.tail_width > max_tail:
            return f"tail width {forecast.tail_width:.2%} above {max_tail:.2%}"

        hl = pair.half_life_days
        max_hl = float(self.p("entry.max_half_life_days", 30.0))
        if hl is not None and hl > max_hl:
            return f"half-life {hl:.1f}d above {max_hl}d - too slow for an overnight hold"

        if state.model.backend == "empirical" and self.p("entry.require_trained_model", False):
            return "model has not trained yet and require_trained_model is set"
        return None

    # ------------------------------------------------------------------ #
    # sizing
    # ------------------------------------------------------------------ #
    def _size(
        self,
        pair: PairSpec,
        ctx: StrategyContext,
        kalman: Any,
        forecast: GapForecast,
        long_ctx: Any,
        short_ctx: Any,
    ) -> tuple[int, int, int, int] | None:
        """Round-lot sizing against the firewall-verified budget.

        Share counts must be multiples of 100 so every share is coverable by
        whole option contracts — a partially covered short leg is not a
        defined-risk position, it is an unhedged one with paperwork.

        That constraint fights market neutrality, and the fight is worse than it
        looks. Take SNDK at 177.93 against MU at 106.79: a price ratio of 1.666
        means the naive choice (size the long to the budget, then round the
        short) lands on 200/300 shares and leaves a **10% residual directional
        exposure** — which defeats the entire point of a market-neutral trade.

        So this searches the lot grid instead of taking the first fit, picking
        the combination with the smallest residual that still fits inside the
        budget. And when no combination gets close enough, it refuses the pair.
        A pair that cannot be built neutrally at this account size is not a
        pairs trade; it is a directional bet with a hedge attached.
        """
        budget = ctx.budget if ctx.budget > 0 else (
            ctx.account.equity * float(self.p("risk.fallback_equity_pct", 0.20))
        )
        # A cautious regime halves the size rather than forfeiting the night.
        budget *= ctx.macro_size_multiplier(self.name)
        if budget <= 0:
            log.debug("%s: no capital budget available", pair.name)
            return None

        long_spot, short_spot = long_ctx.spot, short_ctx.spot
        if long_spot <= 0 or short_spot <= 0:
            return None

        confidence_scale = min(1.0, max(0.25, forecast.confidence * 2.0))
        gross_cap = budget * confidence_scale
        max_contracts = int(self.p("risk.max_contracts_per_leg", 20))
        max_error = float(self.p("risk.max_hedge_error_pct", 0.05))

        best: tuple[float, float, int, int] | None = None
        for n_long in range(1, max_contracts + 1):
            long_notional = n_long * SHARES_PER_CONTRACT * long_spot
            if long_notional > gross_cap:
                break
            ideal_lots = long_notional / short_spot / SHARES_PER_CONTRACT
            # Both neighbours: the nearest lot is not always the better hedge.
            for n_short in {max(1, int(ideal_lots)), int(ideal_lots) + 1}:
                if n_short > max_contracts:
                    continue
                short_notional = n_short * SHARES_PER_CONTRACT * short_spot
                if long_notional + short_notional > gross_cap:
                    continue
                error = abs(short_notional - long_notional) / long_notional
                # Least residual first; among equals, take the larger position -
                # the edge is proportional to notional.
                candidate = (round(error, 6), -(long_notional + short_notional), n_long, n_short)
                if best is None or candidate < best:
                    best = candidate

        if best is None:
            log.debug(
                "%s: budget $%.0f cannot fund a round lot of %s at %.2f",
                pair.name, gross_cap, long_ctx.symbol, long_spot,
            )
            return None

        error, _, contracts_long, contracts_short = best
        if error > max_error:
            log.info(
                "%s: best achievable hedge is %.1f%% off neutral (%d/%d lots at "
                "%.2f/%.2f, cap %.1f%%) - refusing. A pair that cannot be built "
                "neutrally at this size is a directional bet, not a pairs trade.",
                pair.name, 100 * error, contracts_long, contracts_short,
                long_spot, short_spot, 100 * max_error,
            )
            return None

        return (
            contracts_long * SHARES_PER_CONTRACT,
            contracts_short * SHARES_PER_CONTRACT,
            contracts_long,
            contracts_short,
        )

    # ------------------------------------------------------------------ #
    # options overlay
    # ------------------------------------------------------------------ #
    def _build_overlay(
        self,
        ctx: StrategyContext,
        long_symbol: str,
        short_symbol: str,
        long_ctx: Any,
        short_ctx: Any,
        forecast: GapForecast,
        contracts_long: int,
        contracts_short: int,
    ) -> tuple[OptionQuote | None, OptionQuote | None, StructureType] | None:
        """Place the protective strikes at the modelled tails.

        The put on the long leg sits at the worse of (that leg's own historical
        5th-percentile gap) and (the pair's modelled q05). The call on the short
        leg mirrors it with q95. Taking the worse of the two means the hedge is
        priced for a bad night in *either* the leg or the relationship — the two
        ways this trade actually loses.
        """
        mode = str(self.p("overlay.mode", "collar")).lower()
        dte_min = int(self.p("overlay.min_dte", 1))
        dte_max = int(self.p("overlay.max_dte", 9))

        chain_filter = ctx.default_filter(
            min_dte=dte_min,
            max_dte=dte_max,
            min_open_interest=int(self.p("overlay.min_open_interest", 25)),
            min_volume=0,
            max_spread_pct=float(self.p("overlay.max_spread_pct", 0.35)),
            min_price=float(self.p("overlay.min_price", 0.02)),
            max_price=float(self.p("overlay.max_price", 50.0)),
        )

        features = forecast.features
        left_is_long = long_symbol == long_ctx.symbol

        put_tail = min(
            features.get("leg_gap_q05_left" if left_is_long else "leg_gap_q05_right", -0.02),
            -abs(forecast.lower),
        )
        call_tail = max(
            features.get("leg_gap_q95_right" if left_is_long else "leg_gap_q95_left", 0.02),
            abs(forecast.upper),
        )
        # Bound the hedge distance at both ends.
        #   ceiling - a strike 20% away is a lottery ticket, not a floor
        #   floor   - an at-the-money overnight hedge costs about as much as
        #             the move it insures against, so it buys nothing. If the
        #             model says tonight's tail is tiny, the honest response is
        #             to hedge at a sane distance, not to buy ATM protection.
        max_distance = float(self.p("overlay.max_strike_distance_pct", 0.10))
        min_distance = float(self.p("overlay.min_strike_distance_pct", 0.015))
        # The macro lens may push the protection further out on a jumpy tape.
        # It is bounded at 1.0 upstream, so this can only ever widen a hedge.
        widening = ctx.collar_widening()
        min_distance *= widening
        put_tail = max(min(put_tail * widening, -min_distance), -max_distance)
        call_tail = min(max(call_tail * widening, min_distance), max_distance)

        put_quote = self._pick(
            ctx, long_symbol, long_ctx, Right.PUT,
            target_price=long_ctx.spot * (1.0 + put_tail),
            chain_filter=chain_filter,
        )
        if put_quote is None:
            log.debug("%s: no usable protective put", long_symbol)
            return None

        if mode == "put_only":
            return put_quote, None, StructureType.PAIRS_PUT_HEDGE

        call_quote = self._pick(
            ctx, short_symbol, short_ctx, Right.CALL,
            target_price=short_ctx.spot * (1.0 + call_tail),
            chain_filter=chain_filter,
        )
        if call_quote is None:
            if self.p("overlay.require_full_hedge", True):
                log.info(
                    "%s: no usable protective call on the short leg - skipping. "
                    "An unhedged overnight short is the one exposure this "
                    "strategy will not carry.", short_symbol,
                )
                return None
            return put_quote, None, StructureType.PAIRS_PUT_HEDGE

        # Match expiries so the whole structure rolls off together.
        if put_quote.expiry != call_quote.expiry:
            shared = min(put_quote.expiry, call_quote.expiry)
            put_quote = self._pick(
                ctx, long_symbol, long_ctx, Right.PUT,
                target_price=long_ctx.spot * (1.0 + put_tail),
                chain_filter=chain_filter, expiry=shared,
            ) or put_quote
            call_quote = self._pick(
                ctx, short_symbol, short_ctx, Right.CALL,
                target_price=short_ctx.spot * (1.0 + call_tail),
                chain_filter=chain_filter, expiry=shared,
            ) or call_quote

        # Cost check: protection that eats the whole edge is not protection.
        cost = (
            (put_quote.mid or 0) * contracts_long + (call_quote.mid or 0) * contracts_short
        ) * SHARES_PER_CONTRACT
        notional = long_ctx.spot * contracts_long * SHARES_PER_CONTRACT
        max_cost_pct = float(self.p("overlay.max_cost_pct_of_notional", 0.010))
        if notional > 0 and cost / notional > max_cost_pct:
            log.debug(
                "%s/%s: overlay costs %.3f%% of notional, above the %.3f%% cap",
                long_symbol, short_symbol, 100 * cost / notional, 100 * max_cost_pct,
            )
            return None

        return put_quote, call_quote, StructureType.PAIRS_COLLAR

    def _pick(
        self,
        ctx: StrategyContext,
        symbol: str,
        market: Any,
        right: Right,
        target_price: float,
        chain_filter: ChainFilter,
        expiry: dt.date | None = None,
    ) -> OptionQuote | None:
        try:
            view = ChainView.from_quotes(
                symbol=symbol, spot=market.spot, quotes=market.chain,
                chain_filter=chain_filter, asof=market.asof.date(),
            )
            if view.is_empty:
                return None
            chosen_expiry = expiry or view.nearest_expiry(int(self.p("overlay.target_dte", 1)))
            return view.by_strike(chosen_expiry, right, target_price)
        except (DataError, StrategyError) as exc:
            log.debug("%s: strike selection failed - %s", symbol, exc)
            return None

    # ------------------------------------------------------------------ #
    # economics
    # ------------------------------------------------------------------ #
    def _economics(
        self,
        long_ctx: Any,
        short_ctx: Any,
        shares_long: int,
        shares_short: int,
        put_quote: OptionQuote | None,
        call_quote: OptionQuote | None,
        contracts_long: int,
        contracts_short: int,
        forecast: GapForecast,
    ) -> dict[str, Any]:
        """Contractual maximum loss for the collared pair.

            long leg   loss capped at (spot - put strike) + put premium
            short leg  loss capped at (call strike - spot) + call premium

        Both legs bounded means the whole position is bounded. That is the
        property the risk engine checks, and the reason the collar is default.
        """
        long_notional = shares_long * long_ctx.spot
        short_notional = shares_short * short_ctx.spot
        gross = long_notional + short_notional

        put_premium = (put_quote.mid or 0.0) * contracts_long * SHARES_PER_CONTRACT if put_quote else 0.0
        call_premium = (call_quote.mid or 0.0) * contracts_short * SHARES_PER_CONTRACT if call_quote else 0.0
        overlay_cost = put_premium + call_premium

        if put_quote is not None:
            long_leg_loss = max(0.0, long_ctx.spot - put_quote.strike) * shares_long + put_premium
        else:
            long_leg_loss = long_notional  # unhedged: the whole leg can go to zero

        if call_quote is not None:
            short_leg_loss = max(0.0, call_quote.strike - short_ctx.spot) * shares_short + call_premium
        else:
            short_leg_loss = math.inf  # unbounded - the risk engine will refuse this

        max_loss = long_leg_loss + short_leg_loss
        expected_profit = abs(forecast.expected) * long_notional - overlay_cost

        realised_hedge = (short_notional / long_notional) if long_notional else 0.0
        hedge_error = abs(realised_hedge - 1.0)

        return {
            "gross_notional": round(gross, 2),
            "long_notional": round(long_notional, 2),
            "short_notional": round(short_notional, 2),
            "overlay_cost": round(overlay_cost, 2),
            "max_loss": None if math.isinf(max_loss) else round(max_loss, 2),
            "expected_profit": round(expected_profit, 2),
            "net_debit_per_unit": round(overlay_cost / SHARES_PER_CONTRACT, 4),
            "prob_profit": round(_prob_positive(forecast), 3),
            "realised_hedge_ratio": round(realised_hedge, 4),
            "hedge_error_pct": round(100 * hedge_error, 3),
        }

    # ------------------------------------------------------------------ #
    def _thesis(
        self,
        pair: PairSpec,
        kalman: Any,
        forecast: GapForecast,
        economics: dict[str, Any],
        long_symbol: str,
        short_symbol: str,
    ) -> str:
        hl = f"{pair.half_life_days:.1f}d" if pair.half_life_days else "n/a"
        return (
            f"{pair.name} spread is {kalman.zscore:+.2f} sd from its Kalman mean "
            f"(beta {kalman.beta:.3f}, half-life {hl}). The gap model expects "
            f"{forecast.expected:+.3%} overnight with a q05 of {forecast.lower:+.3%} "
            f"and q95 of {forecast.upper:+.3%}, an edge/risk of "
            f"{forecast.edge_to_risk:.2f}. Expressing it long {long_symbol} / short "
            f"{short_symbol}, dollar-neutral to within "
            f"{economics['hedge_error_pct']:.2f}%. Both legs are collared at the "
            f"modelled tails, so the maximum loss is a contractual "
            f"${economics['max_loss']:,.0f} rather than an open overnight gap. "
            f"Held to the {self.p('exit.time', '09:35')} ET liquidation."
        )

    # ------------------------------------------------------------------ #
    def should_exit(self, ctx: StrategyContext, idea: TradeIdea, pnl_pct: float) -> str | None:
        """The overnight book exits on the clock, not on a P&L rule.

        The whole thesis is a close-to-open holding period. Taking profit at
        03:00 is not possible and a stop mid-session would not fill anyway, so
        the exit is the 09:35 firewall liquidation and nothing else.
        """
        return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _bar_date(bar: dict[str, Any]) -> dt.date:
    stamp = bar.get("timestamp")
    if isinstance(stamp, dt.datetime):
        return stamp.date()
    if isinstance(stamp, dt.date):
        return stamp
    if isinstance(stamp, str):
        try:
            return dt.date.fromisoformat(stamp[:10])
        except ValueError:
            pass
    return dt.date.today()


def _aligned_bars(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Intersect two bar series on date.

    Misaligned series are the classic silent bug in pairs research: a missing
    day on one leg shifts the entire spread and the z-score becomes fiction.
    """
    by_date_left = {_bar_date(b): b for b in left}
    by_date_right = {_bar_date(b): b for b in right}
    shared = sorted(set(by_date_left) & set(by_date_right))
    return [by_date_left[d] for d in shared], [by_date_right[d] for d in shared]


def _leg_gap_quantile(bars: list[dict[str, Any]], q: float, lookback: int = 250) -> float:
    """Empirical overnight gap quantile for a single leg."""
    import numpy as np

    window = bars[-lookback:]
    gaps = [
        (float(window[i]["open"]) / float(window[i - 1]["close"])) - 1.0
        for i in range(1, len(window))
        if float(window[i - 1].get("close") or 0) > 0
    ]
    if len(gaps) < 20:
        return -0.02 if q < 0.5 else 0.02
    return float(np.quantile(gaps, q))


def _prob_positive(forecast: GapForecast) -> float:
    """Crude probability the night lands positive, from the quantile spread."""
    width = forecast.tail_width
    if width < 1e-9:
        return 0.5
    # Linear interpolation of the CDF between q05 and q95.
    position = (0.0 - forecast.lower) / width
    probability = 1.0 - min(1.0, max(0.0, position)) * 0.9 - 0.05
    return min(0.95, max(0.05, probability if forecast.expected > 0 else 1.0 - probability))
