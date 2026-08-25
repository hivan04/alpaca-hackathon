"""Walk-forward backtest for the overnight pairs strategy.

This one is a real backtest, not a replay harness. The overnight strategy's
entire holding period is close-to-open, and daily bars contain both ends of it,
so the P&L can be reconstructed exactly from data the Alpaca CLI can fetch.

What is exact
    the equity legs, the hedge ratio, the z-score, the gap that followed

What is modelled
    the options overlay, because no historical option chain with greeks is
    available on the free tier. See `backtest.pricing` — every assumption is
    deliberately pessimistic.

Walk-forward discipline
    On each simulated evening the model is fitted only on nights that had
    already happened. There is no point in the loop where a future bar is
    visible to a decision. That is the difference between a backtest and a
    curve-fitting exercise.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from oaa.backtest.pricing import intrinsic_at_open, overnight_option_cost
from oaa.core.errors import DataError
from oaa.core.logging import get_logger
from oaa.data.indicators import max_drawdown, sharpe
from oaa.quant.features import build_features, overnight_gap_return
from oaa.quant.forecast import OvernightGapModel
from oaa.quant.kalman import KalmanPairFilter

log = get_logger("backtest.overnight")

SHARES_PER_CONTRACT = 100


@dataclass
class NightResult:
    date: dt.date
    pair: str
    traded: bool
    skip_reason: str | None = None
    direction: str = ""
    zscore: float = 0.0
    beta: float = 0.0
    expected: float = 0.0
    q05: float = 0.0
    q95: float = 0.0
    realised: float = 0.0
    shares_long: int = 0
    shares_short: int = 0
    equity_pnl: float = 0.0
    overlay_cost: float = 0.0
    overlay_payoff: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0
    equity_after: float = 0.0

    def as_row(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "pair": self.pair,
            "traded": self.traded,
            "skip_reason": self.skip_reason,
            "direction": self.direction,
            "zscore": round(self.zscore, 4),
            "beta": round(self.beta, 4),
            "expected": round(self.expected, 6),
            "q05": round(self.q05, 6),
            "q95": round(self.q95, 6),
            "realised": round(self.realised, 6),
            "shares_long": self.shares_long,
            "shares_short": self.shares_short,
            "equity_pnl": round(self.equity_pnl, 2),
            "overlay_cost": round(self.overlay_cost, 2),
            "overlay_payoff": round(self.overlay_payoff, 2),
            "slippage": round(self.slippage, 2),
            "net_pnl": round(self.net_pnl, 2),
            "equity_after": round(self.equity_after, 2),
        }


@dataclass
class OvernightBacktestResult:
    pair: str
    start: dt.date | None = None
    end: dt.date | None = None
    initial_equity: float = 0.0
    nights: list[NightResult] = field(default_factory=list)

    @property
    def traded(self) -> list[NightResult]:
        return [n for n in self.nights if n.traded]

    @property
    def equity_curve(self) -> list[float]:
        return [n.equity_after for n in self.nights]

    def metrics(self) -> dict[str, Any]:
        traded = self.traded
        curve = self.equity_curve
        pnls = [n.net_pnl for n in traded]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        returns = [
            (curve[i] - curve[i - 1]) / curve[i - 1]
            for i in range(1, len(curve))
            if curve[i - 1] > 0
        ]
        final = curve[-1] if curve else self.initial_equity
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))

        return {
            "pair": self.pair,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "sessions": len(self.nights),
            "nights_traded": len(traded),
            "participation_rate": round(len(traded) / len(self.nights), 4) if self.nights else 0.0,
            "initial_equity": round(self.initial_equity, 2),
            "final_equity": round(final, 2),
            "total_return": round((final - self.initial_equity) / self.initial_equity, 5)
            if self.initial_equity else 0.0,
            "total_pnl": round(sum(pnls), 2),
            "win_rate": round(len(wins) / len(traded), 4) if traded else 0.0,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "best_night": round(max(pnls), 2) if pnls else 0.0,
            "worst_night": round(min(pnls), 2) if pnls else 0.0,
            "max_drawdown": max_drawdown(curve) if curve else 0.0,
            # Nights, not days: the strategy is only exposed overnight.
            "sharpe_per_night": sharpe(returns, periods_per_year=252) if returns else None,
            "total_overlay_cost": round(sum(n.overlay_cost for n in traded), 2),
            "total_overlay_payoff": round(sum(n.overlay_payoff for n in traded), 2),
            "overlay_net": round(
                sum(n.overlay_payoff - n.overlay_cost for n in traded), 2
            ),
            "total_slippage": round(sum(n.slippage for n in traded), 2),
            "gross_pnl_before_costs": round(sum(n.equity_pnl for n in traded), 2),
            "skip_reasons": _count_reasons(self.nights),
        }

    def summary_lines(self) -> list[str]:
        m = self.metrics()
        return [
            f"Pair            {m['pair']}",
            f"Window          {m['start']} -> {m['end']}  ({m['sessions']} sessions)",
            f"Traded          {m['nights_traded']} nights ({m['participation_rate']:.0%} participation)",
            f"Equity          {m['initial_equity']:>12,.2f}  ->  {m['final_equity']:,.2f}",
            f"Total P&L       {m['total_pnl']:>+12,.2f}  ({m['total_return']:+.2%})",
            f"Win rate        {m['win_rate']:>12.1%}   PF {m['profit_factor']}",
            f"Best / worst    {m['best_night']:>+12,.2f} / {m['worst_night']:+,.2f}",
            f"Max drawdown    {m['max_drawdown']:>12.2%}",
            f"Overlay net     {m['overlay_net']:>+12,.2f}  "
            f"(cost {m['total_overlay_cost']:,.2f}, payoff {m['total_overlay_payoff']:,.2f})",
            f"Slippage        {m['total_slippage']:>12,.2f}",
        ]


class OvernightBacktest:
    """Walk-forward simulator for one pair."""

    def __init__(self, settings: Any, provider: Any, params: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.cfg = settings.config
        self.provider = provider
        self.params = params or {}
        self.bt = self.cfg.backtest

    # ------------------------------------------------------------------ #
    def _p(self, path: str, default: Any = None) -> Any:
        cursor: Any = self.params
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor

    # ------------------------------------------------------------------ #
    def run(
        self,
        left: str,
        right: str,
        start: dt.date | None = None,
        end: dt.date | None = None,
        initial_equity: float | None = None,
    ) -> OvernightBacktestResult:
        equity = initial_equity if initial_equity is not None else self.bt.initial_cash
        result = OvernightBacktestResult(pair=f"{left}/{right}", initial_equity=equity)

        bars_left, bars_right = self._load(left, right, start, end)
        if len(bars_left) < 150:
            raise DataError(
                f"{left}/{right}: only {len(bars_left)} aligned sessions - "
                "need at least 150 for a walk-forward fit"
            )
        result.start = _date(bars_left[0])
        result.end = _date(bars_left[-1])

        closes_left = [b["close"] for b in bars_left]
        closes_right = [b["close"] for b in bars_right]

        kalman = KalmanPairFilter(
            delta=float(self._p("kalman.delta", 1e-4)),
            obs_covariance=float(self._p("kalman.observation_covariance", 1e-3)),
            zscore_window=int(self._p("kalman.zscore_window", 60)),
            warmup=int(self._p("kalman.warmup", 30)),
        )
        kalman.fit(closes_left, closes_right)

        warmup = int(self._p("kalman.warmup", 30))
        train_min = int(self._p("model.min_train_rows", 120))
        refit_every = int(self._p("backtest.refit_every", 21))

        rows: list[dict[str, float]] = []
        targets: list[float] = []
        gaps: list[float] = []
        model = OvernightGapModel(min_train_rows=train_min)
        last_fit = -10_000

        for i in range(warmup, len(bars_left) - 1):
            snapshot = kalman.history[i]
            previous = kalman.history[i - 1]
            session = _date(bars_left[i])

            row = build_features(
                zscore=snapshot.zscore, prev_zscore=previous.zscore,
                beta=snapshot.beta, prev_beta=previous.beta,
                spread=snapshot.spread, spread_std=snapshot.spread_std,
                bars_y=bars_left[: i + 1], bars_x=bars_right[: i + 1],
                recent_gaps=gaps[-10:], asof=session,
            )
            realised = overnight_gap_return(
                close_y=bars_left[i]["close"], open_y=bars_left[i + 1]["open"],
                close_x=bars_right[i]["close"], open_x=bars_right[i + 1]["open"],
                beta=snapshot.beta,
            )

            # -- refit on history only ------------------------------------- #
            if len(rows) >= train_min and (i - last_fit) >= refit_every:
                model = OvernightGapModel(min_train_rows=train_min).fit(rows, targets)
                last_fit = i

            night = self._simulate_night(
                session=session, pair=result.pair, row=row, model=model,
                snapshot=snapshot, realised=realised,
                bars_left=bars_left, bars_right=bars_right, index=i,
                equity=equity, history_len=len(rows),
            )
            equity = night.equity_after
            result.nights.append(night)

            rows.append(row)
            targets.append(realised)
            gaps.append(realised)

        return result

    # ------------------------------------------------------------------ #
    def _simulate_night(
        self,
        session: dt.date,
        pair: str,
        row: dict[str, float],
        model: OvernightGapModel,
        snapshot: Any,
        realised: float,
        bars_left: list[dict[str, Any]],
        bars_right: list[dict[str, Any]],
        index: int,
        equity: float,
        history_len: int,
    ) -> NightResult:
        forecast = model.predict(row)
        night = NightResult(
            date=session, pair=pair, traded=False,
            zscore=snapshot.zscore, beta=snapshot.beta,
            expected=forecast.expected, q05=forecast.lower, q95=forecast.upper,
            realised=realised, equity_after=equity,
        )

        skip = self._gate(snapshot, forecast, history_len)
        if skip:
            night.skip_reason = skip
            return night

        close_left = float(bars_left[index]["close"])
        close_right = float(bars_right[index]["close"])
        open_left = float(bars_left[index + 1]["open"])
        open_right = float(bars_right[index + 1]["open"])

        long_left = forecast.expected > 0
        long_close, short_close = (close_left, close_right) if long_left else (close_right, close_left)
        long_open, short_open = (open_left, open_right) if long_left else (open_right, open_left)

        # -- sizing (mirrors the live path: round lots, dollar-neutral) ----- #
        budget = equity * self.cfg.firewall.overnight_max_equity_pct
        budget *= min(1.0, max(0.25, forecast.confidence * 2.0))
        per_side = budget / 2.0
        contracts_long = int(per_side // (long_close * SHARES_PER_CONTRACT))
        if contracts_long < 1:
            night.skip_reason = "budget below one round lot"
            return night
        contracts_long = min(contracts_long, int(self._p("risk.max_contracts_per_leg", 20)))
        shares_long = contracts_long * SHARES_PER_CONTRACT
        contracts_short = max(1, int(round(shares_long * long_close / short_close / SHARES_PER_CONTRACT)))
        shares_short = contracts_short * SHARES_PER_CONTRACT

        night.traded = True
        night.direction = "long_left" if long_left else "long_right"
        night.shares_long, night.shares_short = shares_long, shares_short

        # -- equity leg P&L -------------------------------------------------- #
        night.equity_pnl = (
            shares_long * (long_open - long_close)
            - shares_short * (short_open - short_close)
        )

        # -- the options collar --------------------------------------------- #
        vol_long = _trailing_vol(bars_left if long_left else bars_right, index)
        vol_short = _trailing_vol(bars_right if long_left else bars_left, index)
        max_distance = float(self._p("overlay.max_strike_distance_pct", 0.10))

        put_strike = _snap(long_close * (1.0 + max(forecast.lower, -max_distance)))
        call_strike = _snap(short_close * (1.0 + min(forecast.upper, max_distance)))

        put_cost = overnight_option_cost(long_close, put_strike, vol_long, is_call=False)
        call_cost = overnight_option_cost(short_close, call_strike, vol_short, is_call=True)
        night.overlay_cost = (put_cost * shares_long) + (call_cost * shares_short)
        night.overlay_payoff = (
            intrinsic_at_open(long_open, put_strike, is_call=False) * shares_long
            + intrinsic_at_open(short_open, call_strike, is_call=True) * shares_short
        )

        # -- frictions -------------------------------------------------------- #
        spread_bps = float(self._p("backtest.equity_spread_bps", 2.0)) / 10_000.0
        fraction = self.bt.slippage_spread_fraction
        # Two legs in at the close, two legs out at the open.
        night.slippage = (
            (shares_long * long_close + shares_short * short_close)
            * spread_bps * fraction * 2.0
        )

        night.net_pnl = (
            night.equity_pnl + night.overlay_payoff - night.overlay_cost - night.slippage
        )
        night.equity_after = equity + night.net_pnl
        return night

    # ------------------------------------------------------------------ #
    def _gate(self, snapshot: Any, forecast: Any, history_len: int) -> str | None:
        """The live entry gates, applied identically here.

        Sharing the thresholds is the point: a backtest that trades on looser
        rules than production is measuring a different strategy.
        """
        if history_len < int(self._p("model.min_train_rows", 120)):
            return "model warming up"
        z = abs(snapshot.zscore)
        if z < float(self._p("entry.min_abs_zscore", 0.75)):
            return "z below floor"
        if z > float(self._p("entry.max_abs_zscore", 3.5)):
            return "z above ceiling (regime break)"
        if abs(forecast.expected) < float(self._p("entry.min_expected_return", 0.0015)):
            return "edge below floor"
        if forecast.edge_to_risk < float(self._p("entry.min_edge_to_risk", 0.12)):
            return "edge/risk below floor"
        if forecast.confidence < float(self._p("entry.min_confidence", 0.10)):
            return "confidence below floor"
        if forecast.tail_width > float(self._p("entry.max_tail_width", 0.08)):
            return "tail too wide"
        return None

    # ------------------------------------------------------------------ #
    def _load(
        self, left: str, right: str, start: dt.date | None, end: dt.date | None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Daily bars via the configured provider (the Alpaca CLI by default)."""
        stop = end or dt.date.fromisoformat(self.bt.end)
        begin = start or dt.date.fromisoformat(self.bt.start)
        lookback = max(400, (stop - begin).days + 400)

        kwargs: dict[str, Any] = {"lookback_days": lookback, "timeframe": "1Day"}
        try:
            bars_left = self.provider.bars(left, start=begin, end=stop, **kwargs)
            bars_right = self.provider.bars(right, start=begin, end=stop, **kwargs)
        except TypeError:
            # The alpaca-py provider has no start/end parameters.
            bars_left = self.provider.bars(left, **kwargs)
            bars_right = self.provider.bars(right, **kwargs)

        return _align(bars_left, bars_right)


# --------------------------------------------------------------------------- #
def _date(bar: dict[str, Any]) -> dt.date:
    stamp = bar.get("timestamp")
    if isinstance(stamp, dt.datetime):
        return stamp.date()
    if isinstance(stamp, dt.date):
        return stamp
    return dt.date.fromisoformat(str(stamp)[:10])


def _align(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_left = {_date(b): b for b in left}
    by_right = {_date(b): b for b in right}
    shared = sorted(set(by_left) & set(by_right))
    return [by_left[d] for d in shared], [by_right[d] for d in shared]


def _trailing_vol(bars: Sequence[dict[str, Any]], index: int, window: int = 20) -> float:
    closes = [float(b["close"]) for b in bars[max(0, index - window) : index + 1]]
    if len(closes) < 3:
        return 0.25
    rets = np.diff(closes) / np.asarray(closes[:-1])
    return float(np.std(rets, ddof=1) * math.sqrt(252)) or 0.25


def _snap(price: float, grid: float = 1.0) -> float:
    """Round to a plausible listed strike.

    Real chains are not evenly spaced, so this is an approximation — and one
    that slightly *disadvantages* the backtest, since a real desk would pick
    the listed strike closest to its target rather than a rounded one.
    """
    if price >= 200:
        grid = 5.0
    elif price >= 50:
        grid = 1.0
    else:
        grid = 0.5
    return round(round(price / grid) * grid, 2)


def _count_reasons(nights: list[NightResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for night in nights:
        if night.skip_reason:
            counts[night.skip_reason] = counts.get(night.skip_reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
