"""Weekend book parameters.

Same discipline as the options books: strategy in YAML, secrets in .env. This
module is the schema for `config/strategies/weekend_crypto.yaml` and nothing
here is read from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from oaa.strategies.weekend.clock import WeekendWindow
from oaa.strategies.weekend.costs import CryptoCostModel

DEFAULT_PARAMS_PATH = "config/strategies/weekend_crypto.yaml"


@dataclass
class SignalParams:
    #: Bar size the whole stack runs on.
    timeframe: str = "15Min"
    #: Bars in the z-score lookback. 96 x 15min = 24h - one full crypto day,
    #: so the mean is a genuine daily anchor rather than a few hours of noise.
    lookback_bars: int = 96
    #: Entry threshold. Long when z <= -entry_z.
    entry_z: float = 2.0
    #: Exit threshold. Close when z >= -exit_z_target (i.e. back near the mean).
    exit_z: float = -0.25
    #: ADX above this means a trend, and a trend eats mean reversion alive.
    adx_max: float = 25.0
    #: ADX period, on the same bars.
    adx_period: int = 14
    #: Stand down if ADX is rising fast even below the ceiling - a trend being
    #: born looks like chop right up until it does not.
    adx_slope_max: float = 4.0
    #: Sigma floor, as a fraction. Below this the band is noise and a 2-sigma
    #: move cannot pay the round trip.
    min_sigma: float = 0.0035
    #: Sigma ceiling. Above this the distribution is not the one we fitted.
    max_sigma: float = 0.030
    #: Single-bar crash guard: refuse an entry when the last bar fell more
    #: than this. Do not catch a knife mid-liquidation; wait for the next bar.
    shock_bar_return: float = 0.015
    #: Required ratio of expected gross reversion to modelled round-trip cost.
    min_edge_multiple: float = 2.5
    #: Minimum bars before the stack will produce any read at all.
    min_bars: int = 120


@dataclass
class ExitParams:
    #: Hard stop as ATR multiple below entry.
    atr_stop_multiple: float = 1.5
    #: Floor and ceiling on the stop distance, as a fraction of entry.
    min_stop_pct: float = 0.008
    max_stop_pct: float = 0.035
    #: Give up on a trade that has not worked in this many hours. The thesis is
    #: a session-length dislocation; past that the position is just exposure.
    max_hold_hours: float = 10.0
    #: Cool-off after a stop-out, in hours. A stop means the regime read was
    #: wrong, and the next bar's z is even more negative - straight back in is
    #: how one bad read becomes four.
    cooldown_hours: float = 6.0


@dataclass
class SizingParams:
    #: Ceiling on the book's gross notional, as a fraction of account equity.
    book_max_equity_pct: float = 0.10
    #: Risk (entry to stop) per trade, as a fraction of account equity.
    max_risk_per_trade_pct: float = 0.02
    #: One position at a time per symbol; this caps concurrent symbols.
    max_concurrent_positions: int = 1
    #: Refuse to send an order smaller than this - the fee floor makes tiny
    #: crypto clips pointless.
    min_order_notional: float = 250.0
    #: Alpaca crypto quantities are fractional; this is the rounding.
    qty_decimals: int = 6


@dataclass
class ExecutionParams:
    #: Entries rest on the bid at this fraction of the spread from the mid.
    #: 0.0 = at the bid, 0.5 = mid.
    entry_limit_ratio: float = 0.25
    #: Exits at the target rest similarly; stops and the flatten cross.
    exit_limit_ratio: float = 0.75
    #: Crypto orders are GTC - Alpaca rejects `day` on a 24/7 asset.
    time_in_force: str = "gtc"
    #: Cancel and re-price an unfilled entry after this long.
    reprice_after_seconds: int = 180
    #: Seconds between engine polls inside the window.
    poll_seconds: int = 60
    dry_run: bool = True


@dataclass
class WeekendParams:
    enabled: bool = False
    symbols: list[str] = field(default_factory=lambda: ["BTC/USD"])
    book: str = "weekend"
    window: WeekendWindow = field(default_factory=WeekendWindow)
    signal: SignalParams = field(default_factory=SignalParams)
    exits: ExitParams = field(default_factory=ExitParams)
    sizing: SizingParams = field(default_factory=SizingParams)
    execution: ExecutionParams = field(default_factory=ExecutionParams)
    costs: CryptoCostModel = field(default_factory=CryptoCostModel)

    def describe(self) -> str:
        s, x = self.signal, self.exits
        return (
            f"{'/'.join(self.symbols)} {s.timeframe} | enter z<=-{s.entry_z} exit z>={s.exit_z} "
            f"| ADX<{s.adx_max} | stop {x.atr_stop_multiple}xATR | "
            f"cost {self.costs.round_trip_bp:.0f}bp round trip"
        )


# --------------------------------------------------------------------------- #
def _build(cls: type, payload: dict[str, Any]) -> Any:
    """Strict-ish dataclass construction: unknown keys are an error, because a
    typo in a risk parameter that silently does nothing is the worst failure
    mode a config file has."""
    known = {f.name for f in fields(cls)}
    unknown = set(payload) - known
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown keys {sorted(unknown)}")
    return cls(**payload)


def load_params(path: str | Path = DEFAULT_PARAMS_PATH) -> WeekendParams:
    raw: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p} must contain a YAML mapping")

    return WeekendParams(
        enabled=bool(raw.get("enabled", False)),
        symbols=[s.upper() for s in raw.get("symbols", ["BTC/USD"])],
        book=str(raw.get("book", "weekend")),
        window=_build(WeekendWindow, raw.get("window", {})),
        signal=_build(SignalParams, raw.get("signal", {})),
        exits=_build(ExitParams, raw.get("exits", {})),
        sizing=_build(SizingParams, raw.get("sizing", {})),
        execution=_build(ExecutionParams, raw.get("execution", {})),
        costs=_build(CryptoCostModel, raw.get("costs", {})),
    )
