"""Strictly causal (zero-lookahead) rolling VWAP, volume-weighted dispersion,
Blended 5-20m VWAP models, and 13-configuration intraday backtester.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple
import numpy as np

from .simulator import MarketConfig, SessionData, compute_side_cost_bps

ALL_HORIZONS: Tuple[int, ...] = (5, 6, 8, 10, 12, 15, 16, 18, 20)
BLEND_HORIZONS: Tuple[int, ...] = (5, 10, 15, 20)
STATIC_WEIGHTS: np.ndarray = np.array([0.35, 0.30, 0.20, 0.15], dtype=np.float64)
LOG_HORIZON_COORD: np.ndarray = np.log(np.array(BLEND_HORIZONS, dtype=np.float64) / 10.0) / np.log(2.0)

SIGMA_FLOOR_FRAC: float = 2.5e-4  # 2.5 bps minimum dispersion floor for z-score stability


@dataclass(frozen=True)
class StrategyConfig:
    """Definition of one of the 13 intraday 5-20m VWAP trade-window configurations."""

    id: str
    name: str
    short_label: str
    family: str
    regime: str  # "micro", "core", "anchor"
    is_blended: bool
    is_flagship: bool
    window_label: str
    signal_source: str  # e.g. "h5", "h10", "static_blend", "vol_weighted_blend", "adaptive_blend"
    entry_z: float
    exit_z: float  # exit once hold >= min_hold and side * z_t >= -exit_z
    min_hold: int
    max_hold: int
    stop_sigma: float = 2.8  # causal price-based adverse excursion stop in units of entry sigma
    min_disloc_bps: float = 6.0  # minimum |close - VWAP| in bps so signal clears spread+impact
    min_rel_vol: float = 0.0
    max_slope_ratio: float = 99.0
    min_horizon_agree: int = 0
    require_flow_confirm: bool = False  # causal deceleration / exhaustion confirmation on bar t vs t-1
    use_anchor_exit: bool = False  # revert to entry-time blended VWAP anchor rather than drifting rolling VWAP
    anchor_reversion_frac: float = 0.85  # fraction of initial dislocation to capture back to entry_vwap
    adaptive_gate: bool = False  # flagship volume-clock consensus & drift gating
    cooldown_bars: int = 2


@dataclass
class TradeRecord:
    """Single executed intraday trade."""

    session_id: int
    trade_id: int
    side: int  # +1 LONG, -1 SHORT
    entry_idx: int
    exit_idx: int
    entry_time: str
    exit_time: str
    hold_min: int
    entry_price: float
    exit_price: float
    entry_vwap: float
    entry_z: float
    exit_z: float
    gross_ret: float
    cost_ret: float
    net_ret: float
    gross_bps: float
    cost_bps: float
    net_bps: float
    slippage_saved_bps: float
    exit_reason: str
    entry_participation: float
    entry_vol_bps: float


@dataclass
class SessionIndicators:
    """Strictly causal indicators computed for a single 390-bar session."""

    typical_price: np.ndarray
    vwap_by_h: Dict[int, np.ndarray]
    sigma_by_h: Dict[int, np.ndarray]
    z_by_h: Dict[int, np.ndarray]
    causal_bar_sigma: np.ndarray
    rel_vol_ema: np.ndarray
    slope_ratio: np.ndarray
    # Static blend
    weights_static: np.ndarray  # shape (N, 4)
    vwap_static: np.ndarray
    sigma_static: np.ndarray
    z_static: np.ndarray
    # Vol-weighted (inverse-dispersion) blend
    weights_vol: np.ndarray  # shape (N, 4)
    vwap_vol: np.ndarray
    sigma_vol: np.ndarray
    z_vol: np.ndarray
    # Adaptive (volume-clock + slope-dispersion) blend
    weights_adaptive: np.ndarray  # shape (N, 4)
    vwap_adaptive: np.ndarray
    sigma_adaptive: np.ndarray
    z_adaptive: np.ndarray


def compute_rolling_vwap_and_dispersion(
    typical_price: np.ndarray,
    volume: np.ndarray,
    horizon: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute strictly causal rolling VWAP and volume-weighted rolling dispersion.

    For bar t, window is [max(0, t - horizon + 1), t] inclusive:
      VWAP_t^(h)  = sum(p_i * V_i) / sum(V_i)
      sigma_t^(h) = sqrt(sum(V_i * (p_i - VWAP_t^(h))^2) / sum(V_i))
    """
    n = len(typical_price)
    vwap = np.empty(n, dtype=np.float64)
    sigma = np.empty(n, dtype=np.float64)

    for t in range(n):
        start = 0 if t + 1 < horizon else t - horizon + 1
        p_win = typical_price[start : t + 1]
        v_win = volume[start : t + 1]
        v_sum = float(np.sum(v_win))
        w_mean = float(np.dot(p_win, v_win) / v_sum)
        diff = p_win - w_mean
        w_var = float(np.dot(v_win, diff * diff) / v_sum)
        vwap[t] = w_mean
        sigma[t] = float(np.sqrt(max(w_var, 0.0)))

    return vwap, sigma


def compute_causal_bar_sigma(close: np.ndarray, lookback: int = 15) -> np.ndarray:
    """Strictly causal rolling 1-minute return volatility estimate (fraction)."""
    n = len(close)
    sigma = np.empty(n, dtype=np.float64)
    log_ret = np.zeros(n, dtype=np.float64)
    if n > 1:
        log_ret[1:] = np.diff(np.log(close))

    prior_sigma = 5.5e-4
    for t in range(n):
        if t < 3:
            sigma[t] = prior_sigma
        else:
            start = max(1, t - lookback + 1)
            win = log_ret[start : t + 1]
            s = float(np.std(win, ddof=1)) if len(win) > 1 else prior_sigma
            sigma[t] = max(s, 2.0e-4)
    return sigma


def _blend_from_weights(
    vwap_mat: np.ndarray,
    sigma_mat: np.ndarray,
    weights: np.ndarray,
    close: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Combine 4 horizon VWAPs and dispersions via exact law of total variance."""
    vwap_blend = np.sum(weights * vwap_mat, axis=1)
    within_var = np.sum(weights * (sigma_mat ** 2), axis=1)
    between_var = np.sum(weights * ((vwap_mat - vwap_blend[:, None]) ** 2), axis=1)
    sigma_blend = np.sqrt(np.maximum(within_var + between_var, 0.0))
    sigma_eff = np.maximum(sigma_blend, SIGMA_FLOOR_FRAC * close)
    z_blend = (close - vwap_blend) / sigma_eff
    return vwap_blend, sigma_blend, z_blend


def compute_session_indicators(session: SessionData) -> SessionIndicators:
    """Compute all causal rolling VWAPs, dispersions, and 3 Blended 5-20m models."""
    n = len(session.close)
    typical_price = (session.high + session.low + session.close) / 3.0
    volume = session.volume
    close = session.close

    vwap_by_h: Dict[int, np.ndarray] = {}
    sigma_by_h: Dict[int, np.ndarray] = {}
    z_by_h: Dict[int, np.ndarray] = {}

    for h in ALL_HORIZONS:
        vw, sg = compute_rolling_vwap_and_dispersion(typical_price, volume, h)
        vwap_by_h[h] = vw
        sigma_by_h[h] = sg
        sg_eff = np.maximum(sg, SIGMA_FLOOR_FRAC * close)
        z_by_h[h] = (close - vw) / sg_eff

    causal_bar_sigma = compute_causal_bar_sigma(close, lookback=15)

    # Causal 5-bar EMA of relative volume (volume clock)
    alpha_v = 2.0 / (5.0 + 1.0)
    rel_vol_ema = np.empty(n, dtype=np.float64)
    rel_vol_ema[0] = float(session.rel_vol[0])
    for t in range(1, n):
        rel_vol_ema[t] = alpha_v * float(session.rel_vol[t]) + (1.0 - alpha_v) * rel_vol_ema[t - 1]

    vwap_mat = np.column_stack([vwap_by_h[h] for h in BLEND_HORIZONS])
    sigma_mat = np.column_stack([sigma_by_h[h] for h in BLEND_HORIZONS])
    sigma_floor = SIGMA_FLOOR_FRAC * close[:, None]
    sigma_safe = np.maximum(sigma_mat, sigma_floor)

    mean_sigma = np.mean(sigma_safe, axis=1)
    slope_ratio = np.abs(vwap_by_h[5] - vwap_by_h[20]) / mean_sigma

    # 1. Static Blend 5-20m: fixed weights (0.35, 0.30, 0.20, 0.15)
    weights_static = np.tile(STATIC_WEIGHTS, (n, 1))
    vwap_static, sigma_static, z_static = _blend_from_weights(vwap_mat, sigma_mat, weights_static, close)

    # 2. Vol-Weighted Blend 5-20m: inverse-dispersion weighted blend across 5m/10m/15m/20m
    inv_disp = 1.0 / sigma_safe
    weights_vol = inv_disp / np.sum(inv_disp, axis=1, keepdims=True)
    vwap_vol, sigma_vol, z_vol = _blend_from_weights(vwap_mat, sigma_mat, weights_vol, close)

    # 3. Adaptive Blend 5-20m (Flagship):
    #    Volume-clock + slope-dispersion adaptive blend tilting toward 5m-10m during
    #    high relative-volume bursts and toward 15m-20m during calm drift regimes.
    g_vol = np.log(np.clip(rel_vol_ema, 0.30, 3.20))
    slope_excess = np.clip(slope_ratio - 0.55, -0.35, 1.35)

    sqrt_h = np.sqrt(np.array(BLEND_HORIZONS, dtype=np.float64))[None, :]
    sigma_per_min = sigma_safe / sqrt_h
    mean_sigma_pm = np.mean(sigma_per_min, axis=1, keepdims=True)
    tilt = (-0.56 * g_vol[:, None] + 0.28 * slope_excess[:, None]) * LOG_HORIZON_COORD[None, :]
    disp_factor = (mean_sigma_pm / sigma_per_min) ** 0.35
    raw_adapt = STATIC_WEIGHTS[None, :] * disp_factor * np.exp(tilt)
    weights_adaptive = raw_adapt / np.sum(raw_adapt, axis=1, keepdims=True)
    vwap_adaptive, sigma_adaptive, z_adaptive = _blend_from_weights(
        vwap_mat, sigma_mat, weights_adaptive, close
    )

    return SessionIndicators(
        typical_price=typical_price,
        vwap_by_h=vwap_by_h,
        sigma_by_h=sigma_by_h,
        z_by_h=z_by_h,
        causal_bar_sigma=causal_bar_sigma,
        rel_vol_ema=rel_vol_ema,
        slope_ratio=slope_ratio,
        weights_static=weights_static,
        vwap_static=vwap_static,
        sigma_static=sigma_static,
        z_static=z_static,
        weights_vol=weights_vol,
        vwap_vol=vwap_vol,
        sigma_vol=sigma_vol,
        z_vol=z_vol,
        weights_adaptive=weights_adaptive,
        vwap_adaptive=vwap_adaptive,
        sigma_adaptive=sigma_adaptive,
        z_adaptive=z_adaptive,
    )


def get_13_strategy_configs() -> List[StrategyConfig]:
    """Return the exact 13 intraday 5-20m VWAP trade-window configurations across 3 regimes."""
    return [
        # --- MICRO-PULSE REGIME (5-8 min avg hold): 4 configs ---
        StrategyConfig(
            id="micro_5m_fast_fade",
            name="5m Fast Fade",
            short_label="5m Fast Fade",
            family="Single-Horizon",
            regime="micro",
            is_blended=False,
            is_flagship=False,
            window_label="5m",
            signal_source="h5",
            entry_z=2.48,
            exit_z=-0.30,
            min_hold=5,
            max_hold=6,
            stop_sigma=3.0,
            min_disloc_bps=10.0,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="micro_5m_flow_sweep",
            name="5m Flow Sweep",
            short_label="5m Flow Sweep",
            family="Flow-Filtered",
            regime="micro",
            is_blended=False,
            is_flagship=False,
            window_label="5m",
            signal_source="h5",
            entry_z=2.12,
            exit_z=-0.45,
            min_hold=5,
            max_hold=8,
            stop_sigma=3.0,
            min_disloc_bps=8.5,
            min_rel_vol=1.08,
            max_slope_ratio=1.55,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="micro_6m_micro_band",
            name="6m Micro-Band",
            short_label="6m Micro-Band",
            family="Single-Horizon",
            regime="micro",
            is_blended=False,
            is_flagship=False,
            window_label="6m",
            signal_source="h6",
            entry_z=2.30,
            exit_z=-0.35,
            min_hold=6,
            max_hold=9,
            stop_sigma=3.0,
            min_disloc_bps=9.0,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="micro_8m_elastic_fade",
            name="8m Elastic Fade",
            short_label="8m Elastic",
            family="Single-Horizon",
            regime="micro",
            is_blended=False,
            is_flagship=False,
            window_label="8m",
            signal_source="h8",
            entry_z=2.18,
            exit_z=-0.40,
            min_hold=7,
            max_hold=10,
            stop_sigma=3.0,
            min_disloc_bps=8.5,
            cooldown_bars=2,
        ),
        # --- CORE BLENDED REGIME (9-14 min avg hold): 5 configs ---
        StrategyConfig(
            id="core_10m_vwap",
            name="10m Core VWAP",
            short_label="10m Core",
            family="Single-Horizon",
            regime="core",
            is_blended=False,
            is_flagship=False,
            window_label="10m",
            signal_source="h10",
            entry_z=2.14,
            exit_z=-0.40,
            min_hold=8,
            max_hold=13,
            stop_sigma=3.0,
            min_disloc_bps=8.5,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="static_blend_5_20",
            name="Static Blend 5–20m",
            short_label="Static 5–20m",
            family="Blended 5–20m",
            regime="core",
            is_blended=True,
            is_flagship=False,
            window_label="5–20m",
            signal_source="static_blend",
            entry_z=1.56,
            exit_z=-0.45,
            min_hold=7,
            max_hold=16,
            stop_sigma=2.9,
            min_disloc_bps=7.5,
            max_slope_ratio=1.60,
            min_horizon_agree=3,
            require_flow_confirm=False,
            use_anchor_exit=True,
            anchor_reversion_frac=0.72,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="vol_weighted_blend_5_20",
            name="Vol-Weighted Blend 5–20m",
            short_label="Vol-Wt 5–20m",
            family="Blended 5–20m",
            regime="core",
            is_blended=True,
            is_flagship=False,
            window_label="5–20m",
            signal_source="vol_weighted_blend",
            entry_z=1.58,
            exit_z=-0.50,
            min_hold=7,
            max_hold=18,
            stop_sigma=2.9,
            min_disloc_bps=7.0,
            max_slope_ratio=1.50,
            min_horizon_agree=4,
            require_flow_confirm=False,
            use_anchor_exit=True,
            anchor_reversion_frac=0.76,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="adaptive_blend_5_20",
            name="Adaptive Blend 5–20m",
            short_label="Adaptive 5–20m",
            family="Blended 5–20m",
            regime="core",
            is_blended=True,
            is_flagship=True,
            window_label="5–20m",
            signal_source="adaptive_blend",
            entry_z=1.58,
            exit_z=-0.55,
            min_hold=5,
            max_hold=20,
            stop_sigma=2.9,
            min_disloc_bps=7.0,
            max_slope_ratio=1.45,
            min_horizon_agree=4,
            require_flow_confirm=False,
            use_anchor_exit=True,
            anchor_reversion_frac=0.72,
            adaptive_gate=True,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="core_12m_slope_filtered",
            name="12m Slope-Filtered",
            short_label="12m Slope",
            family="Slope-Filtered",
            regime="core",
            is_blended=False,
            is_flagship=False,
            window_label="12m",
            signal_source="h12",
            entry_z=2.02,
            exit_z=-0.50,
            min_hold=12,
            max_hold=17,
            stop_sigma=3.0,
            min_disloc_bps=6.5,
            max_slope_ratio=1.38,
            cooldown_bars=2,
        ),
        # --- ANCHOR DRIFT REGIME (15-20 min avg hold): 4 configs ---
        StrategyConfig(
            id="anchor_15m_inst_band",
            name="15m Inst. Band",
            short_label="15m Inst.",
            family="Single-Horizon",
            regime="anchor",
            is_blended=False,
            is_flagship=False,
            window_label="15m",
            signal_source="h15",
            entry_z=2.04,
            exit_z=-0.25,
            min_hold=14,
            max_hold=20,
            stop_sigma=3.1,
            min_disloc_bps=6.5,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="anchor_16m_drift_pullback",
            name="16m Drift Pullback",
            short_label="16m Pullback",
            family="Slope-Filtered",
            regime="anchor",
            is_blended=False,
            is_flagship=False,
            window_label="16m",
            signal_source="h16",
            entry_z=1.98,
            exit_z=-0.35,
            min_hold=15,
            max_hold=20,
            stop_sigma=3.0,
            min_disloc_bps=6.5,
            max_slope_ratio=1.35,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="anchor_18m_slow_reversion",
            name="18m Slow Reversion",
            short_label="18m Slow Rev.",
            family="Single-Horizon",
            regime="anchor",
            is_blended=False,
            is_flagship=False,
            window_label="18m",
            signal_source="h18",
            entry_z=2.06,
            exit_z=-0.30,
            min_hold=17,
            max_hold=20,
            stop_sigma=3.1,
            min_disloc_bps=6.5,
            cooldown_bars=2,
        ),
        StrategyConfig(
            id="anchor_20m_anchor_vwap",
            name="20m Anchor VWAP",
            short_label="20m Anchor",
            family="Single-Horizon",
            regime="anchor",
            is_blended=False,
            is_flagship=False,
            window_label="20m",
            signal_source="h20",
            entry_z=2.10,
            exit_z=-0.35,
            min_hold=18,
            max_hold=20,
            stop_sigma=3.2,
            min_disloc_bps=6.5,
            cooldown_bars=2,
        ),
    ]


def _resolve_signal_arrays(
    indicators: SessionIndicators, signal_source: str
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (vwap_array, sigma_array, z_array) for a strategy's signal_source."""
    if signal_source == "static_blend":
        return indicators.vwap_static, indicators.sigma_static, indicators.z_static
    if signal_source == "vol_weighted_blend":
        return indicators.vwap_vol, indicators.sigma_vol, indicators.z_vol
    if signal_source == "adaptive_blend":
        return indicators.vwap_adaptive, indicators.sigma_adaptive, indicators.z_adaptive
    if signal_source.startswith("h"):
        h = int(signal_source[1:])
        return indicators.vwap_by_h[h], indicators.sigma_by_h[h], indicators.z_by_h[h]
    raise ValueError(f"Unknown signal_source: {signal_source}")


def compute_entry_signals(
    session: SessionData,
    indicators: SessionIndicators,
    strat: StrategyConfig,
) -> np.ndarray:
    """Strictly causal bar-by-bar entry signal array in {-1, 0, +1}.

    +1 = enter LONG (price dislocated below VWAP)
    -1 = enter SHORT (price dislocated above VWAP)
    """
    n = len(session.close)
    vwap_arr, _, z_arr = _resolve_signal_arrays(indicators, strat.signal_source)
    signals = np.zeros(n, dtype=np.int32)

    warmup = 5
    last_entry_bar = n - 2 - strat.min_hold

    for t in range(warmup, last_entry_bar + 1):
        zt = float(z_arr[t])
        rv_ema = float(indicators.rel_vol_ema[t])

        if abs(zt) < strat.entry_z:
            continue

        # Minimum price dislocation in bps so entry clears spread + impact economics
        disloc_bps = abs(float(session.close[t]) - float(vwap_arr[t])) / float(session.close[t]) * 1e4
        if disloc_bps < strat.min_disloc_bps:
            continue

        if strat.min_rel_vol > 0.0 and rv_ema < strat.min_rel_vol:
            continue

        slope_t = float(indicators.slope_ratio[t])
        if slope_t > strat.max_slope_ratio:
            continue

        proposed_side = 1 if zt <= -strat.entry_z else -1

        if strat.require_flow_confirm:
            # Causal flow-exhaustion confirmation (bars t and t-1 only)
            bar_move_t = (-proposed_side) * (float(session.close[t]) - float(session.open[t]))
            bar_move_prev = (-proposed_side) * (float(session.close[t - 1]) - float(session.open[t - 1]))
            if bar_move_t > 0.0 and bar_move_prev <= 0.0:
                if abs(zt) < strat.entry_z + 0.45:
                    continue

        if strat.min_horizon_agree > 0:
            if proposed_side == 1:
                n_agree = sum(
                    1 for h in BLEND_HORIZONS if session.close[t] < indicators.vwap_by_h[h][t]
                )
            else:
                n_agree = sum(
                    1 for h in BLEND_HORIZONS if session.close[t] > indicators.vwap_by_h[h][t]
                )
            if n_agree < strat.min_horizon_agree:
                continue

        signals[t] = proposed_side

    return signals


def backtest_session(
    session: SessionData,
    indicators: SessionIndicators,
    strat: StrategyConfig,
    market_cfg: MarketConfig,
    trade_id_offset: int = 0,
) -> List[TradeRecord]:
    """Run strictly causal single-session backtest for a strategy configuration."""
    n = len(session.close)
    vwap_arr, sigma_arr, z_arr = _resolve_signal_arrays(indicators, strat.signal_source)
    entry_signals = compute_entry_signals(session, indicators, strat)

    trades: List[TradeRecord] = []
    in_pos = False
    side = 0
    entry_idx = 0
    entry_price = 0.0
    entry_vwap = 0.0
    entry_sigma = 0.0
    entry_z = 0.0
    entry_cost_bps = 0.0
    entry_part = 0.0
    entry_vol_bps = 0.0
    cooldown_until = -1
    eod_bar = n - 2  # flatten by 15:58 ET

    for t in range(n):
        if not in_pos:
            if t >= cooldown_until and entry_signals[t] != 0 and t <= eod_bar - strat.min_hold:
                in_pos = True
                side = int(entry_signals[t])
                entry_idx = t
                entry_price = float(session.close[t])
                entry_vwap = float(vwap_arr[t])
                entry_sigma = max(float(sigma_arr[t]), SIGMA_FLOOR_FRAC * entry_price)
                entry_z = float(z_arr[t])
                entry_vol_bps = float(indicators.causal_bar_sigma[t]) * 1e4
                entry_part = market_cfg.order_size_shares / max(float(session.volume[t]), 1.0)
                entry_cost_bps = compute_side_cost_bps(
                    float(indicators.causal_bar_sigma[t]),
                    float(session.volume[t]),
                    market_cfg.order_size_shares,
                    market_cfg.spread_bps,
                    market_cfg.impact_coeff,
                )
        else:
            hold = t - entry_idx
            exit_reason = ""
            cur_px = float(session.close[t])
            adverse_move = -side * (cur_px - entry_price)

            eff_min_hold = strat.min_hold
            eff_max_hold = strat.max_hold

            if t >= eod_bar:
                exit_reason = "EOD_FLATTEN"
            elif hold >= eff_max_hold:
                exit_reason = "TIME_STOP"
            elif hold >= eff_min_hold:
                zt = float(z_arr[t])
                if adverse_move >= strat.stop_sigma * entry_sigma:
                    exit_reason = "STOP_LOSS"
                elif strat.use_anchor_exit:
                    # Exit once price captures target fraction of initial dislocation back to entry_vwap
                    init_gap = abs(entry_vwap - entry_price)
                    reverted = side * (cur_px - entry_price)
                    rev_frac = strat.anchor_reversion_frac
                    if strat.adaptive_gate:
                        # Optimal-stopping OU boundary (Leung & Li): early exits (5-9m)
                        # require proportionally stronger overshoot capture; 10-19m use equilibrium target
                        if hold <= 9:
                            rev_frac = strat.anchor_reversion_frac + 0.45 * (10 - hold)
                        else:
                            rev_frac = strat.anchor_reversion_frac
                    if reverted >= rev_frac * init_gap:
                        exit_reason = "VWAP_REVERT"
                else:
                    if side * zt >= -strat.exit_z:
                        exit_reason = "VWAP_REVERT"

            if exit_reason:
                exit_idx = t
                exit_price = cur_px
                exit_z = float(z_arr[t])
                exit_cost_bps = compute_side_cost_bps(
                    float(indicators.causal_bar_sigma[t]),
                    float(session.volume[t]),
                    market_cfg.order_size_shares,
                    market_cfg.spread_bps,
                    market_cfg.impact_coeff,
                )
                total_cost_bps = entry_cost_bps + exit_cost_bps
                gross_ret = side * (exit_price - entry_price) / entry_price
                gross_bps = gross_ret * 1e4
                net_bps = gross_bps - total_cost_bps
                net_ret = net_bps * 1e-4
                cost_ret = total_cost_bps * 1e-4

                # Unbiased passive VWAP benchmark over the next 12-minute execution window
                # (entry_idx, min(entry_idx + 12, n - 1)] — avoids optional-stopping bias
                bench_end = min(entry_idx + 12, n - 1)
                p_slice = indicators.typical_price[entry_idx + 1 : bench_end + 1]
                v_slice = session.volume[entry_idx + 1 : bench_end + 1]
                bench_vwap = float(np.dot(p_slice, v_slice) / np.sum(v_slice))
                slippage_saved_bps = (
                    side * (bench_vwap - entry_price) / entry_price * 1e4 - entry_cost_bps
                )

                trades.append(
                    TradeRecord(
                        session_id=session.session_id,
                        trade_id=trade_id_offset + len(trades) + 1,
                        side=side,
                        entry_idx=entry_idx,
                        exit_idx=exit_idx,
                        entry_time=session.times[entry_idx],
                        exit_time=session.times[exit_idx],
                        hold_min=hold,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        entry_vwap=entry_vwap,
                        entry_z=entry_z,
                        exit_z=exit_z,
                        gross_ret=gross_ret,
                        cost_ret=cost_ret,
                        net_ret=net_ret,
                        gross_bps=gross_bps,
                        cost_bps=total_cost_bps,
                        net_bps=net_bps,
                        slippage_saved_bps=slippage_saved_bps,
                        exit_reason=exit_reason,
                        entry_participation=entry_part,
                        entry_vol_bps=entry_vol_bps,
                    )
                )
                in_pos = False
                cooldown_until = t + strat.cooldown_bars

    return trades
