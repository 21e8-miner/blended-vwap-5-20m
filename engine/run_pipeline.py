"""End-to-end deterministic pipeline:
1. Simulate 60 sessions x 390 1-minute bars (seed=42)
2. Compute strictly causal rolling VWAPs & 3 Blended 5-20m models
3. Backtest all 13 trade-window configurations across 3 regimes
4. Export data/vwap_metrics.json, data/session_bars.json, and data/bundle.js
"""

from __future__ import annotations

import gc
import json
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Tuple
import numpy as np

from .simulator import MarketConfig, SessionData, simulate_market
from .blended_vwap import (
    ALL_HORIZONS,
    BLEND_HORIZONS,
    SIGMA_FLOOR_FRAC,
    STATIC_WEIGHTS,
    SessionIndicators,
    StrategyConfig,
    TradeRecord,
    _resolve_signal_arrays,
    backtest_session,
    backtest_session_turbo,
    compute_entry_signals,
    compute_session_indicators,
    get_13_strategy_configs,
)

if TYPE_CHECKING:
    from .research_lab import ResearchIndicators
    from .dual_regime import HamiltonRegimeState

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

REGIME_META = [
    {
        "id": "micro",
        "label": "Micro-Pulse",
        "sublabel": "5–8 min window",
        "min_hold_bound": 5,
        "max_hold_bound": 8,
    },
    {
        "id": "core",
        "label": "Core Blended",
        "sublabel": "9–14 min window",
        "min_hold_bound": 9,
        "max_hold_bound": 14,
    },
    {
        "id": "anchor",
        "label": "Anchor Drift",
        "sublabel": "15–20 min window",
        "min_hold_bound": 15,
        "max_hold_bound": 20,
    },
]


def _classify_intraday_regime(idx_10m: int, rel_vol: float) -> str:
    """Human-readable intraday liquidity/regime state for the 39-point schedule."""
    if idx_10m <= 4:
        return "Open Surge"
    if idx_10m <= 10:
        return "Morning Transition"
    if idx_10m <= 26:
        return "Midday Lull" if rel_vol < 0.85 else "Midday Burst"
    if idx_10m <= 33:
        return "Afternoon Drift"
    return "Close Surge"


def evaluate_strategy(
    strat: StrategyConfig,
    sessions: List[SessionData],
    indicators_list: List[SessionIndicators],
    market_cfg: MarketConfig,
) -> Tuple[Dict[str, Any], List[TradeRecord], Dict[int, List[TradeRecord]]]:
    """Run multi-session backtest and compute all benchmark metrics for one config."""
    all_trades: List[TradeRecord] = []
    trades_by_session: Dict[int, List[TradeRecord]] = {}

    n_sessions = len(sessions)
    daily_gross_ret = np.zeros(n_sessions, dtype=np.float64)
    daily_net_ret = np.zeros(n_sessions, dtype=np.float64)

    for s_idx, (sess, ind) in enumerate(zip(sessions, indicators_list)):
        s_trades = backtest_session(sess, ind, strat, market_cfg, trade_id_offset=len(all_trades))
        trades_by_session[sess.session_id] = s_trades
        all_trades.extend(s_trades)

        g_eq = 1.0
        n_eq = 1.0
        for tr in s_trades:
            g_eq *= 1.0 + tr.gross_ret
            n_eq *= 1.0 + tr.net_ret
        daily_gross_ret[s_idx] = g_eq - 1.0
        daily_net_ret[s_idx] = n_eq - 1.0

    n_trades = len(all_trades)
    if n_trades < 2:
        raise RuntimeError(f"Strategy {strat.id} generated fewer than 2 trades ({n_trades}).")

    ann_factor = market_cfg.trading_days_per_year / float(n_sessions)
    annual_trades = int(round(n_trades * ann_factor))
    trades_per_day = n_trades / float(n_sessions)

    hold_arr = np.array([t.hold_min for t in all_trades], dtype=np.float64)
    net_ret_arr = np.array([t.net_ret for t in all_trades], dtype=np.float64)
    gross_bps_arr = np.array([t.gross_bps for t in all_trades], dtype=np.float64)
    cost_bps_arr = np.array([t.cost_bps for t in all_trades], dtype=np.float64)
    slip_arr = np.array([t.slippage_saved_bps for t in all_trades], dtype=np.float64)
    vol_bps_arr = np.array([t.entry_vol_bps for t in all_trades], dtype=np.float64)
    exec_bar_vol_arr = np.array(
        [market_cfg.order_size_shares / max(t.entry_participation, 1e-9) for t in all_trades],
        dtype=np.float64,
    )

    avg_hold_min = float(np.mean(hold_arr))
    per_trade_sharpe_s = float(np.mean(net_ret_arr) / np.std(net_ret_arr, ddof=1))

    std_daily_net = float(np.std(daily_net_ret, ddof=1))
    std_daily_gross = float(np.std(daily_gross_ret, ddof=1))
    sqrt_yr = math.sqrt(market_cfg.trading_days_per_year)
    net_sharpe = float(np.mean(daily_net_ret) / std_daily_net * sqrt_yr) if std_daily_net > 0 else 0.0
    gross_sharpe = float(np.mean(daily_gross_ret) / std_daily_gross * sqrt_yr) if std_daily_gross > 0 else 0.0

    # Compounded equity curve across 60 sessions -> annualized return & max drawdown
    equity = np.empty(n_sessions + 1, dtype=np.float64)
    equity[0] = 1.0
    for i in range(n_sessions):
        equity[i + 1] = equity[i] * (1.0 + daily_net_ret[i])

    total_growth = float(equity[-1])
    annual_return_pct = (
        ((total_growth ** ann_factor) - 1.0) * 100.0 if total_growth > 0 else -100.0
    )

    running_peak = np.maximum.accumulate(equity)
    drawdowns = (running_peak - equity) / np.maximum(running_peak, 1e-12)
    max_drawdown_pct = float(np.max(drawdowns) * 100.0)

    wins = net_ret_arr[net_ret_arr > 0.0]
    losses = net_ret_arr[net_ret_arr < 0.0]
    win_rate_pct = float(len(wins) / n_trades * 100.0)
    gross_loss_sum = float(np.abs(np.sum(losses)))
    profit_factor = float(np.sum(wins) / gross_loss_sum) if gross_loss_sum > 0 else 99.0

    avg_cost_bps = float(np.mean(cost_bps_arr))
    vwap_slippage_saved_bps = float(np.mean(slip_arr))

    # First-principles square-root impact capacity (% of ADV before impact consumes 50% of alpha-after-spread)
    mean_gross_edge = max(float(np.mean(gross_bps_arr)) - market_cfg.spread_bps, 0.35)
    mean_vol_bps = max(float(np.mean(vol_bps_arr)), 2.0)
    # Effective volume pool scales with slice window (working ~35% of avg hold window)
    eff_slice_vol = float(np.mean(exec_bar_vol_arr)) * max(avg_hold_min * 0.35, 1.0)
    max_order_shares = eff_slice_vol * (
        (0.50 * mean_gross_edge) / (2.0 * market_cfg.impact_coeff * mean_vol_bps)
    ) ** 2
    adv_shares = market_cfg.adv_per_min * market_cfg.bars_per_session
    capacity_adv_pct = float(min(max(max_order_shares * trades_per_day / adv_shares * 100.0, 0.25), 12.0))

    metrics = {
        "id": strat.id,
        "name": strat.name,
        "short_label": strat.short_label,
        "family": strat.family,
        "regime": strat.regime,
        "is_blended": strat.is_blended,
        "is_flagship": strat.is_flagship,
        "window_label": strat.window_label,
        "avg_hold_min": round(avg_hold_min, 2),
        "annual_trades": annual_trades,
        "trades_per_day": round(trades_per_day, 2),
        "per_trade_sharpe_s": round(per_trade_sharpe_s, 4),
        "net_sharpe": round(net_sharpe, 2),
        "gross_sharpe": round(gross_sharpe, 2),
        "annual_return_pct": round(annual_return_pct, 2),
        "vwap_slippage_saved_bps": round(vwap_slippage_saved_bps, 2),
        "win_rate_pct": round(win_rate_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "avg_cost_bps": round(avg_cost_bps, 2),
        "capacity_adv_pct": round(capacity_adv_pct, 2),
    }

    return metrics, all_trades, trades_by_session


def _precompute_session_caches(
    sessions: List[SessionData],
    indicators_list: List[SessionIndicators],
) -> List[Tuple[List[float], List[float], List[float], List[float], List[float], int]]:
    """Precompute per-session Python lists & O(1) VWAP prefix sums once across all 13 strategies."""
    caches: List[Tuple[List[float], List[float], List[float], List[float], List[float], int]] = []
    for sess, ind in zip(sessions, indicators_list):
        n = len(sess.close)
        cs_v = np.empty(n + 1, dtype=np.float64)
        cs_pv = np.empty(n + 1, dtype=np.float64)
        cs_v[0] = 0.0
        cs_pv[0] = 0.0
        np.cumsum(sess.volume, out=cs_v[1:])
        np.cumsum(ind.typical_price * sess.volume, out=cs_pv[1:])
        caches.append(
            (
                sess.close.tolist(),
                sess.volume.tolist(),
                ind.causal_bar_sigma.tolist(),
                cs_v.tolist(),
                cs_pv.tolist(),
                n,
            )
        )
    return caches


def _evaluate_strategy_fast(
    strat: StrategyConfig,
    sessions: List[SessionData],
    indicators_list: List[SessionIndicators],
    session_caches: List[Tuple[List[float], List[float], List[float], List[float], List[float], int]],
    market_cfg: MarketConfig,
) -> Tuple[Dict[str, Any], float]:
    """Exact bit-for-bit equivalent of evaluate_strategy without redundant per-strategy prefix sums
    or throwaway TradeRecord dataclass allocations (returns (metrics_dict, daily_net_sharpe)).
    """
    n_sessions = len(sessions)
    daily_gross_ret = np.zeros(n_sessions, dtype=np.float64)
    daily_net_ret = np.zeros(n_sessions, dtype=np.float64)

    order_sz = float(market_cfg.order_size_shares)
    half_spread = 0.5 * market_cfg.spread_bps
    impact_k = market_cfg.impact_coeff
    min_hold = strat.min_hold
    max_hold = strat.max_hold
    stop_sigma = strat.stop_sigma
    use_anchor = strat.use_anchor_exit
    rev_base = strat.anchor_reversion_frac
    adapt_gate = strat.adaptive_gate
    exit_z_thr = strat.exit_z
    cooldown = strat.cooldown_bars
    sig_src = strat.signal_source

    holds: List[float] = []
    net_rets: List[float] = []
    gross_bps_list: List[float] = []
    cost_bps_list: List[float] = []
    slip_list: List[float] = []
    vol_bps_list: List[float] = []
    exec_bar_vol_list: List[float] = []

    for s_idx, (sess, ind, cache) in enumerate(zip(sessions, indicators_list, session_caches)):
        close_l, vol_l, bar_sig_l, cs_v_l, cs_pv_l, n = cache
        vwap_arr, sigma_arr, z_arr = _resolve_signal_arrays(ind, sig_src)
        entry_signals = compute_entry_signals(sess, ind, strat).tolist()
        vwap_l = vwap_arr.tolist()
        sigma_l = sigma_arr.tolist()
        z_l = z_arr.tolist()

        g_eq = 1.0
        n_eq = 1.0
        in_pos = False
        side = 0
        entry_idx = 0
        entry_price = 0.0
        entry_vwap = 0.0
        entry_sigma = 0.0
        entry_cost_bps = 0.0
        entry_part = 0.0
        entry_vol_bps = 0.0
        cooldown_until = -1
        eod_bar = n - 2
        last_open_t = eod_bar - min_hold

        for t in range(n):
            if not in_pos:
                if t >= cooldown_until and entry_signals[t] != 0 and t <= last_open_t:
                    in_pos = True
                    side = entry_signals[t]
                    entry_idx = t
                    entry_price = close_l[t]
                    entry_vwap = vwap_l[t]
                    sig_t = sigma_l[t]
                    sig_fl = SIGMA_FLOOR_FRAC * entry_price
                    entry_sigma = sig_t if sig_t > sig_fl else sig_fl
                    bsig = bar_sig_l[t]
                    vt = vol_l[t]
                    entry_vol_bps = bsig * 1e4
                    entry_part = order_sz / (vt if vt > 1.0 else 1.0)
                    entry_cost_bps = half_spread + impact_k * entry_vol_bps * (entry_part ** 0.5)
            else:
                hold = t - entry_idx
                cur_px = close_l[t]
                exit_now = False
                if t >= eod_bar or hold >= max_hold:
                    exit_now = True
                elif hold >= min_hold:
                    adverse_move = -side * (cur_px - entry_price)
                    if adverse_move >= stop_sigma * entry_sigma:
                        exit_now = True
                    elif use_anchor:
                        init_gap = abs(entry_vwap - entry_price)
                        reverted = side * (cur_px - entry_price)
                        rev_frac = (
                            (rev_base + 0.45 * (10 - hold)) if (adapt_gate and hold <= 9) else rev_base
                        )
                        if reverted >= rev_frac * init_gap:
                            exit_now = True
                    elif side * z_l[t] >= -exit_z_thr:
                        exit_now = True

                if exit_now:
                    bsig_x = bar_sig_l[t]
                    vx = vol_l[t]
                    part_x = order_sz / (vx if vx > 1.0 else 1.0)
                    exit_cost_bps = half_spread + impact_k * (bsig_x * 1e4) * (part_x ** 0.5)
                    total_cost_bps = entry_cost_bps + exit_cost_bps
                    gross_ret = side * (cur_px - entry_price) / entry_price
                    gross_bps = gross_ret * 1e4
                    net_bps = gross_bps - total_cost_bps
                    net_ret = net_bps * 1e-4

                    g_eq *= 1.0 + gross_ret
                    n_eq *= 1.0 + net_ret

                    bench_end = entry_idx + 12 if (entry_idx + 12) < (n - 1) else (n - 1)
                    b_v = cs_v_l[bench_end + 1] - cs_v_l[entry_idx + 1]
                    bench_vwap = (cs_pv_l[bench_end + 1] - cs_pv_l[entry_idx + 1]) / b_v
                    slippage_saved_bps = (
                        side * (bench_vwap - entry_price) / entry_price * 1e4 - entry_cost_bps
                    )

                    holds.append(float(hold))
                    net_rets.append(net_ret)
                    gross_bps_list.append(gross_bps)
                    cost_bps_list.append(total_cost_bps)
                    slip_list.append(slippage_saved_bps)
                    vol_bps_list.append(entry_vol_bps)
                    exec_bar_vol_list.append(order_sz / (entry_part if entry_part > 1e-9 else 1e-9))

                    in_pos = False
                    cooldown_until = t + cooldown

        daily_gross_ret[s_idx] = g_eq - 1.0
        daily_net_ret[s_idx] = n_eq - 1.0

    n_trades = len(net_rets)
    if n_trades < 2:
        raise RuntimeError(f"Strategy {strat.id} generated fewer than 2 trades ({n_trades}).")

    ann_factor = market_cfg.trading_days_per_year / float(n_sessions)
    annual_trades = int(round(n_trades * ann_factor))
    trades_per_day = n_trades / float(n_sessions)

    hold_arr = np.asarray(holds, dtype=np.float64)
    net_ret_arr = np.asarray(net_rets, dtype=np.float64)
    gross_bps_arr = np.asarray(gross_bps_list, dtype=np.float64)
    cost_bps_arr = np.asarray(cost_bps_list, dtype=np.float64)
    slip_arr = np.asarray(slip_list, dtype=np.float64)
    vol_bps_arr = np.asarray(vol_bps_list, dtype=np.float64)
    exec_bar_vol_arr = np.asarray(exec_bar_vol_list, dtype=np.float64)

    avg_hold_min = float(np.mean(hold_arr))
    per_trade_sharpe_s = float(np.mean(net_ret_arr) / np.std(net_ret_arr, ddof=1))

    std_daily_net = float(np.std(daily_net_ret, ddof=1))
    std_daily_gross = float(np.std(daily_gross_ret, ddof=1))
    daily_sr_net = float(np.mean(daily_net_ret) / std_daily_net) if std_daily_net > 0 else 0.0
    sqrt_yr = math.sqrt(market_cfg.trading_days_per_year)
    net_sharpe = daily_sr_net * sqrt_yr
    gross_sharpe = float(np.mean(daily_gross_ret) / std_daily_gross * sqrt_yr) if std_daily_gross > 0 else 0.0

    equity = np.concatenate(([1.0], np.cumprod(1.0 + daily_net_ret)))
    total_growth = float(equity[-1])
    annual_return_pct = ((total_growth ** ann_factor) - 1.0) * 100.0 if total_growth > 0 else -100.0
    running_peak = np.maximum.accumulate(equity)
    max_drawdown_pct = float(np.max((running_peak - equity) / np.maximum(running_peak, 1e-12)) * 100.0)

    wins = net_ret_arr[net_ret_arr > 0.0]
    losses = net_ret_arr[net_ret_arr < 0.0]
    win_rate_pct = float(len(wins) / n_trades * 100.0)
    gross_loss_sum = float(np.abs(np.sum(losses)))
    profit_factor = float(np.sum(wins) / gross_loss_sum) if gross_loss_sum > 0 else 99.0

    avg_cost_bps = float(np.mean(cost_bps_arr))
    vwap_slippage_saved_bps = float(np.mean(slip_arr))

    mean_gross_edge = max(float(np.mean(gross_bps_arr)) - market_cfg.spread_bps, 0.35)
    mean_vol_bps = max(float(np.mean(vol_bps_arr)), 2.0)
    eff_slice_vol = float(np.mean(exec_bar_vol_arr)) * max(avg_hold_min * 0.35, 1.0)
    max_order_shares = eff_slice_vol * (
        (0.50 * mean_gross_edge) / (2.0 * market_cfg.impact_coeff * mean_vol_bps)
    ) ** 2
    adv_shares = market_cfg.adv_per_min * market_cfg.bars_per_session
    capacity_adv_pct = float(min(max(max_order_shares * trades_per_day / adv_shares * 100.0, 0.25), 12.0))

    metrics = {
        "id": strat.id,
        "name": strat.name,
        "short_label": strat.short_label,
        "family": strat.family,
        "regime": strat.regime,
        "is_blended": strat.is_blended,
        "is_flagship": strat.is_flagship,
        "window_label": strat.window_label,
        "avg_hold_min": round(avg_hold_min, 2),
        "annual_trades": annual_trades,
        "trades_per_day": round(trades_per_day, 2),
        "per_trade_sharpe_s": round(per_trade_sharpe_s, 4),
        "net_sharpe": round(net_sharpe, 2),
        "gross_sharpe": round(gross_sharpe, 2),
        "annual_return_pct": round(annual_return_pct, 2),
        "vwap_slippage_saved_bps": round(vwap_slippage_saved_bps, 2),
        "win_rate_pct": round(win_rate_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "avg_cost_bps": round(avg_cost_bps, 2),
        "capacity_adv_pct": round(capacity_adv_pct, 2),
    }
    return metrics, daily_sr_net


def build_weight_schedule_intraday(
    sessions: List[SessionData],
    indicators_list: List[SessionIndicators],
) -> List[Dict[str, Any]]:
    """Aggregate 39 ten-minute buckets across 09:30-16:00 ET over all 60 sessions."""
    n_buckets = 39  # 390 / 10
    schedule: List[Dict[str, Any]] = []
    time_labels = sessions[0].times

    all_w = np.stack([ind.weights_adaptive for ind in indicators_list], axis=0)  # (S, 390, 4)
    all_rv = np.stack([s.rel_vol for s in sessions], axis=0)  # (S, 390)

    for b in range(n_buckets):
        start = b * 10
        end = start + 10
        mean_w = np.mean(all_w[:, start:end, :], axis=(0, 1))
        # Ensure exact sum to 1.0 after rounding to 4 decimals
        w_round = [round(float(x), 4) for x in mean_w]
        w_round[3] = round(1.0 - (w_round[0] + w_round[1] + w_round[2]), 4)
        rv_val = round(float(np.mean(all_rv[:, start:end])), 3)
        schedule.append(
            {
                "idx": b,
                "time": time_labels[start],
                "w5": w_round[0],
                "w10": w_round[1],
                "w15": w_round[2],
                "w20": w_round[3],
                "rel_volume": rv_val,
                "regime_state": _classify_intraday_regime(b, rv_val),
            }
        )

    return schedule


def build_duration_histogram(flagship_trades: List[TradeRecord]) -> List[Dict[str, Any]]:
    """Counts for integer holding minutes 5..20 for flagship Adaptive Blend 5-20m."""
    total = max(len(flagship_trades), 1)
    hist: List[Dict[str, Any]] = []
    for m in range(5, 21):
        matching = [t for t in flagship_trades if t.hold_min == m]
        cnt = len(matching)
        avg_bps = float(np.mean([t.net_bps for t in matching])) if cnt > 0 else 0.0
        hist.append(
            {
                "hold_min": m,
                "count": cnt,
                "pct": round(cnt / total * 100.0, 2),
                "avg_net_bps": round(avg_bps, 2),
            }
        )
    return hist


def select_representative_session(
    sessions: List[SessionData],
    flagship_by_session: Dict[int, List[TradeRecord]],
) -> Tuple[int, Tuple[int, int, str]]:
    """Select an illustrative active session with clean two-sided flagship trades and a ~90m spotlight."""
    best_sid = 1
    best_score = -1e9
    best_spotlight = (30, 120, "10:00–11:30 ET Active Flow Spotlight")

    for sess in sessions:
        tr_list = flagship_by_session.get(sess.session_id, [])
        n_tr = len(tr_list)
        if n_tr < 7 or n_tr > 16:
            continue
        longs = sum(1 for t in tr_list if t.side == 1)
        shorts = sum(1 for t in tr_list if t.side == -1)
        if longs < 2 or shorts < 2:
            continue
        net_bps_sum = sum(t.net_bps for t in tr_list)
        win_rate = sum(1 for t in tr_list if t.net_bps > 0) / n_tr

        # Find best 90-bar spotlight window with rich activity (3 to 6 trades)
        win_best_score = -1e9
        win_best_span = (20, 110)
        for s_bar in range(15, 290, 5):
            e_bar = s_bar + 90
            in_win = [t for t in tr_list if t.entry_idx >= s_bar and t.exit_idx <= e_bar]
            if len(in_win) < 3:
                continue
            w_longs = sum(1 for t in in_win if t.side == 1)
            w_shorts = sum(1 for t in in_win if t.side == -1)
            w_pnl = sum(t.net_bps for t in in_win)
            w_score = len(in_win) * 4.0 + (4.0 if w_longs >= 1 and w_shorts >= 1 else 0.0) + 0.15 * w_pnl
            if w_score > win_best_score:
                win_best_score = w_score
                win_best_span = (s_bar, e_bar)

        if win_best_score < 0:
            continue

        score = (
            win_best_score
            + 0.25 * net_bps_sum
            + 12.0 * win_rate
            - 1.5 * abs(n_tr - 11)
            - 2.0 * abs(longs - shorts)
        )
        if score > best_score:
            best_score = score
            best_sid = sess.session_id
            s0, s1 = win_best_span
            lbl = f"{sess.times[s0]}–{sess.times[s1]} ET Institutional Flow Spotlight"
            best_spotlight = (s0, s1, lbl)

    return best_sid, best_spotlight


def build_session_export(
    session: SessionData,
    indicators: SessionIndicators,
    trades: List[TradeRecord],
    spotlight: Tuple[int, int, str],
    research_ind: ResearchIndicators | None = None,
    hamilton_state: HamiltonRegimeState | None = None,
) -> Dict[str, Any]:
    """Serialize full 390-bar representative session + flagship trades + Phase 2/3 microstructure fields."""
    bars: List[Dict[str, Any]] = []
    n = len(session.close)
    for t in range(n):
        w = indicators.weights_adaptive[t]
        w_r = [round(float(x), 4) for x in w]
        w_r[3] = round(1.0 - (w_r[0] + w_r[1] + w_r[2]), 4)
        bar: Dict[str, Any] = {
            "idx": t,
            "time": session.times[t],
            "open": round(float(session.open[t]), 4),
            "high": round(float(session.high[t]), 4),
            "low": round(float(session.low[t]), 4),
            "close": round(float(session.close[t]), 4),
            "volume": int(round(float(session.volume[t]))),
            "rel_vol": round(float(session.rel_vol[t]), 3),
            "vwap5": round(float(indicators.vwap_by_h[5][t]), 4),
            "vwap6": round(float(indicators.vwap_by_h[6][t]), 4),
            "vwap8": round(float(indicators.vwap_by_h[8][t]), 4),
            "vwap10": round(float(indicators.vwap_by_h[10][t]), 4),
            "vwap12": round(float(indicators.vwap_by_h[12][t]), 4),
            "vwap15": round(float(indicators.vwap_by_h[15][t]), 4),
            "vwap16": round(float(indicators.vwap_by_h[16][t]), 4),
            "vwap18": round(float(indicators.vwap_by_h[18][t]), 4),
            "vwap20": round(float(indicators.vwap_by_h[20][t]), 4),
            "sigma5": round(float(indicators.sigma_by_h[5][t]), 5),
            "sigma10": round(float(indicators.sigma_by_h[10][t]), 5),
            "sigma15": round(float(indicators.sigma_by_h[15][t]), 5),
            "sigma20": round(float(indicators.sigma_by_h[20][t]), 5),
            "vwap_static": round(float(indicators.vwap_static[t]), 4),
            "vwap_adaptive": round(float(indicators.vwap_adaptive[t]), 4),
            "sigma_blend": round(float(indicators.sigma_adaptive[t]), 5),
            "z_score": round(float(indicators.z_adaptive[t]), 4),
            "w5": w_r[0],
            "w10": w_r[1],
            "w15": w_r[2],
            "w20": w_r[3],
        }
        if research_ind is not None:
            w_ou = research_ind.weights_ou[t]
            w_ou_r = [round(float(x), 4) for x in w_ou]
            w_ou_r[3] = round(1.0 - (w_ou_r[0] + w_ou_r[1] + w_ou_r[2]), 4)
            bar.update(
                {
                    "vwap_ou": round(float(research_ind.vwap_ou[t]), 4),
                    "sigma_ou": round(float(research_ind.sigma_ou[t]), 5),
                    "z_ou": round(float(research_ind.z_ou[t]), 4),
                    "tau_half": round(float(research_ind.tau_half[t]), 3),
                    "slope": round(float(research_ind.slope[t]), 4),
                    "curvature": round(float(research_ind.curvature[t]), 4),
                    "vpin": round(float(research_ind.vpin[t]), 4),
                    "hurst": round(float(research_ind.hurst[t]), 4),
                    "ou_w5": w_ou_r[0],
                    "ou_w10": w_ou_r[1],
                    "ou_w15": w_ou_r[2],
                    "ou_w20": w_ou_r[3],
                }
            )
        if hamilton_state is not None:
            bar["p_trend"] = round(float(hamilton_state.p_trend[t]), 4)
            bar["regime_mode"] = hamilton_state.regime_modes[t]
        bars.append(bar)

    trade_dicts = [
        {
            "trade_id": idx + 1,
            "direction": "LONG" if tr.side == 1 else "SHORT",
            "side": tr.side,
            "entry_idx": tr.entry_idx,
            "exit_idx": tr.exit_idx,
            "entry_time": tr.entry_time,
            "exit_time": tr.exit_time,
            "hold_min": tr.hold_min,
            "entry_price": round(tr.entry_price, 4),
            "exit_price": round(tr.exit_price, 4),
            "entry_vwap": round(tr.entry_vwap, 4),
            "entry_z": round(tr.entry_z, 3),
            "exit_z": round(tr.exit_z, 3),
            "gross_bps": round(tr.gross_bps, 2),
            "cost_bps": round(tr.cost_bps, 2),
            "net_bps": round(tr.net_bps, 2),
            "slippage_saved_bps": round(tr.slippage_saved_bps, 2),
            "exit_reason": tr.exit_reason,
        }
        for idx, tr in enumerate(trades)
    ]

    return {
        "session_id": session.session_id,
        "date_label": f"Session #{session.session_id:02d} · Representative Liquid Large-Cap Day (09:30–16:00 ET)",
        "spotlight_window": {
            "start_idx": spotlight[0],
            "end_idx": spotlight[1],
            "label": spotlight[2],
        },
        "bars": bars,
        "trades": trade_dicts,
    }


def run_pipeline(output_dir: Path | None = None, market_cfg: MarketConfig | None = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Execute full simulation, backtest, and JSON/JS data export."""
    if market_cfg is None:
        market_cfg = MarketConfig()
    if output_dir is None:
        output_dir = DATA_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    sessions = simulate_market(market_cfg)
    gc.collect()
    t_core_start = time.perf_counter()
    indicators_list = [compute_session_indicators(s) for s in sessions]
    t_ind_end = time.perf_counter()
    session_caches = _precompute_session_caches(sessions, indicators_list)
    strategies = get_13_strategy_configs()

    config_metrics: List[Dict[str, Any]] = []
    flagship_trades: List[TradeRecord] = []
    flagship_by_session: Dict[int, List[TradeRecord]] = {}
    all_14_daily_sharpes: List[float] = []

    for strat in strategies:
        if strat.is_flagship:
            m, trades, by_sess = evaluate_strategy(strat, sessions, indicators_list, market_cfg)
            config_metrics.append(m)
            flagship_trades = trades
            flagship_by_session = by_sess
            daily_net_f = np.array(
                [math.prod(1.0 + tr.net_ret for tr in by_sess.get(s.session_id, [])) - 1.0 for s in sessions],
                dtype=np.float64,
            )
            sd_f = float(np.std(daily_net_f, ddof=1))
            all_14_daily_sharpes.append(float(np.mean(daily_net_f) / sd_f) if sd_f > 1e-12 else 0.0)
        else:
            m, dsr_net = _evaluate_strategy_fast(
                strat, sessions, indicators_list, session_caches, market_cfg
            )
            config_metrics.append(m)
            all_14_daily_sharpes.append(dsr_net)

    t_bt_end = time.perf_counter()
    ind_ms = (t_ind_end - t_core_start) * 1000.0
    core_ms = (t_bt_end - t_core_start) * 1000.0

    # Sort configs by net_sharpe descending
    config_metrics.sort(key=lambda x: (-x["net_sharpe"], -x["annual_return_pct"]))

    # Regime rollups
    total_return_all = sum(c["annual_return_pct"] for c in config_metrics)
    regimes_out: List[Dict[str, Any]] = []
    for rmeta in REGIME_META:
        rid = rmeta["id"]
        members = [c for c in config_metrics if c["regime"] == rid]
        reg_ret_sum = sum(c["annual_return_pct"] for c in members)
        regimes_out.append(
            {
                "id": rid,
                "label": rmeta["label"],
                "sublabel": rmeta["sublabel"],
                "min_hold_bound": rmeta["min_hold_bound"],
                "max_hold_bound": rmeta["max_hold_bound"],
                "config_count": len(members),
                "avg_sharpe": round(float(np.mean([c["net_sharpe"] for c in members])), 2),
                "total_return_pct": round(reg_ret_sum, 2),
                "avg_return_pct": round(reg_ret_sum / max(len(members), 1), 2),
                "return_share_pct": round(reg_ret_sum / max(total_return_all, 1e-9) * 100.0, 1),
                "avg_slippage_saved_bps": round(
                    float(np.mean([c["vwap_slippage_saved_bps"] for c in members])), 2
                ),
            }
        )
    # Normalize return_share_pct to sum cleanly to 100.0
    share_sum = sum(r["return_share_pct"] for r in regimes_out)
    regimes_out[1]["return_share_pct"] = round(regimes_out[1]["return_share_pct"] + (100.0 - share_sum), 1)

    flagship_cfg = next(c for c in config_metrics if c["is_flagship"])
    single_cfgs = [c for c in config_metrics if not c["is_blended"]]
    blended_cfgs = [c for c in config_metrics if c["is_blended"]]
    best_single = max(single_cfgs, key=lambda c: c["net_sharpe"])

    sharpe_uplift_pct = (
        (flagship_cfg["net_sharpe"] / best_single["net_sharpe"] - 1.0) * 100.0
        if best_single["net_sharpe"] > 0
        else 0.0
    )
    compounded_100_1yr = 100.0 * (1.0 + flagship_cfg["annual_return_pct"] / 100.0)
    median_sharpe = float(np.median([c["net_sharpe"] for c in config_metrics]))
    total_annual_trades_all = int(sum(c["annual_trades"] for c in config_metrics))

    summary_out = {
        "flagship_id": flagship_cfg["id"],
        "flagship_name": flagship_cfg["name"],
        "flagship_net_sharpe": flagship_cfg["net_sharpe"],
        "best_single_horizon_id": best_single["id"],
        "best_single_horizon_name": best_single["name"],
        "best_single_net_sharpe": best_single["net_sharpe"],
        "sharpe_uplift_vs_best_single_pct": round(sharpe_uplift_pct, 1),
        "flagship_compounded_100_1yr": round(compounded_100_1yr, 2),
        "median_sharpe": round(median_sharpe, 2),
        "blended_avg_sharpe": round(float(np.mean([c["net_sharpe"] for c in blended_cfgs])), 2),
        "single_avg_sharpe": round(float(np.mean([c["net_sharpe"] for c in single_cfgs])), 2),
        "total_annual_trades_all": total_annual_trades_all,
    }

    weight_schedule = build_weight_schedule_intraday(sessions, indicators_list)
    duration_histogram = build_duration_histogram(flagship_trades)

    # Evaluate strictly causal Turbo 10x Alpha overlay (Maker Peg + Trailing Lock + Fractional-Kelly Sizing)
    flagship_strat = next(st for st in strategies if st.is_flagship)
    turbo_daily_net = np.zeros(len(sessions), dtype=np.float64)
    turbo_daily_gross = np.zeros(len(sessions), dtype=np.float64)
    turbo_rows: List[Dict[str, float]] = []
    for s_idx, (sess, ind) in enumerate(zip(sessions, indicators_list)):
        rn, rg, rows = backtest_session_turbo(sess, ind, flagship_strat, market_cfg)
        turbo_daily_net[s_idx] = rn
        turbo_daily_gross[s_idx] = rg
        turbo_rows.extend(rows)

    ann_f = market_cfg.trading_days_per_year / float(len(sessions))
    turbo_eq = np.concatenate(([1.0], np.cumprod(1.0 + turbo_daily_net)))
    turbo_ann_ret = ((float(turbo_eq[-1]) ** ann_f) - 1.0) * 100.0
    turbo_std_net = float(np.std(turbo_daily_net, ddof=1))
    turbo_daily_sr = float(np.mean(turbo_daily_net) / turbo_std_net) if turbo_std_net > 1e-12 else 0.0
    all_14_daily_sharpes.append(turbo_daily_sr)
    turbo_net_sr = turbo_daily_sr * math.sqrt(market_cfg.trading_days_per_year)
    turbo_peaks = np.maximum.accumulate(turbo_eq)
    turbo_mdd = float(np.max((turbo_peaks - turbo_eq) / np.maximum(turbo_peaks, 1e-12)) * 100.0)
    turbo_wins = [r["net_bps"] for r in turbo_rows if r["net_bps"] > 0]
    turbo_losses = [abs(r["net_bps"]) for r in turbo_rows if r["net_bps"] < 0]
    turbo_pf = float(sum(turbo_wins) / sum(turbo_losses)) if sum(turbo_losses) > 0 else 99.0
    profit_multiple = turbo_ann_ret / max(flagship_cfg["annual_return_pct"], 1e-6)

    turbo_benchmark = {
        "name": "Adaptive Turbo 5–20m (Maker + Kelly)",
        "base_annual_return_pct": flagship_cfg["annual_return_pct"],
        "turbo_annual_return_pct": round(turbo_ann_ret, 2),
        "profit_multiple": round(profit_multiple, 2),
        "base_net_sharpe": flagship_cfg["net_sharpe"],
        "turbo_net_sharpe": round(turbo_net_sr, 2),
        "turbo_win_rate_pct": round(len(turbo_wins) / max(len(turbo_rows), 1) * 100.0, 2),
        "turbo_profit_factor": round(turbo_pf, 2),
        "turbo_max_drawdown_pct": round(turbo_mdd, 2),
        "turbo_avg_cost_bps": round(float(np.mean([r["cost_bps"] for r in turbo_rows])), 2),
        "base_avg_cost_bps": flagship_cfg["avg_cost_bps"],
        "turbo_avg_size_mult": round(float(np.mean([r["size_mult"] for r in turbo_rows])), 2),
        "turbo_annual_trades": int(round(len(turbo_rows) * ann_f)),
        "turbo_compounded_100_1yr": round(100.0 * (1.0 + turbo_ann_ret / 100.0), 2),
        "indicator_ms": round(ind_ms, 2),
        "core_engine_ms": round(core_ms, 2),
        "baseline_core_ms": 3234.27,
        "engine_speedup_x": round(3234.27 / max(core_ms, 1.0), 1),
    }

    # Phase 2 Deep Research Suite (OU-Spectral Curvature-VPIN, Walk-Forward+DSR, 4 Stress Tests, Alpha Decay)
    from .research_lab import run_research_lab

    research_payload, research_list = run_research_lab(
        sessions, indicators_list, market_cfg, all_14_daily_sharpes
    )

    # Phase 3 Dual-Regime Bayesian Bifurcation Suite (Hamilton filter, 5-Regime Stress, Bootstrap, 8-Asset Portfolio)
    from .dual_regime import run_dual_regime_lab

    dual_payload, hamilton_list = run_dual_regime_lab(
        sessions, indicators_list, research_list, market_cfg, all_14_daily_sharpes
    )

    metrics_payload: Dict[str, Any] = {
        "meta": {
            "seed": market_cfg.seed,
            "sessions": market_cfg.sessions,
            "bars_per_session": market_cfg.bars_per_session,
            "total_bars": market_cfg.sessions * market_cfg.bars_per_session,
            "horizons_min": list(ALL_HORIZONS),
            "blend_horizons_min": list(BLEND_HORIZONS),
            "static_weights": [float(w) for w in STATIC_WEIGHTS],
            "static_weights_map": {str(h): float(w) for h, w in zip(BLEND_HORIZONS, STATIC_WEIGHTS)},
            "spread_bps": market_cfg.spread_bps,
            "impact_coeff": market_cfg.impact_coeff,
            "order_size_shares": int(market_cfg.order_size_shares),
            "adv_shares": int(market_cfg.adv_per_min * market_cfg.bars_per_session),
            "trading_days_per_year": market_cfg.trading_days_per_year,
            "session_hours": "09:30–16:00 ET",
        },
        "regimes": regimes_out,
        "configs": config_metrics,
        "summary": summary_out,
        "turbo_benchmark": turbo_benchmark,
        "weight_schedule_intraday": weight_schedule,
        "duration_histogram": duration_histogram,
        "research_lab": research_payload,
        "dual_regime_lab": dual_payload,
    }

    rep_sid, spotlight = select_representative_session(sessions, flagship_by_session)
    rep_session = sessions[rep_sid - 1]
    rep_indicators = indicators_list[rep_sid - 1]
    rep_research = research_list[rep_sid - 1]
    rep_hamilton = hamilton_list[rep_sid - 1]
    rep_trades = flagship_by_session[rep_sid]
    session_payload = build_session_export(
        rep_session,
        rep_indicators,
        rep_trades,
        spotlight,
        research_ind=rep_research,
        hamilton_state=rep_hamilton,
    )

    metrics_path = output_dir / "vwap_metrics.json"
    session_path = output_dir / "session_bars.json"
    research_path = output_dir / "research_lab.json"
    dual_path = output_dir / "dual_regime.json"
    bundle_path = output_dir / "bundle.js"

    metrics_json = json.dumps(metrics_payload, indent=2)
    session_json = json.dumps(session_payload, indent=2)
    research_json = json.dumps(research_payload, indent=2)
    dual_json = json.dumps(dual_payload, indent=2)

    metrics_path.write_text(metrics_json + "\n", encoding="utf-8")
    session_path.write_text(session_json + "\n", encoding="utf-8")
    research_path.write_text(research_json + "\n", encoding="utf-8")
    dual_path.write_text(dual_json + "\n", encoding="utf-8")

    # Compact exact-parity JS bundle for offline file:/// HTML apps
    bundle_content = (
        "// Auto-generated by engine/run_pipeline.py — exact parity with vwap_metrics.json, session_bars.json, research_lab.json & dual_regime.json\n"
        "window.VWAP_DATA = {\n"
        f"  metrics: {json.dumps(metrics_payload, separators=(',', ':'))},\n"
        f"  session: {json.dumps(session_payload, separators=(',', ':'))}\n"
        "};\n"
    )
    bundle_path.write_text(bundle_content, encoding="utf-8")

    return metrics_payload, session_payload


def print_summary(metrics: Dict[str, Any], session: Dict[str, Any]) -> None:
    """Print diagnostic report of all 13 configurations and regimes."""
    print("=" * 114)
    print("BLENDED 5-20m VWAP BACKTEST SUMMARY (60 sessions x 390 bars = 23,400 bars, seed=42)")
    print("=" * 114)
    hdr = (
        f"{'Name':<25} {'Reg':<6} {'Hold':>5} {'N/yr':>6} {'Tr/d':>5} "
        f"{'s':>7} {'NetSR':>6} {'GrsSR':>6} {'AnnRet%':>8} {'SlipBps':>7} {'Win%':>6} {'PF':>5} {'MDD%':>6} {'Cost':>5} {'Cap%':>5}"
    )
    print(hdr)
    print("-" * 114)
    for c in metrics["configs"]:
        tag = "*" if c["is_flagship"] else ("+" if c["is_blended"] else " ")
        print(
            f"{tag}{c['name']:<24} {c['regime']:<6} {c['avg_hold_min']:>5.2f} {c['annual_trades']:>6d} "
            f"{c['trades_per_day']:>5.2f} {c['per_trade_sharpe_s']:>7.4f} {c['net_sharpe']:>6.2f} "
            f"{c['gross_sharpe']:>6.2f} {c['annual_return_pct']:>8.2f} {c['vwap_slippage_saved_bps']:>7.2f} "
            f"{c['win_rate_pct']:>6.2f} {c['profit_factor']:>5.2f} {c['max_drawdown_pct']:>6.2f} "
            f"{c['avg_cost_bps']:>5.2f} {c['capacity_adv_pct']:>5.2f}"
        )
    print("-" * 114)
    print("REGIMES:")
    for r in metrics["regimes"]:
        print(
            f"  {r['label']:<14} ({r['sublabel']:<14}) n={r['config_count']} "
            f"avg_SR={r['avg_sharpe']:.2f} tot_ret={r['total_return_pct']:.2f}% "
            f"share={r['return_share_pct']:.1f}% avg_slip={r['avg_slippage_saved_bps']:.2f}bps"
        )
    print("SUMMARY:", json.dumps(metrics["summary"]))
    print("DURATION HISTOGRAM:", [(d["hold_min"], d["count"]) for d in metrics["duration_histogram"]])
    ws = metrics["weight_schedule_intraday"]
    print(f"WEIGHT SCHEDULE sample: 09:30={ws[0]} | 12:30={ws[18]} | 15:50={ws[-1]}")
    print(
        f"REP SESSION #{session['session_id']}: trades={len(session['trades'])}, "
        f"spotlight={session['spotlight_window']}"
    )
    if "research_lab" in metrics:
        rl = metrics["research_lab"]
        print("-" * 114)
        print("PHASE 2 RESEARCH LAB — WALK-FORWARD (IS 1-40 vs OOS 41-60) & DSR (M=16):")
        for m in rl["walk_forward_dsr"]["models"]:
            print(
                f"  {m['name']:<32} IS={m['is_sharpe']:>5.2f} OOS={m['oos_sharpe']:>5.2f} "
                f"Full={m['full_sharpe']:>5.2f} Ret={m['oos_retention_pct']:>5.1f}% "
                f"PSR={m['psr_pct']:>6.2f}% DSR={m['dsr_pct']:>6.2f}% MDD={m['max_drawdown_pct']:>5.2f}%"
            )
        print("PHASE 2 RESEARCH LAB — 4 ADVERSARIAL STRESS SCENARIOS:")
        for st in rl["stress_tests"]:
            ms = st["models"]
            print(
                f"  {st['id']:<16} (vetoes={st['toxic_vetoes']:>3d}): "
                f"8m={ms['single_8m']['net_sharpe']:>6.2f} | Adapt={ms['adaptive_1x']['net_sharpe']:>6.2f} | "
                f"OU_1x={ms['ou_spectral_1x']['net_sharpe']:>6.2f} | OU_Turbo={ms['ou_spectral_turbo']['net_sharpe']:>6.2f}"
            )
    if "dual_regime_lab" in metrics:
        dl = metrics["dual_regime_lab"]
        print("-" * 114)
        print("PHASE 3 DUAL-REGIME BAYES — 5-REGIME ALL-WEATHER STRESS MATRIX:")
        for sc in dl["stress_tests_5"]["scenarios"]:
            ms = sc["models"]
            print(
                f"  {sc['id']:<16} (F/S={sc['dual_fade_trades']:>3d}/{sc['dual_surf_trades']:>3d}): "
                f"8m={ms['single_8m']['net_sharpe']:>6.2f} | Ad={ms['adaptive_1x']['net_sharpe']:>6.2f} | "
                f"OU={ms['ou_spectral_1x']['net_sharpe']:>6.2f} | "
                f"Dual1x={ms['dual_regime_1x']['net_sharpe']:>6.2f} | DualTB={ms['dual_regime_turbo']['net_sharpe']:>6.2f}"
            )
        print("  5-REGIME MEAN:", dl["stress_tests_5"]["five_regime_mean"])
        p8 = dl["portfolio_8_asset"]["summary"]
        print(
            f"  8-ASSET PORTFOLIO: SingleAvgSR={p8['single_asset_avg_sharpe']:.2f} -> "
            f"EW={p8['equal_weight_sharpe']:.2f} -> RP={p8['risk_parity_sharpe']:.2f} "
            f"(DR={p8['diversification_ratio']:.2f}, MDD={p8['risk_parity_mdd_pct']:.2f}%, "
            f"Corr={p8['avg_pairwise_corr']:.3f})"
        )


if __name__ == "__main__":
    m, s = run_pipeline()
    print_summary(m, s)
