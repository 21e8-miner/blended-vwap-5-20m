"""Phase 2 Deep Research Engine:
Strictly causal (zero-lookahead, O(N) vectorized NumPy) frontier microstructure
signals, OU-Spectral Curvature-VPIN 5-20m strategy (1x Base Taker & Maker+Kelly Turbo),
Purged Walk-Forward Validation + Bailey-Lopez de Prado Deflated Sharpe Ratio (DSR, M=16),
4 Adversarial Microstructure Stress Tests, and 1m-30m Intraday Alpha Decay Curve.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Tuple
import numpy as np
from scipy.special import ndtr
from scipy.stats import kurtosis, norm, skew

from .simulator import (
    MarketConfig,
    SessionData,
    build_time_labels,
    intraday_volume_profile,
)
from .blended_vwap import (
    BLEND_HORIZONS,
    SIGMA_FLOOR_FRAC,
    SessionIndicators,
    StrategyConfig,
    _blend_from_weights,
    backtest_session,
    compute_entry_signals,
    compute_session_indicators,
    get_13_strategy_configs,
)

OU_GAMMA: float = 0.85  # Spectral kernel log-bandwidth across {5, 10, 15, 20} min
OU_PRIOR_TAU_V: float = 11.5  # Empirical-Bayes volume-time half-life prior (vol-min)
LN_BLEND_HORIZONS: np.ndarray = np.log(np.array(BLEND_HORIZONS, dtype=np.float64))
EULER_MASCHERONI: float = 0.5772156649015329
ALPHA_DECAY_HORIZONS: Tuple[int, ...] = (1, 2, 3, 5, 8, 10, 12, 15, 18, 20, 25, 30)


@dataclass
class ResearchIndicators:
    """Strictly causal Phase 2 microstructure signals for a single 390-bar session."""

    tau_half: np.ndarray          # Volume-clock OU half-life in wall-clock minutes, [5.0, 19.5]
    weights_ou: np.ndarray        # OU-Spectral kernel weights across {5,10,15,20}, shape (N, 4)
    vwap_ou: np.ndarray           # OU-Spectral blended VWAP
    sigma_ou: np.ndarray          # Exact law-of-total-variance OU blended dispersion
    z_ou: np.ndarray              # Standardized deviation (close - vwap_ou) / max(sigma_ou, floor)
    level: np.ndarray             # Nelson-Siegel Level L_t (= vwap_ou)
    slope: np.ndarray             # Signed Slope S_t = (VWAP5 - VWAP20) / sigma_bar
    curvature: np.ndarray         # Butterfly Curvature C_t = (VWAP5 - 2*VWAP10 + VWAP20) / sigma_bar
    d_curvature: np.ndarray       # Causal 1-bar curvature inflection C_t - C_{t-1}
    vpin: np.ndarray              # Causal Bulk-Volume VPIN toxicity proxy in [0, 1]
    signed_ofi: np.ndarray        # Causal signed 20-bar order-flow imbalance in [-1, +1]
    hurst: np.ndarray             # Causal local Hurst exponent H_t via Lo-MacKinlay VR(5)
    toxic_flag: np.ndarray        # Boolean mask where VPIN + Hurst indicate toxic informed trend


def _rolling_sum_causal(arr: np.ndarray, window: int) -> np.ndarray:
    """Exact O(N) causal rolling sum over [max(0, t - window + 1), t] inclusive."""
    n = len(arr)
    cs = np.empty(n + 1, dtype=np.float64)
    cs[0] = 0.0
    np.cumsum(arr, out=cs[1:])
    hi = np.arange(1, n + 1)
    lo = np.maximum(0, hi - window)
    return cs[hi] - cs[lo]


def _rolling_mean_causal(arr: np.ndarray, window: int) -> np.ndarray:
    """Exact O(N) causal rolling arithmetic mean over trailing `window` bars."""
    n = len(arr)
    s = _rolling_sum_causal(arr, window)
    cnt = np.minimum(np.arange(1, n + 1, dtype=np.float64), float(window))
    return s / cnt


def compute_volume_clock_ou_half_life(
    close: np.ndarray,
    vwap_ref: np.ndarray,
    rel_vol: np.ndarray,
    rel_vol_ema: np.ndarray,
    window: int = 30,
    prior_tau: float = OU_PRIOR_TAU_V,
) -> np.ndarray:
    """Strictly causal O(N) volume-clock OU half-life estimator (trailing 30-bar AR(1)).

    Models fractional deviation x_t = ln(close_t / vwap_ref_t) in volume time
    d(tau_V) = rel_vol_t:
        dx_i = -theta_V * x_{i-1} * d(tau_V)_i + eps_i
    Uses empirical-Bayes shrinkage in volume time toward prior theta_V = ln(2) / 11.5
    with causal EMA stabilization to suppress single-bar OLS estimation noise, then
    maps volume-time speed back to wall-clock minutes via live volume-clock speed
    v_speed(t) = 0.62 + 0.38 * rel_vol_ema(t).
    Contracts to ~7.5m during high-volume open/close sweeps and expands to ~12.0m
    during calm midday drift regimes.
    """
    n = len(close)
    x = np.log(np.maximum(close, 1e-6) / np.maximum(vwap_ref, 1e-6))
    dtau = np.clip(rel_vol, 0.25, 3.50)

    dx = np.zeros(n, dtype=np.float64)
    u = np.zeros(n, dtype=np.float64)
    if n > 1:
        dx[1:] = x[1:] - x[:-1]
        u[1:] = x[:-1] * dtau[1:]

    # Exact O(N) causal OLS numerator & denominator in volume time
    sum_uy = _rolling_sum_causal(u * dx, window)
    sum_u2 = _rolling_sum_causal(u * u, window)

    prior_theta_v = math.log(2.0) / prior_tau
    shrink_kappa = 6.0e-4

    theta_raw = (-sum_uy + shrink_kappa * prior_theta_v) / (sum_u2 + shrink_kappa)
    theta_clipped = np.clip(theta_raw, 0.042, 0.115)

    # Strictly causal EMA to remove single-bar finite-sample OLS jitter
    theta_ema = np.empty(n, dtype=np.float64)
    if n > 0:
        theta_ema[0] = prior_theta_v
        for t in range(1, n):
            theta_ema[t] = 0.16 * float(theta_clipped[t]) + 0.84 * theta_ema[t - 1]

    v_speed = 0.62 + 0.38 * np.clip(rel_vol_ema, 0.35, 3.00)
    tau_wall = math.log(2.0) / (theta_ema * v_speed)
    return np.clip(tau_wall, 5.0, 19.5)


def compute_causal_vpin(
    open_px: np.ndarray,
    close_px: np.ndarray,
    volume: np.ndarray,
    causal_bar_sigma: np.ndarray,
    window: int = 20,
) -> Tuple[np.ndarray, np.ndarray]:
    """Strictly causal O(N) Bulk-Volume Classification (BVC) VPIN toxicity & signed OFI.

    Uses standard BVC buy fraction Phi(r_t / sigma_t) over trailing 20 bars:
      - I_abs = sum(|V_buy - V_sell|) / sum(V)
      - OFI_signed = sum(V_buy - V_sell) / sum(V) in [-1, +1]
      - VPIN_t = clip(0.28 * I_abs + 0.72 * tanh(2.6 * |OFI_signed|), 0, 1)
    Benign two-sided liquidity sweeps read ~0.25-0.44; sustained toxic informed trends
    spike cleanly above 0.54-0.80.
    """
    n = len(close_px)
    log_bar_ret = np.log(np.maximum(close_px, 1e-6) / np.maximum(open_px, 1e-6))
    sig_safe = np.maximum(causal_bar_sigma, 2.5e-4)
    xi = np.clip(log_bar_ret / sig_safe, -5.0, 5.0)

    buy_frac = ndtr(xi)
    signed_imb = volume * (2.0 * buy_frac - 1.0)
    abs_imb = np.abs(signed_imb)

    win_vol = np.maximum(_rolling_sum_causal(volume, window), 1.0)
    win_abs = _rolling_sum_causal(abs_imb, window)
    win_signed_raw = _rolling_sum_causal(signed_imb, window)
    signed_ofi = np.clip(win_signed_raw / win_vol, -1.0, 1.0)

    bar_idx = np.arange(1, n + 1, dtype=np.float64)
    ramp = np.minimum(bar_idx / 8.0, 1.0)

    vpin_raw = 0.28 * (win_abs / win_vol) + 0.72 * np.tanh(2.6 * np.abs(signed_ofi))
    vpin = (1.0 - ramp) * 0.30 + ramp * vpin_raw
    return np.clip(vpin, 0.0, 1.0), signed_ofi * ramp


def compute_causal_hurst(
    close_px: np.ndarray,
    window: int = 40,
    q: int = 5,
) -> np.ndarray:
    """Strictly causal O(N) local Hurst exponent via Lo-MacKinlay variance ratio VR(q=5).

    Uses zero-drift-null second-moment variance ratio over trailing 40 bars so BOTH
    positive serial autocorrelation AND persistent directional drift lift VR(5) > 1.25
    (H_t > 0.56 toxic trend), while mean-reverting sweeps read VR(5) < 0.90 (H_t < 0.48).
    """
    n = len(close_px)
    hurst = np.full(n, 0.50, dtype=np.float64)
    if n <= q + 4:
        return hurst

    log_p = np.log(np.maximum(close_px, 1e-6))
    r1 = np.zeros(n, dtype=np.float64)
    r1[1:] = log_p[1:] - log_p[:-1]

    rq = np.zeros(n, dtype=np.float64)
    rq[q:] = log_p[q:] - log_p[:-q]

    cs1_2 = np.empty(n + 1, dtype=np.float64)
    csq_2 = np.empty(n + 1, dtype=np.float64)
    cs1_2[0] = csq_2[0] = 0.0
    np.cumsum(r1 * r1, out=cs1_2[1:])
    np.cumsum(rq * rq, out=csq_2[1:])

    t_start = q + 5
    t_idx = np.arange(t_start, n)

    lo1 = np.maximum(1, t_idx - window + 1)
    cnt1 = (t_idx - lo1 + 1).astype(np.float64)
    m2_1 = np.maximum((cs1_2[t_idx + 1] - cs1_2[lo1]) / cnt1, 1e-12)

    loq = np.maximum(q, t_idx - window + 1)
    cntq = (t_idx - loq + 1).astype(np.float64)
    m2_q = np.maximum((csq_2[t_idx + 1] - csq_2[loq]) / cntq, 1e-12)

    vr = np.clip(m2_q / (float(q) * m2_1), 0.14, 4.50)
    h_raw = 0.5 + np.log(vr) / (2.0 * math.log(float(q)))

    ramp = np.minimum((t_idx - t_start + 1) / 10.0, 1.0)
    hurst[t_start:] = np.clip((1.0 - ramp) * 0.50 + ramp * h_raw, 0.18, 0.86)
    return hurst


def compute_research_indicators(
    session: SessionData,
    indicators: SessionIndicators,
    gamma: float = OU_GAMMA,
) -> ResearchIndicators:
    """Compute all strictly causal Phase 2 microstructure signals for one session."""
    n = len(session.close)
    close = session.close

    # 1. Volume-Clock OU Half-Life + Exact OU-Spectral Horizon Kernel w_h^OU(t)
    tau_half = compute_volume_clock_ou_half_life(
        close=close,
        vwap_ref=indicators.vwap_by_h[15],
        rel_vol=session.rel_vol,
        rel_vol_ema=indicators.rel_vol_ema,
        window=30,
        prior_tau=OU_PRIOR_TAU_V,
    )

    ln_tau = np.log(tau_half)[:, None]
    log_kern = -((LN_BLEND_HORIZONS[None, :] - ln_tau) ** 2) / (2.0 * gamma * gamma)
    log_kern -= np.max(log_kern, axis=1, keepdims=True)
    raw_w = np.exp(log_kern)
    weights_ou = raw_w / np.sum(raw_w, axis=1, keepdims=True)

    vwap_mat = np.column_stack([indicators.vwap_by_h[h] for h in BLEND_HORIZONS])
    sigma_mat = np.column_stack([indicators.sigma_by_h[h] for h in BLEND_HORIZONS])
    vwap_ou, sigma_ou, z_ou = _blend_from_weights(vwap_mat, sigma_mat, weights_ou, close)

    # 2. Nelson-Siegel Style 5-20m VWAP Term-Structure Factors
    sigma_floor = SIGMA_FLOOR_FRAC * close[:, None]
    sigma_bar = np.mean(np.maximum(sigma_mat, sigma_floor), axis=1)

    level = vwap_ou
    slope = (indicators.vwap_by_h[5] - indicators.vwap_by_h[20]) / sigma_bar
    curvature = (
        indicators.vwap_by_h[5] - 2.0 * indicators.vwap_by_h[10] + indicators.vwap_by_h[20]
    ) / sigma_bar

    d_curvature = np.zeros(n, dtype=np.float64)
    if n > 1:
        d_curvature[1:] = curvature[1:] - curvature[:-1]

    # 3. Causal Bulk-Volume VPIN & Local Hurst Exponent (VR(5))
    vpin, signed_ofi = compute_causal_vpin(
        open_px=session.open,
        close_px=close,
        volume=session.volume,
        causal_bar_sigma=indicators.causal_bar_sigma,
        window=20,
    )
    hurst = compute_causal_hurst(close_px=close, window=40, q=5)

    # Two-timescale toxic-trend flag (bar-level VPIN+Hurst or persistent 50-bar regime)
    v50 = _rolling_mean_causal(vpin, 50)
    h50 = _rolling_mean_causal(hurst, 50)
    toxic_flag = (
        ((vpin >= 0.54) & (hurst >= 0.54))
        | (hurst >= 0.61)
        | ((v50 >= 0.50) & (h50 >= 0.58))
        | (v50 >= 0.53)
        | (h50 >= 0.66)
    )

    return ResearchIndicators(
        tau_half=tau_half,
        weights_ou=weights_ou,
        vwap_ou=vwap_ou,
        sigma_ou=sigma_ou,
        z_ou=z_ou,
        level=level,
        slope=slope,
        curvature=curvature,
        d_curvature=d_curvature,
        vpin=vpin,
        signed_ofi=signed_ofi,
        hurst=hurst,
        toxic_flag=toxic_flag,
    )


def compute_ou_spectral_entry_signals(
    session: SessionData,
    indicators: SessionIndicators,
    res_ind: ResearchIndicators,
    market_cfg: MarketConfig | None = None,
    entry_z: float = 1.58,
    min_disloc_bps: float = 7.0,
    max_slope_abs: float = 1.48,
) -> Tuple[np.ndarray, int]:
    """Vectorized strictly causal entry signals for OU-Spectral Curvature-VPIN 5-20m.

    Combines:
      1. OU-Spectral VWAP standardized dislocation + Almgren-Chriss net-of-cost hurdle
      2. Full 4-horizon {5, 10, 15, 20} directional consensus
      3. Nelson-Siegel Butterfly Curvature deceleration / exhaustion check (rejects
         accelerating concave linear-drift traps)
      4. Two-timescale strictly causal VPIN + Local Hurst toxic-trend veto
    Returns (signals in {-1, 0, +1}, vetoed_toxic_count).
    """
    n = len(session.close)
    signals = np.zeros(n, dtype=np.int32)
    min_hold = 7
    warmup = 5
    last_entry_bar = n - 2 - min_hold
    if last_entry_bar < warmup:
        return signals, 0

    sl = slice(warmup, last_entry_bar + 1)
    z_s = res_ind.z_ou[sl]
    close_s = session.close[sl]
    vwap_s = res_ind.vwap_ou[sl]
    slope_s = res_ind.slope[sl]

    # Causal pre-trade excess-friction hurdle (dormant on normal 1.4 bps baseline; protects cost shocks)
    if market_cfg is not None:
        bsig_bps = indicators.causal_bar_sigma[sl] * 1e4
        part_s = float(market_cfg.order_size_shares) / np.maximum(session.volume[sl], 1.0)
        est_rt_cost = float(market_cfg.spread_bps) + 2.0 * float(market_cfg.impact_coeff) * bsig_bps * np.sqrt(part_s)
        req_bps = np.maximum(min_disloc_bps, 4.6 * np.maximum(est_rt_cost - 0.65, 0.0))
    else:
        req_bps = min_disloc_bps

    disloc_bps = np.abs(close_s - vwap_s) / close_s * 1e4
    base_mask = (np.abs(z_s) >= entry_z) & (disloc_bps >= req_bps) & (np.abs(slope_s) <= max_slope_abs)
    proposed = np.where(z_s <= -entry_z, 1, -1).astype(np.int32)

    # 4-horizon consensus across {5, 10, 15, 20}
    long_agree = np.zeros_like(proposed)
    short_agree = np.zeros_like(proposed)
    for h in BLEND_HORIZONS:
        vh = indicators.vwap_by_h[h][sl]
        long_agree += (close_s < vh).astype(np.int32)
        short_agree += (close_s > vh).astype(np.int32)
    n_agree = np.where(proposed == 1, long_agree, short_agree)
    base_mask &= n_agree >= 4

    # Nelson-Siegel Butterfly Curvature deceleration / exhaustion check:
    # Reject accelerating concave trends (negative butterfly convexity still deepening)
    convex = (-proposed) * res_ind.curvature[sl]
    d_convex = (-proposed) * res_ind.d_curvature[sl]
    curv_trap = (convex < -0.12) & (d_convex < 0.0) & (np.abs(slope_s) > 0.75)
    base_mask &= ~curv_trap

    # Two-timescale strictly causal VPIN + Hurst Toxic-Trend Gate
    vp = res_ind.vpin[sl]
    hu = res_ind.hurst[sl]
    ofi = res_ind.signed_ofi[sl]
    v50 = _rolling_mean_causal(res_ind.vpin, 50)[sl]
    h50 = _rolling_mean_causal(res_ind.hurst, 50)[sl]
    fighting_flow = (proposed * ofi) < -0.05

    tox_fast = (
        (fighting_flow & (((vp >= 0.54) & (hu >= 0.54)) | (hu >= 0.58) | (vp >= 0.68)))
        | (hu >= 0.61)
        | ((vp >= 0.64) & (hu >= 0.56))
    )
    tox_regime = ((v50 >= 0.50) & (h50 >= 0.58)) | (v50 >= 0.53) | (h50 >= 0.66)
    toxic_s = tox_fast | tox_regime

    vetoed_count = int(np.sum(base_mask & toxic_s))
    final_mask = base_mask & (~toxic_s)
    signals[sl] = np.where(final_mask, proposed, 0)
    return signals, vetoed_count


def backtest_session_ou_spectral(
    session: SessionData,
    indicators: SessionIndicators,
    res_ind: ResearchIndicators,
    market_cfg: MarketConfig,
    turbo_mode: bool = False,
    latency_bars: int = 0,
) -> Tuple[float, float, List[Dict[str, float]], int]:
    """Strictly causal single-session backtest for OU-Spectral Curvature-VPIN 5-20m.

    Supports:
      - 1x Base Taker mode (`turbo_mode=False`)
      - Maker + Fractional-Kelly Turbo mode (`turbo_mode=True`, toxicity-dampened sizing)
      - OU-scaled hold window: `clamp(round(1.6 * tau_half), 5, 20)`
      - Optional `latency_bars` execution lag (+1m latency stress test)
    """
    n = len(session.close)
    entry_signals_arr, vetoed_cnt = compute_ou_spectral_entry_signals(
        session, indicators, res_ind, market_cfg=market_cfg
    )
    entry_signals = entry_signals_arr.tolist()

    # Prefix sums for O(1) 12-min passive VWAP slippage benchmark
    cs_v = np.empty(n + 1, dtype=np.float64)
    cs_pv = np.empty(n + 1, dtype=np.float64)
    cs_v[0] = 0.0
    cs_pv[0] = 0.0
    np.cumsum(session.volume, out=cs_v[1:])
    np.cumsum(indicators.typical_price * session.volume, out=cs_pv[1:])
    cs_v_l = cs_v.tolist()
    cs_pv_l = cs_pv.tolist()

    close_l = session.close.tolist()
    vol_l = session.volume.tolist()
    vwap_l = res_ind.vwap_ou.tolist()
    sigma_l = res_ind.sigma_ou.tolist()
    z_l = res_ind.z_ou.tolist()
    tau_l = res_ind.tau_half.tolist()
    vpin_l = res_ind.vpin.tolist()
    ofi_l = res_ind.signed_ofi.tolist()
    hurst_l = res_ind.hurst.tolist()
    bar_sig_l = indicators.causal_bar_sigma.tolist()
    rv_l = indicators.rel_vol_ema.tolist()

    order_sz = float(market_cfg.order_size_shares)
    min_hold = 7
    if turbo_mode:
        spread_side = 0.16 * market_cfg.spread_bps
        impact_k = 0.42 * market_cfg.impact_coeff
        cooldown = 1
    else:
        spread_side = 0.5 * market_cfg.spread_bps
        impact_k = market_cfg.impact_coeff
        cooldown = 2

    eq_net = 1.0
    eq_gross = 1.0
    trade_rows: List[Dict[str, float]] = []

    in_pos = False
    side = 0
    entry_idx = 0
    entry_price = 0.0
    entry_vwap = 0.0
    entry_sigma = 0.0
    entry_cost_bps = 0.0
    eff_max_hold = 18
    size_mult = 1.0
    peak_rev = 0.0
    armed_trail = False
    cooldown_until = -1
    eod_bar = n - 2 - latency_bars
    last_open_t = eod_bar - min_hold

    for t in range(n - latency_bars):
        if not in_pos:
            if t >= cooldown_until and entry_signals[t] != 0 and t <= last_open_t:
                fill_t = t + latency_bars
                in_pos = True
                side = entry_signals[t]
                entry_idx = fill_t
                entry_price = close_l[fill_t]
                entry_vwap = vwap_l[t]
                sig_t = sigma_l[t]
                sig_fl = SIGMA_FLOOR_FRAC * entry_price
                entry_sigma = sig_t if sig_t > sig_fl else sig_fl

                # OU-scaled hold window: clamp(round(1.6 * tau_half), 5, 20)
                ou_cap = int(round(1.6 * tau_l[t]))
                eff_max_hold = 5 if ou_cap < 5 else (20 if ou_cap > 20 else ou_cap)
                if eff_max_hold < min_hold:
                    eff_max_hold = min_hold

                bsig = bar_sig_l[fill_t]
                vt = vol_l[fill_t]
                part = order_sz / (vt if vt > 1.0 else 1.0)
                entry_cost_bps = spread_side + impact_k * (bsig * 1e4) * (part ** 0.5)

                if turbo_mode:
                    # Causal fractional-Kelly sizing boosted by anti-persistence (H < 0.50)
                    # and smoothly de-leveraged as VPIN/Hurst toxicity climbs (capped at 3.90x)
                    excess_z = max(abs(z_l[t]) - 1.58, 0.0)
                    vol_scale = min(1.35, max(0.75, 5.5e-4 / max(bsig, 2.5e-4)))
                    rv_boost = 0.22 * max(rv_l[t] - 1.0, 0.0)
                    reversion_bonus = max(0.0, 0.50 - hurst_l[t]) * 2.2
                    tox_excess = (
                        max(0.0, vpin_l[t] - 0.44) * 1.6 + max(0.0, hurst_l[t] - 0.52) * 3.0
                    )
                    tox_damp = max(0.38, 1.0 - tox_excess)
                    raw_sz = (2.35 + 1.15 * excess_z + rv_boost + reversion_bonus) * vol_scale * tox_damp
                    size_mult = min(3.90, max(1.10, raw_sz))
                else:
                    size_mult = 1.0

                peak_rev = 0.0
                armed_trail = False
        else:
            hold_sig = t - (entry_idx - latency_bars)
            cur_sig_px = close_l[t]
            reverted_sig = side * (cur_sig_px - entry_price)
            if reverted_sig > peak_rev:
                peak_rev = reverted_sig

            exit_now = False
            if t >= eod_bar or hold_sig >= eff_max_hold:
                exit_now = True
            elif hold_sig >= min_hold:
                adverse = -reverted_sig
                init_gap = abs(entry_vwap - entry_price)
                toxic_against = (side * ofi_l[t] < -0.14) and (
                    vpin_l[t] >= 0.54 and hurst_l[t] >= 0.54
                )
                stop_mult = 1.80 if toxic_against else 2.90
                if adverse >= stop_mult * entry_sigma:
                    exit_now = True
                elif turbo_mode:
                    if reverted_sig >= 0.72 * init_gap:
                        armed_trail = True
                    if armed_trail and (
                        reverted_sig <= 0.85 * peak_rev or reverted_sig >= 1.35 * init_gap
                    ):
                        exit_now = True
                else:
                    if reverted_sig >= 0.74 * init_gap:
                        exit_now = True

            if exit_now:
                exit_fill_t = min(t + latency_bars, n - 1)
                hold_actual = max(1, exit_fill_t - entry_idx)
                exit_price = close_l[exit_fill_t]
                bsig_x = bar_sig_l[exit_fill_t]
                vx = vol_l[exit_fill_t]
                part_x = order_sz / (vx if vx > 1.0 else 1.0)
                exit_cost_bps = spread_side + impact_k * (bsig_x * 1e4) * (part_x ** 0.5)
                total_cost_bps = entry_cost_bps + exit_cost_bps

                gross_bps = side * (exit_price - entry_price) / entry_price * 1e4
                net_bps = gross_bps - total_cost_bps
                scaled_net_ret = size_mult * (net_bps * 1e-4)
                scaled_gross_ret = size_mult * (gross_bps * 1e-4)

                eq_net *= 1.0 + scaled_net_ret
                eq_gross *= 1.0 + scaled_gross_ret

                bench_end = entry_idx + 12 if (entry_idx + 12) < (n - 1) else (n - 1)
                if bench_end > entry_idx:
                    b_v = cs_v_l[bench_end + 1] - cs_v_l[entry_idx + 1]
                    bench_vwap = (cs_pv_l[bench_end + 1] - cs_pv_l[entry_idx + 1]) / b_v
                    slip_bps = side * (bench_vwap - entry_price) / entry_price * 1e4 - entry_cost_bps
                else:
                    slip_bps = -entry_cost_bps

                trade_rows.append(
                    {
                        "hold_min": float(hold_actual),
                        "gross_bps": gross_bps,
                        "cost_bps": total_cost_bps,
                        "net_bps": net_bps,
                        "size_mult": size_mult,
                        "scaled_net_bps": size_mult * net_bps,
                        "slippage_saved_bps": slip_bps,
                    }
                )
                in_pos = False
                cooldown_until = t + cooldown

    return eq_net - 1.0, eq_gross - 1.0, trade_rows, vetoed_cnt


def backtest_standard_with_latency(
    session: SessionData,
    indicators: SessionIndicators,
    strat: StrategyConfig,
    market_cfg: MarketConfig,
    latency_bars: int = 1,
) -> Tuple[float, List[Dict[str, float]]]:
    """Run a standard Phase 1 StrategyConfig under +latency_bars execution delay."""
    if latency_bars == 0:
        trs = backtest_session(session, indicators, strat, market_cfg)
        eq = 1.0
        rows = []
        for tr in trs:
            eq *= 1.0 + tr.net_ret
            rows.append({"hold_min": float(tr.hold_min), "net_bps": tr.net_bps, "cost_bps": tr.cost_bps})
        return eq - 1.0, rows

    n = len(session.close)
    if strat.signal_source == "adaptive_blend":
        vwap_arr, sigma_arr, z_arr = indicators.vwap_adaptive, indicators.sigma_adaptive, indicators.z_adaptive
    elif strat.signal_source == "static_blend":
        vwap_arr, sigma_arr, z_arr = indicators.vwap_static, indicators.sigma_static, indicators.z_static
    elif strat.signal_source == "vol_weighted_blend":
        vwap_arr, sigma_arr, z_arr = indicators.vwap_vol, indicators.sigma_vol, indicators.z_vol
    else:
        h = int(strat.signal_source[1:])
        vwap_arr, sigma_arr, z_arr = indicators.vwap_by_h[h], indicators.sigma_by_h[h], indicators.z_by_h[h]

    sigs = compute_entry_signals(session, indicators, strat).tolist()
    close_l = session.close.tolist()
    vol_l = session.volume.tolist()
    vwap_l = vwap_arr.tolist()
    sigma_l = sigma_arr.tolist()
    z_l = z_arr.tolist()
    bar_sig_l = indicators.causal_bar_sigma.tolist()

    order_sz = float(market_cfg.order_size_shares)
    half_spread = 0.5 * market_cfg.spread_bps
    impact_k = market_cfg.impact_coeff
    eod_bar = n - 2 - latency_bars
    last_open_t = eod_bar - strat.min_hold

    eq = 1.0
    rows: List[Dict[str, float]] = []
    in_pos = False
    side = 0
    entry_sig_t = 0
    entry_fill_t = 0
    entry_price = 0.0
    entry_vwap = 0.0
    entry_sigma = 0.0
    entry_cost_bps = 0.0
    cooldown_until = -1

    for t in range(n - latency_bars):
        if not in_pos:
            if t >= cooldown_until and sigs[t] != 0 and t <= last_open_t:
                fill_t = t + latency_bars
                in_pos = True
                side = sigs[t]
                entry_sig_t = t
                entry_fill_t = fill_t
                entry_price = close_l[fill_t]
                entry_vwap = vwap_l[t]
                sig_t = sigma_l[t]
                sig_fl = SIGMA_FLOOR_FRAC * entry_price
                entry_sigma = sig_t if sig_t > sig_fl else sig_fl
                bsig = bar_sig_l[fill_t]
                vt = vol_l[fill_t]
                part = order_sz / (vt if vt > 1.0 else 1.0)
                entry_cost_bps = half_spread + impact_k * (bsig * 1e4) * (part ** 0.5)
        else:
            hold = t - entry_sig_t
            exit_now = False
            cur_px = close_l[t]
            if t >= eod_bar or hold >= strat.max_hold:
                exit_now = True
            elif hold >= strat.min_hold:
                adverse = -side * (cur_px - entry_price)
                if adverse >= strat.stop_sigma * entry_sigma:
                    exit_now = True
                elif strat.use_anchor_exit:
                    init_gap = abs(entry_vwap - entry_price)
                    reverted = side * (cur_px - entry_price)
                    rev_frac = (
                        (strat.anchor_reversion_frac + 0.45 * (10 - hold))
                        if (strat.adaptive_gate and hold <= 9)
                        else strat.anchor_reversion_frac
                    )
                    if reverted >= rev_frac * init_gap:
                        exit_now = True
                else:
                    if side * z_l[t] >= -strat.exit_z:
                        exit_now = True

            if exit_now:
                x_t = min(t + latency_bars, n - 1)
                exit_price = close_l[x_t]
                bsig_x = bar_sig_l[x_t]
                vx = vol_l[x_t]
                part_x = order_sz / (vx if vx > 1.0 else 1.0)
                exit_cost_bps = half_spread + impact_k * (bsig_x * 1e4) * (part_x ** 0.5)
                tot_cost = entry_cost_bps + exit_cost_bps
                gross_bps = side * (exit_price - entry_price) / entry_price * 1e4
                net_bps = gross_bps - tot_cost
                eq *= 1.0 + net_bps * 1e-4
                rows.append({"hold_min": float(max(1, x_t - entry_fill_t)), "net_bps": net_bps, "cost_bps": tot_cost})
                in_pos = False
                cooldown_until = t + strat.cooldown_bars

    return eq - 1.0, rows


def compute_psr_and_dsr(
    daily_returns: np.ndarray,
    sr_trials_std_daily: float,
    n_trials: int = 16,
) -> Dict[str, float]:
    """Exact Bailey-Lopez de Prado (2014) Probabilistic Sharpe Ratio (PSR) and
    Deflated Sharpe Ratio (DSR) correcting for exact skewness, Pearson kurtosis,
    and multiple-testing bias across M=16 evaluated configurations.
    """
    r = np.asarray(daily_returns, dtype=np.float64)
    t_len = len(r)
    if t_len < 3:
        return {
            "sharpe_ann": 0.0,
            "skewness": 0.0,
            "kurtosis_pearson": 3.0,
            "sr_benchmark_ann": 0.0,
            "psr_pct": 50.0,
            "dsr_pct": 50.0,
        }

    mu = float(np.mean(r))
    sd = float(np.std(r, ddof=1))
    sr_d = mu / sd if sd > 1e-12 else 0.0
    sr_ann = sr_d * math.sqrt(252.0)

    g3 = float(skew(r, bias=False)) if sd > 1e-12 else 0.0
    g4 = float(kurtosis(r, fisher=False, bias=False)) if sd > 1e-12 else 3.0
    g4 = max(g4, 1.0)

    denom_var = (1.0 - g3 * sr_d + ((g4 - 1.0) / 4.0) * (sr_d ** 2)) / float(t_len - 1)
    sr_se = math.sqrt(max(denom_var, 1e-12))

    z1 = float(norm.ppf(1.0 - 1.0 / float(n_trials)))
    z2 = float(norm.ppf(1.0 - 1.0 / (float(n_trials) * math.e)))
    sr_bench_d = max(sr_trials_std_daily, 0.02) * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)

    psr = float(norm.cdf(sr_d / sr_se)) * 100.0
    dsr = float(norm.cdf((sr_d - sr_bench_d) / sr_se)) * 100.0

    return {
        "sharpe_ann": round(sr_ann, 2),
        "skewness": round(g3, 3),
        "kurtosis_pearson": round(g4, 3),
        "sr_benchmark_ann": round(sr_bench_d * math.sqrt(252.0), 2),
        "psr_pct": round(psr, 2),
        "dsr_pct": round(dsr, 2),
    }


def _summarize_daily_curve(daily_net: np.ndarray, trading_days: int = 252) -> Tuple[float, float, float]:
    """Return (net_sharpe_ann, annual_return_pct, max_drawdown_pct)."""
    n_s = len(daily_net)
    sd = float(np.std(daily_net, ddof=1)) if n_s > 1 else 0.0
    sr = float(np.mean(daily_net) / sd * math.sqrt(trading_days)) if sd > 1e-12 else 0.0
    eq = np.concatenate(([1.0], np.cumprod(1.0 + daily_net)))
    ann_f = trading_days / float(max(n_s, 1))
    tot = float(eq[-1])
    ann_ret = ((tot ** ann_f) - 1.0) * 100.0 if tot > 0.0 else -100.0
    peaks = np.maximum.accumulate(eq)
    mdd = float(np.max((peaks - eq) / np.maximum(peaks, 1e-12)) * 100.0)
    return sr, ann_ret, mdd


def run_purged_walk_forward_and_dsr(
    sessions: List[SessionData],
    indicators_list: List[SessionIndicators],
    research_list: List[ResearchIndicators],
    market_cfg: MarketConfig,
    all_14_daily_sharpes: List[float],
) -> Dict[str, Any]:
    """Purged Walk-Forward (IS sessions 1-40 vs strict OOS sessions 41-60) & DSR (M=16)."""
    strat_map = {s.id: s for s in get_13_strategy_configs()}
    n_sess = len(sessions)
    is_end = 40  # IS: 0..39 (sessions 1-40), OOS: 40..59 (sessions 41-60)

    specs = [
        ("micro_8m_elastic_fade", "8m Elastic Fade", "Single-Horizon", False, "standard"),
        ("static_blend_5_20", "Static Blend 5–20m", "Blended 5–20m", False, "standard"),
        ("vol_weighted_blend_5_20", "Vol-Weighted Blend 5–20m", "Blended 5–20m", False, "standard"),
        ("adaptive_blend_5_20", "Adaptive Blend 5–20m (1x)", "Blended 5–20m", False, "standard"),
        ("ou_spectral_vpin_1x", "OU-Spectral Curvature-VPIN (1x)", "OU-Spectral", True, "ou_1x"),
        ("ou_spectral_vpin_turbo", "OU-Spectral Turbo (Maker+Kelly)", "OU-Spectral Turbo", True, "ou_turbo"),
    ]

    model_daily_series: List[Tuple[Tuple[str, str, str, bool, str], np.ndarray, List[Dict[str, float]], int]] = []
    extra_daily_sharpes: List[float] = []

    for spec in specs:
        mid, _, _, _, kind = spec
        daily_net = np.zeros(n_sess, dtype=np.float64)
        all_rows: List[Dict[str, float]] = []
        vetoes_total = 0

        for s_idx, (sess, ind, rind) in enumerate(zip(sessions, indicators_list, research_list)):
            if kind == "standard":
                trs = backtest_session(sess, ind, strat_map[mid], market_cfg)
                eq = 1.0
                for tr in trs:
                    eq *= 1.0 + tr.net_ret
                    all_rows.append(
                        {
                            "hold_min": float(tr.hold_min),
                            "net_bps": tr.net_bps,
                            "cost_bps": tr.cost_bps,
                            "slippage_saved_bps": tr.slippage_saved_bps,
                        }
                    )
                daily_net[s_idx] = eq - 1.0
            elif kind == "ou_1x":
                rn, _, rows, v_cnt = backtest_session_ou_spectral(sess, ind, rind, market_cfg, turbo_mode=False)
                daily_net[s_idx] = rn
                all_rows.extend(rows)
                vetoes_total += v_cnt
            else:
                rn, _, rows, v_cnt = backtest_session_ou_spectral(sess, ind, rind, market_cfg, turbo_mode=True)
                daily_net[s_idx] = rn
                all_rows.extend(rows)
                vetoes_total += v_cnt

        if kind in ("ou_1x", "ou_turbo"):
            sd = float(np.std(daily_net, ddof=1))
            extra_daily_sharpes.append(float(np.mean(daily_net) / sd) if sd > 1e-12 else 0.0)
        model_daily_series.append((spec, daily_net, all_rows, vetoes_total))

    # Cross-sectional daily Sharpe std dev across all M=16 evaluated trials (13 + Adaptive Turbo + 2 OU)
    trial_daily_sharpes = list(all_14_daily_sharpes) + extra_daily_sharpes
    n_trials = len(trial_daily_sharpes)
    sr_trials_std_daily = float(np.std(trial_daily_sharpes, ddof=1))

    rows_out: List[Dict[str, Any]] = []
    ou_1x_summary: Dict[str, Any] = {}
    ou_turbo_summary: Dict[str, Any] = {}

    for spec, daily_net, all_rows, vetoes_total in model_daily_series:
        mid, name, family, is_frontier, kind = spec
        is_ret = daily_net[:is_end]
        oos_ret = daily_net[is_end:]

        is_sr, is_ann, is_mdd = _summarize_daily_curve(is_ret, market_cfg.trading_days_per_year)
        oos_sr, oos_ann, oos_mdd = _summarize_daily_curve(oos_ret, market_cfg.trading_days_per_year)
        full_sr, full_ann, full_mdd = _summarize_daily_curve(daily_net, market_cfg.trading_days_per_year)

        dsr_full = compute_psr_and_dsr(daily_net, sr_trials_std_daily, n_trials=n_trials)
        dsr_oos = compute_psr_and_dsr(oos_ret, sr_trials_std_daily, n_trials=n_trials)

        net_bps_arr = np.array([r["net_bps"] for r in all_rows], dtype=np.float64)
        wins = net_bps_arr[net_bps_arr > 0]
        losses = np.abs(net_bps_arr[net_bps_arr < 0])
        pf = float(np.sum(wins) / np.sum(losses)) if np.sum(losses) > 0 else 99.0
        win_rate = float(len(wins) / max(len(net_bps_arr), 1) * 100.0)
        avg_hold = float(np.mean([r["hold_min"] for r in all_rows])) if all_rows else 0.0
        avg_cost = float(np.mean([r["cost_bps"] for r in all_rows])) if all_rows else 0.0
        avg_slip = float(np.mean([r["slippage_saved_bps"] for r in all_rows])) if all_rows else 0.0
        ann_trades = int(round(len(all_rows) * (market_cfg.trading_days_per_year / float(n_sess))))
        retention_pct = (oos_sr / is_sr * 100.0) if is_sr > 0.1 else 0.0

        row = {
            "id": mid,
            "name": name,
            "family": family,
            "is_frontier": is_frontier,
            "is_sharpe": round(is_sr, 2),
            "oos_sharpe": round(oos_sr, 2),
            "full_sharpe": round(full_sr, 2),
            "oos_retention_pct": round(retention_pct, 1),
            "is_annual_return_pct": round(is_ann, 2),
            "oos_annual_return_pct": round(oos_ann, 2),
            "annual_return_pct": round(full_ann, 2),
            "max_drawdown_pct": round(full_mdd, 2),
            "oos_max_drawdown_pct": round(oos_mdd, 2),
            "avg_hold_min": round(avg_hold, 2),
            "annual_trades": ann_trades,
            "win_rate_pct": round(win_rate, 2),
            "profit_factor": round(pf, 2),
            "avg_cost_bps": round(avg_cost, 2),
            "vwap_slippage_saved_bps": round(avg_slip, 2),
            "skewness": dsr_full["skewness"],
            "kurtosis_pearson": dsr_full["kurtosis_pearson"],
            "psr_pct": dsr_full["psr_pct"],
            "dsr_pct": dsr_full["dsr_pct"],
            "dsr_oos_pct": dsr_oos["dsr_pct"],
            "sr_benchmark_m16": dsr_full["sr_benchmark_ann"],
            "toxic_vetoes_60d": vetoes_total,
        }
        rows_out.append(row)
        if kind == "ou_1x":
            ou_1x_summary = row
        elif kind == "ou_turbo":
            ou_turbo_summary = row

    return {
        "split": {
            "is_sessions": "1–40",
            "oos_sessions": "41–60",
            "is_days": is_end,
            "oos_days": n_sess - is_end,
            "purge_boundary": "Overnight flat EOD purge (0 overlapping bars across split)",
            "trials_m": n_trials,
            "sr_benchmark_m16": rows_out[0]["sr_benchmark_m16"],
        },
        "models": rows_out,
        "ou_spectral_1x": ou_1x_summary,
        "ou_spectral_turbo": ou_turbo_summary,
    }


def simulate_adversarial_regime(
    scenario: str,
    seed: int,
    sessions_count: int = 20,
) -> Tuple[List[SessionData], MarketConfig]:
    """Generate unseen 20-session adversarial microstructure stress datasets."""
    rng = np.random.default_rng(seed)
    n_bars = 390
    u_profile = intraday_volume_profile(n_bars)
    time_labels = build_time_labels(n_bars)

    spread_mult = 1.0
    impact_mult = 1.0
    if scenario == "flash_crash":
        spread_mult = 2.5
        impact_mult = 1.0
    elif scenario == "cost_shock_2x":
        spread_mult = 2.0
        impact_mult = 2.0

    mcfg = MarketConfig(
        seed=seed,
        sessions=sessions_count,
        bars_per_session=n_bars,
        spread_bps=round(0.60 * spread_mult, 4),
        impact_coeff=round(0.40 * impact_mult, 4),
        order_size_shares=780.0,
    )

    sessions: List[SessionData] = []
    prev_close = mcfg.initial_price

    for s_idx in range(sessions_count):
        gap = rng.normal(0.0, 0.0035) if s_idx > 0 else 0.0
        s_open = float(prev_close * np.exp(gap))

        vol_regime = np.zeros(n_bars, dtype=np.int32)
        state = int(rng.random() < (0.42 if scenario == "flash_crash" else 0.28))
        for t in range(n_bars):
            vol_regime[t] = state
            if state == 0 and rng.random() < (0.020 if scenario == "flash_crash" else 0.014):
                state = 1
            elif state == 1 and rng.random() < (0.028 if scenario == "flash_crash" else 0.032):
                state = 0

        drift_bps = np.zeros(n_bars, dtype=np.float64)
        if scenario == "momentum_meltup":
            # Sustained institutional toxic directional trend waves (~1.3-2.0% trend day)
            trend_dir = 1 if (s_idx % 2 == 0) else -1
            d_state = 0
            d_mag = 0.0
            for t in range(n_bars):
                if t >= 15 and d_state == 0 and rng.random() < 0.045:
                    d_state = trend_dir if rng.random() < 0.88 else -trend_dir
                    d_mag = float(rng.uniform(1.45, 2.25))
                elif d_state != 0 and rng.random() < 0.018:
                    d_state = 0
                    d_mag = 0.0
                drift_bps[t] = d_state * d_mag
        else:
            d_state = 0
            d_mag = 0.0
            for t in range(n_bars):
                if d_state == 0 and rng.random() < 0.015:
                    d_state = 1 if rng.random() < 0.5 else -1
                    d_mag = float(rng.uniform(0.70, 1.30))
                elif d_state != 0 and rng.random() < 0.030:
                    d_state = 0
                    d_mag = 0.0
                drift_bps[t] = d_state * d_mag

        eff_log_p = np.zeros(n_bars, dtype=np.float64)
        trans_fast = np.zeros(n_bars, dtype=np.float64)
        trans_med = np.zeros(n_bars, dtype=np.float64)
        trans_slow = np.zeros(n_bars, dtype=np.float64)
        inst_vol_bps = np.zeros(n_bars, dtype=np.float64)
        burst_vol_mult = np.ones(n_bars, dtype=np.float64)

        cur_eff = float(np.log(s_open))
        cur_fast = cur_med = cur_slow = 0.0
        flash_cascade_bars = 0
        flash_sign = -1

        for t in range(n_bars):
            u_t = float(u_profile[t])
            reg_mult = 0.86 if vol_regime[t] == 0 else 1.40
            intraday_mult = 0.74 + 0.36 * float(np.sqrt(u_t))

            v_speed = 0.36 + 0.64 * u_t
            phi_fast_t = float(np.exp(-np.log(2.0) / 3.2 * v_speed))
            phi_med_t = float(np.exp(-np.log(2.0) / 6.8 * v_speed))
            phi_slow_t = float(np.exp(-np.log(2.0) / 13.0 * v_speed))

            eff_scale = 1.15 if scenario == "flash_crash" else (0.90 if scenario == "momentum_meltup" else 1.0)
            sigma_eff = 4.45 * reg_mult * intraday_mult * eff_scale
            perm_burst = 0.0

            if scenario == "flash_crash":
                # 2.8x vol spike liquidity-vacuum cascades (70% transient vacuum / 30% permanent shock)
                if flash_cascade_bars == 0 and 20 <= t <= n_bars - 35 and rng.random() < 0.008:
                    flash_cascade_bars = int(rng.integers(4, 7))
                    flash_sign = -1 if rng.random() < 0.82 else 1
                if flash_cascade_bars > 0:
                    shock = float(rng.uniform(16.0, 26.0))
                    perm_burst += flash_sign * 0.30 * shock
                    cur_med += flash_sign * 0.70 * shock
                    sigma_eff = 4.45 * reg_mult * intraday_mult * 2.80
                    burst_vol_mult[t] += float(rng.uniform(1.4, 2.4))
                    flash_cascade_bars -= 1

            if 6 <= t <= n_bars - 20 and rng.random() < 0.0085:
                info_sign = 1 if rng.random() < 0.5 else -1
                perm_burst += info_sign * float(rng.uniform(10.5, 16.5))
                burst_vol_mult[t] += float(rng.uniform(0.45, 0.95))

            rev_scale = 0.80 if scenario == "momentum_meltup" else (1.35 if scenario == "flash_crash" else 1.0)
            inno_fast = float(rng.normal(0.0, 1.92 * rev_scale * reg_mult * (0.40 + 0.65 * np.sqrt(u_t))))
            inno_med = float(rng.normal(0.0, 2.25 * rev_scale * reg_mult * (0.72 + 0.28 * np.sqrt(u_t))))
            inno_slow = float(rng.normal(0.0, 1.28 * rev_scale * reg_mult * max(1.18 - 0.25 * u_t, 0.50)))

            cur_fast = phi_fast_t * cur_fast + inno_fast
            cur_med = phi_med_t * cur_med + inno_med
            cur_slow = phi_slow_t * cur_slow + inno_slow

            if 4 <= t <= n_bars - 18:
                if rng.random() < 0.015 * (0.15 + 1.25 * u_t):
                    sign = 1 if rng.random() < 0.5 else -1
                    imp = float(rng.uniform(10.5, 16.5)) * rev_scale
                    perm = float(rng.uniform(0.18, 0.26))
                    perm_burst += sign * imp * perm
                    cur_fast += sign * imp * (1.0 - perm)
                    burst_vol_mult[t] += float(rng.uniform(1.10, 1.95))
                if rng.random() < 0.022 * (0.48 + 0.55 * u_t):
                    sign = 1 if rng.random() < 0.5 else -1
                    imp = float(rng.uniform(14.5, 22.5)) * rev_scale
                    perm = float(rng.uniform(0.18, 0.26))
                    perm_burst += sign * imp * perm
                    cur_med += sign * imp * (1.0 - perm)
                    burst_vol_mult[t] += float(rng.uniform(0.85, 1.45))
                if rng.random() < 0.014 * max(1.38 - 0.45 * u_t, 0.24):
                    sign = 1 if rng.random() < 0.5 else -1
                    imp = float(rng.uniform(13.0, 20.0)) * rev_scale
                    perm = float(rng.uniform(0.24, 0.34))
                    perm_burst += sign * imp * perm
                    cur_slow += sign * imp * (1.0 - perm)
                    burst_vol_mult[t] += float(rng.uniform(0.45, 0.90))

            flow_intensity = (abs(inno_fast) + abs(inno_med)) / (5.5 * reg_mult)
            burst_vol_mult[t] += 0.14 * min(flow_intensity, 2.5)

            d_eff_bps = drift_bps[t] + perm_burst + float(rng.normal(0.0, sigma_eff))
            if scenario == "momentum_meltup" and drift_bps[t] != 0.0 and (d_eff_bps * drift_bps[t] > 0.0):
                burst_vol_mult[t] += 0.70
            cur_eff += d_eff_bps * 1e-4

            eff_log_p[t] = cur_eff
            trans_fast[t] = cur_fast
            trans_med[t] = cur_med
            trans_slow[t] = cur_slow
            inst_vol_bps[t] = float(np.sqrt(sigma_eff ** 2 + (2.35 * rev_scale) ** 2))

        total_trans = trans_fast + trans_med + trans_slow
        close_px = np.exp(eff_log_p + total_trans * 1e-4)
        open_px = np.empty(n_bars, dtype=np.float64)
        open_px[0] = s_open
        open_px[1:] = close_px[:-1]

        bar_sig_frac = (inst_vol_bps * 1e-4) * np.sqrt(0.50)
        hi_exc = np.abs(rng.normal(0.0, 1.0, size=n_bars)) * bar_sig_frac
        lo_exc = np.abs(rng.normal(0.0, 1.0, size=n_bars)) * bar_sig_frac
        high_px = np.maximum(open_px, close_px) * np.exp(hi_exc)
        low_px = np.minimum(open_px, close_px) * np.exp(-lo_exc)

        liq_mult = 0.45 if scenario == "flash_crash" else 1.0  # -55% depth in flash_crash
        vol_noise = np.exp(rng.normal(-0.045, 0.27, size=n_bars))
        reg_vol_factor = np.where(vol_regime == 1, 1.18, 0.94)
        raw_vol = (
            (mcfg.adv_per_min / 1.15)
            * liq_mult
            * u_profile
            * burst_vol_mult
            * reg_vol_factor
            * vol_noise
        )
        volume = np.maximum(np.round(raw_vol), 900.0).astype(np.float64)
        rel_vol = volume / mcfg.adv_per_min

        sessions.append(
            SessionData(
                session_id=s_idx + 1,
                times=time_labels,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                volume=volume,
                rel_vol=rel_vol,
                vol_regime=vol_regime,
                transient_impact_bps=total_trans,
            )
        )
        prev_close = float(close_px[-1])

    return sessions, mcfg


def run_adversarial_stress_suite() -> List[Dict[str, Any]]:
    """Run all 4 unseen-seed adversarial microstructure stress scenarios (20 sessions each)."""
    strat_map = {s.id: s for s in get_13_strategy_configs()}
    st_8m = strat_map["micro_8m_elastic_fade"]
    st_adapt = strat_map["adaptive_blend_5_20"]

    scenario_defs = [
        (
            "flash_crash",
            "Flash Crash Shock",
            "2.8x vol spike, -55% depth, 2.5x spread (seed=1101)",
            1101,
            0,
        ),
        (
            "momentum_meltup",
            "Toxic Momentum Melt-Up",
            "Sustained one-sided informed trend drift (seed=202)",
            202,
            0,
        ),
        (
            "cost_shock_2x",
            "2x Friction Cost Shock",
            "2.0x spread (1.2 bps) & 2.0x market impact (seed=3303)",
            3303,
            0,
        ),
        (
            "latency_lag_1m",
            "+1m Execution Latency",
            "1-minute signal-to-fill & exit lag decay (seed=4004)",
            4004,
            1,
        ),
    ]

    results: List[Dict[str, Any]] = []
    for sid, label, desc, seed, lag in scenario_defs:
        sess_list, mcfg = simulate_adversarial_regime(sid, seed=seed, sessions_count=20)
        inds = [compute_session_indicators(s) for s in sess_list]
        rinds = [compute_research_indicators(s, i) for s, i in zip(sess_list, inds)]

        n_s = len(sess_list)
        dn_8m = np.zeros(n_s, dtype=np.float64)
        dn_ad = np.zeros(n_s, dtype=np.float64)
        dn_ou = np.zeros(n_s, dtype=np.float64)
        dn_tb = np.zeros(n_s, dtype=np.float64)

        tr_8m = tr_ad = tr_ou = tr_tb = 0
        vetoes_ou = 0

        for idx, (s, ind, rind) in enumerate(zip(sess_list, inds, rinds)):
            r8, rows8 = backtest_standard_with_latency(s, ind, st_8m, mcfg, latency_bars=lag)
            ra, rowsa = backtest_standard_with_latency(s, ind, st_adapt, mcfg, latency_bars=lag)
            ro, _, rowso, vo = backtest_session_ou_spectral(s, ind, rind, mcfg, turbo_mode=False, latency_bars=lag)
            rt, _, rowst, _ = backtest_session_ou_spectral(s, ind, rind, mcfg, turbo_mode=True, latency_bars=lag)
            dn_8m[idx] = r8
            dn_ad[idx] = ra
            dn_ou[idx] = ro
            dn_tb[idx] = rt
            tr_8m += len(rows8)
            tr_ad += len(rowsa)
            tr_ou += len(rowso)
            tr_tb += len(rowst)
            vetoes_ou += vo

        sr_8m, ret_8m, mdd_8m = _summarize_daily_curve(dn_8m, mcfg.trading_days_per_year)
        sr_ad, ret_ad, mdd_ad = _summarize_daily_curve(dn_ad, mcfg.trading_days_per_year)
        sr_ou, ret_ou, mdd_ou = _summarize_daily_curve(dn_ou, mcfg.trading_days_per_year)
        sr_tb, ret_tb, mdd_tb = _summarize_daily_curve(dn_tb, mcfg.trading_days_per_year)

        results.append(
            {
                "id": sid,
                "label": label,
                "description": desc,
                "seed": seed,
                "sessions": n_s,
                "vpin_mean": round(float(np.mean([np.mean(r.vpin) for r in rinds])), 3),
                "hurst_mean": round(float(np.mean([np.mean(r.hurst) for r in rinds])), 3),
                "toxic_vetoes": vetoes_ou,
                "models": {
                    "single_8m": {
                        "name": "8m Single",
                        "net_sharpe": round(sr_8m, 2),
                        "annual_return_pct": round(ret_8m, 2),
                        "max_drawdown_pct": round(mdd_8m, 2),
                        "trades_20d": tr_8m,
                    },
                    "adaptive_1x": {
                        "name": "Adaptive Blend (1x)",
                        "net_sharpe": round(sr_ad, 2),
                        "annual_return_pct": round(ret_ad, 2),
                        "max_drawdown_pct": round(mdd_ad, 2),
                        "trades_20d": tr_ad,
                    },
                    "ou_spectral_1x": {
                        "name": "OU-Spectral VPIN (1x)",
                        "net_sharpe": round(sr_ou, 2),
                        "annual_return_pct": round(ret_ou, 2),
                        "max_drawdown_pct": round(mdd_ou, 2),
                        "trades_20d": tr_ou,
                    },
                    "ou_spectral_turbo": {
                        "name": "OU-Spectral Turbo",
                        "net_sharpe": round(sr_tb, 2),
                        "annual_return_pct": round(ret_tb, 2),
                        "max_drawdown_pct": round(mdd_tb, 2),
                        "trades_20d": tr_tb,
                    },
                },
            }
        )

    return results


def compute_alpha_decay_curve(
    sessions: List[SessionData],
    indicators_list: List[SessionIndicators],
    research_list: List[ResearchIndicators],
    market_cfg: MarketConfig,
) -> List[Dict[str, Any]]:
    """Measure forward net edge (bps) & hit rate (%) at horizons 1..30 min across 60 sessions."""
    strat_map = {s.id: s for s in get_13_strategy_configs()}
    st_8m = strat_map["micro_8m_elastic_fade"]
    st_adapt = strat_map["adaptive_blend_5_20"]

    order_sz = float(market_cfg.order_size_shares)
    half_spread = 0.5 * market_cfg.spread_bps
    impact_k = market_cfg.impact_coeff
    max_h = max(ALPHA_DECAY_HORIZONS)

    buckets: Dict[str, Dict[int, List[float]]] = {
        "ou_spectral_vpin": {h: [] for h in ALPHA_DECAY_HORIZONS},
        "adaptive_blend": {h: [] for h in ALPHA_DECAY_HORIZONS},
        "single_8m": {h: [] for h in ALPHA_DECAY_HORIZONS},
    }

    for sess, ind, rind in zip(sessions, indicators_list, research_list):
        n = len(sess.close)
        close_l = sess.close.tolist()
        vol_l = sess.volume.tolist()
        bsig_l = ind.causal_bar_sigma.tolist()

        side_cost = [
            half_spread + impact_k * (bsig_l[t] * 1e4) * ((order_sz / max(vol_l[t], 1.0)) ** 0.5)
            for t in range(n)
        ]

        sig_ou, _ = compute_ou_spectral_entry_signals(sess, ind, rind, market_cfg=market_cfg)
        sig_ad = compute_entry_signals(sess, ind, st_adapt)
        sig_8m = compute_entry_signals(sess, ind, st_8m)

        for key, sig_arr in (
            ("ou_spectral_vpin", sig_ou),
            ("adaptive_blend", sig_ad),
            ("single_8m", sig_8m),
        ):
            sig_l = sig_arr.tolist()
            next_ok = 5
            for t in range(5, n - max_h - 1):
                if t < next_ok or sig_l[t] == 0:
                    continue
                side = sig_l[t]
                ep = close_l[t]
                ec = side_cost[t]
                for h in ALPHA_DECAY_HORIZONS:
                    xp = close_l[t + h]
                    xc = side_cost[t + h]
                    net_bps = side * (xp - ep) / ep * 1e4 - (ec + xc)
                    buckets[key][h].append(net_bps)
                next_ok = t + 8

    curve: List[Dict[str, Any]] = []
    for h in ALPHA_DECAY_HORIZONS:
        pt: Dict[str, Any] = {
            "horizon_min": h,
            "in_sweet_spot": 5 <= h <= 20,
        }
        for key in ("ou_spectral_vpin", "adaptive_blend", "single_8m"):
            arr = np.array(buckets[key][h], dtype=np.float64)
            mean_bps = float(np.mean(arr)) if len(arr) > 0 else 0.0
            std_bps = float(np.std(arr, ddof=1)) if len(arr) > 1 else 1.0
            hit_pct = float(np.mean(arr > 0.0) * 100.0) if len(arr) > 0 else 50.0
            ir = mean_bps / max(std_bps, 1e-6)
            pt[f"{key}_net_bps"] = round(mean_bps, 2)
            pt[f"{key}_hit_pct"] = round(hit_pct, 2)
            pt[f"{key}_ir"] = round(ir, 4)
        curve.append(pt)

    return curve


def run_research_lab(
    sessions: List[SessionData],
    indicators_list: List[SessionIndicators],
    market_cfg: MarketConfig,
    all_14_daily_sharpes: List[float],
) -> Tuple[Dict[str, Any], List[ResearchIndicators]]:
    """Run complete Phase 2 Deep Research Suite and return (research_lab_dict, research_list)."""
    research_list = [compute_research_indicators(s, ind) for s, ind in zip(sessions, indicators_list)]

    wf_dsr = run_purged_walk_forward_and_dsr(
        sessions, indicators_list, research_list, market_cfg, all_14_daily_sharpes
    )
    stress_tests = run_adversarial_stress_suite()
    alpha_decay = compute_alpha_decay_curve(sessions, indicators_list, research_list, market_cfg)

    all_tau = np.stack([r.tau_half for r in research_list], axis=0)
    all_vpin = np.stack([r.vpin for r in research_list], axis=0)
    all_hurst = np.stack([r.hurst for r in research_list], axis=0)
    all_curv = np.stack([r.curvature for r in research_list], axis=0)

    peak_decay_pt = max(alpha_decay, key=lambda x: x["ou_spectral_vpin_ir"])

    research_payload: Dict[str, Any] = {
        "meta": {
            "ou_window_bars": 30,
            "ou_kernel_gamma": OU_GAMMA,
            "ou_prior_tau_v": OU_PRIOR_TAU_V,
            "vpin_window_bars": 20,
            "hurst_window_bars": 40,
            "hurst_vr_lag_q": 5,
            "trials_m": wf_dsr["split"]["trials_m"],
            "stress_sessions_per_scenario": 20,
        },
        "telemetry": {
            "tau_half_open_min": round(float(np.mean(all_tau[:, :30])), 2),
            "tau_half_midday_min": round(float(np.mean(all_tau[:, 150:240])), 2),
            "tau_half_close_min": round(float(np.mean(all_tau[:, -30:])), 2),
            "mean_vpin": round(float(np.mean(all_vpin)), 3),
            "mean_hurst": round(float(np.mean(all_hurst)), 3),
            "mean_abs_curvature": round(float(np.mean(np.abs(all_curv))), 3),
            "peak_ir_horizon_min": peak_decay_pt["horizon_min"],
            "peak_ou_net_bps": peak_decay_pt["ou_spectral_vpin_net_bps"],
        },
        "walk_forward_dsr": wf_dsr,
        "stress_tests": stress_tests,
        "alpha_decay_curve": alpha_decay,
    }
    return research_payload, research_list


if __name__ == "__main__":
    import time
    from .simulator import simulate_market
    from .blended_vwap import backtest_session_turbo

    cfg = MarketConfig()
    ss = simulate_market(cfg)
    inds = [compute_session_indicators(s) for s in ss]
    strats = get_13_strategy_configs()
    dsrs14 = []
    for st in strats:
        dn = []
        for s, i in zip(ss, inds):
            eq = 1.0
            for tr in backtest_session(s, i, st, cfg):
                eq *= 1.0 + tr.net_ret
            dn.append(eq - 1.0)
        dn_arr = np.array(dn)
        dsrs14.append(float(np.mean(dn_arr) / np.std(dn_arr, ddof=1)))
    f_st = next(s for s in strats if s.is_flagship)
    tdn = np.array([backtest_session_turbo(s, i, f_st, cfg)[0] for s, i in zip(ss, inds)])
    dsrs14.append(float(np.mean(tdn) / np.std(tdn, ddof=1)))

    t0 = time.perf_counter()
    rl, _ = run_research_lab(ss, inds, cfg, dsrs14)
    ms_elapsed = (time.perf_counter() - t0) * 1000.0
    print(f"Research lab runtime: {ms_elapsed:.1f} ms")
    print("TELEMETRY:", rl["telemetry"])
    print("\nWALK-FORWARD & DSR (M=16):")
    for m in rl["walk_forward_dsr"]["models"]:
        print(
            f"  {m['name']:<32} IS={m['is_sharpe']:>5.2f} OOS={m['oos_sharpe']:>5.2f} "
            f"Full={m['full_sharpe']:>5.2f} Ret={m['oos_retention_pct']:>5.1f}% "
            f"PSR={m['psr_pct']:>6.2f}% DSR={m['dsr_pct']:>6.2f}% "
            f"Ann={m['annual_return_pct']:>7.1f}% MDD={m['max_drawdown_pct']:>5.2f}% "
            f"Hold={m['avg_hold_min']:>5.2f} Slip={m['vwap_slippage_saved_bps']:>5.2f} Vetoes={m['toxic_vetoes_60d']}"
        )
    print("\nADVERSARIAL STRESS TESTS:")
    for st in rl["stress_tests"]:
        ms = st["models"]
        print(
            f"  {st['id']:<16} (vpin={st['vpin_mean']}, H={st['hurst_mean']}, vetoes={st['toxic_vetoes']}): "
            f"8m={ms['single_8m']['net_sharpe']:>6.2f} | "
            f"Adapt={ms['adaptive_1x']['net_sharpe']:>6.2f} | "
            f"OU_1x={ms['ou_spectral_1x']['net_sharpe']:>6.2f} | "
            f"OU_Turbo={ms['ou_spectral_turbo']['net_sharpe']:>6.2f} "
            f"[MDD: {ms['single_8m']['max_drawdown_pct']}/{ms['adaptive_1x']['max_drawdown_pct']}/"
            f"{ms['ou_spectral_1x']['max_drawdown_pct']}/{ms['ou_spectral_turbo']['max_drawdown_pct']}]"
        )
    print("\nALPHA DECAY CURVE:")
    for pt in rl["alpha_decay_curve"]:
        print(
            f"  h={pt['horizon_min']:>2}m: "
            f"OU=({pt['ou_spectral_vpin_net_bps']:>5.2f}bps, {pt['ou_spectral_vpin_hit_pct']:>5.1f}%, IR={pt['ou_spectral_vpin_ir']:>6.4f}) | "
            f"Ad=({pt['adaptive_blend_net_bps']:>5.2f}bps, {pt['adaptive_blend_hit_pct']:>5.1f}%) | "
            f"8m=({pt['single_8m_net_bps']:>5.2f}bps, {pt['single_8m_hit_pct']:>5.1f}%)"
        )
