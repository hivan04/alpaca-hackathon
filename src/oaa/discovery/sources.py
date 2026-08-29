"""Where "hottest" comes from.

Alpaca already answers this question — `screener most-actives`, `screener
movers` and `news` are in both the CLI and the MCP server. Using them beats
scraping on every axis that matters here: no ToS exposure, no parser that
breaks when a page changes, no rate-limit games, and it scores better on
Technology Implementation than a BeautifulSoup script would. A scraper that
dies on day 3 of a 7-day window costs the P&L score.

`HttpJsonSource` is the escape hatch: point it at any JSON endpoint you have
rights to use — a sentiment API, a sponsor feed, StockTwits' or Reddit's
official API — and it contributes into the same score. Nothing about the
pipeline assumes a particular provider.
"""

from __future__ import annotations

import abc
import datetime as dt
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from oaa.core.logging import get_logger

log = get_logger("discovery.sources")


@dataclass
class SourceResult:
    """One source's contribution, keyed by symbol."""

    name: str
    #: symbol -> raw metric (volume, |%move|, article count, external score)
    values: dict[str, float] = field(default_factory=dict)
    #: symbol -> free-form detail the macro lens may read (headlines etc.)
    detail: dict[str, Any] = field(default_factory=dict)
    #: True when this source can be reconstructed for a past date. Only
    #: replayable sources may ever feed a model feature.
    replayable: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class AttentionSource(abc.ABC):
    name: str = "base"
    replayable: bool = False

    def __init__(self, cfg: Any, runner: Any) -> None:
        self.cfg = cfg
        self.run = runner          # callable(args: list[str]) -> parsed JSON

    @abc.abstractmethod
    def fetch(self, asof: dt.date | None = None) -> SourceResult: ...

    def _empty(self, error: str | None = None) -> SourceResult:
        return SourceResult(name=self.name, replayable=self.replayable, error=error)


# --------------------------------------------------------------------------- #
# Alpaca sources
# --------------------------------------------------------------------------- #
class MostActivesSource(AttentionSource):
    """`alpaca data screener most-actives` — attention measured in volume."""

    name = "most_actives"
    replayable = False              # live snapshot, no history

    def fetch(self, asof: dt.date | None = None) -> SourceResult:
        limit = int(self.cfg.get("limit", 30))
        try:
            payload = self.run(["data", "screener", "most-actives", "--top", str(limit)])
        except Exception as exc:  # noqa: BLE001
            return self._empty(str(exc))

        rows = _rows(payload, ("most_actives", "actives", "data"))
        result = self._empty()
        for row in rows[:limit]:
            symbol = _sym(row)
            if not symbol:
                continue
            volume = _num(row.get("volume")) or _num(row.get("v")) or 0.0
            trades = _num(row.get("trade_count")) or 0.0
            result.values[symbol] = volume
            result.detail[symbol] = {"volume": volume, "trade_count": trades}
        log.debug("most-actives: %d symbols", len(result.values))
        return result


class MoversSource(AttentionSource):
    """`alpaca data screener movers` — attention measured in price dislocation.

    Also the cheapest read on market breadth available: the gainer/loser split
    is what the macro lens uses when no model is configured.
    """

    name = "movers"
    replayable = False

    def fetch(self, asof: dt.date | None = None) -> SourceResult:
        limit = int(self.cfg.get("limit", 20))
        try:
            payload = self.run(["data", "screener", "movers", "--top", str(limit)])
        except Exception as exc:  # noqa: BLE001
            return self._empty(str(exc))

        result = self._empty()
        gainers = _rows(payload, ("gainers",))
        losers = _rows(payload, ("losers",))
        if not gainers and not losers:
            gainers = _rows(payload, ("movers", "data"))

        for row, direction in [(r, "up") for r in gainers] + [(r, "down") for r in losers]:
            symbol = _sym(row)
            if not symbol:
                continue
            change = abs(_num(row.get("percent_change")) or _num(row.get("change_percent")) or 0.0)
            result.values[symbol] = change
            result.detail[symbol] = {
                "percent_change": change,
                "direction": direction,
                "price": _num(row.get("price")),
            }
        result.detail["__breadth__"] = {
            "gainers": len(gainers),
            "losers": len(losers),
        }
        log.debug("movers: %d up, %d down", len(gainers), len(losers))
        return result


#: Alpaca's news endpoint refuses anything above this and returns a 400, which
#: kills the whole source - and with it the only component of the attention
#: score that surfaces large-caps. Found live on 28 Aug: a configured limit of
#: 200 meant discovery ran on most-actives and movers alone all night.
NEWS_MAX_LIMIT = 50


class NewsSource(AttentionSource):
    """`alpaca data news` — attention measured in coverage.

    The only source here that is **replayable**: the news endpoint accepts a
    date range, so a past day's article counts can be reconstructed exactly.
    That is what makes it safe to backtest against, and why anything that needs
    to feed a model comes from here and nowhere else.
    """

    name = "news"
    replayable = True

    def fetch(self, asof: dt.date | None = None) -> SourceResult:
        lookback = int(self.cfg.get("lookback_days", 3))
        baseline = int(self.cfg.get("baseline_days", 20))
        # Clamped rather than validated: a config asking for more news than the
        # API allows should cost fewer articles, never the entire source.
        configured = int(self.cfg.get("limit", NEWS_MAX_LIMIT))
        limit = min(configured, NEWS_MAX_LIMIT)
        if configured > NEWS_MAX_LIMIT:
            log.warning(
                "news limit %d exceeds Alpaca's maximum of %d - clamped",
                configured, NEWS_MAX_LIMIT,
            )
        end = asof or dt.date.today()
        start = end - dt.timedelta(days=lookback)
        base_start = end - dt.timedelta(days=baseline)

        def pull(begin: dt.date, finish: dt.date) -> list[dict[str, Any]]:
            payload = self.run([
                "data", "news",
                "--start", begin.isoformat(),
                "--end", finish.isoformat(),
                "--limit", str(limit),
            ])
            return _rows(payload, ("news", "data"))

        try:
            recent = pull(start, end)
            history = pull(base_start, start) if baseline > lookback else []
        except Exception as exc:  # noqa: BLE001
            return self._empty(str(exc))

        counts: dict[str, int] = defaultdict(int)
        headlines: dict[str, list[str]] = defaultdict(list)
        for article in recent:
            headline = str(article.get("headline") or article.get("title") or "").strip()
            for symbol in _symbols_of(article):
                counts[symbol] += 1
                if headline and len(headlines[symbol]) < 4:
                    headlines[symbol].append(headline)

        base_counts: dict[str, int] = defaultdict(int)
        for article in history:
            for symbol in _symbols_of(article):
                base_counts[symbol] += 1

        baseline_days = max(1, baseline - lookback)
        result = self._empty()
        for symbol, count in counts.items():
            per_day = count / max(1, lookback)
            base_per_day = base_counts.get(symbol, 0) / baseline_days
            # Velocity, not level. A name that is always in the news is not
            # newsworthy today; a name that has gone from 0 to 5 articles is.
            velocity = per_day / base_per_day if base_per_day > 0.05 else per_day
            result.values[symbol] = float(velocity)
            result.detail[symbol] = {
                "articles": count,
                "per_day": round(per_day, 2),
                "baseline_per_day": round(base_per_day, 2),
                "velocity": round(velocity, 2),
                "headlines": headlines.get(symbol, []),
            }
        log.debug("news: %d symbols across %d articles", len(result.values), len(recent))
        return result


# --------------------------------------------------------------------------- #
# generic external source
# --------------------------------------------------------------------------- #
class HttpJsonSource(AttentionSource):
    """Any JSON endpoint you have the right to call.

    Deliberately generic rather than a scraper for a named site: no ToS
    assumptions baked in, no selector to break, and a sponsor sentiment API
    drops in with three config lines instead of a rewrite.

        symbol_path: "symbols[].ticker"    dotted path, [] walks a list
        score_path:  "symbols[].score"
    """

    name = "external"
    replayable = False

    def fetch(self, asof: dt.date | None = None) -> SourceResult:
        url = str(self.cfg.get("url") or "").strip()
        if not url:
            return self._empty("no url configured")

        headers: dict[str, str] = {"Accept": "application/json"}
        key_env = self.cfg.get("api_key_env")
        if key_env:
            key = os.getenv(str(key_env))
            if not key:
                return self._empty(f"{key_env} is not set")
            header_name = str(self.cfg.get("api_key_header", "Authorization"))
            template = str(self.cfg.get("api_key_format", "Bearer {key}"))
            headers[header_name] = template.format(key=key)

        try:
            import httpx

            response = httpx.get(
                url, headers=headers,
                params=self.cfg.get("params") or None,
                timeout=float(self.cfg.get("timeout_seconds", 15)),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return self._empty(str(exc))

        symbols = _walk(payload, str(self.cfg.get("symbol_path", "")))
        scores = _walk(payload, str(self.cfg.get("score_path", "")))
        result = self._empty()
        for index, symbol in enumerate(symbols):
            ticker = str(symbol).strip().upper()
            if not ticker:
                continue
            score = _num(scores[index]) if index < len(scores) else 1.0
            result.values[ticker] = float(score if score is not None else 1.0)
        log.info("external source '%s': %d symbols", url, len(result.values))
        return result


# --------------------------------------------------------------------------- #
def build_sources(discovery_cfg: Any, runner: Any) -> list[AttentionSource]:
    """Instantiate every enabled source from config."""
    table = {
        "most_actives": MostActivesSource,
        "movers": MoversSource,
        "news": NewsSource,
        "external": HttpJsonSource,
    }
    sources: list[AttentionSource] = []
    raw = discovery_cfg.sources if hasattr(discovery_cfg, "sources") else discovery_cfg
    for key, cls in table.items():
        spec = getattr(raw, key, None) if hasattr(raw, key) else (raw or {}).get(key)
        spec = spec if isinstance(spec, dict) else (spec.model_dump() if spec else {})
        if not spec.get("enabled"):
            continue
        sources.append(cls(spec, runner))
    log.info("discovery sources: %s", ", ".join(s.name for s in sources) or "<none>")
    return sources


# --------------------------------------------------------------------------- #
# tolerant parsing - the CLI envelope has moved between alpha builds
# --------------------------------------------------------------------------- #
def _rows(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        node = payload.get(key)
        if isinstance(node, list):
            return [r for r in node if isinstance(r, dict)]
    return []


def _sym(row: dict[str, Any]) -> str:
    value = row.get("symbol") or row.get("S") or row.get("ticker") or ""
    return str(value).strip().upper()


def _symbols_of(article: dict[str, Any]) -> list[str]:
    raw = article.get("symbols") or article.get("tickers") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(s).strip().upper() for s in raw if str(s).strip()]


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _walk(payload: Any, path: str) -> list[Any]:
    """Dotted path with `[]` to walk a list. 'data[].symbol' -> [...]"""
    if not path:
        return []
    node: Any = payload
    for part in path.split("."):
        if part.endswith("[]"):
            key = part[:-2]
            node = node.get(key, []) if isinstance(node, dict) and key else node
            if not isinstance(node, list):
                return []
            node = list(node)
            continue
        if isinstance(node, list):
            node = [n.get(part) if isinstance(n, dict) else None for n in node]
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return []
    if isinstance(node, list):
        return node
    return [node] if node is not None else []


def cli_runner(binary: str, credentials: Any, paper: bool, timeout: int = 45) -> Any:
    """A `run(args)` callable backed by the Alpaca CLI."""

    def run(args: list[str]) -> Any:
        env = os.environ.copy()
        if credentials and getattr(credentials, "configured", False):
            env["ALPACA_API_KEY"] = credentials.api_key
            env["ALPACA_SECRET_KEY"] = credentials.secret_key
        env["ALPACA_LIVE_TRADE"] = "false" if paper else "true"
        env["ALPACA_OUTPUT"] = "json"

        proc = subprocess.run(  # noqa: S603
            [binary, *args], capture_output=True, text=True,
            timeout=timeout, env=env, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`{binary} {' '.join(args)}` exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
            )
        text = (proc.stdout or "").strip()
        return json.loads(text) if text else {}

    return run
