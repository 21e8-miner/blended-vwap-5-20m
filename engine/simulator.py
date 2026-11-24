"""Deterministic intraday 1-minute market simulator based on a multi-scale
Hasbrouck / Obizhaeva-Wang state-space microstructure model:
  - U-shaped intraday volume profile (open surge, midday lull, close surge)
  - 2-state Markov regime-switching volatility & slow informed drift regimes
  - Continuous 3-scale transient liquidity deviations (fast/core/slow OU pools)
    punctuated by high-volume institutional slicing bursts + permanent info share
  - Realistic bid-ask spread (0.6 bps) + square-root market impact cost model
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List
import numpy as np


@dataclass(frozen=True)
class MarketConfig:
    """Configuration for the 60-session x 390-bar intraday market simulation."""

    seed: int = 42
    sessions: int = 60
    bars_per_session: int = 390
    initial_price: float = 150.0
    adv_per_min: float = 50_000.0
    spread_bps: float = 0.60
    impact_coeff: float = 0.40
    order_size_shares: float = 780.0
    trading_days_per_year: int = 252


@dataclass
class SessionData:
    """Container for one simulated 390-bar regular trading session (09:30-16:00 ET)."""

    session_id: int
    times: List[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    rel_vol: np.ndarray
    vol_regime: np.ndarray
    transient_impact_bps: np.ndarray


def build_time_labels(bars_per_session: int = 390) -> List[str]:
    """Return 1-minute bar start labels from 09:30 through 15:59 ET."""
    labels: List[str] = []
    start_minutes = 9 * 60 + 30
    for i in range(bars_per_session):
        total_min = start_minutes + i
        hh = total_min // 60
        mm = total_min % 60
        labels.append(f"{hh:02d}:{mm:02d}")
    return labels


def intraday_volume_profile(bars_per_session: int = 390) -> np.ndarray:
    """Normalized U-shaped US equity intraday volume profile (mean = 1.0)."""
    t = np.arange(bars_per_session, dtype=np.float64)
    u = t / max(bars_per_session - 1, 1)
    raw = (
        0.48
        + 2.25 * np.exp(-t / 25.0)
        + 1.80 * np.exp(-(bars_per_session - 1 - t) / 28.0)
        + 0.08 * (np.sin(np.pi * u) ** 2)
    )
    return raw / float(np.mean(raw))


def compute_side_cost_bps(
    sigma_bar_frac: float,
    bar_volume: float,
    order_shares: float,
    spread_bps: float = 0.60,
    impact_coeff: float = 0.40,
) -> float:
    """Single-side execution cost in bps: half-spread + eta * sigma * sqrt(q / V)."""
    vol_bps = max(float(sigma_bar_frac), 1.5e-4) * 1e4
    participation = max(float(order_shares), 1.0) / max(float(bar_volume), 1.0)
    impact_bps = impact_coeff * vol_bps * float(np.sqrt(participation))
    return 0.5 * spread_bps + impact_bps


def simulate_market(cfg: MarketConfig | None = None) -> List[SessionData]:
    """Simulate deterministic multi-session 1-minute OHLCV equity bars."""
    if cfg is None:
        cfg = MarketConfig()

    rng = np.random.default_rng(cfg.seed)
    n_bars = cfg.bars_per_session
    u_profile = intraday_volume_profile(n_bars)
    time_labels = build_time_labels(n_bars)

    sessions: List[SessionData] = []
    prev_session_close = cfg.initial_price

    # OU persistence factors for the three transient liquidity resilience scales
    phi_fast = float(np.exp(-np.log(2.0) / 3.0))   # 3.0m half-life (fast sweeps)
    phi_med = float(np.exp(-np.log(2.0) / 6.5))    # 6.5m half-life (core institutional slicing)
    phi_slow = float(np.exp(-np.log(2.0) / 13.0))  # 13.0m half-life (slow block inventory)

    for s_idx in range(cfg.sessions):
        overnight_gap = rng.normal(0.0, 0.0030) if s_idx > 0 else 0.0
        session_open_price = float(prev_session_close * np.exp(overnight_gap))

        # 1. Two-state Markov volatility regime (0 = calm, 1 = elevated)
        vol_regime = np.zeros(n_bars, dtype=np.int32)
        state = int(rng.random() < 0.28)
        for t in range(n_bars):
            vol_regime[t] = state
            if state == 0:
                if rng.random() < 0.014:
                    state = 1
            else:
                if rng.random() < 0.032:
                    state = 0

        # 2. Slow intraday informed drift regimes (persistent directional moves)
        drift_bps = np.zeros(n_bars, dtype=np.float64)
        drift_state = 0
        drift_mag = 0.0
        for t in range(n_bars):
            if drift_state == 0:
                if rng.random() < 0.015:
                    drift_state = 1 if rng.random() < 0.5 else -1
                    drift_mag = float(rng.uniform(0.70, 1.30))
            else:
                if rng.random() < 0.030:
                    drift_state = 0
                    drift_mag = 0.0
            drift_bps[t] = drift_state * drift_mag

        # 3. Coupled efficient price + 3-scale continuous OU liquidity pools + institutional bursts
        eff_log_p = np.zeros(n_bars, dtype=np.float64)
        trans_fast = np.zeros(n_bars, dtype=np.float64)
        trans_med = np.zeros(n_bars, dtype=np.float64)
        trans_slow = np.zeros(n_bars, dtype=np.float64)
        inst_vol_bps = np.zeros(n_bars, dtype=np.float64)
        burst_vol_mult = np.ones(n_bars, dtype=np.float64)

        cur_eff = float(np.log(session_open_price))
        cur_fast = 0.0
        cur_med = 0.0
        cur_slow = 0.0

        for t in range(n_bars):
            u_t = float(u_profile[t])
            reg_mult = 0.86 if vol_regime[t] == 0 else 1.40
            intraday_mult = 0.74 + 0.36 * float(np.sqrt(u_t))

            # Volume-clock liquidity resilience (Easley-Lopez de Prado-O'Hara):
            # Order books replenish faster in wall-clock time during high-volume open/close
            # surges and slower during calm midday lulls
            v_speed = 0.36 + 0.64 * u_t
            phi_fast_t = float(np.exp(-np.log(2.0) / 3.2 * v_speed))
            phi_med_t = float(np.exp(-np.log(2.0) / 6.8 * v_speed))
            phi_slow_t = float(np.exp(-np.log(2.0) / 13.0 * v_speed))

            # Fundamental permanent innovation (~4.3 bps calm / ~6.6 bps elevated)
            sigma_eff = 4.45 * reg_mult * intraday_mult
            perm_burst = 0.0

            # Permanent information repricing impulses (macro/sector/informed news: ~3.2/session)
            if 6 <= t <= n_bars - 20 and rng.random() < 0.0085:
                info_sign = 1 if rng.random() < 0.5 else -1
                perm_burst += info_sign * float(rng.uniform(10.5, 16.5))
                burst_vol_mult[t] += float(rng.uniform(0.45, 0.95))

            # Continuous background liquidity imbalance innovations across 3 timescales
            inno_fast = float(rng.normal(0.0, 1.92 * reg_mult * (0.40 + 0.65 * np.sqrt(u_t))))
            inno_med = float(rng.normal(0.0, 2.25 * reg_mult * (0.72 + 0.28 * np.sqrt(u_t))))
            inno_slow = float(rng.normal(0.0, 1.28 * reg_mult * max(1.18 - 0.25 * u_t, 0.50)))

            cur_fast = phi_fast_t * cur_fast + inno_fast
            cur_med = phi_med_t * cur_med + inno_med
            cur_slow = phi_slow_t * cur_slow + inno_slow

            # Punctuated institutional slicing order-flow bursts (carrying elevated volume)
            if 4 <= t <= n_bars - 18:
                # Fast sweep bursts during high-volume open/close
                if rng.random() < 0.015 * (0.15 + 1.25 * u_t):
                    sign = 1 if rng.random() < 0.5 else -1
                    imp = float(rng.uniform(10.5, 16.5))
                    perm = float(rng.uniform(0.18, 0.26))
                    perm_burst += sign * imp * perm
                    cur_fast += sign * imp * (1.0 - perm)
                    burst_vol_mult[t] += float(rng.uniform(1.10, 1.95))

                # Medium institutional VWAP/TWAP slicing bursts across session
                if rng.random() < 0.022 * (0.48 + 0.55 * u_t):
                    sign = 1 if rng.random() < 0.5 else -1
                    imp = float(rng.uniform(14.5, 22.5))
                    perm = float(rng.uniform(0.18, 0.26))
                    perm_burst += sign * imp * perm
                    cur_med += sign * imp * (1.0 - perm)
                    burst_vol_mult[t] += float(rng.uniform(0.85, 1.45))

                # Slow block inventory shocks during calm midday windows
                if rng.random() < 0.014 * max(1.38 - 0.45 * u_t, 0.24):
                    sign = 1 if rng.random() < 0.5 else -1
                    imp = float(rng.uniform(13.0, 20.0))
                    perm = float(rng.uniform(0.24, 0.34))
                    perm_burst += sign * imp * perm
                    cur_slow += sign * imp * (1.0 - perm)
                    burst_vol_mult[t] += float(rng.uniform(0.45, 0.90))

            # Volume mildly responds to aggregate liquidity flow intensity
            flow_intensity = (abs(inno_fast) + abs(inno_med)) / (5.5 * reg_mult)
            burst_vol_mult[t] += 0.14 * min(flow_intensity, 2.5)

            d_eff_bps = drift_bps[t] + perm_burst + float(rng.normal(0.0, sigma_eff))
            cur_eff += d_eff_bps * 1e-4

            eff_log_p[t] = cur_eff
            trans_fast[t] = cur_fast
            trans_med[t] = cur_med
            trans_slow[t] = cur_slow
            inst_vol_bps[t] = float(np.sqrt(sigma_eff ** 2 + 2.35 ** 2))

        total_transient_bps = trans_fast + trans_med + trans_slow
        obs_log_close = eff_log_p + total_transient_bps * 1e-4
        close_px = np.exp(obs_log_close)

        open_px = np.empty(n_bars, dtype=np.float64)
        open_px[0] = session_open_price
        open_px[1:] = close_px[:-1]

        bar_sig_frac = (inst_vol_bps * 1e-4) * np.sqrt(0.50)
        hi_exc = np.abs(rng.normal(0.0, 1.0, size=n_bars)) * bar_sig_frac
        lo_exc = np.abs(rng.normal(0.0, 1.0, size=n_bars)) * bar_sig_frac

        high_px = np.maximum(open_px, close_px) * np.exp(hi_exc)
        low_px = np.minimum(open_px, close_px) * np.exp(-lo_exc)

        # 4. Realistic bar volume scaled so long-run average rel_vol ~ 1.00
        vol_noise = np.exp(rng.normal(-0.045, 0.27, size=n_bars))
        reg_vol_factor = np.where(vol_regime == 1, 1.18, 0.94)
        raw_vol = (
            (cfg.adv_per_min / 1.15)
            * u_profile
            * burst_vol_mult
            * reg_vol_factor
            * vol_noise
        )
        volume = np.maximum(np.round(raw_vol), 1_500.0).astype(np.float64)
        rel_vol = volume / cfg.adv_per_min

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
                transient_impact_bps=total_transient_bps,
            )
        )
        prev_session_close = float(close_px[-1])

    return sessions
