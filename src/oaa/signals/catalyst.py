"""Catalyst confirmation - the non-generic layer of the intraday book.

A VWAP cross with no reason behind it is noise, and noise mean-reverts. A VWAP
cross on a dated catalyst is a move with a *mechanism*, and mechanisms persist.
Everything above this line in the intraday signal stack ships with every
charting platform on earth; this gate is what makes the book a strategy rather
than a default.

Sources are Alpaca-native and reuse the existing discovery layer - no scraper,
no ToS exposure, nothing to break on day three:

    news            headline presence, recency-decayed relevance to the symbol
    movers          is the whole tape moving, or just this name?
    most-actives    volume confirmation independent of the bar data

The three are blended after **rank-normalisation within each source**, because
raw units across sources are not comparable. News carries the highest weight
because it is the only factor with a mechanism attached: breadth and volume
tell you a move is happening, news tells you why.

Polarity note. The macro lens's shared/idiosyncratic judgement ports across
from the carry book with the sign INVERTED:

                    carry book (short vol)      intraday book (long vol)
    shared          safe to sell premium        TRADABLE - a broad move continues
    idiosyncratic   veto - fat tail             barely applies on an index universe
    no catalyst     fine, quiet is the point    VETO - the move has no mechanism
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaa.core.logging import get_logger
from oaa.signals.gates import GateResult

log = get_logger("signals.catalyst")

DEFAULT_WEIGHTS = {"news": 0.50, "breadth": 0.35, "volume": 0.15}


# --------------------------------------------------------------------------- #
# scheduled macro events
# --------------------------------------------------------------------------- #
@dataclass
class MacroEvent:
    name: str
    when: dt.datetime
    kind: str = "macro"
    importance: str = "high"

    def minutes_from(self, moment: dt.datetime) -> float:
        base = moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)
        return (self.when - base).total_seconds() / 60.0


@dataclass
class MacroCalendar:
    """A static, committed calendar of CPI / FOMC / NFP / PMI prints.

    Deliberately a file in the repo rather than a live feed. A live dependency
    that fails on the morning of the print fails at exactly the moment it was
    needed, and there is no upside to that trade.
    """

    events: list[MacroEvent] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None) -> MacroCalendar:
        if not path:
            return cls()
        target = Path(path)
        if not target.exists():
            log.info("no macro calendar at %s - the scheduled-event branch is inert", target)
            return cls()
        try:
            import yaml

            raw = yaml.safe_load(target.read_text()) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the macro calendar (%s)", exc)
            return cls()

        events: list[MacroEvent] = []
        for row in raw.get("events", []) or []:
            when = row.get("when") or row.get("at")
            parsed = _parse_when(when)
            if parsed is None:
                continue
            events.append(MacroEvent(
                name=str(row.get("name", "scheduled event")),
                when=parsed,
                kind=str(row.get("kind", "macro")),
                importance=str(row.get("importance", "high")),
            ))
        events.sort(key=lambda e: e.when)
        log.info("macro calendar: %d scheduled event(s) loaded", len(events))
        return cls(events=events)

    def within(
        self, moment: dt.datetime, minutes_before: int = 60, minutes_after: int = 120
    ) -> list[MacroEvent]:
        return [
            e for e in self.events
            if -minutes_after <= e.minutes_from(moment) <= minutes_before
        ]

    def between(self, start: dt.date, end: dt.date) -> list[MacroEvent]:
        return [e for e in self.events if start <= e.when.date() <= end]

    def next_event(self, moment: dt.datetime) -> MacroEvent | None:
        upcoming = [e for e in self.events if e.minutes_from(moment) >= 0]
        return upcoming[0] if upcoming else None


def _parse_when(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time(13, 30), tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


# --------------------------------------------------------------------------- #
# the view
# --------------------------------------------------------------------------- #
@dataclass
class CatalystView:
    """One symbol's catalyst read at one moment."""

    symbol: str
    asof: dt.datetime
    news_count: int = 0
    max_relevance: float = 0.0
    news_score: float = 0.0
    breadth_up_pct: float | None = None
    breadth_score: float = 0.0
    volume_score: float = 0.0
    score: float = 0.0
    scheduled_event: str | None = None
    headlines: list[str] = field(default_factory=list)
    source: str = "rules"

    def breadth_agrees(self, bullish: bool, minimum: float) -> bool:
        if self.breadth_up_pct is None:
            return False
        aligned = self.breadth_up_pct if bullish else 1.0 - self.breadth_up_pct
        return aligned >= minimum

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": self.asof.isoformat(),
            "news_count": self.news_count,
            "max_relevance": round(self.max_relevance, 4),
            "news_score": round(self.news_score, 4),
            "breadth_up_pct": self.breadth_up_pct,
            "breadth_score": round(self.breadth_score, 4),
            "volume_score": round(self.volume_score, 4),
            "score": round(self.score, 4),
            "scheduled_event": self.scheduled_event,
            "headlines": self.headlines[:3],
            "source": self.source,
        }


def _rank_normalise(values: dict[str, float]) -> dict[str, float]:
    """Percentile rank within one source. Raw units across sources are not
    comparable, and blending them unnormalised silently lets whichever source
    has the largest numbers dominate the weights."""
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 1.0}
    return {sym: round(i / (n - 1), 4) for i, (sym, _) in enumerate(ordered)}


class CatalystEngine:
    """Builds `CatalystView`s from the discovery snapshot plus per-symbol news."""

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        lookback_minutes: int = 30,
        calendar: MacroCalendar | None = None,
    ) -> None:
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.lookback_minutes = lookback_minutes
        self.calendar = calendar or MacroCalendar()

    # ------------------------------------------------------------------ #
    def view(
        self,
        symbol: str,
        now: dt.datetime,
        news: list[dict[str, Any]] | None = None,
        snapshot: Any = None,
    ) -> CatalystView:
        moment = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
        view = CatalystView(symbol=symbol.upper(), asof=moment)

        # -- news: count and recency-decayed relevance --------------------- #
        fresh = _recent_news(news or [], moment, self.lookback_minutes)
        view.news_count = len(fresh)
        view.headlines = [str(a.get("headline") or a.get("title") or "")[:180] for a in fresh[:5]]
        view.max_relevance = max(
            (_relevance(article, symbol, moment, self.lookback_minutes) for article in fresh),
            default=0.0,
        )
        view.news_score = min(1.0, 0.5 * min(view.news_count, 4) / 4 + 0.5 * view.max_relevance)

        # -- breadth: is the whole tape moving, or just this name? ---------- #
        if snapshot is not None:
            view.breadth_up_pct = getattr(snapshot, "breadth_ratio", None)
            view.breadth_score = (
                abs((view.breadth_up_pct or 0.5) - 0.5) * 2 if view.breadth_up_pct is not None else 0.0
            )
            # -- volume participation, rank-normalised within most-actives --- #
            volumes = {
                sym: float((entry.raw.get("most_actives") or {}).get("volume") or 0.0)
                for sym, entry in getattr(snapshot, "symbols", {}).items()
            }
            ranked = _rank_normalise({k: v for k, v in volumes.items() if v > 0})
            view.volume_score = ranked.get(symbol.upper(), 0.0)

        # -- scheduled macro print ------------------------------------------ #
        due = self.calendar.within(moment)
        if due:
            view.scheduled_event = due[0].name

        view.score = round(
            self.weights["news"] * view.news_score
            + self.weights["breadth"] * view.breadth_score
            + self.weights["volume"] * view.volume_score,
            4,
        )
        return view

    # ------------------------------------------------------------------ #
    def gate(
        self,
        view: CatalystView,
        bullish: bool,
        min_headlines: int = 1,
        relevance_floor: float = 0.5,
        breadth_min: float = 0.60,
        required: bool = True,
    ) -> GateResult:
        """The deterministic path, and deliberately the DEFAULT path.

        An LLM call inside an intraday loop is a latency risk on a signal that
        decays in minutes. The model enriches this read; it does not gate it.
        """
        metrics = {
            "score": view.score,
            "news_count": float(view.news_count),
            "max_relevance": view.max_relevance,
            "breadth_up_pct": view.breadth_up_pct if view.breadth_up_pct is not None else -1.0,
        }
        if not required:
            return GateResult.ok("catalyst", **metrics)

        catalyst_present = (
            view.news_count >= min_headlines and view.max_relevance >= relevance_floor
        ) or view.scheduled_event is not None

        if not catalyst_present:
            return GateResult.veto(
                "catalyst",
                f"no catalyst behind the move: {view.news_count} headline(s) in the "
                f"window, best relevance {view.max_relevance:.2f}, no scheduled print. "
                "A VWAP cross with no mechanism is drift, and drift reverts",
                **metrics,
            )

        if not view.breadth_agrees(bullish, breadth_min):
            aligned = (
                view.breadth_up_pct if bullish
                else (1.0 - view.breadth_up_pct if view.breadth_up_pct is not None else None)
            )
            return GateResult.veto(
                "catalyst",
                "breadth does not confirm: only "
                + (f"{aligned:.0%}" if aligned is not None else "an unknown share")
                + f" of the movers list is moving with the signal, floor is {breadth_min:.0%}. "
                "An index rising on mixed breadth is one mega-cap dragging the tape",
                **metrics,
            )
        return GateResult.ok("catalyst", **metrics)


# --------------------------------------------------------------------------- #
def _recent_news(
    articles: list[dict[str, Any]], moment: dt.datetime, lookback_minutes: int
) -> list[dict[str, Any]]:
    out = []
    for article in articles:
        stamp = _parse_when(article.get("created_at") or article.get("updated_at"))
        if stamp is None:
            continue
        age = (moment - stamp).total_seconds() / 60.0
        if 0 <= age <= lookback_minutes:
            out.append(article)
    return out


def _relevance(
    article: dict[str, Any], symbol: str, moment: dt.datetime, lookback_minutes: int
) -> float:
    """Recency-decayed relevance: does it name the symbol, and how fresh is it?"""
    symbols = [str(s).upper() for s in (article.get("symbols") or [])]
    named = 1.0 if symbol.upper() in symbols else 0.4
    focus = 1.0 if len(symbols) <= 3 else 0.6
    stamp = _parse_when(article.get("created_at") or article.get("updated_at"))
    if stamp is None:
        return named * focus * 0.5
    age = max(0.0, (moment - stamp).total_seconds() / 60.0)
    decay = max(0.0, 1.0 - age / max(1.0, lookback_minutes))
    return round(named * focus * (0.4 + 0.6 * decay), 4)
