"""The macro lens — the one agent that reads rather than computes.

Four deterministic specialists already run: a vol lens, a trend lens, an event
lens and a statistical-arbitrage lens. Each reads a feature vector and each is
backtestable, which is why they are Python and not prompts.

The gap they cannot fill is *unstructured*. Nothing in a z-score tells you the
tape is risk-off because three chip names gapped on a supply headline. That
requires reading, and reading is the thing a language model is genuinely better
at than a feature vector.

So this lens emits a **regime, not a trade**. It never proposes a position and
it cannot approve one. It answers three questions:

    which strategies should be live tonight
    how much wider the protective collars should sit
    which symbols carry too much headline risk to hold overnight

Everything it produces is an overlay on strategies that remain independently
testable. If the model is unavailable, a deterministic breadth rule takes over
and the system carries on — the regime is coarser, nothing stops.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oaa.core.logging import get_logger
from oaa.discovery.score import AttentionSnapshot

log = get_logger("discovery.macro")

REGIMES = ("risk_on", "risk_off", "neutral", "high_dispersion")
STANCES = ("trade", "reduce", "stand_down")


@dataclass
class MacroView:
    """A structured regime read. Consumed by the strategies as an overlay."""

    regime: str = "neutral"
    vol_expectation: str = "stable"          # expanding | stable | contracting
    overnight_risk: float = 0.3              # 0..1
    collar_widening: float = 1.0             # multiplier on hedge distance
    guidance: dict[str, str] = field(default_factory=dict)
    #: Legs whose catalyst their pair partner does NOT share. High attention on
    #: its own is not grounds for flagging - a sector-wide move leaves the
    #: spread intact and is often a better environment, not a worse one.
    flagged_symbols: dict[str, str] = field(default_factory=dict)
    #: Sector-wide moves deliberately NOT flagged. Kept because "we saw this and
    #: decided it was shared" is the reasoning a judge wants to read.
    shared_themes: list[str] = field(default_factory=list)
    rationale: str = ""
    source: str = "rules"
    asof: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    def stance_for(self, strategy: str) -> str:
        return self.guidance.get(strategy, "trade")

    def may_trade(self, strategy: str) -> bool:
        return self.stance_for(strategy) != "stand_down"

    def is_flagged(self, symbol: str) -> bool:
        return symbol.upper() in self.flagged_symbols

    def size_multiplier(self, strategy: str) -> float:
        """Reduce, don't stop. A cautious regime halves size rather than
        forfeiting the night entirely — an idle book scores zero P&L too."""
        return {"trade": 1.0, "reduce": 0.5, "stand_down": 0.0}.get(
            self.stance_for(strategy), 1.0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "vol_expectation": self.vol_expectation,
            "overnight_risk": round(self.overnight_risk, 3),
            "collar_widening": round(self.collar_widening, 3),
            "guidance": self.guidance,
            "flagged_symbols": self.flagged_symbols,
            "shared_themes": self.shared_themes,
            "rationale": self.rationale,
            "source": self.source,
            "asof": self.asof.isoformat(),
        }

    def summary(self) -> str:
        flagged = f", {len(self.flagged_symbols)} flagged" if self.flagged_symbols else ""
        stances = " ".join(f"{k}={v}" for k, v in sorted(self.guidance.items()))
        return (
            f"[{self.source}] {self.regime} / vol {self.vol_expectation} / "
            f"overnight risk {self.overnight_risk:.2f} / collar x{self.collar_widening:.2f}"
            f"{flagged}" + (f" | {stances}" if stances else "")
        )


class MacroLens:
    def __init__(self, cfg: Any, llm: Any = None, journal: Any = None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.journal = journal
        self.settings = getattr(cfg, "macro", None)

    @property
    def enabled(self) -> bool:
        return bool(self.settings is None or self.settings.enabled)

    @property
    def use_llm(self) -> bool:
        return bool(
            self.enabled
            and (self.settings is None or self.settings.use_llm)
            and self.llm is not None
            and getattr(self.llm, "provider", "null") != "null"
        )

    # ------------------------------------------------------------------ #
    def view(
        self,
        snapshot: AttentionSnapshot,
        strategies: list[str] | None = None,
        pairs: list[tuple[str, str]] | None = None,
        extra_context: str = "",
    ) -> MacroView:
        if not self.enabled:
            return MacroView(rationale="macro lens disabled", source="disabled")

        baseline = self._rules_view(snapshot, strategies or [], pairs or [])
        if not self.use_llm:
            self._record(baseline, snapshot)
            return baseline

        view = self._llm_view(snapshot, strategies or [], pairs or [], extra_context, baseline)
        self._record(view, snapshot)
        return view

    # ------------------------------------------------------------------ #
    def _llm_view(
        self,
        snapshot: AttentionSnapshot,
        strategies: list[str],
        pairs: list[tuple[str, str]],
        extra_context: str,
        fallback: MacroView,
    ) -> MacroView:
        from oaa.agents.prompts import MACRO_SYSTEM, MACRO_USER

        max_headlines = int(getattr(self.settings, "max_headlines", 25) or 25)
        top = snapshot.top(int(getattr(self.settings, "max_symbols", 15) or 15))

        lines: list[str] = []
        headline_budget = max_headlines
        for entry in top:
            bits = [f"  {entry.symbol:<6} attention {entry.score:.2f}"]
            if entry.percent_change is not None:
                arrow = "+" if entry.direction == "up" else "-"
                bits.append(f"move {arrow}{entry.percent_change:.1f}%")
            if entry.news_velocity is not None:
                bits.append(f"news x{entry.news_velocity:.1f} vs baseline")
            lines.append("  ".join(bits))
            for headline in entry.headlines[:2]:
                if headline_budget <= 0:
                    break
                lines.append(f"        - {headline[:150]}")
                headline_budget -= 1

        # Show the model each leg's attention next to its partner's - the
        # comparison IS the judgement it is being asked to make.
        pair_lines: list[str] = []
        for left, right in pairs:
            left_entry = snapshot.symbols.get(left)
            right_entry = snapshot.symbols.get(right)
            pair_lines.append(
                f"  {left}/{right}"
                f"   {left} attention {(left_entry.score if left_entry else 0):.2f}"
                f" (news x{(left_entry.news_velocity if left_entry and left_entry.news_velocity else 1):.1f})"
                f"   {right} attention {(right_entry.score if right_entry else 0):.2f}"
                f" (news x{(right_entry.news_velocity if right_entry and right_entry.news_velocity else 1):.1f})"
            )

        breadth = snapshot.breadth_ratio
        prompt = MACRO_USER.format(
            asof=snapshot.asof.strftime("%Y-%m-%d %H:%M UTC"),
            breadth=(f"{breadth:.0%} of movers are gainers" if breadth is not None else "unknown"),
            gainers=snapshot.breadth.get("gainers", 0),
            losers=snapshot.breadth.get("losers", 0),
            attention="\n".join(lines) or "  (no attention data)",
            pairs="\n".join(pair_lines) or "  (none configured)",
            strategies="\n".join(f"  - {name}" for name in strategies) or "  (none)",
            extra=extra_context or "",
        )

        payload = self.llm.json_complete(MACRO_SYSTEM, prompt, default={})
        if not payload:
            log.info("macro lens returned nothing - keeping the rules-based view")
            return fallback

        return self._parse(payload, strategies, fallback)

    # ------------------------------------------------------------------ #
    def _parse(
        self, payload: dict[str, Any], strategies: list[str], fallback: MacroView
    ) -> MacroView:
        """Validate hard. A model that returns nonsense must not widen risk."""
        view = MacroView(source="llm")

        regime = str(payload.get("regime", "")).strip().lower()
        view.regime = regime if regime in REGIMES else fallback.regime

        vol = str(payload.get("vol_expectation", "")).strip().lower()
        view.vol_expectation = vol if vol in {"expanding", "stable", "contracting"} else "stable"

        view.overnight_risk = _clamp(payload.get("overnight_risk"), 0.0, 1.0, fallback.overnight_risk)
        # Bounded at 1.0 below: the lens may widen a hedge, never narrow one.
        view.collar_widening = _clamp(payload.get("collar_widening"), 1.0, 2.5, 1.0)

        raw_guidance = payload.get("guidance")
        if isinstance(raw_guidance, dict):
            for name, stance in raw_guidance.items():
                cleaned = str(stance).strip().lower()
                if cleaned in STANCES and (not strategies or name in strategies):
                    view.guidance[str(name)] = cleaned

        flagged = payload.get("flagged_symbols")
        if isinstance(flagged, dict):
            view.flagged_symbols = {
                str(k).upper(): str(v)[:200] for k, v in list(flagged.items())[:25]
            }
        elif isinstance(flagged, list):
            view.flagged_symbols = {
                str(item).upper(): "flagged by the macro lens" for item in flagged[:25]
            }

        themes = payload.get("shared_themes")
        if isinstance(themes, list):
            view.shared_themes = [str(t)[:200] for t in themes[:10]]

        view.rationale = str(payload.get("rationale", ""))[:1200]
        if not view.rationale:
            view.rationale = fallback.rationale
        log.info("macro lens: %s", view.summary())
        return view

    # ------------------------------------------------------------------ #
    def _rules_view(
        self,
        snapshot: AttentionSnapshot,
        strategies: list[str],
        pairs: list[tuple[str, str]] | None = None,
    ) -> MacroView:
        """Deterministic fallback, from breadth and news velocity alone.

        Coarse on purpose - it exists so the system keeps a defensible regime
        read with no model configured, not to approximate one. It does get the
        shared-versus-idiosyncratic test approximately right, though, by a crude
        proxy: if BOTH legs of a pair are newsy, the catalyst is probably shared
        and the spread is probably intact.
        """
        view = MacroView(source="rules")
        breadth = snapshot.breadth_ratio
        news_driven = snapshot.news_driven()

        if breadth is None:
            view.regime = "neutral"
        elif breadth >= 0.65:
            view.regime = "risk_on"
        elif breadth <= 0.35:
            view.regime = "risk_off"
        elif len(news_driven) >= 6:
            # Lots of individually newsy names on a balanced tape is dispersion,
            # not direction - good for pairs, bad for index premium selling.
            view.regime = "high_dispersion"
        else:
            view.regime = "neutral"

        # A crowded news tape means more names capable of gapping tonight.
        crowding = min(1.0, len(news_driven) / 12.0)
        skew = abs((breadth or 0.5) - 0.5) * 2
        view.overnight_risk = round(min(1.0, 0.20 + 0.5 * crowding + 0.3 * skew), 3)
        view.vol_expectation = (
            "expanding" if view.overnight_risk > 0.6
            else "contracting" if view.overnight_risk < 0.25
            else "stable"
        )
        view.collar_widening = round(1.0 + 0.5 * max(0.0, view.overnight_risk - 0.4), 3)

        threshold = float(getattr(self.settings, "stand_down_threshold", 0.75) or 0.75)
        for name in strategies:
            if view.overnight_risk >= threshold:
                view.guidance[name] = "stand_down"
            elif view.overnight_risk >= threshold - 0.2:
                view.guidance[name] = "reduce"
            else:
                view.guidance[name] = "trade"

        # Shared vs idiosyncratic, approximated. A leg is only flagged when its
        # partner is NOT also on unusual news - a sector-wide move leaves the
        # spread intact and flagging it would cost a session for nothing.
        newsy = {entry.symbol for entry in news_driven}
        flagged: dict[str, str] = {}
        shared: list[str] = []

        for left, right in (pairs or []):
            left_hot, right_hot = left in newsy, right in newsy
            if left_hot and right_hot:
                shared.append(
                    f"{left}/{right}: both legs on unusual news - reading it as a "
                    "shared catalyst, so the spread should hold"
                )
                continue
            for hot, cold in ((left, right), (right, left)):
                if hot in newsy:
                    velocity = snapshot.symbols[hot].news_velocity or 0
                    flagged[hot] = (
                        f"news velocity x{velocity:.1f} vs baseline while {cold} is quiet "
                        "- looks idiosyncratic, so the hedge would not cover it"
                    )

        paired = {s for pair in (pairs or []) for s in pair}
        for entry in news_driven[:20]:
            if entry.symbol not in paired and entry.symbol not in flagged:
                flagged[entry.symbol] = (
                    f"news velocity x{entry.news_velocity:.1f} vs baseline, no pair "
                    "partner to compare against"
                )

        view.flagged_symbols = flagged
        view.shared_themes = shared
        view.rationale = (
            f"Breadth {('unknown' if breadth is None else f'{breadth:.0%} gainers')}, "
            f"{len(news_driven)} names on unusual news volume. "
            f"{len(flagged)} leg(s) flagged as idiosyncratic, {len(shared)} pair(s) "
            f"reading as a shared catalyst. Regime {view.regime}; overnight risk "
            f"{view.overnight_risk:.2f}."
        )
        return view

    def _record(self, view: MacroView, snapshot: AttentionSnapshot) -> None:
        if self.journal is None:
            return
        try:
            self.journal.event(
                "macro_view",
                **view.as_dict(),
                attention_symbols=len(snapshot.symbols),
                breadth=snapshot.breadth,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("macro journal write failed: %s", exc)


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        return round(min(high, max(low, float(value))), 4)
    except (TypeError, ValueError):
        return default
