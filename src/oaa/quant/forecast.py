"""Two-stage overnight gap forecast.

Stage 1 - Huber regression
    A robust linear baseline. Overnight gap distributions have fat tails and a
    handful of enormous outliers (a gap-down on news is not the same process as
    a normal night). Ordinary least squares chases those outliers and produces a
    baseline that is wrong on the 95% of nights that matter. Huber's loss is
    quadratic near zero and linear in the tails, so the fit describes the
    typical night and lets the outliers be outliers.

Stage 2 - Quantile gradient boosting on the residuals
    The linear baseline cannot express "the tail is wide tonight because
    realised vol has doubled". A quantile model on the *residuals* of stage 1
    can, and each quantile does a different job:

        q50  the directional edge, and therefore the position size
        q05  the bad-night scenario, which sets the protective put strike
        q95  the good-night scenario, which sets the protective call strike

That is the whole point of the ensemble: it does not just predict a number, it
predicts the shape of the distribution, and the option strikes are read
directly off the tails rather than picked by a human at 15:45.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from oaa.core.logging import get_logger
from oaa.quant.features import FEATURES, to_matrix

log = get_logger("quant.forecast")

QUANTILES = (0.05, 0.50, 0.95)


@dataclass
class GapForecast:
    """The distribution of tonight's spread return, per $1 of long notional."""

    expected: float               # q50 - the directional edge
    lower: float                  # q05 - the bad night
    upper: float                  # q95 - the good night
    baseline: float = 0.0         # the Huber component alone
    confidence: float = 0.0       # 0..1, scaled edge-to-tail-width
    model: str = "untrained"
    train_rows: int = 0
    features: dict[str, float] = field(default_factory=dict)

    @property
    def direction(self) -> str:
        if self.expected > 0:
            return "long_spread"      # buy y, sell x
        if self.expected < 0:
            return "short_spread"     # sell y, buy x
        return "flat"

    @property
    def tail_width(self) -> float:
        return abs(self.upper - self.lower)

    @property
    def edge_to_risk(self) -> float:
        """Expected move divided by the width of the bad tail.

        This is the number that decides whether a night is worth trading: a
        0.1% edge against a 4% downside tail is not a trade, however confident
        the median looks.
        """
        downside = abs(self.expected - self.lower)
        return round(abs(self.expected) / downside, 4) if downside > 1e-9 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected": round(self.expected, 6),
            "lower_q05": round(self.lower, 6),
            "upper_q95": round(self.upper, 6),
            "baseline": round(self.baseline, 6),
            "direction": self.direction,
            "confidence": round(self.confidence, 4),
            "edge_to_risk": self.edge_to_risk,
            "tail_width": round(self.tail_width, 6),
            "model": self.model,
            "train_rows": self.train_rows,
        }

    def describe(self) -> str:
        return (
            f"{self.direction} E[r]={self.expected:+.3%} "
            f"[q05 {self.lower:+.3%}, q95 {self.upper:+.3%}] "
            f"edge/risk={self.edge_to_risk:.2f} conf={self.confidence:.2f} ({self.model})"
        )


class OvernightGapModel:
    """Huber baseline + quantile residual ensemble, fitted walk-forward.

    Falls back gracefully: LightGBM if installed, scikit-learn's quantile
    gradient booster if not, and an empirical-quantile rule if there is not
    enough data to fit anything. The system keeps producing a usable forecast
    rather than refusing to trade — which matters when the judged window is
    seven days long.
    """

    def __init__(
        self,
        min_train_rows: int = 120,
        quantiles: Sequence[float] = QUANTILES,
        n_estimators: int = 150,
        learning_rate: float = 0.05,
        max_depth: int = 3,
        random_state: int = 7,
    ) -> None:
        self.min_train_rows = min_train_rows
        self.quantiles = tuple(quantiles)
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.random_state = random_state

        self._huber: Any = None
        self._quantile_models: dict[float, Any] = {}
        self._empirical: dict[float, float] = {}
        self._train_rows = 0
        self._backend = "untrained"
        self._residual_scale = 0.0

    # ------------------------------------------------------------------ #
    @property
    def trained(self) -> bool:
        return self._backend != "untrained"

    @property
    def backend(self) -> str:
        return self._backend

    # ------------------------------------------------------------------ #
    def fit(self, rows: Sequence[dict[str, float]], targets: Sequence[float]) -> OvernightGapModel:
        X = to_matrix(rows)
        y = np.asarray(targets, dtype=float)
        if X.shape[0] != y.shape[0]:
            raise ValueError("features and targets must have the same number of rows")

        finite = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
        X, y = X[finite], y[finite]
        self._train_rows = int(X.shape[0])

        # Always keep the empirical quantiles: they are the safety net when a
        # model refuses to fit, and the sanity check when one fits too well.
        if y.size:
            self._empirical = {q: float(np.quantile(y, q)) for q in self.quantiles}

        if self._train_rows < self.min_train_rows:
            self._backend = "empirical"
            log.info(
                "only %d training rows (need %d) - using empirical quantiles",
                self._train_rows, self.min_train_rows,
            )
            return self

        # -- stage 1: robust linear baseline ------------------------------ #
        try:
            from sklearn.linear_model import HuberRegressor
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import StandardScaler

            self._huber = Pipeline([
                ("scale", StandardScaler()),
                ("huber", HuberRegressor(epsilon=1.35, alpha=1e-3, max_iter=500)),
            ])
            self._huber.fit(X, y)
            baseline = self._huber.predict(X)
        except Exception as exc:  # noqa: BLE001
            log.warning("Huber stage failed (%s) - falling back to the mean", exc)
            self._huber = None
            baseline = np.full_like(y, float(np.mean(y)))

        residuals = y - baseline
        self._residual_scale = float(np.std(residuals, ddof=1)) if residuals.size > 1 else 0.0

        # -- stage 2: quantile model on the residuals ---------------------- #
        self._quantile_models = {}
        backend = self._fit_quantiles(X, residuals)
        self._backend = backend
        log.info(
            "gap model trained on %d rows (%s), residual sd %.4f",
            self._train_rows, backend, self._residual_scale,
        )
        return self

    def _fit_quantiles(self, X: np.ndarray, residuals: np.ndarray) -> str:
        try:
            import lightgbm as lgb

            for q in self.quantiles:
                model = lgb.LGBMRegressor(
                    objective="quantile",
                    alpha=q,
                    n_estimators=self.n_estimators,
                    learning_rate=self.learning_rate,
                    max_depth=self.max_depth,
                    num_leaves=max(4, 2 ** self.max_depth - 1),
                    min_child_samples=max(5, X.shape[0] // 40),
                    subsample=0.9,
                    subsample_freq=1,
                    colsample_bytree=0.9,
                    random_state=self.random_state,
                    verbose=-1,
                )
                model.fit(X, residuals)
                self._quantile_models[q] = model
            return "huber+lightgbm"
        except ImportError:
            log.info("lightgbm not installed - using sklearn quantile boosting")
        except Exception as exc:  # noqa: BLE001
            log.warning("lightgbm quantile fit failed (%s) - trying sklearn", exc)

        try:
            from sklearn.ensemble import GradientBoostingRegressor

            for q in self.quantiles:
                model = GradientBoostingRegressor(
                    loss="quantile", alpha=q,
                    n_estimators=self.n_estimators,
                    learning_rate=self.learning_rate,
                    max_depth=self.max_depth,
                    random_state=self.random_state,
                )
                model.fit(X, residuals)
                self._quantile_models[q] = model
            return "huber+sklearn"
        except Exception as exc:  # noqa: BLE001
            log.warning("quantile stage unavailable (%s) - residual quantiles only", exc)
            self._quantile_models = {}
            for q in self.quantiles:
                self._empirical[q] = float(np.quantile(residuals, q))
            return "huber+empirical"

    # ------------------------------------------------------------------ #
    def predict(self, row: dict[str, float]) -> GapForecast:
        X = to_matrix([row])

        if self._backend in ("untrained", "empirical") or self._huber is None:
            return self._empirical_forecast(row)

        try:
            baseline = float(self._huber.predict(X)[0])
        except Exception as exc:  # noqa: BLE001
            log.warning("Huber prediction failed (%s)", exc)
            return self._empirical_forecast(row)

        quantile_values: dict[float, float] = {}
        for q in self.quantiles:
            model = self._quantile_models.get(q)
            if model is None:
                quantile_values[q] = self._empirical.get(q, 0.0)
                continue
            try:
                quantile_values[q] = float(model.predict(X)[0])
            except Exception:  # noqa: BLE001
                quantile_values[q] = self._empirical.get(q, 0.0)

        lower = baseline + quantile_values.get(0.05, 0.0)
        median = baseline + quantile_values.get(0.50, 0.0)
        upper = baseline + quantile_values.get(0.95, 0.0)

        # Quantile models are fitted independently and can cross. Sorting is
        # the standard, honest fix - a q05 above q50 is a fitting artefact,
        # not information.
        lower, median, upper = sorted((lower, median, upper))

        return GapForecast(
            expected=median,
            lower=lower,
            upper=upper,
            baseline=baseline,
            confidence=self._confidence(median, lower, upper),
            model=self._backend,
            train_rows=self._train_rows,
            features=dict(row),
        )

    def _empirical_forecast(self, row: dict[str, float]) -> GapForecast:
        """No model yet: mean-revert on the z-score with empirical tails.

        Deliberately simple and deliberately weak — it exists so day one of the
        event still trades while the model warms up, and its low confidence
        means the risk layer sizes it small.
        """
        zscore = float(row.get("zscore", 0.0))
        lower = self._empirical.get(0.05, -0.01)
        median = self._empirical.get(0.50, 0.0)
        upper = self._empirical.get(0.95, 0.01)
        # A stretched spread is expected to pull back toward its mean.
        drift = -math.tanh(zscore / 2.0) * abs(upper - lower) * 0.25
        expected = median + drift
        return GapForecast(
            expected=expected,
            lower=min(lower, expected),
            upper=max(upper, expected),
            baseline=median,
            confidence=min(0.35, abs(zscore) / 10.0),
            model="empirical",
            train_rows=self._train_rows,
            features=dict(row),
        )

    @staticmethod
    def _confidence(median: float, lower: float, upper: float) -> float:
        """Edge relative to tail width, squashed into 0..1.

        Wide tails with a small median = low confidence, which is exactly the
        night you do not want to be leveraged into.
        """
        width = abs(upper - lower)
        if width < 1e-9:
            return 0.0
        return round(min(1.0, abs(median) / width * 3.0), 4)

    # ------------------------------------------------------------------ #
    def feature_importance(self, top_n: int = 10) -> list[tuple[str, float]]:
        """Which features the q50 model actually leans on. Good deck material."""
        model = self._quantile_models.get(0.50)
        importances = getattr(model, "feature_importances_", None)
        if importances is None:
            return []
        pairs = list(zip(FEATURES, [float(v) for v in importances], strict=False))
        return sorted(pairs, key=lambda kv: -kv[1])[:top_n]

    def summary(self) -> dict[str, Any]:
        return {
            "backend": self._backend,
            "train_rows": self._train_rows,
            "residual_scale": round(self._residual_scale, 6),
            "empirical_quantiles": {str(k): round(v, 6) for k, v in self._empirical.items()},
            "top_features": self.feature_importance(5),
        }
