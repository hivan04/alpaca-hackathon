"""The critic.

Scores candidate ideas and writes the reasoning that goes in the journal. When
no LLM is configured it falls back to a transparent heuristic score, so the
system behaves identically minus the prose.
"""

from __future__ import annotations

from typing import Any

from oaa.agents.llm import LLMClient
from oaa.agents.prompts import CRITIC_SYSTEM, CRITIC_USER
from oaa.config.schema import Config
from oaa.core.logging import get_logger
from oaa.core.types import AccountSnapshot, MarketContext, TradeIdea

log = get_logger("agents.critic")


class Critic:
    def __init__(self, cfg: Config, llm: LLMClient) -> None:
        self.cfg = cfg
        self.llm = llm
        self.settings = cfg.agents.critic

    def score(
        self,
        idea: TradeIdea,
        market: MarketContext,
        account: AccountSnapshot,
        opened_today: int = 0,
        memory: str = "",
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            return {"score": idea.confidence, "verdict": "trade",
                    "reasoning": idea.thesis, "source": "disabled"}

        if self.llm.provider == "null":
            return self._heuristic(idea, market)

        prompt = CRITIC_USER.format(
            symbol=idea.symbol,
            strategy=idea.strategy,
            structure=idea.structure.value,
            legs=", ".join(f"{leg.side.value} {leg.symbol}" for leg in idea.legs),
            net_price=idea.net_price,
            credit_or_debit="credit received" if idea.is_credit else "debit paid",
            max_loss=f"{idea.max_loss:,.0f}" if idea.max_loss else "unknown",
            max_profit=f"{idea.max_profit:,.0f}" if idea.max_profit else "open-ended",
            reward_risk=idea.reward_risk or "n/a",
            pop=idea.probability_of_profit or "n/a",
            thesis=idea.thesis or "(none given)",
            spot=f"{market.spot:.2f}",
            iv_rank=_fmt(market.iv_rank),
            iv_rv=_fmt(market.iv_rv_ratio),
            trend=_fmt(market.trend_strength),
            adx=_fmt(market.adx),
            equity=account.equity,
            open_positions=len(account.option_positions()),
            same_symbol=len(account.by_underlying(idea.symbol)),
            opened_today=opened_today,
            memory=f"RECENT OUTCOMES\n{memory}" if memory else "",
            min_score=self.settings.min_score_to_trade,
        )
        result = self.llm.json_complete(CRITIC_SYSTEM, prompt, default={})
        if not result:
            log.debug("critic returned nothing for %s - falling back", idea.symbol)
            return self._heuristic(idea, market)

        result.setdefault("score", idea.confidence)
        # THE SCORE IS THE DECISION; the model's own verdict word is an opinion
        # about it. Keeping the model's string as the verdict let the two
        # disagree, and on 2 Sep they disagreed eight times out of eight: every
        # declined idea carried `verdict: "pass"`, three of them at a score of
        # exactly 0.55 - the configured bar. `min_score_to_trade` was therefore
        # decorative; moving it would have changed nothing. The model's word is
        # preserved as `model_verdict` so the disagreement stays auditable.
        result["model_verdict"] = result.get("verdict")
        result["verdict"] = (
            "trade" if float(result["score"]) >= self.settings.min_score_to_trade else "pass"
        )
        result["source"] = "llm"
        return result

    def accepts(self, verdict: dict[str, Any]) -> bool:
        """One number decides. See `score` - the model's `verdict` string is
        advisory and is not consulted here, because a free-text word that can
        veto a passing score makes the configured threshold unfalsifiable.

        A malformed or missing score is 0.0, so a broken response still stands
        the trade down; `require_thesis` still refuses a call citing nothing."""
        try:
            score = float(verdict.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        if self.settings.require_thesis and not str(verdict.get("reasoning", "")).strip():
            return False
        return score >= self.settings.min_score_to_trade

    # -- fallback ------------------------------------------------------------ #
    def _heuristic(self, idea: TradeIdea, market: MarketContext) -> dict[str, Any]:
        """Transparent, explainable scoring when no LLM is available.

        Deliberately simple: the point is that the system keeps trading and the
        reasoning stays auditable, not that this approximates a model.
        """
        score = idea.confidence
        notes: list[str] = []

        # A defined-risk CREDIT structure has reward/risk bounded by
        # credit/(width - credit). A 25-delta condor is 0.43 by construction, so
        # a flat "below 0.5 is poor" bar penalises every one of them for having
        # the shape that defines them - and combined with a confidence formula
        # anchored on a stale threshold, that alone declined 100% of this book's
        # candidates. Credit structures are judged on whether the hit rate the
        # EXIT policy needs looks attainable; debit structures keep the ratio
        # test, where it means what it says.
        breakeven = idea.meta.get("breakeven_hit_rate")
        if idea.is_credit and breakeven:
            notes.append(f"breakeven hit rate {float(breakeven):.0%}")
        elif idea.reward_risk and idea.reward_risk >= 1.5:
            score += 0.10
            notes.append(f"reward/risk {idea.reward_risk:.2f}")
        elif idea.reward_risk and idea.reward_risk < 0.5:
            score -= 0.10
            notes.append(f"poor reward/risk {idea.reward_risk:.2f}")

        if idea.probability_of_profit and idea.probability_of_profit > 0.65:
            score += 0.08
            notes.append(f"PoP {idea.probability_of_profit:.0%}")

        if idea.is_credit and (market.iv_rank or 0) > 0.5:
            score += 0.05
            notes.append("selling premium into rich IV")
        if not idea.is_credit and (market.iv_rank or 1) < 0.4:
            score += 0.05
            notes.append("buying premium while IV is cheap")

        score = round(max(0.0, min(1.0, score)), 3)
        return {
            "score": score,
            "verdict": "trade" if score >= self.settings.min_score_to_trade else "pass",
            "reasoning": (idea.thesis + (" Heuristic: " + ", ".join(notes) if notes else "")).strip(),
            "concerns": [],
            "source": "heuristic",
        }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"
