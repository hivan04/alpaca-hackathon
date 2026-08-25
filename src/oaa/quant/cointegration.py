"""Offline pair selection.

Run once (or weekly) against 1-2 years of daily closes to produce the approved
pair universe in `config/pairs.yaml`. This is deliberately *not* in the live
path: re-screening cointegration intraday is how you overfit a pair into
existence at 15:45 and lose money on it at 09:35.

Screen criteria, all of which must pass:
  * Engle-Granger p-value below the threshold (the relationship is real)
  * mean-reversion half-life inside a usable range (it reverts fast enough)
  * correlation above a floor (the legs actually move together)
  * both legs liquid enough to short and to hedge with options
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from oaa.core.logging import get_logger
from oaa.quant.kalman import half_life

log = get_logger("quant.cointegration")


@dataclass
class CointegrationResult:
    left: str                      # the long-side candidate (dependent variable)
    right: str                     # the hedge leg (independent variable)
    pvalue: float
    tstat: float
    hedge_ratio: float
    correlation: float
    half_life_days: float | None
    spread_std: float
    observations: int
    passed: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.left}/{self.right}"

    def as_dict(self) -> dict[str, object]:
        return {
            "left": self.left,
            "right": self.right,
            "pvalue": round(self.pvalue, 6),
            "tstat": round(self.tstat, 4),
            "hedge_ratio": round(self.hedge_ratio, 6),
            "correlation": round(self.correlation, 4),
            "half_life_days": (
                round(self.half_life_days, 2) if self.half_life_days is not None else None
            ),
            "spread_std": round(self.spread_std, 6),
            "observations": self.observations,
            "passed": self.passed,
            "reasons": self.reasons,
        }


def _ols_beta(y: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    """Intercept and slope from a plain OLS of y on x."""
    design = np.column_stack([np.ones_like(x), x])
    coeffs, *_ = np.linalg.lstsq(design, y, rcond=None)
    return float(coeffs[0]), float(coeffs[1])


def test_pair(
    left: str,
    right: str,
    left_closes: Sequence[float],
    right_closes: Sequence[float],
    max_pvalue: float = 0.05,
    min_correlation: float = 0.70,
    half_life_range: tuple[float, float] = (1.0, 30.0),
    min_observations: int = 250,
) -> CointegrationResult:
    """Engle-Granger test plus the practical filters."""
    y = np.asarray(left_closes, dtype=float)
    x = np.asarray(right_closes, dtype=float)
    n = min(y.size, x.size)
    y, x = y[-n:], x[-n:]

    reasons: list[str] = []
    if n < min_observations:
        return CointegrationResult(
            left=left, right=right, pvalue=1.0, tstat=0.0, hedge_ratio=0.0,
            correlation=0.0, half_life_days=None, spread_std=0.0, observations=n,
            passed=False, reasons=[f"only {n} observations, need {min_observations}"],
        )

    try:
        from statsmodels.tsa.stattools import coint
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "statsmodels is required for the cointegration screen - "
            "pip install -e '.[quant]'"
        ) from exc

    tstat, pvalue, _ = coint(y, x)
    alpha, beta = _ols_beta(y, x)
    spread = y - (alpha + beta * x)
    correlation = float(np.corrcoef(y, x)[0, 1])
    hl = half_life(spread)

    if pvalue > max_pvalue:
        reasons.append(f"p-value {pvalue:.4f} above {max_pvalue}")
    if abs(correlation) < min_correlation:
        reasons.append(f"correlation {correlation:.3f} below {min_correlation}")
    if hl is None:
        reasons.append("spread does not mean-revert")
    elif not half_life_range[0] <= hl <= half_life_range[1]:
        reasons.append(
            f"half-life {hl:.1f}d outside [{half_life_range[0]}, {half_life_range[1]}]"
        )
    if beta <= 0:
        reasons.append(f"hedge ratio {beta:.3f} is not positive - not a long/short pair")

    return CointegrationResult(
        left=left, right=right, pvalue=float(pvalue), tstat=float(tstat),
        hedge_ratio=beta, correlation=correlation, half_life_days=hl,
        spread_std=float(np.std(spread, ddof=1)), observations=n,
        passed=not reasons, reasons=reasons,
    )


def find_pairs(
    closes: dict[str, Sequence[float]],
    max_pvalue: float = 0.05,
    min_correlation: float = 0.70,
    half_life_range: tuple[float, float] = (1.0, 30.0),
    min_observations: int = 250,
    top_n: int | None = None,
) -> list[CointegrationResult]:
    """Screen every ordered pair in the universe.

    Both directions are tested because Engle-Granger is not symmetric: which
    series is the dependent variable changes the result, and the better
    direction is the one we want to be long.
    """
    symbols = sorted(closes)
    results: list[CointegrationResult] = []

    for left, right in itertools.permutations(symbols, 2):
        try:
            result = test_pair(
                left, right, closes[left], closes[right],
                max_pvalue=max_pvalue, min_correlation=min_correlation,
                half_life_range=half_life_range, min_observations=min_observations,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("pair %s/%s failed: %s", left, right, exc)
            continue
        results.append(result)

    passing = sorted((r for r in results if r.passed), key=lambda r: r.pvalue)
    log.info("cointegration screen: %d/%d ordered pairs passed", len(passing), len(results))

    # Keep only the better direction of each unordered pair.
    seen: set[frozenset[str]] = set()
    deduped: list[CointegrationResult] = []
    for result in passing:
        key = frozenset({result.left, result.right})
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)

    return deduped[:top_n] if top_n else deduped
