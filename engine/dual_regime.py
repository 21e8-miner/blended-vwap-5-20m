"""Phase 3 Frontier Research Engine:
Strictly causal 2-State Hamilton Online Bayesian Regime Filter (Fade / Flat / Surf),
Dual-Regime OU-Bayes 5-20m strategy (1x Base Taker & Maker+Kelly Turbo),
5-Regime All-Weather Adversarial Stress Suite (adding Whipsaw Flip),
Toxic Melt-Up Bifurcation Trajectory, 1,000-Trial Stationary Block-Bootstrap 95% CIs & CVaR 95%,
Walk-Forward & DSR (M=20), and 8-Asset Intraday Portfolio Engine.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Tuple
import numpy as np

from .simulator import (
    MarketConfig,
    SessionData,
    build_time_labels,
    intraday_volume_profile,
)
from .blended_vwap import (
    SIGMA_FLOOR_FRAC,
    SessionIndicators,
    StrategyConfig,
    backtest_session,
    compute_entry_signals,
    compute_session_indicators,
    get_13_strategy_configs,
)
from .research_lab import (
    ResearchIndicators,
    _rolling_mean_causal,
    _summarize_daily_curve,
    backtest_session_ou_spectral,
    backtest_standard_with_latency,
    compute_ou_spectral_entry_signals,
    compute_psr_and_dsr,
    compute_research_indicators,
    simulate_adversarial_regime,
)

# 2-State Hamilton Online Bayesian Regime Filter hyperparameters
HAMILTON_P00: float = 0.975  # P(Reverting_t | Reverting_{t-1})
HAMILTON_P11: float = 0.945  # P(Trend_t | Trend_{t-1})
HAMILTON_TEMP: float = 0.28  # Tempered likelihood scale for autocorrelated rolling windows
HAMILTON_BIAS: float = -1.65  # Rare toxic-trend log-odds prior
FADE_THRESHOLD: float = 0.38  # p_trend < 0.38 -> Fade Mode
SURF_THRESHOLD: float = 0.62  # p_trend > 0.62 -> Surf Mode (0.38..0.62 = Flat Veto Zone)

# 8-Asset Intraday Portfolio universe matching Interactive Workbench Live Radar
ASSET_SPECS: Tuple[Dict[str, Any], ...] = (
    {"symbol": "ES", "asset_class": "Equity Index Futures", "init_px": 5420.0, "beta": 1.00, "vol_mult": 0.96, "spread_bps": 0.45, "impact_coeff": 0.32, "adv_mult": 2.40},
    {"symbol": "SPY", "asset_class": "US Large-Cap ETF", "init_px": 542.0, "beta": 1.00, "vol_mult": 0.98, "spread_bps": 0.48, "impact_coeff": 0.34, "adv_mult": 2.10},
    {"symbol": "QQQ", "asset_class": "Tech Index ETF", "init_px": 465.0, "beta": 1.15, "vol_mult": 1.10, "spread_bps": 0.55, "impact_coeff": 0.38, "adv_mult": 1.65},
    {"symbol": "NVDA", "asset_class": "Semiconductors", "init_px": 132.0, "beta": 1.38, "vol_mult": 1.45, "spread_bps": 0.72, "impact_coeff": 0.44, "adv_mult": 1.40},
    {"symbol": "AAPL", "asset_class": "Mega-Cap Tech", "init_px": 226.0, "beta": 1.04, "vol_mult": 1.04, "spread_bps": 0.58, "impact_coeff": 0.39, "adv_mult": 1.35},
    {"symbol": "TSLA", "asset_class": "High-Beta Auto/AI", "init_px": 258.0, "beta": 1.45, "vol_mult": 1.62, "spread_bps": 0.85, "impact_coeff": 0.46, "adv_mult": 1.30},
    {"symbol": "MSFT", "asset_class": "Enterprise Software", "init_px": 428.0, "beta": 1.02, "vol_mult": 1.00, "spread_bps": 0.56, "impact_coeff": 0.38, "adv_mult": 1.25},
    {"symbol": "BTC-USD", "asset_class": "Digital Asset Spot", "init_px": 68500.0, "beta": 0.32, "vol_mult": 1.72, "spread_bps": 1.15, "impact_coeff": 0.48, "adv_mult": 1.15},
)


@dataclass
class HamiltonRegimeState:
    """Strictly causal bar-by-bar 2-state Hamilton Bayesian posterior & fan telemetry."""

    p_trend: np.ndarray        # Posterior P(S_t = Trend | F_t) in [0.005, 0.995]
    fan_score: np.ndarray      # 4-horizon monotonic VWAP fan alignment in [-1, +1]
    fan_persist: np.ndarray    # Causal 22-bar |mean(fan_score)| persistence
    llr: np.ndarray            # Tempered log-likelihood ratio ln[f(y_t|S=1)/f(y_t|S=0)]
    regime_modes: List[str]    # "FADE" (<0.38), "FLAT" ([0.38,0.62]), "SURF" (>0.62)


def compute_hamilton_regime_filter(
    session: SessionData,
    indicators: SessionIndicators,
    res_ind: ResearchIndicators,
    p00: float = HAMILTON_P00,
    p11: float = HAMILTON_P11,
    temp: float = HAMILTON_TEMP,
    bias: float = HAMILTON_BIAS,
) -> HamiltonRegimeState:
    """Strictly causal 2-State Hamilton Online Bayesian Regime Filter (Equation 5).

    State 0 = Reverting Liquidity Sweep, State 1 = Informed Momentum Trend.
    At every bar t, updates posterior p_t^trend = P(S_t = 1 | F_t) using ONLY
    observables <= t: causal VPIN (20m & 35m), causal Hurst VR(5) (40m & 35m),
    |Slope|, 4-horizon monotonic VWAP fan alignment persistence, and aligned OFI.
    """
    n = len(session.close)
    c = session.close
    v5 = indicators.vwap_by_h[5]
    v10 = indicators.vwap_by_h[10]
    v15 = indicators.vwap_by_h[15]
    v20 = indicators.vwap_by_h[20]

    # Exact 4-horizon monotonic VWAP fan alignment:
    # +1.0 iff P > VWAP5 > VWAP10 > VWAP15 > VWAP20; -1.0 iff exact bearish reverse
    s1 = np.sign(c - v5)
    s2 = np.sign(v5 - v10)
    s3 = np.sign(v10 - v15)
    s4 = np.sign(v15 - v20)
    fan_score = 0.25 * (s1 + s2 + s3 + s4)
    fan_persist = np.abs(_rolling_mean_causal(fan_score, 22))

    vp = res_ind.vpin
    hu = res_ind.hurst
    vp_35 = _rolling_mean_causal(vp, 35)
    hu_35 = _rolling_mean_causal(hu, 35)
    slp = np.abs(res_ind.slope)
    ofi_align = np.sign(v5 - v20) * res_ind.signed_ofi

    def _gauss_llr(y: np.ndarray, mu0: float, s0: float, mu1: float, s1_: float) -> np.ndarray:
        return math.log(s0 / s1_) - 0.5 * ((y - mu1) / s1_) ** 2 + 0.5 * ((y - mu0) / s0) ** 2

    llr_vp = _gauss_llr(vp, 0.34, 0.11, 0.58, 0.12)
    llr_vp35 = _gauss_llr(vp_35, 0.34, 0.06, 0.50, 0.08)
    llr_hu = _gauss_llr(hu, 0.46, 0.08, 0.60, 0.08)
    llr_hu35 = _gauss_llr(hu_35, 0.46, 0.055, 0.56, 0.07)
    llr_sl = _gauss_llr(slp, 0.58, 0.45, 1.45, 0.55)
    llr_fn = _gauss_llr(fan_persist, 0.30, 0.22, 0.82, 0.20)
    llr_of = _gauss_llr(ofi_align, 0.04, 0.12, 0.28, 0.13)

    llr_tot = temp * (
        0.65 * llr_vp
        + 0.90 * llr_vp35
        + 0.65 * llr_hu
        + 0.85 * llr_hu35
        + 0.45 * llr_sl
        + 0.95 * llr_fn
        + 0.65 * llr_of
    ) + bias
    llr_tot = np.clip(llr_tot, -6.5, 6.5)

    p_trend = np.empty(n, dtype=np.float64)
    regime_modes: List[str] = []
    p_cur = 0.08
    p01 = 1.0 - p00
    llr_l = llr_tot.tolist()

    for t in range(n):
        p_prior = p_cur * p11 + (1.0 - p_cur) * p01
        if t < 12:
            p_cur = 0.08
        else:
            ev = math.exp(llr_l[t])
            num = p_prior * ev
            p_cur = num / (num + (1.0 - p_prior))
            if p_cur < 0.005:
                p_cur = 0.005
            elif p_cur > 0.995:
                p_cur = 0.995
        p_trend[t] = p_cur
        if p_cur < FADE_THRESHOLD:
            regime_modes.append("FADE")
        elif p_cur > SURF_THRESHOLD:
            regime_modes.append("SURF")
        else:
            regime_modes.append("FLAT")

    return HamiltonRegimeState(
        p_trend=p_trend,
        fan_score=fan_score,
        fan_persist=fan_persist,
        llr=llr_tot,
        regime_modes=regime_modes,
    )


def backtest_session_dual_regime(
    session: SessionData,
    indicators: SessionIndicators,
    res_ind: ResearchIndicators,
    ham: HamiltonRegimeState,
    market_cfg: MarketConfig,
    turbo_mode: bool = False,
    latency_bars: int = 0,
    mode_override: str = "dual",  # "dual" | "fade_only" | "surf_only"
    return_equity_curve: bool = False,
) -> Tuple[float, float, List[Dict[str, Any]], np.ndarray | None]:
    """Strictly causal single-session backtest for Dual-Regime OU-Bayes 5-20m (Equation 6).

    - p_trend < 0.38 (Fade Mode): OU-Spectral Curvature mean-reversion scaled by (1 - p_trend)
    - 0.38 <= p_trend <= 0.62 (Flat Veto Zone): zero new entries (prevents transition whipsaw)
    - p_trend > 0.62 (Surf Mode): follows aligned 5-20m VWAP fan with trailing 10m VWAP cross
      stop, regime-collapse stop, hard volatility stop, and 20m max hold cap.
    """
    n = len(session.close)
    fade_sigs_arr, _ = compute_ou_spectral_entry_signals(
        session, indicators, res_ind, market_cfg=market_cfg
    )
    fade_sigs = fade_sigs_arr.tolist()

    # O(1) prefix sums for 12-min passive VWAP slippage benchmark
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
    v5_l = indicators.vwap_by_h[5].tolist()
    v10_l = indicators.vwap_by_h[10].tolist()
    v20_l = indicators.vwap_by_h[20].tolist()
    vwap_ou_l = res_ind.vwap_ou.tolist()
    sig_ou_l = res_ind.sigma_ou.tolist()
    z_ou_l = res_ind.z_ou.tolist()
    tau_l = res_ind.tau_half.tolist()
    slope_l = res_ind.slope.tolist()
    curv_l = res_ind.curvature.tolist()
    vpin_l = res_ind.vpin.tolist()
    v35_l = _rolling_mean_causal(res_ind.vpin, 35).tolist()
    h35_l = _rolling_mean_causal(res_ind.hurst, 35).tolist()
    ofi_l = res_ind.signed_ofi.tolist()
    hurst_l = res_ind.hurst.tolist()
    pt_l = ham.p_trend.tolist()
    fan_l = ham.fan_score.tolist()
    bsig_l = indicators.causal_bar_sigma.tolist()
    rv_l = indicators.rel_vol_ema.tolist()

    osz = float(market_cfg.order_size_shares)
    fade_sp = (0.16 if turbo_mode else 0.5) * market_cfg.spread_bps
    fade_ik = (0.42 if turbo_mode else 1.0) * market_cfg.impact_coeff
    surf_sp = (0.28 if turbo_mode else 0.5) * market_cfg.spread_bps
    surf_ik = (0.55 if turbo_mode else 1.0) * market_cfg.impact_coeff
    cd_bars = 1 if turbo_mode else 2

    allow_fade = mode_override in ("dual", "fade_only")
    allow_surf = mode_override in ("dual", "surf_only")

    eq_net = 1.0
    eq_gross = 1.0
    eq_curve = np.ones(n, dtype=np.float64) if return_equity_curve else None
    trade_rows: List[Dict[str, Any]] = []

    in_pos = False
    mode = ""
    side = 0
    ei = 0
    ep = ev = es = ec = 0.0
    eff_max = 18
    sz = 1.0
    pk = 0.0
    armed = False
    cd = -1
    eod = n - 2 - latency_bars

    for t in range(n - latency_bars):
        if not in_pos:
            if t >= cd and t <= eod - 5:
                pt = pt_l[t]
                if allow_fade and pt < FADE_THRESHOLD and fade_sigs[t] != 0 and t <= eod - 7:
                    ft = t + latency_bars
                    in_pos = True
                    mode = "FADE"
                    side = fade_sigs[t]
                    ei = ft
                    ep = close_l[ft]
                    ev = vwap_ou_l[t]
                    st = sig_ou_l[t]
                    sf = SIGMA_FLOOR_FRAC * ep
                    es = st if st > sf else sf
                    oc = int(round(1.6 * tau_l[t]))
                    eff_max = 7 if oc < 7 else (20 if oc > 20 else oc)
                    bs = bsig_l[ft]
                    vt = vol_l[ft]
                    ec = fade_sp + fade_ik * (bs * 1e4) * ((osz / (vt if vt > 1.0 else 1.0)) ** 0.5)
                    if turbo_mode:
                        ez = max(abs(z_ou_l[t]) - 1.58, 0.0)
                        vs = min(1.35, max(0.75, 5.5e-4 / max(bs, 2.5e-4)))
                        rvb = 0.22 * max(rv_l[t] - 1.0, 0.0)
                        revb = max(0.0, 0.50 - hurst_l[t]) * 2.2
                        tox_excess = (
                            max(0.0, vpin_l[t] - 0.44) * 1.6 + max(0.0, hurst_l[t] - 0.52) * 3.0
                        )
                        td = max(0.38, 1.0 - tox_excess)
                        bayes_conf = max(0.72, 1.0 - 0.35 * pt)
                        sz = min(3.90, max(1.10, (2.35 + 1.15 * ez + rvb + revb) * vs * td * bayes_conf))
                    else:
                        sz = min(1.0, max(0.70, 1.0 - 0.45 * pt))
                    pk = 0.0
                    armed = False
                elif allow_surf and pt > SURF_THRESHOLD and t <= eod - 5:
                    f_sc = fan_l[t]
                    s_dir = 1 if v5_l[t] > v20_l[t] else -1
                    bs_bps = bsig_l[t] * 1e4
                    if (
                        s_dir * f_sc >= 0.99
                        and s_dir * ofi_l[t] >= 0.18
                        and v35_l[t] >= 0.46
                        and h35_l[t] >= 0.52
                        and abs(slope_l[t]) >= 0.72
                        and (s_dir * curv_l[t]) <= 0.35
                        and bs_bps <= 10.5
                        and abs(z_ou_l[t]) <= 2.55
                        and s_dir * (close_l[t] - v10_l[t]) > 0.0
                    ):
                        ft = t + latency_bars
                        in_pos = True
                        mode = "SURF"
                        side = s_dir
                        ei = ft
                        ep = close_l[ft]
                        ev = v10_l[t]
                        st = sig_ou_l[t]
                        sf = SIGMA_FLOOR_FRAC * ep
                        es = st if st > sf else sf
                        eff_max = 20
                        bs = bsig_l[ft]
                        vt = vol_l[ft]
                        ec = surf_sp + surf_ik * (bs * 1e4) * ((osz / (vt if vt > 1.0 else 1.0)) ** 0.5)
                        if turbo_mode:
                            vs = min(1.30, max(0.70, 5.5e-4 / max(bs, 2.5e-4)))
                            conf = (pt - SURF_THRESHOLD) / (1.0 - SURF_THRESHOLD)
                            sz = min(3.60, max(1.40, (1.90 + 1.60 * conf) * vs))
                        else:
                            sz = min(1.0, max(0.70, 0.55 + 0.50 * pt))
                        pk = 0.0
                        armed = False
        else:
            hs = t - (ei - latency_bars)
            cp = close_l[t]
            rev = side * (cp - ep)
            if rev > pk:
                pk = rev
            ex = False
            if mode == "FADE":
                if t >= eod or hs >= eff_max:
                    ex = True
                elif hs >= 7:
                    adv = -rev
                    ig = abs(ev - ep)
                    tox_ag = (side * ofi_l[t] < -0.14) and (
                        (vpin_l[t] >= 0.54 and hurst_l[t] >= 0.54) or pt_l[t] > SURF_THRESHOLD
                    )
                    sm = 1.80 if tox_ag else 2.90
                    if adv >= sm * es:
                        ex = True
                    elif turbo_mode:
                        if rev >= 0.72 * ig:
                            armed = True
                        if armed and (rev <= 0.85 * pk or rev >= 1.35 * ig):
                            ex = True
                    else:
                        if rev >= 0.74 * ig:
                            ex = True
            else:
                # SURF EXIT: causal trailing 10m VWAP cross stop, regime flip, hard stop, or 20m cap
                if t >= eod or hs >= eff_max:
                    ex = True
                elif side * (cp - v10_l[t]) < 0.0 and hs >= 2:
                    ex = True
                elif pt_l[t] < FADE_THRESHOLD and hs >= 2:
                    ex = True
                elif -rev >= 2.20 * es:
                    ex = True

            if ex:
                xt = min(t + latency_bars, n - 1)
                hold_actual = max(1, xt - ei)
                xpx = close_l[xt]
                bsx = bsig_l[xt]
                vx = vol_l[xt]
                sp_x = surf_sp if mode == "SURF" else fade_sp
                ik_x = surf_ik if mode == "SURF" else fade_ik
                xc = sp_x + ik_x * (bsx * 1e4) * ((osz / (vx if vx > 1.0 else 1.0)) ** 0.5)
                tot_cost = ec + xc
                gross_bps = side * (xpx - ep) / ep * 1e4
                net_bps = gross_bps - tot_cost
                eq_net *= 1.0 + sz * net_bps * 1e-4
                eq_gross *= 1.0 + sz * gross_bps * 1e-4

                bench_end = ei + 12 if (ei + 12) < (n - 1) else (n - 1)
                if bench_end > ei:
                    b_v = cs_v_l[bench_end + 1] - cs_v_l[ei + 1]
                    bench_vwap = (cs_pv_l[bench_end + 1] - cs_pv_l[ei + 1]) / b_v
                    slip_bps = side * (bench_vwap - ep) / ep * 1e4 - ec
                else:
                    slip_bps = -ec

                trade_rows.append(
                    {
                        "mode": mode,
                        "side": side,
                        "entry_idx": ei,
                        "exit_idx": xt,
                        "hold_min": float(hold_actual),
                        "gross_bps": gross_bps,
                        "cost_bps": tot_cost,
                        "net_bps": net_bps,
                        "size_mult": sz,
                        "scaled_net_bps": sz * net_bps,
                        "slippage_saved_bps": slip_bps,
                    }
                )
                in_pos = False
                cd = t + (1 if mode == "SURF" else cd_bars)

        if eq_curve is not None:
            if in_pos and t >= ei:
                mtm_bps = side * (close_l[t] - ep) / ep * 1e4 - ec
                eq_curve[t] = eq_net * (1.0 + sz * mtm_bps * 1e-4)
            else:
                eq_curve[t] = eq_net

    if eq_curve is not None and latency_bars > 0:
        eq_curve[n - latency_bars :] = eq_net

    return eq_net - 1.0, eq_gross - 1.0, trade_rows, eq_curve


def _mark_to_market_standard_curve(
    session: SessionData,
    indicators: SessionIndicators,
    strat: StrategyConfig,
    market_cfg: MarketConfig,
) -> np.ndarray:
    """Exact bar-by-bar mark-to-market equity curve for a standard Phase-1 strategy."""
    n = len(session.close)
    trades = backtest_session(session, indicators, strat, market_cfg)
    close_l = session.close.tolist()
    eq_curve = np.ones(n, dtype=np.float64)
    eq = 1.0
    t_ptr = 0
    for tr in trades:
        while t_ptr < tr.entry_idx:
            eq_curve[t_ptr] = eq
            t_ptr += 1
        entry_cost = 0.5 * tr.cost_bps
        for k in range(tr.entry_idx, tr.exit_idx):
            mtm_bps = tr.side * (close_l[k] - tr.entry_price) / tr.entry_price * 1e4 - entry_cost
            eq_curve[k] = eq * (1.0 + mtm_bps * 1e-4)
        eq *= 1.0 + tr.net_ret
        eq_curve[tr.exit_idx] = eq
        t_ptr = tr.exit_idx + 1
    while t_ptr < n:
        eq_curve[t_ptr] = eq
        t_ptr += 1
    return eq_curve


def _mark_to_market_ou_curve(
    session: SessionData,
    indicators: SessionIndicators,
    res_ind: ResearchIndicators,
    market_cfg: MarketConfig,
) -> np.ndarray:
    """Exact bar-by-bar mark-to-market equity curve for Phase-2 OU-VPIN Fade-Only (1x)."""
    n = len(session.close)
    entry_signals = compute_ou_spectral_entry_signals(
        session, indicators, res_ind, market_cfg=market_cfg
    )[0].tolist()
    close_l = session.close.tolist()
    vol_l = session.volume.tolist()
    vwap_l = res_ind.vwap_ou.tolist()
    sig_l = res_ind.sigma_ou.tolist()
    tau_l = res_ind.tau_half.tolist()
    vpin_l = res_ind.vpin.tolist()
    ofi_l = res_ind.signed_ofi.tolist()
    hurst_l = res_ind.hurst.tolist()
    bsig_l = indicators.causal_bar_sigma.tolist()

    osz = float(market_cfg.order_size_shares)
    sp = 0.5 * market_cfg.spread_bps
    ik = market_cfg.impact_coeff
    eq = 1.0
    eq_curve = np.ones(n, dtype=np.float64)
    in_pos = False
    side = 0
    ei = 0
    ep = ev = es = ec = 0.0
    eff_max = 18
    cd = -1
    eod = n - 2

    for t in range(n):
        if not in_pos:
            if t >= cd and entry_signals[t] != 0 and t <= eod - 7:
                in_pos = True
                side = entry_signals[t]
                ei = t
                ep = close_l[t]
                ev = vwap_l[t]
                st = sig_l[t]
                sf = SIGMA_FLOOR_FRAC * ep
                es = st if st > sf else sf
                oc = int(round(1.6 * tau_l[t]))
                eff_max = 7 if oc < 7 else (20 if oc > 20 else oc)
                ec = sp + ik * (bsig_l[t] * 1e4) * ((osz / max(vol_l[t], 1.0)) ** 0.5)
        else:
            hs = t - ei
            cp = close_l[t]
            rev = side * (cp - ep)
            ex = False
            if t >= eod or hs >= eff_max:
                ex = True
            elif hs >= 7:
                adv = -rev
                ig = abs(ev - ep)
                tox_ag = (side * ofi_l[t] < -0.14) and (vpin_l[t] >= 0.54 and hurst_l[t] >= 0.54)
                if adv >= (1.80 if tox_ag else 2.90) * es or rev >= 0.74 * ig:
                    ex = True
            if ex:
                xc = sp + ik * (bsig_l[t] * 1e4) * ((osz / max(vol_l[t], 1.0)) ** 0.5)
                nb = side * (cp - ep) / ep * 1e4 - (ec + xc)
                eq *= 1.0 + nb * 1e-4
                in_pos = False
                cd = t + 2
        if in_pos:
            mtm_bps = side * (close_l[t] - ep) / ep * 1e4 - ec
            eq_curve[t] = eq * (1.0 + mtm_bps * 1e-4)
        else:
            eq_curve[t] = eq
    return eq_curve


def simulate_whipsaw_flip(
    seed: int = 5505,
    sessions_count: int = 20,
) -> Tuple[List[SessionData], MarketConfig]:
    """5th brutal stress scenario: morning toxic melt-up -> abrupt mid-session flash reversal -> choppy afternoon."""
    rng = np.random.default_rng(seed)
    n_bars = 390
    u_profile = intraday_volume_profile(n_bars)
    time_labels = build_time_labels(n_bars)
    mcfg = MarketConfig(
        seed=seed,
        sessions=sessions_count,
        bars_per_session=n_bars,
        spread_bps=0.85,
        impact_coeff=0.45,
        order_size_shares=780.0,
    )
    sessions: List[SessionData] = []
    prev_close = mcfg.initial_price

    for s_idx in range(sessions_count):
        gap = rng.normal(0.0, 0.0035) if s_idx > 0 else 0.0
        s_open = float(prev_close * np.exp(gap))

        vol_regime = np.zeros(n_bars, dtype=np.int32)
        state = int(rng.random() < 0.35)
        for t in range(n_bars):
            vol_regime[t] = state
            if state == 0 and rng.random() < 0.018:
                state = 1
            elif state == 1 and rng.random() < 0.028:
                state = 0

        trend_dir = 1 if (s_idx % 2 == 0) else -1
        flip_bar = int(rng.integers(165, 215))
        flip_len = int(rng.integers(6, 10))
        d_mag = float(rng.uniform(1.55, 2.25))

        eff_log_p = np.zeros(n_bars, dtype=np.float64)
        trans_fast = np.zeros(n_bars, dtype=np.float64)
        trans_med = np.zeros(n_bars, dtype=np.float64)
        trans_slow = np.zeros(n_bars, dtype=np.float64)
        inst_vol_bps = np.zeros(n_bars, dtype=np.float64)
        burst_vol_mult = np.ones(n_bars, dtype=np.float64)

        cur_eff = float(np.log(s_open))
        cur_fast = cur_med = cur_slow = 0.0

        for t in range(n_bars):
            u_t = float(u_profile[t])
            reg_mult = 0.86 if vol_regime[t] == 0 else 1.40
            intraday_mult = 0.74 + 0.36 * float(np.sqrt(u_t))
            v_speed = 0.36 + 0.64 * u_t
            phi_fast_t = float(np.exp(-np.log(2.0) / 3.2 * v_speed))
            phi_med_t = float(np.exp(-np.log(2.0) / 6.8 * v_speed))
            phi_slow_t = float(np.exp(-np.log(2.0) / 13.0 * v_speed))

            perm_burst = 0.0
            drift_t = 0.0
            if 15 <= t < flip_bar:
                drift_t = trend_dir * d_mag
                sigma_eff = 4.45 * reg_mult * intraday_mult * 0.90
                rev_scale = 0.80
            elif flip_bar <= t < flip_bar + flip_len:
                shock = float(rng.uniform(14.0, 22.0))
                perm_burst += (-trend_dir) * 0.45 * shock
                cur_med += (-trend_dir) * 0.55 * shock
                sigma_eff = 4.45 * reg_mult * intraday_mult * 2.40
                burst_vol_mult[t] += float(rng.uniform(1.5, 2.5))
                rev_scale = 1.35
            else:
                sigma_eff = 4.45 * reg_mult * intraday_mult * 1.05
                rev_scale = 1.20

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
            d_eff_bps = drift_t + perm_burst + float(rng.normal(0.0, sigma_eff))
            if drift_t != 0.0 and (d_eff_bps * drift_t > 0.0):
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
        high_px = np.maximum(open_px, close_px) * np.exp(
            np.abs(rng.normal(0.0, 1.0, size=n_bars)) * bar_sig_frac
        )
        low_px = np.minimum(open_px, close_px) * np.exp(
            -np.abs(rng.normal(0.0, 1.0, size=n_bars)) * bar_sig_frac
        )
        vol_noise = np.exp(rng.normal(-0.045, 0.27, size=n_bars))
        reg_vol_factor = np.where(vol_regime == 1, 1.18, 0.94)
        raw_vol = (mcfg.adv_per_min / 1.15) * u_profile * burst_vol_mult * reg_vol_factor * vol_noise
        volume = np.maximum(np.round(raw_vol), 900.0).astype(np.float64)
        sessions.append(
            SessionData(
                session_id=s_idx + 1,
                times=time_labels,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                volume=volume,
                rel_vol=volume / mcfg.adv_per_min,
                vol_regime=vol_regime,
                transient_impact_bps=total_trans,
            )
        )
        prev_close = float(close_px[-1])

    return sessions, mcfg


def _build_meltup_trajectory(
    meltup_sessions: List[SessionData],
    meltup_inds: List[SessionIndicators],
    meltup_rinds: List[ResearchIndicators],
    meltup_hams: List[HamiltonRegimeState],
    meltup_cfg: MarketConfig,
) -> Dict[str, Any]:
    """Extract an illustrative ~130-bar Toxic Melt-Up bifurcation trajectory from seed=202
    showing Dual-Regime Bayes climbing while 8m Single / Adaptive collapse and p_trend crosses 0.62.
    """
    strat_map = {s.id: s for s in get_13_strategy_configs()}
    st_8m = strat_map["micro_8m_elastic_fade"]
    st_ad = strat_map["adaptive_blend_5_20"]

    best_score = -1e9
    best_payload: Dict[str, Any] = {}
    win_len = 130

    for s_idx, (sess, ind, rind, ham) in enumerate(
        zip(meltup_sessions, meltup_inds, meltup_rinds, meltup_hams)
    ):
        eq_8m = _mark_to_market_standard_curve(sess, ind, st_8m, meltup_cfg)
        eq_ad = _mark_to_market_standard_curve(sess, ind, st_ad, meltup_cfg)
        eq_ou = _mark_to_market_ou_curve(sess, ind, rind, meltup_cfg)
        _, _, _, eq_du = backtest_session_dual_regime(
            sess, ind, rind, ham, meltup_cfg, turbo_mode=False, return_equity_curve=True
        )
        assert eq_du is not None
        pt = ham.p_trend

        for start in range(15, 390 - win_len - 10, 5):
            end = start + win_len
            pt_seg = pt[start:end]
            # Require clean regime transition: calm Fade start (<0.38), crossing >0.62 mid-left [14..58]
            if pt_seg[0] >= 0.35 or np.sum(pt_seg > SURF_THRESHOLD) < 30:
                continue
            cross_rel = int(np.argmax(pt_seg > SURF_THRESHOLD))
            if cross_rel < 14 or cross_rel > 58:
                continue
            du_ret = eq_du[end - 1] / eq_du[start] - 1.0
            ou_ret = eq_ou[end - 1] / eq_ou[start] - 1.0
            ad_ret = eq_ad[end - 1] / eq_ad[start] - 1.0
            s8_ret = eq_8m[end - 1] / eq_8m[start] - 1.0
            px_move = abs(sess.close[end - 1] / sess.close[start] - 1.0)
            if du_ret <= 0.0025 or ad_ret >= -0.0020 or s8_ret >= -0.0025:
                continue

            # Prefer upward melt-up + active divergence across all 4 models
            bull_bonus = 0.008 if sess.close[end - 1] > sess.close[start] else 0.0
            ou_active_bonus = 0.004 if ou_ret < -0.0010 else 0.0
            spread_score = (
                (du_ret - ad_ret)
                + 0.7 * (du_ret - ou_ret)
                + 0.5 * px_move
                + bull_bonus
                + ou_active_bonus
            )
            if spread_score > best_score:
                best_score = spread_score
                p0 = float(sess.close[start])
                pts = []
                for k in range(win_len):
                    idx = start + k
                    pts.append(
                        {
                            "bar": k,
                            "idx": idx,
                            "time": sess.times[idx],
                            "price_norm": round(float(sess.close[idx] / p0 * 100.0), 3),
                            "p_trend": round(float(pt[idx]), 4),
                            "regime_mode": ham.regime_modes[idx],
                            "eq_single_8m": round(float(eq_8m[idx] / eq_8m[start] * 100.0), 3),
                            "eq_adaptive_1x": round(float(eq_ad[idx] / eq_ad[start] * 100.0), 3),
                            "eq_ou_vpin_1x": round(float(eq_ou[idx] / eq_ou[start] * 100.0), 3),
                            "eq_dual_1x": round(float(eq_du[idx] / eq_du[start] * 100.0), 3),
                        }
                    )
                best_payload = {
                    "scenario": "momentum_meltup",
                    "seed": meltup_cfg.seed,
                    "session_id": sess.session_id,
                    "start_idx": start,
                    "end_idx": end - 1,
                    "start_time": sess.times[start],
                    "end_time": sess.times[end - 1],
                    "surf_cross_bar": cross_rel,
                    "surf_cross_time": sess.times[start + cross_rel],
                    "fade_threshold": FADE_THRESHOLD,
                    "surf_threshold": SURF_THRESHOLD,
                    "end_equity": {
                        "single_8m": pts[-1]["eq_single_8m"],
                        "adaptive_1x": pts[-1]["eq_adaptive_1x"],
                        "ou_vpin_1x": pts[-1]["eq_ou_vpin_1x"],
                        "dual_1x": pts[-1]["eq_dual_1x"],
                    },
                    "points": pts,
                }

    return best_payload


def run_stationary_block_bootstrap(
    daily_returns_by_model: Dict[str, np.ndarray],
    n_trials: int = 1000,
    block_len: int = 5,
    seed: int = 777,
    trading_days: int = 252,
) -> Dict[str, Dict[str, Any]]:
    """Politis-Romano stationary block bootstrap (1,000 trials, mean block = 5 sessions)
    yielding exact 95% Sharpe CIs, Prob(Loss), and Daily CVaR 95% (bps).
    """
    rng = np.random.default_rng(seed)
    first_arr = next(iter(daily_returns_by_model.values()))
    n_obs = len(first_arr)
    p_new_block = 1.0 / float(block_len)

    # Generate shared block-bootstrap index matrix (n_trials, n_obs) so model comparisons
    # are paired on identical resampled market paths!
    idx_mat = np.empty((n_trials, n_obs), dtype=np.int32)
    starts = rng.integers(0, n_obs, size=(n_trials, n_obs))
    u_draw = rng.random(size=(n_trials, n_obs))
    idx_mat[:, 0] = starts[:, 0]
    for col in range(1, n_obs):
        cont = (idx_mat[:, col - 1] + 1) % n_obs
        idx_mat[:, col] = np.where(u_draw[:, col] < p_new_block, starts[:, col], cont)

    sqrt_yr = math.sqrt(float(trading_days))
    out: Dict[str, Dict[str, Any]] = {}

    for key, ret in daily_returns_by_model.items():
        r = np.asarray(ret, dtype=np.float64)
        samples = r[idx_mat]  # (n_trials, n_obs)
        means = np.mean(samples, axis=1)
        stds = np.std(samples, axis=1, ddof=1)
        sharpes = np.where(stds > 1e-12, means / stds * sqrt_yr, 0.0)

        # Daily CVaR 95% (Expected Shortfall in worst 5% of daily returns, in bps)
        var95 = float(np.percentile(r, 5.0))
        tail = r[r <= var95]
        cvar95_bps = float(np.mean(tail) * 1e4) if len(tail) > 0 else float(var95 * 1e4)

        emp_sd = float(np.std(r, ddof=1))
        emp_sr = float(np.mean(r) / emp_sd * sqrt_yr) if emp_sd > 1e-12 else 0.0

        out[key] = {
            "empirical_sharpe": round(emp_sr, 2),
            "median_sharpe": round(float(np.median(sharpes)), 2),
            "mean_sharpe": round(float(np.mean(sharpes)), 2),
            "ci95_lo": round(float(np.percentile(sharpes, 2.5)), 2),
            "ci95_hi": round(float(np.percentile(sharpes, 97.5)), 2),
            "prob_loss_pct": round(float(np.mean(sharpes < 0.0) * 100.0), 2),
            "daily_cvar95_bps": round(cvar95_bps, 2),
            "n_sessions": n_obs,
        }

    return out


def run_eight_asset_portfolio(
    seed: int = 808,
    sessions_count: int = 60,
) -> Dict[str, Any]:
    """Simulate an 8-asset intraday portfolio (ES, SPY, QQQ, NVDA, AAPL, TSLA, MSFT, BTC-USD)
    with correlated equity market factor + idiosyncratic institutional slicing & momentum bursts,
    evaluated with strictly causal Dual-Regime OU-Bayes 5-20m (1x & Turbo).
    """
    rng = np.random.default_rng(seed)
    n_bars = 390
    u_profile = intraday_volume_profile(n_bars)
    time_labels = build_time_labels(n_bars)
    n_assets = len(ASSET_SPECS)

    # Pre-generate shared intraday market factor innovations & volatility regime per session
    mkt_inno = rng.normal(0.0, 1.0, size=(sessions_count, n_bars))
    mkt_gaps = rng.normal(0.0, 0.0028, size=sessions_count)

    daily_ret_1x = np.zeros((sessions_count, n_assets), dtype=np.float64)
    daily_ret_tb = np.zeros((sessions_count, n_assets), dtype=np.float64)
    asset_summaries: List[Dict[str, Any]] = []

    for a_idx, spec in enumerate(ASSET_SPECS):
        mcfg = MarketConfig(
            seed=seed + a_idx * 17,
            sessions=sessions_count,
            bars_per_session=n_bars,
            initial_price=spec["init_px"],
            spread_bps=spec["spread_bps"],
            impact_coeff=spec["impact_coeff"],
            adv_per_min=55_000.0 * spec["adv_mult"],
            order_size_shares=780.0 * spec["adv_mult"],
        )
        beta = float(spec["beta"])
        vol_m = float(spec["vol_mult"])
        # Equity correlation ~ 0.62 for high-beta equity names, ~0.22 for BTC-USD
        rho_mkt = min(0.70, max(0.18, 0.55 * beta))
        rho_idio = math.sqrt(max(1.0 - rho_mkt * rho_mkt, 0.15))

        prev_close = mcfg.initial_price
        fade_total = 0
        surf_total = 0

        for s_idx in range(sessions_count):
            gap = (beta * float(mkt_gaps[s_idx]) + float(rng.normal(0.0, 0.0022 * vol_m))) if s_idx > 0 else 0.0
            s_open = float(prev_close * np.exp(gap))

            vol_regime = np.zeros(n_bars, dtype=np.int32)
            state = int(rng.random() < 0.28)
            for t in range(n_bars):
                vol_regime[t] = state
                if state == 0 and rng.random() < 0.014:
                    state = 1
                elif state == 1 and rng.random() < 0.032:
                    state = 0

            # Occasional asset-specific institutional trend-day waves (~18% of sessions)
            is_trend_day = rng.random() < 0.18
            trend_dir = 1 if rng.random() < 0.5 else -1
            drift_bps = np.zeros(n_bars, dtype=np.float64)
            d_state = 0
            d_mag = 0.0
            for t in range(n_bars):
                if is_trend_day and t >= 15:
                    if d_state == 0 and rng.random() < 0.040:
                        d_state = trend_dir if rng.random() < 0.85 else -trend_dir
                        d_mag = float(rng.uniform(1.35, 2.05)) * math.sqrt(vol_m)
                    elif d_state != 0 and rng.random() < 0.020:
                        d_state = 0
                        d_mag = 0.0
                else:
                    if d_state == 0 and rng.random() < 0.014:
                        d_state = 1 if rng.random() < 0.5 else -1
                        d_mag = float(rng.uniform(0.65, 1.25)) * math.sqrt(vol_m)
                    elif d_state != 0 and rng.random() < 0.032:
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
            mkt_row = mkt_inno[s_idx]

            for t in range(n_bars):
                u_t = float(u_profile[t])
                reg_mult = 0.86 if vol_regime[t] == 0 else 1.40
                intraday_mult = 0.74 + 0.36 * float(np.sqrt(u_t))
                v_speed = 0.36 + 0.64 * u_t
                phi_fast_t = float(np.exp(-np.log(2.0) / 3.2 * v_speed))
                phi_med_t = float(np.exp(-np.log(2.0) / 6.8 * v_speed))
                phi_slow_t = float(np.exp(-np.log(2.0) / 13.0 * v_speed))

                sigma_eff = 4.45 * vol_m * reg_mult * intraday_mult
                perm_burst = 0.0
                if 6 <= t <= n_bars - 20 and rng.random() < 0.008:
                    info_sign = 1 if rng.random() < 0.5 else -1
                    perm_burst += info_sign * float(rng.uniform(10.0, 16.0)) * vol_m
                    burst_vol_mult[t] += float(rng.uniform(0.45, 0.95))

                inno_fast = float(rng.normal(0.0, 1.92 * vol_m * reg_mult * (0.40 + 0.65 * np.sqrt(u_t))))
                inno_med = float(rng.normal(0.0, 2.25 * vol_m * reg_mult * (0.72 + 0.28 * np.sqrt(u_t))))
                inno_slow = float(rng.normal(0.0, 1.28 * vol_m * reg_mult * max(1.18 - 0.25 * u_t, 0.50)))
                cur_fast = phi_fast_t * cur_fast + inno_fast
                cur_med = phi_med_t * cur_med + inno_med
                cur_slow = phi_slow_t * cur_slow + inno_slow

                if 4 <= t <= n_bars - 18:
                    if rng.random() < 0.015 * (0.15 + 1.25 * u_t):
                        sign = 1 if rng.random() < 0.5 else -1
                        imp = float(rng.uniform(10.5, 16.5)) * vol_m
                        perm = float(rng.uniform(0.18, 0.26))
                        perm_burst += sign * imp * perm
                        cur_fast += sign * imp * (1.0 - perm)
                        burst_vol_mult[t] += float(rng.uniform(1.10, 1.95))
                    if rng.random() < 0.022 * (0.48 + 0.55 * u_t):
                        sign = 1 if rng.random() < 0.5 else -1
                        imp = float(rng.uniform(14.5, 22.5)) * vol_m
                        perm = float(rng.uniform(0.18, 0.26))
                        perm_burst += sign * imp * perm
                        cur_med += sign * imp * (1.0 - perm)
                        burst_vol_mult[t] += float(rng.uniform(0.85, 1.45))
                    if rng.random() < 0.014 * max(1.38 - 0.45 * u_t, 0.24):
                        sign = 1 if rng.random() < 0.5 else -1
                        imp = float(rng.uniform(13.0, 20.0)) * vol_m
                        perm = float(rng.uniform(0.24, 0.34))
                        perm_burst += sign * imp * perm
                        cur_slow += sign * imp * (1.0 - perm)
                        burst_vol_mult[t] += float(rng.uniform(0.45, 0.90))

                flow_intensity = (abs(inno_fast) + abs(inno_med)) / (5.5 * vol_m * reg_mult)
                burst_vol_mult[t] += 0.14 * min(flow_intensity, 2.5)
                z_corr = rho_mkt * float(mkt_row[t]) + rho_idio * float(rng.normal(0.0, 1.0))
                d_eff_bps = drift_bps[t] + perm_burst + sigma_eff * z_corr
                if is_trend_day and drift_bps[t] != 0.0 and (d_eff_bps * drift_bps[t] > 0.0):
                    burst_vol_mult[t] += 0.65
                cur_eff += d_eff_bps * 1e-4

                eff_log_p[t] = cur_eff
                trans_fast[t] = cur_fast
                trans_med[t] = cur_med
                trans_slow[t] = cur_slow
                inst_vol_bps[t] = float(np.sqrt(sigma_eff ** 2 + (2.35 * vol_m) ** 2))

            total_trans = trans_fast + trans_med + trans_slow
            close_px = np.exp(eff_log_p + total_trans * 1e-4)
            open_px = np.empty(n_bars, dtype=np.float64)
            open_px[0] = s_open
            open_px[1:] = close_px[:-1]
            bar_sig_frac = (inst_vol_bps * 1e-4) * np.sqrt(0.50)
            high_px = np.maximum(open_px, close_px) * np.exp(
                np.abs(rng.normal(0.0, 1.0, size=n_bars)) * bar_sig_frac
            )
            low_px = np.minimum(open_px, close_px) * np.exp(
                -np.abs(rng.normal(0.0, 1.0, size=n_bars)) * bar_sig_frac
            )
            vol_noise = np.exp(rng.normal(-0.045, 0.27, size=n_bars))
            reg_vol_factor = np.where(vol_regime == 1, 1.18, 0.94)
            raw_vol = (mcfg.adv_per_min / 1.15) * u_profile * burst_vol_mult * reg_vol_factor * vol_noise
            volume = np.maximum(np.round(raw_vol), 900.0).astype(np.float64)

            sess = SessionData(
                session_id=s_idx + 1,
                times=time_labels,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                volume=volume,
                rel_vol=volume / mcfg.adv_per_min,
                vol_regime=vol_regime,
                transient_impact_bps=total_trans,
            )
            prev_close = float(close_px[-1])

            ind = compute_session_indicators(sess)
            rind = compute_research_indicators(sess, ind)
            ham = compute_hamilton_regime_filter(sess, ind, rind)
            rn1, _, rows1, _ = backtest_session_dual_regime(sess, ind, rind, ham, mcfg, turbo_mode=False)
            rnt, _, _, _ = backtest_session_dual_regime(sess, ind, rind, ham, mcfg, turbo_mode=True)
            daily_ret_1x[s_idx, a_idx] = rn1
            daily_ret_tb[s_idx, a_idx] = rnt
            for row in rows1:
                if row["mode"] == "FADE":
                    fade_total += 1
                else:
                    surf_total += 1

        r_a = daily_ret_1x[:, a_idx]
        sr_a, ann_a, mdd_a = _summarize_daily_curve(r_a, 252)
        var95_a = float(np.percentile(r_a, 5.0))
        tail_a = r_a[r_a <= var95_a]
        cvar_a = float(np.mean(tail_a) * 1e4) if len(tail_a) > 0 else float(var95_a * 1e4)
        tot_tr = max(fade_total + surf_total, 1)
        asset_summaries.append(
            {
                "symbol": spec["symbol"],
                "asset_class": spec["asset_class"],
                "beta": spec["beta"],
                "vol_mult": spec["vol_mult"],
                "spread_bps": spec["spread_bps"],
                "net_sharpe": round(sr_a, 2),
                "annual_return_pct": round(ann_a, 2),
                "max_drawdown_pct": round(mdd_a, 2),
                "daily_cvar95_bps": round(cvar_a, 2),
                "fade_trades_60d": fade_total,
                "surf_trades_60d": surf_total,
                "surf_share_pct": round(surf_total / tot_tr * 100.0, 1),
            }
        )

    # Portfolio aggregation: Equal-Weight & Risk-Parity (Inverse-Vol weighted)
    asset_vols = np.std(daily_ret_1x, axis=0, ddof=1)
    inv_vol = 1.0 / np.maximum(asset_vols, 1e-6)
    w_rp = inv_vol / np.sum(inv_vol)
    w_eq = np.full(n_assets, 1.0 / float(n_assets), dtype=np.float64)

    for a_idx, w in enumerate(w_rp):
        asset_summaries[a_idx]["risk_parity_weight_pct"] = round(float(w * 100.0), 1)

    port_eq = daily_ret_1x @ w_eq
    port_rp = daily_ret_1x @ w_rp

    tb_vols = np.std(daily_ret_tb, axis=0, ddof=1)
    w_rp_tb = (1.0 / np.maximum(tb_vols, 1e-6))
    w_rp_tb /= np.sum(w_rp_tb)
    port_rp_tb = daily_ret_tb @ w_rp_tb

    eq_sr, eq_ann, eq_mdd = _summarize_daily_curve(port_eq, 252)
    rp_sr, rp_ann, rp_mdd = _summarize_daily_curve(port_rp, 252)
    tb_sr, tb_ann, tb_mdd = _summarize_daily_curve(port_rp_tb, 252)

    rp_var95 = float(np.percentile(port_rp, 5.0))
    rp_tail = port_rp[port_rp <= rp_var95]
    rp_cvar_bps = float(np.mean(rp_tail) * 1e4) if len(rp_tail) > 0 else float(rp_var95 * 1e4)

    # Exact Diversification Ratio DR = (w^T sigma) / sigma_p and average off-diagonal correlation
    rp_vol = float(np.std(port_rp, ddof=1))
    div_ratio = float(np.dot(w_rp, asset_vols) / max(rp_vol, 1e-9))

    corr_mat = np.corrcoef(daily_ret_1x, rowvar=False)
    off_diag = corr_mat[~np.eye(n_assets, dtype=bool)]
    avg_pairwise_corr = float(np.mean(off_diag))

    single_avg_sr = float(np.mean([a["net_sharpe"] for a in asset_summaries]))
    single_avg_mdd = float(np.mean([a["max_drawdown_pct"] for a in asset_summaries]))
    uplift_pct = (rp_sr / max(single_avg_sr, 1e-6) - 1.0) * 100.0

    return {
        "meta": {
            "seed": seed,
            "sessions": sessions_count,
            "asset_count": n_assets,
            "symbols": [a["symbol"] for a in asset_summaries],
        },
        "summary": {
            "single_asset_avg_sharpe": round(single_avg_sr, 2),
            "equal_weight_sharpe": round(eq_sr, 2),
            "risk_parity_sharpe": round(rp_sr, 2),
            "sharpe_uplift_vs_single_pct": round(uplift_pct, 1),
            "equal_weight_ann_return_pct": round(eq_ann, 2),
            "risk_parity_ann_return_pct": round(rp_ann, 2),
            "single_asset_avg_mdd_pct": round(single_avg_mdd, 2),
            "equal_weight_mdd_pct": round(eq_mdd, 2),
            "risk_parity_mdd_pct": round(rp_mdd, 2),
            "risk_parity_cvar95_bps": round(rp_cvar_bps, 2),
            "diversification_ratio": round(div_ratio, 2),
            "avg_pairwise_corr": round(avg_pairwise_corr, 3),
            "turbo_risk_parity_sharpe": round(tb_sr, 2),
            "turbo_risk_parity_ann_return_pct": round(tb_ann, 2),
            "turbo_risk_parity_mdd_pct": round(tb_mdd, 2),
        },
        "assets": asset_summaries,
    }


def run_dual_regime_lab(
    sessions: List[SessionData],
    indicators_list: List[SessionIndicators],
    research_list: List[ResearchIndicators],
    market_cfg: MarketConfig,
    all_14_daily_sharpes: List[float],
) -> Tuple[Dict[str, Any], List[HamiltonRegimeState]]:
    """Run full Phase 3 Dual-Regime Bayesian Bifurcation Suite and return (dual_regime_lab, hamilton_list)."""
    hamilton_list = [
        compute_hamilton_regime_filter(s, ind, rind)
        for s, ind, rind in zip(sessions, indicators_list, research_list)
    ]

    # 1. Baseline 60-session Purged Walk-Forward (IS 1-40 vs OOS 41-60) & DSR (M=20)
    strat_map = {s.id: s for s in get_13_strategy_configs()}
    n_sess = len(sessions)
    is_end = 40

    wf_specs = [
        ("micro_8m_elastic_fade", "8m Elastic Fade", "Single-Horizon", False, "standard"),
        ("static_blend_5_20", "Static Blend 5–20m", "Blended 5–20m", False, "standard"),
        ("vol_weighted_blend_5_20", "Vol-Weighted Blend 5–20m", "Blended 5–20m", False, "standard"),
        ("adaptive_blend_5_20", "Adaptive Blend 5–20m (1x)", "Blended 5–20m", False, "standard"),
        ("ou_spectral_vpin_1x", "OU-Spectral Curvature-VPIN (1x)", "OU-Spectral", False, "ou_1x"),
        ("ou_spectral_vpin_turbo", "OU-Spectral Turbo (Maker+Kelly)", "OU-Spectral Turbo", False, "ou_turbo"),
        ("dual_regime_bayes_1x", "Dual-Regime OU-Bayes (1x)", "Dual-Regime Bayes", True, "dual_1x"),
        ("dual_regime_bayes_turbo", "Dual-Regime Turbo (Maker+Kelly)", "Dual-Regime Turbo", True, "dual_turbo"),
    ]

    wf_series: List[Tuple[Tuple[str, str, str, bool, str], np.ndarray, List[Dict[str, Any]]]] = []
    extra_6_daily_sharpes: List[float] = []
    base_daily_map: Dict[str, np.ndarray] = {}

    for spec in wf_specs:
        mid, _, _, _, kind = spec
        daily_net = np.zeros(n_sess, dtype=np.float64)
        all_rows: List[Dict[str, Any]] = []
        for s_idx, (sess, ind, rind, ham) in enumerate(
            zip(sessions, indicators_list, research_list, hamilton_list)
        ):
            if kind == "standard":
                trs = backtest_session(sess, ind, strat_map[mid], market_cfg)
                eq = 1.0
                for tr in trs:
                    eq *= 1.0 + tr.net_ret
                    all_rows.append(
                        {
                            "mode": "FADE",
                            "hold_min": float(tr.hold_min),
                            "net_bps": tr.net_bps,
                            "cost_bps": tr.cost_bps,
                            "slippage_saved_bps": tr.slippage_saved_bps,
                        }
                    )
                daily_net[s_idx] = eq - 1.0
            elif kind == "ou_1x":
                rn, _, rows, _ = backtest_session_ou_spectral(sess, ind, rind, market_cfg, turbo_mode=False)
                daily_net[s_idx] = rn
                for r in rows:
                    r_copy = dict(r)
                    r_copy["mode"] = "FADE"
                    all_rows.append(r_copy)
            elif kind == "ou_turbo":
                rn, _, rows, _ = backtest_session_ou_spectral(sess, ind, rind, market_cfg, turbo_mode=True)
                daily_net[s_idx] = rn
                for r in rows:
                    r_copy = dict(r)
                    r_copy["mode"] = "FADE"
                    all_rows.append(r_copy)
            elif kind == "dual_1x":
                rn, _, rows, _ = backtest_session_dual_regime(
                    sess, ind, rind, ham, market_cfg, turbo_mode=False
                )
                daily_net[s_idx] = rn
                all_rows.extend(rows)
            else:
                rn, _, rows, _ = backtest_session_dual_regime(
                    sess, ind, rind, ham, market_cfg, turbo_mode=True
                )
                daily_net[s_idx] = rn
                all_rows.extend(rows)

        if kind in ("ou_1x", "ou_turbo", "dual_1x", "dual_turbo"):
            sd = float(np.std(daily_net, ddof=1))
            extra_6_daily_sharpes.append(float(np.mean(daily_net) / sd) if sd > 1e-12 else 0.0)
        wf_series.append((spec, daily_net, all_rows))
        base_daily_map[mid] = daily_net

    # Exact 2 additional Phase-3 peer trials (Hamilton Fade-Only 1x & Turbo) -> exact M = 20 candidate models
    for tb_flag in (False, True):
        dn_ab = np.array(
            [
                backtest_session_dual_regime(s, i, r, h, market_cfg, turbo_mode=tb_flag, mode_override="fade_only")[0]
                for s, i, r, h in zip(sessions, indicators_list, research_list, hamilton_list)
            ],
            dtype=np.float64,
        )
        sd_ab = float(np.std(dn_ab, ddof=1))
        extra_6_daily_sharpes.append(float(np.mean(dn_ab) / sd_ab) if sd_ab > 1e-12 else 0.0)

    trial_daily_sharpes = list(all_14_daily_sharpes) + extra_6_daily_sharpes
    n_trials_m = len(trial_daily_sharpes)  # Exact M = 20
    sr_trials_std_m20 = float(np.std(trial_daily_sharpes, ddof=1))

    wf_rows_out: List[Dict[str, Any]] = []
    dual_1x_row: Dict[str, Any] = {}
    dual_tb_row: Dict[str, Any] = {}

    for spec, daily_net, all_rows in wf_series:
        mid, name, family, is_dual, kind = spec
        is_ret = daily_net[:is_end]
        oos_ret = daily_net[is_end:]
        is_sr, is_ann, _ = _summarize_daily_curve(is_ret, market_cfg.trading_days_per_year)
        oos_sr, oos_ann, oos_mdd = _summarize_daily_curve(oos_ret, market_cfg.trading_days_per_year)
        full_sr, full_ann, full_mdd = _summarize_daily_curve(daily_net, market_cfg.trading_days_per_year)
        dsr_m20 = compute_psr_and_dsr(daily_net, sr_trials_std_m20, n_trials=n_trials_m)

        net_bps_arr = np.array([r["net_bps"] for r in all_rows], dtype=np.float64)
        wins = net_bps_arr[net_bps_arr > 0]
        losses = np.abs(net_bps_arr[net_bps_arr < 0])
        pf = float(np.sum(wins) / np.sum(losses)) if np.sum(losses) > 0 else 99.0
        win_rate = float(len(wins) / max(len(net_bps_arr), 1) * 100.0)
        avg_hold = float(np.mean([r["hold_min"] for r in all_rows])) if all_rows else 0.0
        avg_cost = float(np.mean([r["cost_bps"] for r in all_rows])) if all_rows else 0.0
        avg_slip = float(np.mean([r["slippage_saved_bps"] for r in all_rows])) if all_rows else 0.0
        ann_trades = int(round(len(all_rows) * (market_cfg.trading_days_per_year / float(n_sess))))
        fade_cnt = sum(1 for r in all_rows if r.get("mode", "FADE") == "FADE")
        surf_cnt = sum(1 for r in all_rows if r.get("mode", "FADE") == "SURF")
        retention_pct = (oos_sr / is_sr * 100.0) if is_sr > 0.1 else 0.0

        row = {
            "id": mid,
            "name": name,
            "family": family,
            "is_dual_regime": is_dual,
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
            "fade_trades_60d": fade_cnt,
            "surf_trades_60d": surf_cnt,
            "win_rate_pct": round(win_rate, 2),
            "profit_factor": round(pf, 2),
            "avg_cost_bps": round(avg_cost, 2),
            "vwap_slippage_saved_bps": round(avg_slip, 2),
            "psr_pct": dsr_m20["psr_pct"],
            "dsr_m20_pct": dsr_m20["dsr_pct"],
            "sr_benchmark_m20": dsr_m20["sr_benchmark_ann"],
        }
        wf_rows_out.append(row)
        if kind == "dual_1x":
            dual_1x_row = row
        elif kind == "dual_turbo":
            dual_tb_row = row

    # 2. 5-Regime All-Weather Adversarial Stress Matrix + Melt-Up Bifurcation Trajectory
    st_8m = strat_map["micro_8m_elastic_fade"]
    st_ad = strat_map["adaptive_blend_5_20"]
    scenario_defs = [
        ("flash_crash", "Flash Crash Shock", "2.8x vol spike, -55% depth, 2.5x spread (seed=1101)", 1101, 0),
        ("momentum_meltup", "Toxic Momentum Melt-Up", "Sustained one-sided informed trend drift (seed=202)", 202, 0),
        ("cost_shock_2x", "2x Friction Cost Shock", "2.0x spread (1.2 bps) & 2.0x impact (seed=3303)", 3303, 0),
        ("latency_lag_1m", "+1m Execution Latency", "1-minute signal-to-fill & exit lag (seed=4004)", 4004, 1),
        ("whipsaw_flip", "Mid-Session Whipsaw Flip", "Morning melt-up -> abrupt midday flash reversal (seed=5505)", 5505, 0),
    ]

    stress_5_out: List[Dict[str, Any]] = []
    stress_daily_concat: Dict[str, List[np.ndarray]] = {
        "single_8m": [],
        "adaptive_1x": [],
        "ou_spectral_1x": [],
        "dual_regime_1x": [],
        "dual_regime_turbo": [],
    }
    meltup_trajectory: Dict[str, Any] = {}

    for sid, label, desc, seed, lag in scenario_defs:
        if sid == "whipsaw_flip":
            sls, mc = simulate_whipsaw_flip(seed=seed, sessions_count=20)
        else:
            sls, mc = simulate_adversarial_regime(sid, seed=seed, sessions_count=20)
        si = [compute_session_indicators(s) for s in sls]
        sr = [compute_research_indicators(s, i) for s, i in zip(sls, si)]
        sh = [compute_hamilton_regime_filter(s, i, r) for s, i, r in zip(sls, si, sr)]

        if sid == "momentum_meltup":
            meltup_trajectory = _build_meltup_trajectory(sls, si, sr, sh, mc)

        n_s = len(sls)
        dn_8m = np.zeros(n_s, dtype=np.float64)
        dn_ad = np.zeros(n_s, dtype=np.float64)
        dn_ou = np.zeros(n_s, dtype=np.float64)
        dn_d1 = np.zeros(n_s, dtype=np.float64)
        dn_dt = np.zeros(n_s, dtype=np.float64)

        tr_8m = tr_ad = tr_ou = tr_d1_fade = tr_d1_surf = tr_dt = 0
        for idx, (s, ind, rind, ham) in enumerate(zip(sls, si, sr, sh)):
            r8, rows8 = backtest_standard_with_latency(s, ind, st_8m, mc, latency_bars=lag)
            ra, rowsa = backtest_standard_with_latency(s, ind, st_ad, mc, latency_bars=lag)
            ro, _, rowso, _ = backtest_session_ou_spectral(s, ind, rind, mc, turbo_mode=False, latency_bars=lag)
            rd1, _, rowsd1, _ = backtest_session_dual_regime(s, ind, rind, ham, mc, turbo_mode=False, latency_bars=lag)
            rdt, _, rowsdt, _ = backtest_session_dual_regime(s, ind, rind, ham, mc, turbo_mode=True, latency_bars=lag)
            dn_8m[idx] = r8
            dn_ad[idx] = ra
            dn_ou[idx] = ro
            dn_d1[idx] = rd1
            dn_dt[idx] = rdt
            tr_8m += len(rows8)
            tr_ad += len(rowsa)
            tr_ou += len(rowso)
            tr_d1_fade += sum(1 for x in rowsd1 if x["mode"] == "FADE")
            tr_d1_surf += sum(1 for x in rowsd1 if x["mode"] == "SURF")
            tr_dt += len(rowsdt)

        stress_daily_concat["single_8m"].append(dn_8m)
        stress_daily_concat["adaptive_1x"].append(dn_ad)
        stress_daily_concat["ou_spectral_1x"].append(dn_ou)
        stress_daily_concat["dual_regime_1x"].append(dn_d1)
        stress_daily_concat["dual_regime_turbo"].append(dn_dt)

        s8, a8, m8 = _summarize_daily_curve(dn_8m, 252)
        sa, aa, ma = _summarize_daily_curve(dn_ad, 252)
        so, ao, mo = _summarize_daily_curve(dn_ou, 252)
        sd1, ad1, md1 = _summarize_daily_curve(dn_d1, 252)
        sdt, adt, mdt = _summarize_daily_curve(dn_dt, 252)

        pt_cat = np.concatenate([h.p_trend for h in sh])
        stress_5_out.append(
            {
                "id": sid,
                "label": label,
                "description": desc,
                "seed": seed,
                "sessions": n_s,
                "mean_p_trend": round(float(np.mean(pt_cat)), 3),
                "surf_bar_pct": round(float(np.mean(pt_cat > SURF_THRESHOLD) * 100.0), 1),
                "flat_bar_pct": round(
                    float(np.mean((pt_cat >= FADE_THRESHOLD) & (pt_cat <= SURF_THRESHOLD)) * 100.0), 1
                ),
                "dual_fade_trades": tr_d1_fade,
                "dual_surf_trades": tr_d1_surf,
                "models": {
                    "single_8m": {"name": "8m Single", "net_sharpe": round(s8, 2), "annual_return_pct": round(a8, 2), "max_drawdown_pct": round(m8, 2), "trades_20d": tr_8m},
                    "adaptive_1x": {"name": "Adaptive Blend (1x)", "net_sharpe": round(sa, 2), "annual_return_pct": round(aa, 2), "max_drawdown_pct": round(ma, 2), "trades_20d": tr_ad},
                    "ou_spectral_1x": {"name": "OU-VPIN Fade (1x)", "net_sharpe": round(so, 2), "annual_return_pct": round(ao, 2), "max_drawdown_pct": round(mo, 2), "trades_20d": tr_ou},
                    "dual_regime_1x": {"name": "Dual-Regime Bayes (1x)", "net_sharpe": round(sd1, 2), "annual_return_pct": round(ad1, 2), "max_drawdown_pct": round(md1, 2), "trades_20d": tr_d1_fade + tr_d1_surf},
                    "dual_regime_turbo": {"name": "Dual-Regime Turbo", "net_sharpe": round(sdt, 2), "annual_return_pct": round(adt, 2), "max_drawdown_pct": round(mdt, 2), "trades_20d": tr_dt},
                },
            }
        )

    five_regime_mean = {
        mk: round(float(np.mean([sc["models"][mk]["net_sharpe"] for sc in stress_5_out])), 2)
        for mk in ("single_8m", "adaptive_1x", "ou_spectral_1x", "dual_regime_1x", "dual_regime_turbo")
    }
    five_regime_mdd_mean = {
        mk: round(float(np.mean([sc["models"][mk]["max_drawdown_pct"] for sc in stress_5_out])), 2)
        for mk in ("single_8m", "adaptive_1x", "ou_spectral_1x", "dual_regime_1x", "dual_regime_turbo")
    }

    # 3. 1,000-Trial Stationary Block-Bootstrap (block_len=5, seed=777)
    # Combined All-Weather OOS+Stress panel: 20 strict OOS baseline + 100 unseen stress = 120 unseen sessions!
    all_weather_daily = {
        "single_8m": np.concatenate([base_daily_map["micro_8m_elastic_fade"][is_end:]] + stress_daily_concat["single_8m"]),
        "adaptive_1x": np.concatenate([base_daily_map["adaptive_blend_5_20"][is_end:]] + stress_daily_concat["adaptive_1x"]),
        "ou_spectral_1x": np.concatenate([base_daily_map["ou_spectral_vpin_1x"][is_end:]] + stress_daily_concat["ou_spectral_1x"]),
        "dual_regime_1x": np.concatenate([base_daily_map["dual_regime_bayes_1x"][is_end:]] + stress_daily_concat["dual_regime_1x"]),
        "dual_regime_turbo": np.concatenate([base_daily_map["dual_regime_bayes_turbo"][is_end:]] + stress_daily_concat["dual_regime_turbo"]),
    }
    baseline_60d_daily = {
        "single_8m": base_daily_map["micro_8m_elastic_fade"],
        "adaptive_1x": base_daily_map["adaptive_blend_5_20"],
        "ou_spectral_1x": base_daily_map["ou_spectral_vpin_1x"],
        "dual_regime_1x": base_daily_map["dual_regime_bayes_1x"],
        "dual_regime_turbo": base_daily_map["dual_regime_bayes_turbo"],
    }

    boot_all_weather = run_stationary_block_bootstrap(all_weather_daily, n_trials=1000, block_len=5, seed=777)
    boot_baseline = run_stationary_block_bootstrap(baseline_60d_daily, n_trials=1000, block_len=5, seed=778)

    # 4. 8-Asset Intraday Portfolio Diversification Engine
    portfolio_8 = run_eight_asset_portfolio(seed=808, sessions_count=60)

    # Baseline Hamilton telemetry
    pt_base_all = np.concatenate([h.p_trend for h in hamilton_list])
    dual_payload: Dict[str, Any] = {
        "meta": {
            "p00": HAMILTON_P00,
            "p11": HAMILTON_P11,
            "likelihood_temp": HAMILTON_TEMP,
            "prior_log_odds_bias": HAMILTON_BIAS,
            "fade_threshold": FADE_THRESHOLD,
            "surf_threshold": SURF_THRESHOLD,
            "trials_m": n_trials_m,
            "bootstrap_trials": 1000,
            "bootstrap_block_len": 5,
            "bootstrap_seed": 777,
            "stress_regime_count": 5,
        },
        "telemetry": {
            "baseline_mean_p_trend": round(float(np.mean(pt_base_all)), 3),
            "baseline_fade_pct": round(float(np.mean(pt_base_all < FADE_THRESHOLD) * 100.0), 1),
            "baseline_flat_pct": round(
                float(np.mean((pt_base_all >= FADE_THRESHOLD) & (pt_base_all <= SURF_THRESHOLD)) * 100.0), 1
            ),
            "baseline_surf_pct": round(float(np.mean(pt_base_all > SURF_THRESHOLD) * 100.0), 1),
            "meltup_sharpe_turnaround_1x": round(
                stress_5_out[1]["models"]["dual_regime_1x"]["net_sharpe"]
                - stress_5_out[1]["models"]["ou_spectral_1x"]["net_sharpe"],
                2,
            ),
            "five_regime_positive_count_1x": sum(
                1 for sc in stress_5_out if sc["models"]["dual_regime_1x"]["net_sharpe"] > 0.0
            ),
            "five_regime_positive_count_turbo": sum(
                1 for sc in stress_5_out if sc["models"]["dual_regime_turbo"]["net_sharpe"] > 0.0
            ),
        },
        "walk_forward_dsr_m20": {
            "split": {
                "is_sessions": "1–40",
                "oos_sessions": "41–60",
                "trials_m": n_trials_m,
                "sr_benchmark_m20": wf_rows_out[0]["sr_benchmark_m20"],
            },
            "models": wf_rows_out,
            "dual_regime_1x": dual_1x_row,
            "dual_regime_turbo": dual_tb_row,
        },
        "stress_tests_5": {
            "scenarios": stress_5_out,
            "five_regime_mean": five_regime_mean,
            "five_regime_mdd_mean": five_regime_mdd_mean,
        },
        "meltup_trajectory": meltup_trajectory,
        "bootstrap_ci": {
            "n_trials": 1000,
            "block_len_sessions": 5,
            "seed": 777,
            "all_weather_120d_desc": "20 strict OOS baseline sessions + 100 unseen adversarial stress sessions (5 regimes x 20d)",
            "all_weather_120d": boot_all_weather,
            "baseline_60d": boot_baseline,
        },
        "portfolio_8_asset": portfolio_8,
    }
    return dual_payload, hamilton_list


if __name__ == "__main__":
    import time
    from .simulator import simulate_market
    from .blended_vwap import backtest_session_turbo

    cfg = MarketConfig()
    ss = simulate_market(cfg)
    inds = [compute_session_indicators(s) for s in ss]
    rinds = [compute_research_indicators(s, i) for s, i in zip(ss, inds)]
    strats = get_13_strategy_configs()
    dsrs14 = []
    for st in strats:
        dn = np.array(
            [math.prod(1.0 + tr.net_ret for tr in backtest_session(s, i, st, cfg)) - 1.0 for s, i in zip(ss, inds)]
        )
        dsrs14.append(float(np.mean(dn) / np.std(dn, ddof=1)))
    f_st = next(s for s in strats if s.is_flagship)
    tdn = np.array([backtest_session_turbo(s, i, f_st, cfg)[0] for s, i in zip(ss, inds)])
    dsrs14.append(float(np.mean(tdn) / np.std(tdn, ddof=1)))

    t0 = time.perf_counter()
    dl, _ = run_dual_regime_lab(ss, inds, rinds, cfg, dsrs14)
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"Dual-Regime Lab runtime: {ms:.1f} ms")
    print("TELEMETRY:", dl["telemetry"])
    print("\nWALK-FORWARD & DSR (M=20):")
    for m in dl["walk_forward_dsr_m20"]["models"]:
        print(
            f"  {m['name']:<32} IS={m['is_sharpe']:>5.2f} OOS={m['oos_sharpe']:>5.2f} "
            f"Full={m['full_sharpe']:>5.2f} Ret={m['oos_retention_pct']:>5.1f}% "
            f"PSR={m['psr_pct']:>6.2f}% DSR20={m['dsr_m20_pct']:>5.2f}% MDD={m['max_drawdown_pct']:>5.2f}% "
            f"F/S={m['fade_trades_60d']}/{m['surf_trades_60d']}"
        )
    print("\n5-REGIME ALL-WEATHER STRESS MATRIX:")
    for sc in dl["stress_tests_5"]["scenarios"]:
        ms_ = sc["models"]
        print(
            f"  {sc['id']:<16} (surf%={sc['surf_bar_pct']:4.1f} F/S={sc['dual_fade_trades']:3d}/{sc['dual_surf_trades']:3d}): "
            f"8m={ms_['single_8m']['net_sharpe']:6.2f} | Ad={ms_['adaptive_1x']['net_sharpe']:6.2f} | "
            f"OU={ms_['ou_spectral_1x']['net_sharpe']:6.2f} | "
            f"Dual1x={ms_['dual_regime_1x']['net_sharpe']:6.2f}({ms_['dual_regime_1x']['max_drawdown_pct']:4.1f}%) | "
            f"DualTB={ms_['dual_regime_turbo']['net_sharpe']:6.2f}({ms_['dual_regime_turbo']['max_drawdown_pct']:4.1f}%)"
        )
    print("  5-REGIME MEAN:", dl["stress_tests_5"]["five_regime_mean"])
    print("\nBLOCK-BOOTSTRAP 95% CIs (ALL-WEATHER 120d UNSEEN OOS+STRESS):")
    for k, v in dl["bootstrap_ci"]["all_weather_120d"].items():
        print(
            f"  {k:<18}: Emp={v['empirical_sharpe']:>6.2f} Med={v['median_sharpe']:>6.2f} "
            f"95%CI=[{v['ci95_lo']:>6.2f}, {v['ci95_hi']:>6.2f}] P(Loss)={v['prob_loss_pct']:>5.1f}% CVaR95={v['daily_cvar95_bps']:>7.2f}bps"
        )
    print("\nMELT-UP TRAJECTORY:", {k: v for k, v in dl["meltup_trajectory"].items() if k != "points"}, f"n_pts={len(dl['meltup_trajectory'].get('points', []))}")
    print("\n8-ASSET PORTFOLIO SUMMARY:", dl["portfolio_8_asset"]["summary"])
    for a in dl["portfolio_8_asset"]["assets"]:
        print(f"  {a['symbol']:<8} beta={a['beta']:.2f} SR={a['net_sharpe']:>5.2f} Ann={a['annual_return_pct']:>6.1f}% MDD={a['max_drawdown_pct']:>4.2f}% wRP={a['risk_parity_weight_pct']:>4.1f}% surf={a['surf_share_pct']:>4.1f}%")
