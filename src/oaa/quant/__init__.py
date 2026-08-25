"""Quantitative machinery for the overnight pairs strategy.

    cointegration  offline Engle-Granger screen -> the approved pair universe
    kalman         online dynamic hedge ratio and spread z-score
    features       the design matrix for the overnight gap model
    forecast       Huber baseline + quantile ensemble -> direction and tails

Every module here is pure: numpy/pandas in, numbers out, no I/O and no broker.
That is what makes the same code run live and inside the walk-forward backtest.
"""

from oaa.quant.cointegration import CointegrationResult, find_pairs, test_pair
from oaa.quant.features import build_features, feature_names
from oaa.quant.forecast import GapForecast, OvernightGapModel
from oaa.quant.kalman import KalmanPairFilter, KalmanState

__all__ = [
    "CointegrationResult",
    "GapForecast",
    "KalmanPairFilter",
    "KalmanState",
    "OvernightGapModel",
    "build_features",
    "feature_names",
    "find_pairs",
    "test_pair",
]
