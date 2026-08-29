"""Replay the weekend book over Alpaca crypto history.

Rules of the replay, chosen to be pessimistic rather than flattering:

  * The signal is computed on bar `i` using only bars `<= i`. The lookback is
    allowed to reach back into weekday bars - the 24-hour mean on Friday night
    genuinely includes Friday's session, and pretending otherwise would fit a
    mean to eight hours of thin tape.
  * Entries fill at the NEXT bar's open, never at the close that generated the
    signal. A limit resting on the bid would sometimes do better; assuming so
    is how backtests lie.
  * Exits are checked low-first: if a bar's low touches the stop and its high
    touches the target, the stop wins. Intrabar path is unknowable, so it is
    resolved against us.
  * Every fill pays the cost model - fee, half spread and slippage - with the
    entry treated as resting and stops and flattens as crossing.
  * A gate tally is kept, because "why did it not trade" is the question this
    book has to answer more often than "how much did it make".
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from oaa.core.logging import get_logger
from oaa.strategies.weekend.clock import WindowPhase
from oaa.strategies.weekend.data import bar_time, cached_bars
from oaa.strategies.weekend.params import WeekendParams
from oaa.strategies.weekend.signals import (
    evaluate,
    stop_price,
    target_price,
    window_of,
)

log = get_logger("weekend.backtest")
UTC = dt.timezone.utc
Bar = dict[str, Any]


@dataclass
class Trade:
    symbol: str
    entered_at: dt.datetime
    entry: float
    qty: float
    stop: float
    target: float
    z: float
    sigma: float
    adx: float
    exited_at: dt.datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_bp: float = 0.0
    bars_held: int = 0

    @property
    def notional(self) -> float:
        return self.entry * self.qty

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["entered_at"] = self.entered_at.isoformat()
        row["exited_at"] = self.exited_at.isoformat() if self.exited_at else None
        return row


@dataclass
class BacktestResult:
    symbol: str
    start: dt.datetime
    end: dt.datetime
    equity_start: float
    trades: list[Trade] = field(default_factory=list)
    gate_rejections: Counter = field(default_factory=Counter)
    bars_scanned: int = 0
    weekends: int = 0

    # -- headline numbers -------------------------------------------------- #
    @property
    def pnl(self) -> float:
        return round(sum(t.pnl for t in self.trades), 2)

    @property
    def gross_pnl(self) -> float:
        return round(
            sum((t.exit_price - t.entry) * t.qty for t in self.trades if t.exit_price), 2
        )

    @property
    def cost_drag(self) -> float:
        return round(self.gross_pnl - self.pnl, 2)

    @property
    def return_pct(self) -> float:
        return round(self.pnl / self.equity_start, 5) if self.equity_start else 0.0

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def hit_rate(self) -> float:
        return round(self.wins / len(self.trades), 4) if self.trades else 0.0

    @property
    def avg_pnl_bp(self) -> float:
        return round(sum(t.pnl_bp for t in self.trades) / len(self.trades), 1) if self.trades else 0.0

    @property
    def avg_hold_hours(self) -> float:
        if not self.trades:
            return 0.0
        return round(sum(t.bars_held for t in self.trades) / len(self.trades) / 4, 2)

    @property
    def profit_factor(self) -> float | None:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return round(gains / losses, 2) if losses > 0 else None

    @property
    def max_drawdown(self) -> float:
        equity, peak, worst = self.equity_start, self.equity_start, 0.0
        for trade in self.trades:
            equity += trade.pnl
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return round(worst, 2)

    @property
    def exit_mix(self) -> dict[str, int]:
        return dict(Counter(t.exit_reason for t in self.trades))

    @property
    def trades_per_weekend(self) -> float:
        return round(len(self.trades) / self.weekends, 2) if self.weekends else 0.0

    def by_weekend(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for trade in self.trades:
            key = _weekend_key(trade.entered_at)
            row = out.setdefault(key, {"trades": 0, "pnl": 0.0, "wins": 0})
            row["trades"] += 1
            row["pnl"] = round(row["pnl"] + trade.pnl, 2)
            row["wins"] += 1 if trade.pnl > 0 else 0
        return dict(sorted(out.items()))

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "window": f"{self.start:%Y-%m-%d} to {self.end:%Y-%m-%d}",
            "weekends": self.weekends,
            "bars_scanned": self.bars_scanned,
            "trades": len(self.trades),
            "trades_per_weekend": self.trades_per_weekend,
            "hit_rate": self.hit_rate,
            "net_pnl": self.pnl,
            "gross_pnl": self.gross_pnl,
            "cost_drag": self.cost_drag,
            "return_pct": self.return_pct,
            "avg_pnl_bp": self.avg_pnl_bp,
            "avg_hold_hours": self.avg_hold_hours,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "exit_mix": self.exit_mix,
            "top_rejections": dict(self.gate_rejections.most_common(8)),
        }


# --------------------------------------------------------------------------- #
def run_backtest(
    params: WeekendParams,
    symbol: str | None = None,
    days: int = 365,
    equity: float = 100_000.0,
    end: dt.datetime | None = None,
    start: dt.datetime | None = None,
    bars: Sequence[Bar] | None = None,
    refresh: bool = False,
) -> BacktestResult:
    """`start` wins over `days` when both are given.

    A single-weekend replay is a legitimate request - "what would it have done
    last Saturday" - but note what it is: one sample. The summary reports
    `weekends` so a result computed from one of them is never mistaken for a
    distribution.
    """
    symbol = symbol or params.symbols[0]
    end = end or dt.datetime.now(UTC)
    start = start or (end - dt.timedelta(days=days))
    # The signal needs a 24h mean and a Wilder ADX before the window even
    # opens, so the DATA range is padded backwards past the replay range. Only
    # entries are confined to the window; the lookback may - and must - reach
    # into the weekday tape that formed the mean.
    warmup_bars = max(params.signal.min_bars, params.signal.lookback_bars + 1) + 40
    pad = dt.timedelta(minutes=15 * warmup_bars)
    series = list(bars) if bars is not None else cached_bars(
        symbol, params.signal.timeframe, start - pad, end, refresh=refresh
    )
    if len(series) < params.signal.min_bars + 10:
        raise ValueError(f"only {len(series)} bars for {symbol}; need history to replay")

    result = BacktestResult(symbol=symbol, start=start, end=end, equity_start=equity)
    window, sizing, exits, costs = params.window, params.sizing, params.exits, params.costs
    bars_per_hour = _bars_per_hour(params.signal.timeframe)

    open_trade: Trade | None = None
    cooldown_until: dt.datetime | None = None
    seen_weekends: set[str] = set()
    warmup = max(params.signal.min_bars, params.signal.lookback_bars + 1)

    for i in range(warmup, len(series) - 1):
        bar = series[i]
        now = bar_time(bar)
        phase = window.phase(now)
        if phase in {WindowPhase.OPEN, WindowPhase.MANAGE_ONLY}:
            seen_weekends.add(_weekend_key(now))

        # -- manage an open position first ---------------------------------- #
        if open_trade is not None:
            exit_reason, exit_price, crossing = _check_exit(
                open_trade, bar, now, phase, exits, bars_per_hour
            )
            if exit_reason:
                _close(open_trade, now, exit_price, exit_reason, crossing, costs)
                result.trades.append(open_trade)
                if exit_reason == "stop":
                    cooldown_until = now + dt.timedelta(hours=exits.cooldown_hours)
                open_trade = None
            else:
                open_trade.bars_held += 1
                continue

        # -- entries only inside the window --------------------------------- #
        if phase is not WindowPhase.OPEN:
            continue
        if cooldown_until and now < cooldown_until:
            result.gate_rejections["cooldown"] += 1
            continue
        if open_trade is not None:
            continue

        result.bars_scanned += 1
        # A BOUNDED window, not the whole prefix. Passing series[:i+1] made the
        # replay quadratic - at bar 38,000 every ADX call walked 38,000 bars -
        # and, worse, gave the replay a longer memory than the live book, which
        # fetches only a few days. Same slice on both sides now.
        signal = evaluate(symbol, window_of(series, params, i), params)
        if not signal.actionable:
            result.gate_rejections[signal.blocked_by or "unknown"] += 1
            continue

        fill = float(series[i + 1]["open"])
        stop = stop_price(fill, signal.atr, params)
        target = target_price(fill, signal.z or 0.0, signal.sigma or 0.0, params)
        qty = size_position(fill, stop, equity + result.pnl, sizing)
        if qty <= 0:
            result.gate_rejections["size"] += 1
            continue

        open_trade = Trade(
            symbol=symbol,
            entered_at=bar_time(series[i + 1]),
            entry=fill,
            qty=qty,
            stop=stop,
            target=target,
            z=round(signal.z or 0.0, 3),
            sigma=round(signal.sigma or 0.0, 5),
            adx=round(signal.adx or 0.0, 2),
        )

    # An open position at the end of the data is marked to the last close.
    if open_trade is not None:
        last = series[-1]
        _close(open_trade, bar_time(last), float(last["close"]), "end_of_data", True, costs)
        result.trades.append(open_trade)

    result.weekends = len(seen_weekends)
    return result


# --------------------------------------------------------------------------- #
def size_position(entry: float, stop: float, equity: float, sizing: Any) -> float:
    """Risk-first, exactly as the options books size from max loss.

    Two independent caps, the smaller wins: dollars at risk to the stop, and
    gross notional as a share of equity. The second is what keeps a very tight
    stop from buying a very large amount of bitcoin.
    """
    risk_per_unit = entry - stop
    if risk_per_unit <= 0 or equity <= 0:
        return 0.0
    by_risk = (equity * sizing.max_risk_per_trade_pct) / risk_per_unit
    by_notional = (equity * sizing.book_max_equity_pct) / entry
    qty = round(min(by_risk, by_notional), sizing.qty_decimals)
    return qty if qty * entry >= sizing.min_order_notional else 0.0


def _check_exit(
    trade: Trade,
    bar: Bar,
    now: dt.datetime,
    phase: WindowPhase,
    exits: Any,
    bars_per_hour: float,
) -> tuple[str | None, float, bool]:
    if phase is WindowPhase.FLATTEN or phase is WindowPhase.CLOSED:
        return "window_flatten", float(bar["close"]), True
    if float(bar["low"]) <= trade.stop:
        return "stop", trade.stop, True
    if float(bar["high"]) >= trade.target:
        return "target", trade.target, False
    if trade.bars_held >= exits.max_hold_hours * bars_per_hour:
        return "time_stop", float(bar["close"]), True
    return None, 0.0, False


def _close(
    trade: Trade,
    now: dt.datetime,
    price: float,
    reason: str,
    crossing: bool,
    costs: Any,
) -> None:
    trade.exited_at = now
    trade.exit_price = price
    trade.exit_reason = reason
    trade.pnl = costs.net_of_costs(trade.entry, price, trade.qty, crossing_exit=crossing)
    trade.pnl_bp = round(trade.pnl / trade.notional * 1e4, 1) if trade.notional else 0.0


def _bars_per_hour(timeframe: str) -> float:
    digits = "".join(c for c in timeframe if c.isdigit())
    minutes = int(digits) if digits else 15
    if "H" in timeframe.upper() and "MIN" not in timeframe.upper():
        minutes *= 60
    return 60 / minutes if minutes else 4.0


def _weekend_key(ts: dt.datetime) -> str:
    """Label a weekend by the Friday it started on."""
    friday = ts - dt.timedelta(days=(ts.weekday() - 4) % 7)
    return f"{friday:%Y-%m-%d}"


def sharpe_of_weekends(result: BacktestResult, equity: float) -> float | None:
    """Weekend-by-weekend Sharpe, annualised on 52 weekends. Trade-level
    Sharpe on a handful of trades is noise; the weekend is the natural unit."""
    rows = result.by_weekend()
    if len(rows) < 3:
        return None
    returns = [row["pnl"] / equity for row in rows.values()]
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = math.sqrt(var)
    return round(mean / sd * math.sqrt(52), 2) if sd > 0 else None
