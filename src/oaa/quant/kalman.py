"""Kalman filter for a cointegrated pair.

A static OLS hedge ratio is wrong the moment the relationship drifts, and
overnight pairs live or die on the hedge being right at 15:55. This tracks
the intercept and slope as a two-state random walk observed through the
price relationship:

    state   x_t = [alpha_t, beta_t]        (random walk, covariance Q)
    obs     y_t = alpha_t + beta_t * x_t + e_t     (noise variance R)

The formulation follows Chan's dynamic-hedge-ratio treatment. Implemented in
plain numpy so there is no unmaintained third-party dependency in the hot path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass
class KalmanState:
    """The filter's belief after the most recent observation."""

    alpha: float = 0.0
    beta: float = 1.0
    spread: float = 0.0            # observation minus prediction
    spread_mean: float = 0.0
    spread_std: float = 0.0
    zscore: float = 0.0
    prediction_variance: float = 0.0
    observations: int = 0

    @property
    def hedge_ratio(self) -> float:
        return self.beta

    def as_dict(self) -> dict[str, float | int]:
        return {
            "alpha": round(self.alpha, 6),
            "beta": round(self.beta, 6),
            "spread": round(self.spread, 6),
            "spread_mean": round(self.spread_mean, 6),
            "spread_std": round(self.spread_std, 6),
            "zscore": round(self.zscore, 4),
            "observations": self.observations,
        }


class KalmanPairFilter:
    """Online estimator of (alpha, beta) and the standardised spread.

    Parameters
    ----------
    delta:
        How fast the hedge ratio is allowed to move. Small = sticky, large =
        jumpy. 1e-4 is the usual starting point; above ~1e-3 the beta chases
        noise and the z-score stops meaning anything.
    obs_covariance:
        Measurement noise. Larger values trust the model over the latest print.
    zscore_window:
        Rolling window for the spread's mean and standard deviation. This is
        what turns a raw residual into a tradable z-score.
    """

    def __init__(
        self,
        delta: float = 1e-4,
        obs_covariance: float = 1e-3,
        zscore_window: int = 60,
        warmup: int = 20,
    ) -> None:
        if not 0 < delta < 1:
            raise ValueError("delta must be in (0, 1)")
        self.delta = delta
        self.obs_covariance = obs_covariance
        self.zscore_window = max(5, zscore_window)
        self.warmup = max(2, warmup)

        # State transition noise: a random walk on [alpha, beta].
        self._Q = delta / (1 - delta) * np.eye(2)
        self._x = np.zeros(2)          # [alpha, beta]
        self._P = np.eye(2)            # state covariance
        self._initialised = False
        #: Observations buffered before the OLS seed is computed. Seeding from
        #: y/x alone folds the intercept into beta and the filter then spends
        #: hundreds of observations walking it back out.
        self._seed_size = max(10, min(60, self.warmup * 2))
        self._seed_y: list[float] = []
        self._seed_x: list[float] = []
        self._residuals: list[float] = []
        self.state = KalmanState()
        self.history: list[KalmanState] = []

    # ------------------------------------------------------------------ #
    def update(self, y: float, x: float) -> KalmanState:
        """Feed one (dependent, independent) price pair.

        `y` is the long leg, `x` the hedge leg. Returns the updated state.
        """
        obs = np.array([1.0, float(x)])

        if not self._initialised:
            self._seed_y.append(float(y))
            self._seed_x.append(float(x))
            if len(self._seed_y) < self._seed_size:
                # Not enough to seed yet: hold a provisional state so the caller
                # still gets a well-formed KalmanState back.
                self._x = np.array([0.0, (y / x) if x else 1.0])
                self.state = KalmanState(
                    alpha=0.0, beta=float(self._x[1]),
                    observations=self.state.observations + 1,
                )
                self.history.append(self.state)
                return self.state
            self._seed(self._seed_y, self._seed_x)

        # Predict
        P_prior = self._P + self._Q
        prediction = float(obs @ self._x)
        residual = float(y) - prediction
        prediction_var = float(obs @ P_prior @ obs.T) + self.obs_covariance

        # Update
        gain = (P_prior @ obs) / prediction_var
        self._x = self._x + gain * residual
        self._P = P_prior - np.outer(gain, obs) @ P_prior

        self._residuals.append(residual)
        if len(self._residuals) > self.zscore_window:
            del self._residuals[: -self.zscore_window]

        window = self._residuals[-self.zscore_window :]
        mean = float(np.mean(window))
        std = float(np.std(window, ddof=1)) if len(window) > 1 else 0.0
        zscore = (residual - mean) / std if std > 1e-12 else 0.0

        self.state = KalmanState(
            alpha=float(self._x[0]),
            beta=float(self._x[1]),
            spread=residual,
            spread_mean=mean,
            spread_std=std,
            zscore=float(zscore),
            prediction_variance=prediction_var,
            observations=self.state.observations + 1,
        )
        self.history.append(self.state)
        return self.state

    # ------------------------------------------------------------------ #
    def _seed(self, y_buffer: list[float], x_buffer: list[float]) -> None:
        """Seed [alpha, beta] from an OLS over the buffered observations.

        Starting the filter at the right place matters more than it sounds:
        a badly seeded beta biases the spread, which biases the z-score, which
        is the number the whole strategy trades on.
        """
        y_arr = np.asarray(y_buffer, dtype=float)
        x_arr = np.asarray(x_buffer, dtype=float)

        # Regress the DIFFERENCES, not the levels. Over a short window a
        # random-walk x barely moves, so a level regression is near-collinear
        # with the constant and hands the intercept an absurd value that beta
        # then has to cancel out. Differencing removes the intercept entirely.
        dy, dx = np.diff(y_arr), np.diff(x_arr)
        variance = float(dx @ dx)
        if dy.size >= 2 and variance > 1e-12:
            beta = float(dx @ dy) / variance
        else:  # pragma: no cover - degenerate input
            beta = float(y_arr[-1] / x_arr[-1]) if x_arr[-1] else 1.0
        alpha = float(np.mean(y_arr) - beta * np.mean(x_arr))
        self._x = np.array([alpha, beta])
        self._initialised = True

    def fit(self, y_series: Sequence[float], x_series: Sequence[float]) -> KalmanState:
        """Run the filter over a whole history. Returns the final state."""
        if len(y_series) != len(x_series):
            raise ValueError("series must be the same length")
        for y, x in zip(y_series, x_series, strict=True):
            self.update(y, x)
        return self.state

    @property
    def ready(self) -> bool:
        """Enough observations for the z-score to be meaningful."""
        return self.state.observations >= self.warmup and self.state.spread_std > 1e-12

    def zscores(self) -> list[float]:
        return [s.zscore for s in self.history]

    def betas(self) -> list[float]:
        return [s.beta for s in self.history]

    def reset(self) -> None:
        self._x = np.zeros(2)
        self._P = np.eye(2)
        self._initialised = False
        self._residuals.clear()
        self._seed_y.clear()
        self._seed_x.clear()
        self.history.clear()
        self.state = KalmanState()


def half_life(spread: Sequence[float]) -> float | None:
    """Mean-reversion half-life in periods, via an AR(1) fit on the spread.

    A pair whose half-life is longer than the holding period is not a pairs
    trade, it is a directional bet with extra steps. For an overnight strategy
    anything much beyond a few days is a red flag.
    """
    series = np.asarray(spread, dtype=float)
    if series.size < 10:
        return None
    lagged = series[:-1]
    delta = np.diff(series)
    centred = lagged - lagged.mean()
    denom = float(centred @ centred)
    if denom < 1e-12:
        return None
    theta = float(centred @ (delta - delta.mean())) / denom
    if theta >= 0:
        return None  # diverging, not mean-reverting
    return float(-np.log(2) / theta)
