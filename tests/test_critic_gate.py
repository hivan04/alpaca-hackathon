"""The critic's score is the decision.

Until 2 Sep the model's free-text `verdict` word could veto an idea whose score
had cleared `min_score_to_trade`, and it did: all eight declines that session
carried `verdict: "pass"`, three of them at a score of exactly 0.55 - the
configured bar. The threshold was unfalsifiable, and lowering it would have
changed nothing. These tests pin the number as the authority.
"""

from __future__ import annotations

from typing import Any

import pytest

from oaa.agents.critic import Critic
from oaa.agents.prompts import CRITIC_SYSTEM, CRITIC_USER


class _StubLLM:
    """Returns whatever the test hands it, in the shape `json_complete` gives."""

    provider = "stub"

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}
        self.prompts: list[str] = []

    def json_complete(self, system: str, prompt: str, default: Any = None) -> Any:
        self.prompts.append(prompt)
        return dict(self.payload) if self.payload else default


@pytest.fixture
def critic(cfg) -> Critic:
    return Critic(cfg, _StubLLM())


def _verdict(score: float, word: str = "trade", reasoning: str = "a mechanism") -> dict:
    return {"score": score, "verdict": word, "reasoning": reasoning}


def test_a_score_at_the_bar_trades_even_when_the_model_says_pass(critic):
    """The 2 Sep case, three times over: score 0.55, bar 0.55, model word 'pass'."""
    critic.settings.min_score_to_trade = 0.55
    assert critic.accepts(_verdict(0.55, "pass")) is True


def test_a_score_below_the_bar_never_trades_however_the_model_words_it(critic):
    critic.settings.min_score_to_trade = 0.55
    assert critic.accepts(_verdict(0.54, "trade")) is False
    assert critic.accepts(_verdict(0.35, "trade")) is False


def test_a_missing_or_malformed_score_stands_the_trade_down(critic):
    critic.settings.min_score_to_trade = 0.55
    assert critic.accepts({"verdict": "trade", "reasoning": "a mechanism"}) is False
    assert critic.accepts(_verdict("not a number", "trade")) is False  # type: ignore[arg-type]


def test_a_call_citing_nothing_is_still_refused(critic):
    """`require_thesis` is the one non-numeric veto that survives."""
    critic.settings.min_score_to_trade = 0.55
    critic.settings.require_thesis = True
    assert critic.accepts(_verdict(0.90, "trade", reasoning="   ")) is False


def test_the_stored_verdict_follows_the_score_and_keeps_the_models_word(cfg, account):
    """The journal must not record a decision the gate did not make. The
    model's own word survives as `model_verdict`, so the disagreement that
    started this - eight 'pass' words over eight declines - stays auditable."""
    import datetime as dt

    from oaa.core.types import Leg, MarketContext, Side, StructureType, TradeIdea

    llm = _StubLLM({"score": 0.55, "verdict": "pass", "reasoning": "marginal"})
    critic = Critic(cfg, llm)
    critic.settings.enabled = True
    critic.settings.min_score_to_trade = 0.55

    market = MarketContext(
        symbol="SPY", asof=dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc), spot=765.0,
    )
    idea = TradeIdea(
        symbol="SPY", strategy="intraday_momentum", structure=StructureType.SINGLE_LONG,
        legs=[Leg(symbol="SPY260903C00765000", side=Side.BUY)], net_price=1.50,
        thesis="a VWAP cross", confidence=0.47,
    )

    result = critic.score(idea, market, account)
    assert result["verdict"] == "trade", "the score cleared the bar"
    assert result["model_verdict"] == "pass", "the model's own word is kept"
    assert critic.accepts(result) is True

    # And the prompt the model saw stated the bar it was being scored against.
    assert "0.55" in llm.prompts[0]


def test_the_bar_is_stated_in_the_prompt_the_model_sees(cfg):
    """A model asked to emit a verdict word without being told the threshold
    will disagree with the number it just wrote. Now it is told."""
    assert "{min_score:.2f}" in CRITIC_USER
    assert "SCORE is what decides" in CRITIC_SYSTEM
