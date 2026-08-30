"""Typed parameters for the events book.

Everything tunable lives in `config/strategies/earnings_event.yaml` and is
loaded once into these frozen dataclasses. The strategy never reads raw dicts:
a typo in the YAML becomes an error at load time rather than a silently missing
gate at 15:45 on the day of a print.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from oaa.core.errors import ConfigError

DEFAULT_PARAMS_PATH = "config/strategies/earnings_event.yaml"
DEFAULT_CALENDAR_PATH = "config/events/earnings_calendar.json"


def _build(cls: type, raw: dict[str, Any] | None, where: str) -> Any:
    """Construct a params dataclass, rejecting keys it does not define.

    An unknown key is almost always a rename that did not land everywhere, and
    the failure mode of ignoring it is a gate that quietly stops applying.
    """
    raw = dict(raw or {})
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
    return cls(**raw)


@dataclass(frozen=True)
class ScreenParams:
    """The volatility screen that ranks confirmed events."""

    #: How many names survive to the direction stage. The LLM stage costs a
    #: call per name, and every extra name is another position competing for
    #: the same overnight capital.
    top_n: int = 10
    #: Skip a name whose front-weekly straddle prices a move this small: there
    #: is no event to trade, and the debit still crosses two spreads.
    min_implied_move_pct: float = 3.0
    #: Round-trip spread as a fraction of the DEBIT paid. A vertical crosses
    #: four half-spreads on a round trip. This is the gate that actually
    #: removes candidates - see README.
    max_relative_spread: float = 0.25
    #: Below this the option is priced in nickels and one tick is a full
    #: percent of the debit.
    min_option_price: float = 0.30
    #: Per-contract price ceiling for this book's chain. None = no ceiling,
    #: which is now also the global default: the cap used to be $25 and on
    #: the 1-4 Sep calendar it stripped the near-the-money contracts from
    #: MDB (ATM leg ~$37) and DELL (~$26) without refusing either trade.
    #: Left here so a ceiling can be re-imposed for this book alone.
    max_option_price: float | None = None
    #: Rank by |implied - realised| rather than the raw ratio, so a name whose
    #: options are cheap ranks alongside one whose options are rich. Direction
    #: is decided later; this stage only asks "is there a real event here".
    rank_by: str = "abs_divergence"


@dataclass(frozen=True)
class SentimentParams:
    """What text the model is allowed to read, and how much of it."""

    news_lookback_days: int = 7
    max_headlines: int = 25
    stocktwits_enabled: bool = True
    max_messages: int = 30
    stocktwits_url: str = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
    timeout_seconds: float = 8.0
    #: Hard cap on characters handed to the model per name. Third-party text is
    #: untrusted input; a smaller window is both cheaper and a smaller target.
    max_chars: int = 12000


@dataclass(frozen=True)
class DirectionParams:
    """The LLM call that predicts post-print direction."""

    #: Below this the model has not found enough to act on and the name is
    #: skipped. An abstention is a valid, and common, answer.
    min_confidence: float = 0.55
    #: A model that never abstains is not filtering. Recorded per run so the
    #: abstention rate can be checked against this expectation.
    expected_abstention_rate: float = 0.40
    #: Refuse a call that cites no source. The prompt asks for the evidence it
    #: used; an empty list means it reasoned from priors, which for a specific
    #: company's specific quarter is a guess.
    require_evidence: bool = True
    max_tokens: int = 1200
    temperature: float = 0.1
    #: Replay only. The engine supplies the direction call live, so a context
    #: arriving WITHOUT one is a backtest by construction - there is no LLM in
    #: the replay loop. With this on, the direction is derived from the tape
    #: instead (band position, above or below the Bollinger midline) and the
    #: idea is tagged `derived` so it can never be mistaken in the journal for
    #: a model's call. It is what makes the technical layer measurable on its
    #: own: run it and you are asking "does the setup have any edge before the
    #: LLM is added", which is the only honest way to find out.
    derive_from_tape_when_no_call: bool = True
    #: Confidence assigned to a derived call. Deliberately at the floor: a
    #: mechanical read is not a conviction read, and it should size like it.
    derived_confidence: float = 0.55
    #: Featherless serves open-weight models; pin one and a seed so a run can
    #: be repeated. None inherits `agents.llm` from the main config.
    model: str | None = None
    seed: int | None = 11


@dataclass(frozen=True)
class TechnicalParams:
    """Three indicators, three jobs. See `technicals.py` for why each is
    confined to the one it has."""

    enabled: bool = True
    #: Daily bars pulled back for the indicator stack.
    bars_lookback: int = 120

    # -- Bollinger: the setup ------------------------------------------- #
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    #: Window the current band width is ranked against.
    width_lookback: int = 100
    #: A squeeze is width in the bottom quartile of its own recent range.
    squeeze_max_percentile: float = 0.25
    #: When False the squeeze is measured and recorded but does not veto -
    #: useful for measuring how much the gate actually costs in a backtest
    #: before committing to it.
    require_squeeze: bool = True

    # -- RSI: the veto --------------------------------------------------- #
    rsi_period: int = 14
    #: One-sided extremes only. RSI is never an entry signal here: a short into
    #: a tape already at 18 is a short into the bounce.
    rsi_oversold: float = 20.0
    rsi_overbought: float = 80.0

    # -- ATR: size and stop, never entry --------------------------------- #
    atr_period: int = 14
    #: Stop distance on the UNDERLYING, in ATRs. Wide on purpose: the job is to
    #: survive post-print noise, not to be tight.
    atr_stop_multiple: float = 2.0
    #: ATR as a fraction of spot at which a name takes FULL size. Above this,
    #: size scales down proportionally; below it, no bonus.
    atr_reference_pct: float = 0.02
    atr_min_size_multiple: float = 0.40


@dataclass(frozen=True)
class SizingParams:
    """Confidence -> contracts. Higher conviction, bigger bet, bounded."""

    #: Fraction of account equity risked on the WHOLE book of event trades in
    #: one night. Every position's max loss is charged against this.
    nightly_risk_budget_pct: float = 0.04
    #: Fraction of equity a single name may risk at full confidence.
    max_risk_per_trade_pct: float = 0.012
    #: Confidence maps linearly onto [min_size_multiple, 1.0] across
    #: [DirectionParams.min_confidence, 1.0]. At the floor you take the
    #: smallest position the structure allows, not zero.
    min_size_multiple: float = 0.30
    min_contracts: int = 1
    max_contracts: int = 10


@dataclass(frozen=True)
class StructureParams:
    """The option expression, chosen by the SIGN of the vol divergence.

    The screen measures one thing: how far the option's implied move sits from
    what this name actually did on its last four prints. That is a volatility
    reading, and until 30 Aug it was expressed as a vertical debit spread -
    a DIRECTIONAL bet whose payoff is orthogonal to the quantity measured.

    The consequence is arithmetic, not empirical. A directional structure
    bought at a fair-to-rich implied move, with a direction call no better than
    a coin flip, has expectancy of minus the round trip. No sample size fixes
    that and no gate tightening rescues it; the edge that was measured simply
    is not the edge the structure collects.

    So the expression now follows the sign:

      * ratio >= `rich_ratio_threshold` - the market is charging more than the
        stock has historically paid. SELL premium, defined risk, shorts outside
        the implied move. The edge collected is the overpricing itself.
      * ratio <= `cheap_ratio_threshold` - the market is charging less than the
        stock has paid. BUY the move as a vertical debit in the called
        direction, which is the one case where a directional structure is
        buying something measurably cheap.
      * in between - no measured mispricing, no trade. This band is where the
        book used to do all of its trading.

    The direction call stops being the thesis and becomes a tilt: it skews the
    condor's short strikes, and it still picks the side of the debit vertical.
    """

    #: The expiry must contain the print. 1-9 days covers the front weekly for
    #: a Monday-to-Thursday report.
    dte_window: tuple[int, int] = (1, 9)
    long_delta: float = 0.45
    short_delta: float = 0.25
    #: Debit as a fraction of strike width. Above this the spread is paying for
    #: most of its own maximum profit.
    max_debit_to_width: float = 0.45
    #: Reward:risk floor after the actual strikes are priced.
    min_reward_risk: float = 1.2

    # -- expression selection ------------------------------------------- #
    #: false restores the pre-30-Aug behaviour: always a debit vertical,
    #: whatever the divergence says. Kept so the change can be measured
    #: against the book it replaced rather than asserted.
    expression_follows_divergence: bool = True
    #: implied/realised at or above which the options are rich enough to sell.
    #: 1.35 is deliberately well clear of 1.0: the ratio is computed off four
    #: prints, so a name at 1.1 is inside its own sampling error.
    rich_ratio_threshold: float = 1.35
    #: implied/realised at or below which they are cheap enough to buy.
    cheap_ratio_threshold: float = 0.80

    # -- the short-premium expression ----------------------------------- #
    #: Where the short strikes sit, as a multiple of the event's own IMPLIED
    #: MOVE. This is the thesis, not a tuning dial: at 1.0 the market has to
    #: be wrong about the SIZE of the move before the position loses, which is
    #: exactly the claim the screen makes when the ratio is rich. Strikes are
    #: placed here and the delta falls out - not the other way round, because
    #: the front-weekly surface across a print is too deformed for a delta to
    #: stand in for distance. See `iron_condor_outside_move`.
    shorts_at_implied_move: float = 1.0
    #: Extra multiples added to the side the direction call points AT. Only
    #: ever pushes a short further out, never pulls one in - collecting more
    #: premium by moving a short inside the implied move would surrender the
    #: one property this structure is built on.
    condor_direction_tilt: float = 0.25
    #: Wings as a FRACTION OF SPOT, so one number works across a universe
    #: spanning $4 to $600.
    condor_wing_pct: float = 0.04
    #: Credit as a fraction of the widest wing. Below this the structure is
    #: risking most of the width to collect very little, which is how a
    #: premium sale turns into a lottery ticket sold at cost.
    min_credit_to_width: float = 0.18
    #: Floor on what the LISTED strikes actually delivered. Strike-grid
    #: snapping can pull a short back inside the priced move on a coarse
    #: ladder; below this the structure is no longer the trade that was
    #: reasoned about and is declined rather than quietly downgraded.
    min_shorts_clearance: float = 0.85


@dataclass(frozen=True)
class ScheduleParams:
    """When the book acts. US/Eastern, HH:MM."""

    #: Arm shortly before the close on the session BEFORE an after-close print
    #: (or before the open on the day of a before-open print).
    arm_time: str = "15:45"
    #: Close into the post-print vol crush the following morning.
    exit_time: str = "09:45"
    #: Never open inside this many minutes of the close - a late fill on a wide
    #: quote is the one execution risk this schedule cannot manage away.
    no_entry_after: str = "15:55"
    #: Hard flatten. Nothing from this book survives the session after the print.
    hard_exit_time: str = "10:30"


@dataclass(frozen=True)
class EventsParams:
    book: str = "events"
    calendar_path: str = DEFAULT_CALENDAR_PATH
    #: Names the LLM screener is nudged towards. It may propose others; every
    #: proposal is verified against the calendar file regardless.
    universe_hint: list[str] = field(default_factory=list)
    screen: ScreenParams = field(default_factory=ScreenParams)
    sentiment: SentimentParams = field(default_factory=SentimentParams)
    direction: DirectionParams = field(default_factory=DirectionParams)
    technicals: TechnicalParams = field(default_factory=TechnicalParams)
    sizing: SizingParams = field(default_factory=SizingParams)
    structure: StructureParams = field(default_factory=StructureParams)
    schedule: ScheduleParams = field(default_factory=ScheduleParams)

    def arm_at(self) -> dt.time:
        return _hhmm(self.schedule.arm_time)

    def exit_at(self) -> dt.time:
        return _hhmm(self.schedule.exit_time)


def _hhmm(value: str) -> dt.time:
    try:
        hour, minute = (int(part) for part in str(value).split(":"))
        return dt.time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"bad HH:MM time '{value}'") from exc


def load_params(path: str | Path = DEFAULT_PARAMS_PATH) -> EventsParams:
    """Read the YAML into typed params. Missing file -> documented defaults."""
    source = Path(path)
    raw: dict[str, Any] = {}
    if source.exists():
        loaded = yaml.safe_load(source.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{source}: expected a mapping at the top level")
        raw = loaded

    structure_raw = dict(raw.get("structure") or {})
    if "dte_window" in structure_raw:
        structure_raw["dte_window"] = tuple(structure_raw["dte_window"])

    return EventsParams(
        book=raw.get("book", "events"),
        calendar_path=raw.get("calendar_path", DEFAULT_CALENDAR_PATH),
        universe_hint=[s.upper() for s in (raw.get("universe_hint") or [])],
        screen=_build(ScreenParams, raw.get("screen"), "screen"),
        sentiment=_build(SentimentParams, raw.get("sentiment"), "sentiment"),
        direction=_build(DirectionParams, raw.get("direction"), "direction"),
        technicals=_build(TechnicalParams, raw.get("technicals"), "technicals"),
        sizing=_build(SizingParams, raw.get("sizing"), "sizing"),
        structure=_build(StructureParams, structure_raw, "structure"),
        schedule=_build(ScheduleParams, raw.get("schedule"), "schedule"),
    )
