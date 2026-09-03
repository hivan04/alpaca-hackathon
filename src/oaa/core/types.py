"""The domain model.

These types are the contract between layers. A strategy emits a TradeIdea; the
risk engine turns it into a RiskVerdict; the executor turns an approved idea
into an OrderTicket; the broker returns Fills. No layer reaches past its
neighbour, which is what makes any one of them swappable.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------- #
# enums
# --------------------------------------------------------------------------- #
class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Right(str, Enum):
    CALL = "call"
    PUT = "put"


class Intent(str, Enum):
    BUY_TO_OPEN = "buy_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_OPEN = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"

    @classmethod
    def opening(cls, side: Side) -> Intent:
        return cls.BUY_TO_OPEN if side is Side.BUY else cls.SELL_TO_OPEN

    @classmethod
    def closing(cls, side: Side) -> Intent:
        return cls.BUY_TO_CLOSE if side is Side.BUY else cls.SELL_TO_CLOSE


class StructureType(str, Enum):
    """Every structure here has a bounded maximum loss by construction."""

    SINGLE_LONG = "single_long"
    VERTICAL_DEBIT = "vertical_debit"
    VERTICAL_CREDIT = "vertical_credit"
    IRON_CONDOR = "iron_condor"
    IRON_BUTTERFLY = "iron_butterfly"
    CALENDAR = "calendar"
    DIAGONAL = "diagonal"
    STRADDLE = "straddle"
    STRANGLE = "strangle"
    RATIO = "ratio"

    @property
    def is_defined_risk(self) -> bool:
        return self not in {
            StructureType.STRADDLE,
            StructureType.STRANGLE,
            StructureType.RATIO,
        }

    @property
    def is_multileg(self) -> bool:
        return self is not StructureType.SINGLE_LONG


class DecisionAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    ADJUST = "adjust"
    HOLD = "hold"
    SKIP = "skip"


# --------------------------------------------------------------------------- #
# market data
# --------------------------------------------------------------------------- #
class Greeks(Model):
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None


class OptionQuote(Model):
    """One contract, as we see it right now."""

    symbol: str
    underlying: str
    expiry: dt.date
    strike: float
    right: Right
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    implied_volatility: float | None = None
    #: Where `implied_volatility` came from. None = the feed quoted it (live).
    #: In replay `backtest/realchain.py` sets one of "recovered from the traded
    #: price", "modelled (no bar)" or "modelled (price carries no vega)".
    #: It used to compute all three and then discard them on the next line,
    #: which meant nothing downstream could tell a measured surface from an
    #: invented one - and the modelled surface's term structure is a CONSTANT
    #: from config, so a signal read off it fires identically forever.
    iv_source: str | None = None
    greeks: Greeks = Field(default_factory=Greeks)
    open_interest: int | None = None
    volume: int | None = None
    asof: dt.datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.last

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        if mid and self.bid is not None and self.ask is not None and mid > 0:
            return round((self.ask - self.bid) / mid, 4)
        return None

    def dte(self, asof: dt.date | None = None) -> int:
        return (self.expiry - (asof or dt.date.today())).days

    def is_liquid(self, max_spread_pct: float, min_oi: int, min_volume: int) -> bool:
        if self.bid is None or self.ask is None or self.bid <= 0:
            return False
        if self.spread_pct is None or self.spread_pct > max_spread_pct:
            return False
        if self.open_interest is not None and self.open_interest < min_oi:
            return False
        if self.volume is not None and self.volume < min_volume:
            return False
        return True


class TermStructure(Model):
    """The ATM implied-vol slope between a front and a back expiry.

    Computed by `oaa.data.term_structure`, which is the only thing that should
    build one. `measured` is load-bearing: False means at least one anchor came
    off a model, and in replay the modelled surface's term slope is a constant
    from `backtest.chain.term_slope`. Gating on an unmeasured slope is gating
    on a config value.
    """

    front_expiry: dt.date
    back_expiry: dt.date
    front_dte: int
    back_dte: int
    front_iv: float
    back_iv: float
    #: front_iv - back_iv, in vol points.
    slope: float
    #: (front_iv - back_iv) / back_iv. Scale-free, and the one to gate on: a
    #: 2-point slope means different things on a 12-vol and a 45-vol name, and
    #: this universe spans both.
    slope_pct: float
    measured: bool = False
    source: str = ""

    @property
    def backwardated(self) -> bool:
        return self.slope > 0


class MarketContext(Model):
    """Everything a strategy is allowed to look at for one underlying.

    Strategies read this snapshot and nothing else - no live calls from inside
    a strategy. That keeps them deterministic and cheap to backtest.
    """

    symbol: str
    asof: dt.datetime
    spot: float
    prev_close: float | None = None
    bars: list[dict[str, Any]] = Field(default_factory=list)
    #: Intraday bars for the momentum book, spanning `data.intraday_lookback_days`
    #: sessions - the same window the live provider fetches. `vwap_series`
    #: restarts on each day boundary, so a multi-day window still yields a
    #: SESSION VWAP; what the extra days buy is a time-of-day volume baseline
    #: and enough bars for the momentum book's `min_bars` floor early in a
    #: session. Replay carrying only the current day made the backtest a
    #: strictly more restrictive strategy than the live one.
    intraday_bars: list[dict[str, Any]] = Field(default_factory=list)
    chain: list[OptionQuote] = Field(default_factory=list)
    realised_vol: float | None = None
    implied_vol: float | None = None
    iv_rank: float | None = None
    #: ATM IV term structure, front vs back expiry. None means the chain could
    #: not answer - NOT that the surface is flat. A strategy that reads None as
    #: 0.0 has invented a measurement.
    term_structure: TermStructure | None = None
    trend_strength: float | None = None
    adx: float | None = None
    volume_ratio: float | None = None
    earnings_date: dt.date | None = None
    news: list[dict[str, Any]] = Field(default_factory=list)
    # Technology-partner adapters write here. Core code never assumes a key.
    enrichment: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def iv_rv_ratio(self) -> float | None:
        if self.implied_vol and self.realised_vol and self.realised_vol > 0:
            return round(self.implied_vol / self.realised_vol, 4)
        return None

    def expiries(self) -> list[dt.date]:
        return sorted({q.expiry for q in self.chain})


class Signal(Model):
    """A strategy's directional/vol read, before it becomes a structure."""

    symbol: str
    source: str
    direction: Literal["bullish", "bearish", "neutral"] = "neutral"
    strength: float = 0.0  # 0..1
    horizon_days: int = 21
    rationale: str = ""
    features: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# trade construction
# --------------------------------------------------------------------------- #
class AssetKind(str, Enum):
    OPTION = "option"
    EQUITY = "equity"
    #: Spot crypto (BTC/USD). Fractional quantities, no contract multiplier,
    #: no position_intent, and `day` is not a valid time-in-force on a 24/7
    #: asset. The weekend book is the only thing that emits these.
    CRYPTO = "crypto"


class Leg(Model):
    symbol: str            # OCC symbol for options, ticker for equities
    side: Side
    ratio: int = 1
    intent: Intent | None = None
    quote: OptionQuote | None = None
    limit_price: float | None = None
    kind: AssetKind = AssetKind.OPTION
    #: Absolute share count for an equity leg. Options use `ratio` x order qty.
    qty: float | None = None

    @property
    def is_equity(self) -> bool:
        """True for anything that is not an option contract.

        Kept deliberately broad: every call site that asks this is really
        asking "should I skip the options-only plumbing (position_intent, the
        x100 multiplier, OCC parsing)?", and for spot crypto the answer is the
        same as for a share.
        """
        return self.kind in {AssetKind.EQUITY, AssetKind.CRYPTO}

    @property
    def is_option(self) -> bool:
        return self.kind is AssetKind.OPTION

    @property
    def is_crypto(self) -> bool:
        return self.kind is AssetKind.CRYPTO

    def resolved_intent(self, closing: bool = False) -> Intent:
        if self.intent:
            return self.intent
        return Intent.closing(self.side) if closing else Intent.opening(self.side)


class TradeIdea(Model):
    """A fully-specified, priced structure a strategy wants to put on."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    symbol: str
    strategy: str
    structure: StructureType
    legs: list[Leg]
    quantity: int = 1
    #: Which capital book this belongs to. The temporal firewall gates on it.
    book: str = "intraday"
    # Positive = net debit paid, negative = net credit received (Alpaca's
    # convention for multi-leg limit prices).
    net_price: float = 0.0
    max_loss: float | None = None       # per structure, in dollars
    max_profit: float | None = None
    probability_of_profit: float | None = None
    thesis: str = ""
    confidence: float = 0.5
    score: float | None = None
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_credit(self) -> bool:
        return self.net_price < 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def leg_count(self) -> int:
        return len(self.legs)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_risk(self) -> float | None:
        """Dollar risk for the whole order, not one structure."""
        return None if self.max_loss is None else round(self.max_loss * self.quantity, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reward_risk(self) -> float | None:
        if self.max_loss and self.max_profit and self.max_loss > 0:
            return round(self.max_profit / self.max_loss, 3)
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_crypto(self) -> bool:
        return any(leg.is_crypto for leg in self.legs)

    def net_cash(self) -> float:
        """Signed dollars: negative = cash leaves the account.

        Options are quoted per share and trade in 100-lots; spot crypto is
        quoted in dollars and trades in fractions. Applying the option
        multiplier to a coin overstates the cash movement by 100x, which is the
        sort of error that only shows up as a buying-power rejection.
        """
        multiplier = 1 if self.is_crypto else 100
        return round(-self.net_price * multiplier * self.quantity, 2)

    def describe(self) -> str:
        legs = " / ".join(f"{leg.side.value[0].upper()}{leg.ratio} {leg.symbol}" for leg in self.legs)
        kind = "credit" if self.is_credit else "debit"
        return (
            f"{self.symbol} {self.structure.value} x{self.quantity} "
            f"@ {abs(self.net_price):.2f} {kind} [{legs}]"
        )


class RiskVerdict(Model):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    adjusted_quantity: int | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
    #: What the engine MEASURED, not merely whether it passed. Portfolio delta,
    #: vega, notional and the coverage of the Greek recovery land here on every
    #: verdict including approvals - a limit that only leaves a trace when it
    #: fires cannot be shown to have been binding, or to have been calibrated.
    metrics: dict[str, Any] = Field(default_factory=dict)
    stamp: str | None = None   # execution refuses tickets without this

    @classmethod
    def approve(cls, quantity: int, checks: dict[str, bool] | None = None) -> RiskVerdict:
        return cls(
            approved=True,
            adjusted_quantity=quantity,
            checks=checks or {},
            stamp=uuid.uuid4().hex[:16],
        )

    @classmethod
    def reject(cls, reason: str, checks: dict[str, bool] | None = None) -> RiskVerdict:
        return cls(approved=False, reasons=[reason], checks=checks or {})


class OrderTicket(Model):
    """What actually goes to the broker."""

    idea_id: str
    client_order_id: str
    symbol: str
    legs: list[Leg]
    quantity: int
    order_type: Literal["limit", "market"] = "limit"
    limit_price: float | None = None
    time_in_force: Literal["day", "gtc"] = "day"
    risk_stamp: str | None = None
    dry_run: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_multileg(self) -> bool:
        return len(self.legs) > 1


class Fill(Model):
    order_id: str
    client_order_id: str | None = None
    symbol: str
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    submitted_at: dt.datetime | None = None
    filled_at: dt.datetime | None = None
    legs: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status in {"filled", "partially_filled"}


# --------------------------------------------------------------------------- #
# account state
# --------------------------------------------------------------------------- #
class PositionSnapshot(Model):
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float = 0.0
    unrealized_pl: float = 0.0
    unrealized_plpc: float = 0.0
    asset_class: str = "us_option"
    underlying: str | None = None
    expiry: dt.date | None = None
    strike: float | None = None
    right: Right | None = None

    @property
    def is_option(self) -> bool:
        return self.asset_class == "us_option"

    @property
    def is_short(self) -> bool:
        return self.qty < 0


class AccountSnapshot(Model):
    account_id: str | None = None
    equity: float = 0.0
    last_equity: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    options_buying_power: float | None = None
    #: Overnight limit (2x). This is the number the overnight book sizes against.
    regt_buying_power: float | None = None
    #: Intraday limit (4x). Never available to a position held past the close.
    daytrading_buying_power: float | None = None
    multiplier: float | None = None
    shorting_enabled: bool | None = None
    daytrade_count: int | None = None
    pattern_day_trader: bool | None = None
    options_trading_level: int | None = None
    positions: list[PositionSnapshot] = Field(default_factory=list)
    open_orders: int = 0
    asof: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def day_pl(self) -> float:
        return round(self.equity - self.last_equity, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def day_pl_pct(self) -> float:
        if self.last_equity <= 0:
            return 0.0
        return round((self.equity - self.last_equity) / self.last_equity, 5)

    def option_positions(self) -> list[PositionSnapshot]:
        return [p for p in self.positions if p.is_option]

    def by_underlying(self, symbol: str) -> list[PositionSnapshot]:
        return [p for p in self.positions if (p.underlying or p.symbol) == symbol]

    def equity_positions(self) -> list[PositionSnapshot]:
        return [p for p in self.positions if not p.is_option]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_flat(self) -> bool:
        """Truly nothing on: no positions and no working orders."""
        return not self.positions and self.open_orders == 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def leverage_headroom(self) -> float | None:
        """How much of the overnight limit is already consumed, 0..1.

        Above 1.0 means the book is carrying more than Reg T permits overnight -
        the state that gets an account force-liquidated at the close.
        """
        if not self.regt_buying_power or self.regt_buying_power <= 0:
            return None
        gross = sum(abs(p.market_value) for p in self.positions)
        return round(gross / self.regt_buying_power, 4)


class Decision(Model):
    """One row of the audit trail. Every cycle appends these to the journal.

    This is the artefact the judges read: what the agent saw, what it chose,
    and why - including the trades it declined to make.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    cycle: str = "manual"
    action: DecisionAction = DecisionAction.SKIP
    symbol: str | None = None
    strategy: str | None = None
    idea: TradeIdea | None = None
    verdict: RiskVerdict | None = None
    fill: Fill | None = None
    rationale: str = ""
    #: Realised P&L on a CLOSE, in dollars, as marked at the moment the close
    #: was confirmed. `manage_positions` already holds it on the position
    #: snapshot; without it on the decision the daily report has no per-book
    #: P&L at all and prints +0.00 across every strategy on a winning day.
    #: `telemetry/daily.py` reads exactly this key.
    realized_pl: float | None = None
    agent_notes: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
