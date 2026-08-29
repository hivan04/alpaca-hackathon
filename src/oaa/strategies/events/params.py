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
    #: Featherless serves open-weight models; pin one and a seed so a run can
    #: be repeated. None inherits `agents.llm` from the main config.
    model: str | None = None
    seed: int | None = 11


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
    """The option expression: a vertical debit spread in the called direction."""

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
        sizing=_build(SizingParams, raw.get("sizing"), "sizing"),
        structure=_build(StructureParams, structure_raw, "structure"),
        schedule=_build(ScheduleParams, raw.get("schedule"), "schedule"),
    )
