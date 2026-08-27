"""The critic, in replay.

The live decision path in `agents/orchestrator.py` is:

    modelled cost -> CRITIC scores and may pass -> risk engine -> partner veto

A backtest that skips the critic is not replaying that path. It over-reports
trades (every candidate the critic would have passed on gets taken) and it
throws away the reasoning that is the most interesting artefact the system
produces. So the replay runs the same `Critic` class the live agent runs.

Three modes, and the default is deliberate.

``off``
    Skip the critic entirely. Useful exactly once: to measure what the critic
    is contributing, by diffing a run against it.

``heuristic`` (default)
    The real `Critic` with a null LLM client, which is the documented
    degraded path - `agents/llm.py` falls back to rules whenever the provider
    is unreachable, and the live system is expected to keep trading when it
    does. Deterministic, free, and identical code.

``llm``
    The real model - by default a DIFFERENT one from the live agent's.
    `backtest.critic.llm` overrides `agents.llm` for replay only, because the
    two have opposite cost shapes: live is a handful of calls a day, a replay
    scores every candidate in every session and gets re-run whenever a
    parameter moves. The default points the replay at Gemini and leaves the
    live loop on Anthropic. Set `backtest.critic.llm: null` to share one.

    Two things make this usable in a backtest rather than a novelty:

      * **Caching.** Every verdict is keyed by a hash of the exact prompt
        inputs and written to disk, so a re-run of the same window costs
        nothing and returns byte-identical verdicts. A backtest whose numbers
        move when you re-run it is not a backtest.
      * **Budget.** A hard cap on calls per run, because a 60-session window
        over six symbols generates thousands of candidates and each one is a
        model call. Past the cap the critic degrades to the heuristic - the
        same degradation the live system has - and the run records how many
        times that happened.

**The honesty problem with ``llm`` mode, which must be stated wherever a
figure from it is quoted:** the model is being asked about a period that may
sit inside its training data. Asked whether to sell QQQ volatility on 4 June
2026, it may not be reasoning from the inputs in the prompt alone. That is
lookahead of a kind no amount of engineering removes, and it is why
``heuristic`` is the default and why the dashboard flags any run that used
``llm``. Use ``llm`` mode to inspect the QUALITY OF THE REASONING on a handful
of trades. Do not use it to produce a P&L number for the deck.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaa.agents.critic import Critic
from oaa.agents.llm import LLMClient, NullClient, get_llm
from oaa.config.schema import Config
from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot, MarketContext, TradeIdea

log = get_logger("backtest.critic")

MODE_OFF = "off"
MODE_HEURISTIC = "heuristic"
MODE_LLM = "llm"
MODES = (MODE_OFF, MODE_HEURISTIC, MODE_LLM)


def _fingerprint(
    idea: TradeIdea, market: MarketContext, memory: str, model: str = ""
) -> str:
    """A stable key for one critic question.

    Everything the prompt actually varies on, and nothing else - no timestamps,
    no idea id - so the same question always hits the same cache entry.
    """
    payload = {
        "symbol": idea.symbol,
        "strategy": idea.strategy,
        "structure": idea.structure.value,
        "legs": sorted(f"{leg.side.value}:{leg.symbol}:{leg.ratio}" for leg in idea.legs),
        "net_price": round(idea.net_price, 4),
        "max_loss": idea.max_loss,
        "max_profit": idea.max_profit,
        "pop": idea.probability_of_profit,
        "thesis": idea.thesis,
        "spot": round(market.spot, 4),
        "iv_rank": market.iv_rank,
        "iv_rv": market.iv_rv_ratio,
        "adx": market.adx,
        "trend": market.trend_strength,
        "memory": memory,
        "model": model,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


@dataclass
class CriticStats:
    mode: str = MODE_HEURISTIC
    scored: int = 0
    passed: int = 0          # candidates the critic declined
    llm_calls: int = 0
    cache_hits: int = 0
    budget_exhausted: int = 0
    provider: str = "none"
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scored": self.scored,
            "declined": self.passed,
            "llm_calls": self.llm_calls,
            "cache_hits": self.cache_hits,
            "degraded_to_heuristic": self.budget_exhausted,
            "provider": self.provider,
            "model": self.model,
        }


@dataclass
class ReplayCritic:
    """Wraps the live `Critic` with caching, a call budget and bookkeeping."""

    cfg: Config
    mode: str = MODE_HEURISTIC
    cache_dir: Path | None = None
    max_llm_calls: int = 250
    stats: CriticStats = field(default_factory=CriticStats)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"critic mode must be one of {MODES}, got {self.mode!r}")
        self.stats.mode = self.mode
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_path: Path | None = None

        # The replay's own provider, falling back to the live one when the
        # backtest block is null.
        self.llm_cfg = self.cfg.backtest.critic.llm or self.cfg.agents.llm

        client: LLMClient
        if self.mode == MODE_LLM:
            client = get_llm(self.llm_cfg)
            if client.provider == "null":
                log.warning(
                    "critic mode 'llm' requested but no provider is reachable "
                    "(backtest.critic.llm -> %s, model %s) - running the "
                    "heuristic critic instead",
                    self.llm_cfg.provider, self.llm_cfg.model,
                )
                self.mode = MODE_HEURISTIC
                self.stats.mode = MODE_HEURISTIC
        else:
            client = NullClient(self.llm_cfg)

        # "null" is the class name of the no-op client and reads like a
        # failure in the run artefact. In heuristic mode there is nothing to
        # fail - no model is meant to be called - so say that instead.
        self.stats.provider = (
            client.provider if self.mode == MODE_LLM
            else ("none (heuristic critic)" if self.mode == MODE_HEURISTIC else "none (off)")
        )
        self.stats.model = self.llm_cfg.model if self.mode == MODE_LLM else ""
        self.critic = Critic(self.cfg, client)

        if self.mode == MODE_LLM and self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            model = self.llm_cfg.model.replace("/", "-")
            self._cache_path = self.cache_dir / f"critic__{model}.json"
            if self._cache_path.exists():
                try:
                    self._cache = json.loads(self._cache_path.read_text())
                except json.JSONDecodeError:
                    log.warning("corrupt critic cache - starting fresh")

    # ------------------------------------------------------------------ #
    @property
    def active(self) -> bool:
        return self.mode != MODE_OFF

    def score(
        self,
        idea: TradeIdea,
        market: MarketContext,
        account: AccountSnapshot,
        opened_today: int = 0,
        memory: str = "",
    ) -> dict[str, Any]:
        if not self.active:
            return {
                "score": idea.confidence, "verdict": "trade",
                "reasoning": idea.thesis, "source": "critic_off",
            }

        self.stats.scored += 1
        if self.mode != MODE_LLM:
            return self.critic.score(idea, market, account, opened_today, memory)

        key = _fingerprint(idea, market, memory, self.llm_cfg.model)
        cached = self._cache.get(key)
        if cached is not None:
            self.stats.cache_hits += 1
            return dict(cached)

        if self.stats.llm_calls >= self.max_llm_calls:
            self.stats.budget_exhausted += 1
            verdict = self.critic._heuristic(idea, market)
            verdict["source"] = "heuristic (llm budget exhausted)"
            return verdict

        self.stats.llm_calls += 1
        verdict = self.critic.score(idea, market, account, opened_today, memory)
        if verdict.get("source") == "llm":
            self._cache[key] = verdict
            self._flush()
        else:
            # The call failed and `Critic` fell back to rules. Caching that
            # would freeze an outage into the run and make it look like a
            # verdict the model actually gave.
            self.stats.budget_exhausted += 1
        return verdict

    def accepts(self, verdict: dict[str, Any]) -> bool:
        if not self.active:
            return True
        allowed = self.critic.accepts(verdict)
        if not allowed:
            self.stats.passed += 1
        return allowed

    # ------------------------------------------------------------------ #
    def _flush(self) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.write_text(json.dumps(self._cache, indent=1, default=str))
        except OSError as exc:  # noqa: BLE001
            log.warning("could not write the critic cache: %s", exc)

    def describe(self) -> dict[str, Any]:
        payload = self.stats.as_dict()
        payload.update(
            {
                "provider_config": {
                    "provider": self.llm_cfg.provider,
                    "model": self.llm_cfg.model,
                    "temperature": self.llm_cfg.temperature,
                    "seed": self.llm_cfg.seed,
                    "api_key_env": self.llm_cfg.api_key_env,
                    "shared_with_live": self.cfg.backtest.critic.llm is None,
                },
                "min_score_to_trade": self.cfg.agents.critic.min_score_to_trade,
                "require_thesis": self.cfg.agents.critic.require_thesis,
                "max_llm_calls": self.max_llm_calls,
                "cache": str(self._cache_path) if self._cache_path else None,
                "lookahead_warning": (
                    "the model may have the replayed period in its training data"
                    if self.mode == MODE_LLM else None
                ),
            }
        )
        return payload
