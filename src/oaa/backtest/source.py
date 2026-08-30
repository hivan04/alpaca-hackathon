"""Turning historical bars into the MarketContexts a strategy reads.

The one rule this file exists to enforce is **no lookahead**. A context stamped
10:00 on 4 June may contain:

  * every daily bar up to and including 3 June  (complete sessions)
  * 4 June's OPEN, as the spot                  (known at 09:30)
  * intraday bars for 4 June up to 10:00        (if the intraday feed is on)
  * headlines published before 10:00 on 4 June  (real Alpaca timestamps)

and nothing else. In particular it must NOT contain 4 June's close, which is
the single easiest way to write a backtest that prints a beautiful equity curve
and means nothing. Indicators are therefore computed on complete prior sessions
only, and the spot the strategy prices against is the open.

Everything derived - realised vol, ADX, trend strength, the modelled IV and its
rank - is computed once as a series over the full history and then read at
index i, which makes the no-lookahead property structural rather than a thing
you have to remember in five different places.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from oaa.backtest.chain import ChainModel
from oaa.backtest.engine import ContextSource
from oaa.backtest.ivmodel import IVModel
from oaa.backtest.realchain import RealChainBuilder
from oaa.core.logging import get_logger
from oaa.core.types import MarketContext
from oaa.data.indicators import adx, trend_strength, volume_ratio
from oaa.data.term_structure import term_structure

log = get_logger("backtest.source")

_ET = "America/New_York"

# A deliberately small, auditable lexicon. This is NOT sentiment analysis in
# the modelling sense - it is a keyword count, it is stated as one everywhere it
# is displayed, and nothing gates on it. It exists so the per-trade
# justification can say what the tape was saying about the name that morning.
_POSITIVE = {
    "beats", "beat", "surges", "surge", "jumps", "rally", "rallies", "upgrade",
    "upgraded", "raises", "raised", "record", "strong", "outperform", "wins",
    "approval", "approved", "gains", "soars", "tops", "boosts", "buyback",
}
_NEGATIVE = {
    "misses", "miss", "plunges", "plunge", "falls", "slumps", "downgrade",
    "downgraded", "cuts", "cut", "warns", "warning", "probe", "lawsuit",
    "recall", "halts", "weak", "underperform", "loss", "losses", "sinks", "slashes",
}


def headline_sentiment(articles: list[dict[str, Any]]) -> float:
    """Net keyword polarity in [-1, 1]. A word count, not a model."""
    if not articles:
        return 0.0
    score = 0
    for article in articles:
        words = str(article.get("headline", "")).lower().replace(",", " ").split()
        score += sum(1 for w in words if w in _POSITIVE)
        score -= sum(1 for w in words if w in _NEGATIVE)
    return round(max(-1.0, min(1.0, score / max(1.0, len(articles) * 2.0))), 3)


def _et_utc(day: dt.date, hhmm: str) -> dt.datetime:
    """'10:00' Eastern on this date, as UTC."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    try:
        from zoneinfo import ZoneInfo

        local = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=ZoneInfo(_ET))
        return local.astimezone(dt.timezone.utc)
    except Exception:  # noqa: BLE001
        return dt.datetime.combine(day, dt.time(hour + 4, minute), tzinfo=dt.timezone.utc)


def _parse_ts(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- #
def _latest_session(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the most recent day's bars.

    Contexts carry several sessions so the volume baseline and `min_bars` work,
    but anything reading "the session open" has to slice or it reads a week-old
    price as this morning's.
    """
    if not bars:
        return []
    last = _parse_ts(bars[-1]["timestamp"]).date()
    return [b for b in bars if _parse_ts(b["timestamp"]).date() == last]


@dataclass
class ReplayAttention:
    """A breadth snapshot rebuilt from the replayed universe's own bars.

    `CatalystEngine.gate` refuses a candidate whose breadth does not confirm,
    and `breadth_agrees` returns False when breadth is unknown - so without one
    of these the catalyst gate vetoes every intraday candidate and the book
    never trades in replay.

    Live, breadth comes from Alpaca's movers list: hundreds of names. Here it is
    the share of THIS universe trading above its own session open, which for six
    symbols is a much coarser measure and one that correlates with the signal it
    is meant to confirm. It is a genuine reading of the tape available at that
    moment - no lookahead - but it is weaker evidence than the live path, and
    the intraday book's backtested trade count should be read with that in mind.
    """

    asof: dt.datetime
    symbols: dict[str, Any] = field(default_factory=dict)
    breadth: dict[str, int] = field(default_factory=dict)

    @property
    def breadth_ratio(self) -> float | None:
        up, down = self.breadth.get("gainers", 0), self.breadth.get("losers", 0)
        total = up + down
        return round(up / total, 4) if total else None

    def top(self, n: int = 20) -> list[Any]:
        return sorted(self.symbols.values(), key=lambda s: -getattr(s, "score", 0.0))[:n]

    def news_driven(self, min_velocity: float = 2.0) -> list[Any]:
        return []


@dataclass
class _Attention:
    """One symbol inside the replay breadth snapshot."""

    symbol: str
    score: float = 0.0
    news_velocity: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SymbolHistory:
    """Everything precomputed for one symbol, indexed by daily bar."""

    symbol: str
    bars: list[dict[str, Any]]
    iv: list[float | None] = field(default_factory=list)
    rv: list[float | None] = field(default_factory=list)
    iv_rank: list[float | None] = field(default_factory=list)
    adx: list[float | None] = field(default_factory=list)
    trend: list[float | None] = field(default_factory=list)
    vol_ratio: list[float | None] = field(default_factory=list)
    intraday: dict[dt.date, list[dict[str, Any]]] = field(default_factory=dict)

    def date_at(self, index: int) -> dt.date:
        return _parse_ts(self.bars[index]["timestamp"]).date()


# --------------------------------------------------------------------------- #
class HistoricalContextSource(ContextSource):
    """Chronological MarketContexts built from real bars and a modelled chain."""

    def __init__(
        self,
        bars_by_symbol: dict[str, list[dict[str, Any]]],
        *,
        start: dt.date,
        end: dt.date,
        chain_model: ChainModel | None = None,
        iv_model: IVModel | None = None,
        news: list[dict[str, Any]] | None = None,
        news_lookback_hours: float = 18.0,
        intraday_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
        session_times_et: tuple[str, ...] = ("10:00",),
        mark_interval_minutes: int = 1,
        market_symbol: str = "SPY",
        earnings_dates: dict[str, dt.date] | None = None,
        min_history: int = 40,
        intraday_history_sessions: int = 5,
        real_chain: RealChainBuilder | None = None,
        min_iv_observations: int = 20,
        options_dte: tuple[int, int] = (3, 45),
        term_anchors: tuple[int, int, int] = (1, 30, 7),
        term_max_abs_slope_pct: float = 1.0,
    ) -> None:
        self.start = start
        self.end = end
        self.chain_model = chain_model or ChainModel()
        self.iv_model = iv_model or IVModel()
        self.session_times = session_times_et
        #: Cadence, in minutes, at which OPEN positions are re-marked between
        #: scans. Entries stay on `session_times`; see `marks_between`.
        #: 0 disables the fine loop and restores scan-grid-only management.
        self.mark_interval_minutes = int(mark_interval_minutes)
        self.news_lookback = dt.timedelta(hours=news_lookback_hours)
        self.earnings_dates = {k.upper(): v for k, v in (earnings_dates or {}).items()}
        self.min_history = min_history
        #: Prior sessions of intraday bars carried in each context. Mirrors
        #: the live provider's `data.intraday_lookback_days` so the two
        #: paths hand a strategy the same shape of window.
        self.intraday_history_sessions = intraday_history_sessions
        self.market_symbol = market_symbol.upper()
        #: When present, strikes, expiries, marks and implied vol come from real
        #: Alpaca history rather than the model. See backtest/realchain.py.
        self.real_chain = real_chain
        self.min_iv_observations = min_iv_observations
        self.options_dte = options_dte
        #: (front_dte, back_dte, min_separation_days) for the ATM IV term
        #: structure. Handed down from `data.term_*` so replay and the live
        #: providers anchor the slope at the same points on the ladder - the
        #: failure in `claude/iv-rank-divergence.md` was two paths computing
        #: different numbers under one name.
        self.term_anchors = term_anchors
        self.term_max_abs_slope_pct = term_max_abs_slope_pct
        self.iv_provenance: dict[str, str] = {}
        #: sessions where the real chain had nothing to offer. If this equals
        #: the session count the run measured NOTHING, which is a failure to
        #: report loudly rather than a quiet zero-trade result.
        self.empty_chain_sessions = 0
        self.chain_requests = 0
        #: rebuilt each session; the engine hands it to the strategies
        self.attention: ReplayAttention | None = None

        market_bars = bars_by_symbol.get(self.market_symbol)
        factor = self.iv_model.market_factor(market_bars) if market_bars else []

        self.histories: dict[str, SymbolHistory] = {}
        for symbol, bars in bars_by_symbol.items():
            ordered = sorted(bars, key=lambda b: _parse_ts(b["timestamp"]))
            series = self.iv_model.build(
                ordered, factor if symbol.upper() == self.market_symbol or factor else None
            )
            iv_values, rank_values, provenance = self._implied_vol_series(
                symbol.upper(), ordered, series
            )
            self.iv_provenance[symbol.upper()] = provenance
            history = SymbolHistory(
                symbol=symbol.upper(),
                bars=ordered,
                iv=iv_values,
                rv=series["rv"],
                iv_rank=rank_values,
                adx=_rolling(ordered, adx, 40),
                trend=_rolling(ordered, trend_strength, 40),
                vol_ratio=_rolling(ordered, volume_ratio, 30),
            )
            for row in (intraday_by_symbol or {}).get(symbol, []):
                stamp = _parse_ts(row["timestamp"])
                history.intraday.setdefault(stamp.date(), []).append(row)
            self.histories[symbol.upper()] = history

        # News, bucketed by symbol and sorted, so each session is a slice.
        self.news_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for article in news or []:
            stamp = _parse_ts(article.get("created_at"))
            article["_ts"] = stamp
            for sym in article.get("symbols") or []:
                self.news_by_symbol.setdefault(str(sym).upper(), []).append(article)
        for items in self.news_by_symbol.values():
            items.sort(key=lambda a: a["_ts"])

    # ------------------------------------------------------------------ #
    def _implied_vol_series(
        self,
        symbol: str,
        bars: list[dict[str, Any]],
        modelled: dict[str, list[float | None]],
    ) -> tuple[list[float | None], list[float | None], str]:
        """Real recovered IV where the option tape allows it, modelled where not.

        IV rank is the input the premium gate actually trades on, so where it
        comes from is the single most consequential provenance question in the
        harness. With real option bars the series is the name's ACTUAL implied
        vol history and the rank is a real percentile. Without them it is the
        model in `ivmodel.py`, which is an assumption.

        A session with no usable print carries the last known value forward
        rather than inventing one - implied vol does not reset because nothing
        traded - and a symbol that never accumulates `min_iv_observations`
        falls back to the modelled series wholesale rather than ranking against
        a handful of points.
        """
        if self.real_chain is None:
            return modelled["iv"], modelled["iv_rank"], "modelled"

        recovered: list[float | None] = []
        carried: float | None = None
        observations = 0
        for bar in bars:
            day = _parse_ts(bar["timestamp"]).date()
            value = self.real_chain.atm_iv(symbol, float(bar["close"]), day)
            if value is not None:
                observations += 1
                carried = value
            recovered.append(carried)

        if observations < self.min_iv_observations:
            log.warning(
                "%s: only %d sessions with a usable option print - falling back "
                "to the modelled IV series", symbol, observations,
            )
            return modelled["iv"], modelled["iv_rank"], f"modelled ({observations} prints)"

        ranks = _rank_series(recovered, self.iv_model.rank_lookback, self.min_iv_observations)
        return recovered, ranks, f"recovered from real option prints ({observations} sessions)"

    # ------------------------------------------------------------------ #
    def sessions(self) -> list[dt.date]:
        """Trading days in the window, taken from the market symbol's bars."""
        reference = self.histories.get(self.market_symbol) or next(
            iter(self.histories.values()), None
        )
        if reference is None:
            return []
        return [
            d for d in (_parse_ts(b["timestamp"]).date() for b in reference.bars)
            if self.start <= d <= self.end
        ]

    def __iter__(self) -> Iterator[tuple[dt.datetime, dict[str, MarketContext]]]:
        index_by_symbol = {
            symbol: {history.date_at(i): i for i in range(len(history.bars))}
            for symbol, history in self.histories.items()
        }
        for day in self.sessions():
            for hhmm in self.session_times:
                moment = _et_utc(day, hhmm)
                contexts: dict[str, MarketContext] = {}
                for symbol, history in self.histories.items():
                    index = index_by_symbol[symbol].get(day)
                    if index is None or index < self.min_history:
                        continue
                    context = self._context(history, index, moment)
                    if context is not None:
                        contexts[symbol] = context
                if contexts:
                    self.attention = self._attention(contexts, moment)
                    yield moment, contexts

    # ------------------------------------------------------------------ #
    def marks_between(
        self,
        start: dt.datetime,
        end: dt.datetime,
        contexts: dict[str, MarketContext],
        symbols: list[str],
    ) -> Iterator[tuple[dt.datetime, dict[str, MarketContext]]]:
        """Contexts at the mark cadence, strictly between two scan moments.

        Built by EXTENDING the last scan's context rather than rebuilding it:
        the intraday bars published since `start` are appended, the spot is
        taken from the newest of them, and `asof` moves. Everything else - the
        chain, the news, the term structure, the daily indicators - is carried
        across unchanged.

        That division is the whole reason this is cheap. The carried fields are
        what an ENTRY reads, and this loop opens nothing; the replaced fields
        are what a MARK reads (`_leg_marks` prices off `spot` and
        `implied_vol`) and what an EXIT reads (`exit_on_vwap_recross` walks
        `intraday_bars`). Rebuilding the chain sixty times an hour would cost
        the run the same as sixty extra scans and change no mark by a cent.

        No-lookahead still holds structurally: a bar is appended only once its
        timestamp is at or before the moment being priced, which is the same
        rule `_context` applies.

        Bounded to one session on purpose. `end` is simply the next scan the
        source produced, and over a night or a weekend that is the next
        morning - marking a resident condor once a minute until then would be
        both meaningless and slow.
        """
        step = self.mark_interval_minutes
        if step <= 0 or not symbols or end <= start:
            return
        day = start.astimezone(dt.timezone.utc).date()
        if end.astimezone(dt.timezone.utc).date() != day:
            return

        # Today's bars after the last scan, per symbol, sliced once.
        pending: dict[str, list[dict[str, Any]]] = {}
        for symbol in symbols:
            history = self.histories.get(symbol)
            if history is None or contexts.get(symbol) is None:
                continue
            rows = [
                row for row in history.intraday.get(day, [])
                if start < _parse_ts(row["timestamp"]) <= end
            ]
            if rows:
                pending[symbol] = rows
        if not pending:
            return

        moment = start + dt.timedelta(minutes=step)
        while moment < end:
            refined: dict[str, MarketContext] = {}
            for symbol, rows in pending.items():
                base = contexts[symbol]
                fresh = [r for r in rows if _parse_ts(r["timestamp"]) <= moment]
                if not fresh:
                    continue
                spot = float(fresh[-1]["close"])
                refined[symbol] = base.model_copy(update={
                    "asof": moment,
                    "spot": round(spot, 4) if spot > 0 else base.spot,
                    "intraday_bars": base.intraday_bars + fresh,
                })
            if refined:
                yield moment, refined
            moment += dt.timedelta(minutes=step)

    # ------------------------------------------------------------------ #
    def _attention(
        self, contexts: dict[str, MarketContext], moment: dt.datetime
    ) -> ReplayAttention:
        gainers = losers = 0
        symbols: dict[str, Any] = {}
        for symbol, market in contexts.items():
            session = _latest_session(market.intraday_bars)
            if session:
                first = float(session[0]["open"])
                last = float(session[-1]["close"])
                volume = sum(float(b.get("volume", 0)) for b in session)
            else:
                first, last = market.spot, market.spot
                volume = 0.0
            if last > first:
                gainers += 1
            elif last < first:
                losers += 1
            move = (last / first - 1.0) if first else 0.0
            symbols[symbol] = _Attention(
                symbol=symbol,
                score=abs(move),
                raw={"most_actives": {"volume": volume}},
            )
        return ReplayAttention(
            asof=moment, symbols=symbols,
            breadth={"gainers": gainers, "losers": losers},
        )

    # ------------------------------------------------------------------ #
    def _context(
        self, history: SymbolHistory, index: int, moment: dt.datetime
    ) -> MarketContext | None:
        bar = history.bars[index]
        spot = float(bar["open"])           # known at 09:30; the close is not
        if spot <= 0:
            return None

        prior = index - 1                   # last COMPLETE session
        atm_iv = history.iv[prior]
        if atm_iv is None:
            return None

        day = history.date_at(index)
        intraday = [
            row for row in history.intraday.get(day, [])
            if _parse_ts(row["timestamp"]) <= moment
        ]
        # The session OPEN is the right no-lookahead spot only at 09:30. Held
        # for the whole day it makes the underlying immobile: every context
        # from 10:00 to 15:10 saw the same price, so an option could only ever
        # decay and an intraday momentum book could not win a single trade by
        # construction. Measured 17-21 Aug: 38 trades, 38 losers, every one of
        # them between -0.3% and -1.5% - the spread plus a little theta, which
        # is exactly what "the underlying never moved" costs.
        #
        # The intraday bars are already filtered to `<= moment`, so their last
        # close is the price at this moment with no lookahead at all. It also
        # fixes strike selection, which was picking the strike that was ATM at
        # the OPEN rather than the one that is ATM now.
        if intraday:
            last_close = float(intraday[-1]["close"])
            if last_close > 0:
                spot = last_close
        # Prior COMPLETE sessions, ahead of today's bars. This is the window
        # the LIVE provider fetches (`data.intraday_lookback_days`), and the
        # backtest carrying only the current day made replay a strictly more
        # restrictive strategy than live: `min_bars: 30` on 5-minute bars is
        # satisfied instantly live but not until 12:00 ET in replay, and
        # `volume_zscore_by_bucket` never reached its three-sample floor at all,
        # so `require_volume` vetoed every single candidate.
        prior_days = [d for d in sorted(history.intraday) if d < day]
        for earlier in prior_days[-self.intraday_history_sessions:]:
            intraday = history.intraday[earlier] + intraday
        articles = self._news_for(history.symbol, moment)
        # Built once and read twice: the term structure must be measured on the
        # chain the strategy is handed, not on a second one built beside it.
        chain = self._chain(history.symbol, spot, moment, atm_iv)

        return MarketContext(
            symbol=history.symbol,
            asof=moment,
            spot=round(spot, 4),
            prev_close=float(history.bars[prior]["close"]),
            bars=history.bars[:index],      # complete sessions only
            intraday_bars=intraday,
            chain=chain,
            realised_vol=history.rv[prior],
            implied_vol=atm_iv,
            iv_rank=history.iv_rank[prior],
            term_structure=term_structure(
                chain, spot, moment.date(),
                front_dte=self.term_anchors[0],
                back_dte=self.term_anchors[1],
                min_separation_days=self.term_anchors[2],
                max_abs_slope_pct=self.term_max_abs_slope_pct,
            ),
            trend_strength=history.trend[prior],
            adx=history.adx[prior],
            volume_ratio=history.vol_ratio[prior],
            news=articles,
            earnings_date=self.earnings_dates.get(history.symbol),
            enrichment={
                "news_count": len(articles),
                "news_sentiment": headline_sentiment(articles),
                "headlines": [str(a.get("headline", ""))[:160] for a in articles[:4]],
                "iv_source": self.iv_provenance.get(history.symbol, "modelled"),
                "chain_source": (
                    "real Alpaca option bars, spread modelled - see backtest/realchain.py"
                    if self.real_chain is not None
                    else "modelled - see backtest/chain.py"
                ),
                "spot_source": (
                    "real Alpaca intraday bar (last close at or before this "
                    "moment)" if intraday else "real Alpaca bar (session open)"
                ),
            },
        )

    def _chain(
        self, symbol: str, spot: float, moment: dt.datetime, atm_iv: float
    ) -> list[Any]:
        self.chain_requests += 1
        if self.real_chain is not None:
            quotes = self.real_chain.build(
                symbol, spot, moment, atm_iv,
                min_dte=self.options_dte[0], max_dte=self.options_dte[1],
            )
            if quotes:
                return quotes
            # No listed contract survived for this session. Falling through to
            # the model would quietly manufacture a chain that did not exist,
            # so return nothing and let the strategy find no structure.
            self.empty_chain_sessions += 1
            return []
        return self.chain_model.build(symbol, spot, moment, atm_iv)

    def _news_for(self, symbol: str, moment: dt.datetime) -> list[dict[str, Any]]:
        floor = moment - self.news_lookback
        return [
            {k: v for k, v in article.items() if k != "_ts"}
            for article in self.news_by_symbol.get(symbol, [])
            if floor <= article["_ts"] <= moment
        ]

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "symbols": sorted(self.histories),
            "sessions": len(self.sessions()),
            "session_times_et": list(self.session_times),
            "mark_interval_minutes": self.mark_interval_minutes,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "news_articles": sum(len(v) for v in self.news_by_symbol.values()),
            "news_lookback_hours": self.news_lookback.total_seconds() / 3600,
            "iv_model": self.iv_model.describe(),
            "chain_model": self.chain_model.describe(),
            "chain_source": "real" if self.real_chain is not None else "modelled",
            "iv_provenance": dict(self.iv_provenance),
            "coverage": (
                self.real_chain.coverage.as_dict() if self.real_chain is not None else None
            ),
            "chain_requests": self.chain_requests,
            "empty_chain_sessions": self.empty_chain_sessions,
        }


# --------------------------------------------------------------------------- #
def _rank_series(
    values: list[float | None], lookback: int, minimum: int
) -> list[float | None]:
    """Percentile of each value within its own trailing window.

    The same definition a live feed uses for IV rank. Fewer than `minimum`
    observations returns None, which the premium gate treats as a veto - not as
    0.5, which would let a name with no history trade on a coin flip.
    """
    out: list[float | None] = []
    for i, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        history = [v for v in values[max(0, i - lookback) : i + 1] if v is not None]
        if len(history) < minimum:
            out.append(None)
            continue
        below = sum(1 for v in history if v <= value)
        out.append(round(below / len(history), 4))
    return out


# --------------------------------------------------------------------------- #
def _rolling(
    bars: list[dict[str, Any]], fn: Any, window: int
) -> list[float | None]:
    """Apply an indicator at every bar using only prior data."""
    out: list[float | None] = []
    for i in range(len(bars)):
        chunk = bars[max(0, i - window * 2) : i + 1]
        try:
            out.append(fn(chunk) if len(chunk) > 3 else None)
        except Exception:  # noqa: BLE001
            out.append(None)
    return out


# --------------------------------------------------------------------------- #
def synthetic_bars(
    symbol: str,
    start: dt.date,
    end: dt.date,
    seed: int = 7,
    spot: float = 500.0,
    drift: float = 0.05,
    base_vol: float = 0.16,
) -> list[dict[str, Any]]:
    """A price path for demo and test runs ONLY.

    This is not data. Anything produced from it is a smoke test of the wiring,
    and the dashboard labels a run built on it as SYNTHETIC in red. It exists
    so the dashboard can be demonstrated with no keys and no network.
    """
    import random

    rng = random.Random(f"{symbol}-{seed}")
    price = spot
    daily = base_vol / math.sqrt(252)
    vol = daily
    out: list[dict[str, Any]] = []
    day, session = start, 0
    while day <= end:
        if day.weekday() < 5:
            session += 1
            # Volatility CYCLES rather than drifting. Without a cycle the
            # modelled IV series trends in one direction, IV rank pins to 0 or
            # 1 for the whole path, and the premium gate either never fires or
            # always does - so the fixture stops exercising the thing it exists
            # to exercise. Roughly a two-month vol cycle, which is the horizon
            # a 7-14 DTE short-premium book actually lives on.
            cycle = 1.0 + 0.45 * math.sin(2 * math.pi * session / 42.0)
            vol = max(0.004, min(0.06, 0.90 * vol + 0.10 * daily * cycle
                                 + 0.04 * abs(rng.gauss(0, 0.010))))
            open_price = price
            ret = rng.gauss(drift / 252, vol)
            price = max(1.0, price * math.exp(ret))
            # The intraday RANGE has to be realistic, not decorative. A real
            # session's high-low spans roughly two to three times its
            # open-to-close move, and range-based estimators (Garman-Klass,
            # Parkinson) read exactly that. Generating a token range made
            # synthetic bars look far calmer to those estimators than to a
            # close-to-close one, which is a difference no real tape has.
            # Calibrated so a range estimator and a close-to-close estimator
            # read the same volatility off these bars (ratio ~1.0), which is
            # what a clean tape looks like. The IEX gap the live feed shows is
            # a data-quality artefact and does not belong in a fixture.
            excursion = abs(rng.gauss(0, vol * 0.20)) + abs(ret) * 0.25
            out.append({
                "timestamp": dt.datetime.combine(day, dt.time(13, 30), tzinfo=dt.timezone.utc),
                "open": round(open_price, 2),
                "high": round(max(open_price, price) * (1 + excursion), 2),
                "low": round(min(open_price, price) * (1 - excursion), 2),
                "close": round(price, 2),
                "volume": round(rng.uniform(2e6, 9e6)),
            })
        day += dt.timedelta(days=1)
    return out


def synthetic_intraday_bars(
    symbol: str,
    daily: list[dict[str, Any]],
    seed: int = 7,
    interval_minutes: int = 5,
) -> list[dict[str, Any]]:
    """Expand each synthetic daily bar into a plausible 5-minute session.

    Without this the synthetic source produces no intraday bars at all, so the
    intraday book dies at its `data` gate on every single candidate and a
    wiring test reports "the intraday book never trades" when what it has
    actually measured is "the fixture has no intraday data". That is a
    misleading result, not a finding.

    The path is a Brownian bridge from the day's open to its close, rescaled so
    the session's high and low match the daily bar's. Volume follows the usual
    U-shape, which matters because the momentum gate's volume z-score is
    bucketed by time of day - a flat volume profile makes that gate untestable.

    Still a fixture. It is not data and nothing measured on it is a result.
    """
    import random

    rng = random.Random(f"{symbol}-intraday-{seed}")
    per_session = int(390 / interval_minutes)
    out: list[dict[str, Any]] = []

    for bar in daily:
        stamp = bar["timestamp"]
        if isinstance(stamp, str):
            stamp = dt.datetime.fromisoformat(stamp)
        day = stamp.date()
        session_open = dt.datetime.combine(
            day, dt.time(13, 30), tzinfo=dt.timezone.utc
        )
        o, hi_px, lo_px, c = (float(bar["open"]), float(bar["high"]),
                              float(bar["low"]), float(bar["close"]))
        day_volume = float(bar.get("volume", 5e6))

        # Brownian bridge open -> close, then affine-rescaled onto [low, high].
        steps = [rng.gauss(0.0, 1.0) for _ in range(per_session)]
        cumulative, total = [], 0.0
        for i, step in enumerate(steps, start=1):
            total += step
            cumulative.append(total - (i / per_session) * total if i == per_session else total)
        drift_free = [
            value - (idx + 1) / per_session * cumulative[-1]
            for idx, value in enumerate(cumulative)
        ]
        path = [o + (c - o) * (idx + 1) / per_session + d * (hi_px - lo_px) * 0.18
                for idx, d in enumerate(drift_free)]
        path[-1] = c
        lo, hi = min(min(path), o), max(max(path), o)
        if hi > lo:
            path = [lo_px + (p - lo) * (hi_px - lo_px) / (hi - lo) for p in path]
            path[-1] = c

        previous = o
        for idx, close_px in enumerate(path):
            # U-shaped volume: heavy open and close, light midday.
            phase = idx / max(1, per_session - 1)
            shape = 0.55 + 1.35 * ((phase - 0.5) ** 2) * 4
            volume = day_volume / per_session * shape * rng.uniform(0.75, 1.25)
            wick = abs(rng.gauss(0, (hi_px - lo_px) * 0.05))
            out.append({
                "timestamp": session_open + dt.timedelta(minutes=interval_minutes * idx),
                "open": round(previous, 2),
                "high": round(max(previous, close_px) + wick, 2),
                "low": round(min(previous, close_px) - wick, 2),
                "close": round(close_px, 2),
                "volume": round(volume),
            })
            previous = close_px
    return out
