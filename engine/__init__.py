"""Blended 5-20m VWAP quantitative simulation and backtesting engine."""

from .simulator import MarketConfig, SessionData, simulate_market
from .blended_vwap import (
    ALL_HORIZONS,
    BLEND_HORIZONS,
    STATIC_WEIGHTS,
    compute_session_indicators,
    compute_rolling_vwap_and_dispersion,
)

__all__ = [
    "MarketConfig",
    "SessionData",
    "simulate_market",
    "ALL_HORIZONS",
    "BLEND_HORIZONS",
    "STATIC_WEIGHTS",
    "compute_session_indicators",
    "compute_rolling_vwap_and_dispersion",
]
