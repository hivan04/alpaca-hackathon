"""The evidence pack: what the model is allowed to read before it calls a print.

Two feeds, deliberately different in kind:

  * **Alpaca news** - press releases, wire copy and analyst-action headlines.
    This is the sell-side half of the picture and it is the same endpoint the
    discovery engine already uses.
  * **StockTwits** - the public symbol stream. Retail positioning, in retail's
    own words. Noisy by construction; it is here because a crowded one-sided
    retail book is itself information about the reaction to a print, not
    because the crowd is right.

Both are third-party text going into a language model, which makes this the
system's largest prompt-injection surface: anyone can post "ignore previous
instructions" to a StockTwits symbol stream. Three things contain it - the text
is truncated, it is stripped of control characters, and it is delivered inside
a fenced block that the system prompt names as untrusted data. The model's
answer is then parsed as JSON with a fixed schema and clamped, so the worst a
poisoned message can do is move a confidence score that is already bounded and
that the risk engine still has to approve.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from oaa.core.logging import get_logger
from oaa.strategies.events.params import SentimentParams

log = get_logger("strategies.events.sentiment")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCE = re.compile(r"[`  ]")

NewsFetcher = Callable[[str, int, int], list[dict[str, Any]]]


@dataclass
class EvidencePack:
    """Everything the direction model sees for one name."""

    symbol: str
    headlines: list[dict[str, str]] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Dated notes accumulated by the watch in the days before this print, set
    #: by `EventWatcher.attach`. Empty on a name the watch never saw - a print
    #: added to the calendar the same afternoon, or the watch switched off.
    notes: list[dict[str, Any]] = field(default_factory=list)
    #: The dossier's own salience-weighted lean, carried for the journal so a
    #: week of bearish notes ending in a bullish call is visible rather than
    #: buried. Never enforced: the direction call reads the evidence itself.
    watch_lean: str = "unknown"
    watch_score: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.headlines and not self.messages and not self.notes

    def counts(self) -> dict[str, int]:
        return {
            "headlines": len(self.headlines),
            "messages": len(self.messages),
            "watch_notes": len(self.notes),
        }

    def as_prompt_block(self, max_chars: int) -> str:
        """The untrusted-data block. Fenced, labelled, and budget-capped."""
        lines: list[str] = []
        if self.notes:
            lines.append(
                "WHAT THE DESK LOGGED IN THE DAYS BEFORE THIS PRINT "
                "(oldest first; salience is how material that day's batch was "
                "judged to be, 0-1)"
            )
            for note in self.notes:
                lines.append(
                    f"- [{note.get('asof', '?')}, salience "
                    f"{float(note.get('salience') or 0):.2f}, lean "
                    f"{note.get('lean', 'neutral')}] {note.get('summary', '')}"
                )
            lines.append("")
        if self.headlines:
            lines.append("NEWS AND ANALYST HEADLINES (most recent first)")
            for item in self.headlines:
                lines.append(f"- [{item.get('ts', '?')}] {item.get('headline', '')}")
        if self.messages:
            lines.append("")
            lines.append("RETAIL POSTS (StockTwits public stream, most recent first)")
            for item in self.messages:
                tag = item.get("sentiment") or "none"
                lines.append(f"- [{item.get('ts', '?')}, poster tag: {tag}] {item.get('body', '')}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [truncated to the context budget]"
        return text


def _clean(text: Any, limit: int = 400) -> str:
    """Strip control characters and backticks, collapse whitespace, truncate.

    Backticks go because the pack is delivered inside a fenced block and a
    stray fence would let a post escape it.
    """
    value = _FENCE.sub(" ", _CONTROL.sub(" ", str(text or "")))
    return " ".join(value.split())[:limit]


def _ts(value: Any) -> str:
    raw = str(value or "")[:19]
    return raw or "?"


def fetch_news(
    symbol: str, params: SentimentParams, news_fn: NewsFetcher | None
) -> list[dict[str, str]]:
    """Headlines via the injected fetcher. No fetcher -> no news, not an error.

    Injection rather than a hard import keeps this module testable offline and
    lets the caller decide whether news arrives over the Alpaca REST client,
    the MCP bridge, or a replay fixture.
    """
    if news_fn is None:
        return []
    try:
        raw = news_fn(symbol, params.news_lookback_days, params.max_headlines) or []
    except Exception as exc:  # noqa: BLE001 - a dead feed must not stop the book
        log.warning("%s: news fetch failed (%s) - continuing without it", symbol, exc)
        raise
    items: list[dict[str, str]] = []
    for article in raw[: params.max_headlines]:
        headline = _clean(article.get("headline") or article.get("title"))
        if not headline:
            continue
        items.append({
            "ts": _ts(article.get("created_at") or article.get("updated_at")),
            "headline": headline,
            "source": _clean(article.get("source"), 40),
        })
    return items


def fetch_stocktwits(symbol: str, params: SentimentParams) -> list[dict[str, str]]:
    """The public symbol stream. Best effort - it is a free endpoint."""
    if not params.stocktwits_enabled:
        return []
    try:
        import httpx

        url = params.stocktwits_url.format(symbol=symbol.upper())
        response = httpx.get(
            url,
            timeout=params.timeout_seconds,
            headers={"User-Agent": "oaa-events/1.0"},
        )
        if response.status_code != 200:
            raise RuntimeError(f"status {response.status_code}")
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log.info("%s: stocktwits unavailable (%s) - news only", symbol, exc)
        return []

    messages: list[dict[str, str]] = []
    for row in (payload.get("messages") or [])[: params.max_messages]:
        body = _clean(row.get("body"))
        if not body:
            continue
        basic = ((row.get("entities") or {}).get("sentiment") or {}) or {}
        messages.append({
            "ts": _ts(row.get("created_at")),
            "body": body,
            "sentiment": _clean(basic.get("basic"), 12).lower(),
        })
    return messages


def gather(
    symbol: str, params: SentimentParams, news_fn: NewsFetcher | None = None
) -> EvidencePack:
    """Both feeds, with each failure recorded rather than raised."""
    pack = EvidencePack(symbol=symbol)
    try:
        pack.headlines = fetch_news(symbol, params, news_fn)
    except Exception as exc:  # noqa: BLE001
        pack.errors.append(f"news: {exc}")
    pack.messages = fetch_stocktwits(symbol, params)
    if not pack.messages and params.stocktwits_enabled:
        pack.errors.append("stocktwits: no messages")
    log.info(
        "%s evidence: %d headline(s), %d retail post(s)%s",
        symbol, len(pack.headlines), len(pack.messages),
        f" [{'; '.join(pack.errors)}]" if pack.errors else "",
    )
    return pack


def alpaca_news_fetcher(data_provider: Any) -> NewsFetcher | None:
    """Adapt whatever news surface the data provider exposes to NewsFetcher."""
    getter = getattr(data_provider, "news", None)
    if getter is None:
        return None

    def fetch(symbol: str, days: int, limit: int) -> list[dict[str, Any]]:
        start = dt.date.today() - dt.timedelta(days=days)
        try:
            return getter(symbol, start=start, limit=limit) or []
        except TypeError:
            return getter(symbol) or []

    return fetch
