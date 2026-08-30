"""Cost and clock guards.

Per COST_STRUCTURE.md, the bid-ask spread is 50-100x the entire regulatory fee
load. Crossing one $0.05-wide option quote once costs more than the regulatory
fees on twenty iron condors. That single fact, not a preference, is why the
intraday book trades two symbols.

Every gate here returns a `GateResult` rather than a bool, because the reason a
candidate was refused is the artefact worth keeping: the rejection log is the
evidence that the agent reasons rather than fires.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oaa.core.types import Leg, Side, TradeIdea

MULTIPLIER = 100


@dataclass
class GateResult:
    passed: bool
    gate: str
    reason: str = ""
    metrics: dict[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.passed

    @classmethod
    def ok(cls, gate: str, **metrics: float) -> GateResult:
        return cls(passed=True, gate=gate, metrics=metrics)

    @classmethod
    def veto(cls, gate: str, reason: str, **metrics: float) -> GateResult:
        return cls(passed=False, gate=gate, reason=reason, metrics=metrics)


# --------------------------------------------------------------------------- #
# spread
# --------------------------------------------------------------------------- #
def relative_spread(idea: TradeIdea) -> float | None:
    """Worst (ask-bid)/mid across the legs whose percentage width MEANS
    something.

    On a CREDIT structure the bought legs are protective wings, and a wing is a
    cheap option by construction - that is what makes it a hedge. A $0.05 wing
    quoted 0.04/0.06 is 40% of mid and always will be, however deep the market:
    a percentage test on it measures the leg's price, not its liquidity, and it
    vetoed XLF and TLT out of the book entirely (3,878 spread declines in
    `20260830-013506__wing-fix`, up from 1,616, as the wings recovered by the
    `min_price` fix arrived and were failed here instead).

    What the wing actually costs is bounded and small - a $0.01 half-spread is
    $1 per contract per side - and it is already charged in full by
    `round_trip_spread_cost` below, which every caller checks against the
    credit. So the percentage ceiling applies to the legs being SOLD, where a
    wide quote means selling into a bad market and the size of the mistake
    scales with the premium collected.

    On a DEBIT structure the bought leg is the thesis, not a hedge, and stays
    gated - `intraday_momentum` buys a single long option and must keep its
    ceiling.
    """
    def counted(leg: Leg) -> bool:
        if leg.quote is None or leg.quote.spread_pct is None:
            return False
        return not (idea.is_credit and leg.side is Side.BUY)

    widths = [leg.quote.spread_pct for leg in idea.legs if counted(leg)]
    if not widths:
        # Every leg exempt (or unquoted): fall back to the old behaviour rather
        # than silently passing a structure nobody measured.
        widths = [
            leg.quote.spread_pct
            for leg in idea.legs
            if leg.quote is not None and leg.quote.spread_pct is not None
        ]
    return max(widths) if widths else None


def round_trip_spread_cost(idea: TradeIdea) -> float:
    """Dollars lost to crossing every leg twice, for one structure.

    Half the quoted width per leg per side, two sides, times the contract
    multiplier. This is the number that decides whether the intraday book has
    an edge at all, and it is invisible in raw P&L.
    """
    total = 0.0
    for leg in idea.legs:
        quote = leg.quote
        if quote is None or quote.bid is None or quote.ask is None:
            continue
        total += (quote.ask - quote.bid) * leg.ratio
    return round(total * MULTIPLIER, 2)


def spread_gate(
    idea: TradeIdea,
    max_relative_spread: float,
    target_profit: float | None = None,
    max_cost_fraction: float = 0.30,
) -> GateResult:
    """Mandatory. Expect this to reject more candidates than any other gate -
    that is the finding, not a bug."""
    worst = relative_spread(idea)
    cost = round_trip_spread_cost(idea)
    metrics = {"relative_spread": worst or 0.0, "round_trip_spread_cost": cost}

    if worst is not None and worst > max_relative_spread:
        return GateResult.veto(
            "spread",
            f"widest leg quote is {worst:.1%} of mid, ceiling is "
            f"{max_relative_spread:.1%}",
            **metrics,
        )
    if target_profit and target_profit > 0 and cost > max_cost_fraction * target_profit:
        return GateResult.veto(
            "spread",
            f"round-trip spread ${cost:,.2f} is {cost / target_profit:.0%} of the "
            f"${target_profit:,.2f} target, ceiling is {max_cost_fraction:.0%}",
            **metrics,
        )
    return GateResult.ok("spread", **metrics)


# --------------------------------------------------------------------------- #
# time of day
# --------------------------------------------------------------------------- #
def _hhmm(value: str) -> dt.time:
    hour, minute = (int(part) for part in str(value).split(":")[:2])
    return dt.time(hour, minute)


def time_gate(
    now_et: dt.datetime,
    no_entry_before: str = "09:45",
    no_entry_after: str = "14:45",
    skip_lunch: bool = True,
    lunch_window: tuple[str, str] | list[str] = ("11:30", "13:30"),
) -> GateResult:
    """The open is wide and unstable; the last hour has no runway before the
    firewall cutoff; the lunch tape thins out and VWAP signals degrade there."""
    clock = now_et.time()
    if clock < _hhmm(no_entry_before):
        return GateResult.veto(
            "time_of_day",
            f"{clock:%H:%M} is before {no_entry_before} - the open is wide and unstable",
        )
    if clock >= _hhmm(no_entry_after):
        return GateResult.veto(
            "time_of_day",
            f"{clock:%H:%M} is past {no_entry_after} - insufficient runway before the "
            "15:15 firewall cutoff, and a position that cannot be closed calmly is "
            "a position that should not be opened",
        )
    if skip_lunch:
        lo, hi = _hhmm(lunch_window[0]), _hhmm(lunch_window[1])
        if lo <= clock < hi:
            return GateResult.veto(
                "time_of_day",
                f"{clock:%H:%M} is inside the {lunch_window[0]}-{lunch_window[1]} "
                "lunch window - thin volume degrades the VWAP signal",
            )
    return GateResult.ok("time_of_day")


# --------------------------------------------------------------------------- #
# dated windows (entry cutoff / submission flatten)
# --------------------------------------------------------------------------- #
def parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def entry_window_gate(now: dt.datetime, entry_cutoff_utc: str | None) -> GateResult:
    """Stop opening structures once the remaining window is shorter than one can
    meaningfully decay."""
    cutoff = parse_utc(entry_cutoff_utc)
    if cutoff is None:
        return GateResult.ok("entry_window")
    moment = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
    if moment >= cutoff:
        return GateResult.veto(
            "entry_window",
            f"past the {cutoff:%Y-%m-%d %H:%M UTC} entry cutoff - a structure opened "
            "now cannot decay before the book is flattened",
        )
    return GateResult.ok("entry_window")


def gates_summary(results: list[GateResult]) -> dict[str, Any]:
    """The rejection log row: which gate vetoed, and every metric it measured."""
    failed = [r for r in results if not r.passed]
    metrics: dict[str, float] = {}
    for result in results:
        metrics.update({f"{result.gate}.{k}": v for k, v in result.metrics.items()})
    return {
        "passed": not failed,
        "vetoed_by": failed[0].gate if failed else None,
        "reason": failed[0].reason if failed else "",
        "checked": [r.gate for r in results],
        "metrics": metrics,
    }
