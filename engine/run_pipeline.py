"""End-to-end deterministic pipeline:
1. Simulate 60 sessions x 390 1-minute bars (seed=42)
2. Compute strictly causal rolling VWAPs & 3 Blended 5-20m models
3. Backtest all 13 trade-window configurations across 3 regimes
4. Export data/vwap_metrics.json, data/session_bars.json, and data/bundle.js
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple
import numpy as np

from .simulator import MarketConfig, SessionData, simulate_market
from .blended_vwap import (
    ALL_HORIZONS,
    BLEND_HORIZONS,
    STATIC_WEIGHTS,
    SessionIndicators,
    StrategyConfig,
    TradeRecord,
    backtest_session,
    compute_session_indicators,
    get_13_strategy_configs,
)

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
) -> Dict[str, Any]:
    """Serialize full 390-bar representative session + flagship trades."""
    bars: List[Dict[str, Any]] = []
    n = len(session.close)
    for t in range(n):
        w = indicators.weights_adaptive[t]
        w_r = [round(float(x), 4) for x in w]
        w_r[3] = round(1.0 - (w_r[0] + w_r[1] + w_r[2]), 4)
        bars.append(
            {
                "idx": t,
                "time": session.times[t],
                "open": round(float(session.open[t]), 4),
                "high": round(float(session.high[t]), 4),
                "low": round(float(session.low[t]), 4),
                "close": round(float(session.close[t]), 4),
                "volume": int(round(float(session.volume[t]))),
                "rel_vol": round(float(session.rel_vol[t]), 3),
                "vwap5": round(float(indicators.vwap_by_h[5][t]), 4),
                "vwap10": round(float(indicators.vwap_by_h[10][t]), 4),
                "vwap15": round(float(indicators.vwap_by_h[15][t]), 4),
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
        )

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
    indicators_list = [compute_session_indicators(s) for s in sessions]
    strategies = get_13_strategy_configs()

    config_metrics: List[Dict[str, Any]] = []
    flagship_trades: List[TradeRecord] = []
    flagship_by_session: Dict[int, List[TradeRecord]] = {}

    for strat in strategies:
        m, trades, by_sess = evaluate_strategy(strat, sessions, indicators_list, market_cfg)
        config_metrics.append(m)
        if strat.is_flagship:
            flagship_trades = trades
            flagship_by_session = by_sess

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
        "weight_schedule_intraday": weight_schedule,
        "duration_histogram": duration_histogram,
    }

    rep_sid, spotlight = select_representative_session(sessions, flagship_by_session)
    rep_session = sessions[rep_sid - 1]
    rep_indicators = indicators_list[rep_sid - 1]
    rep_trades = flagship_by_session[rep_sid]
    session_payload = build_session_export(rep_session, rep_indicators, rep_trades, spotlight)

    metrics_path = output_dir / "vwap_metrics.json"
    session_path = output_dir / "session_bars.json"
    bundle_path = output_dir / "bundle.js"

    metrics_json = json.dumps(metrics_payload, indent=2)
    session_json = json.dumps(session_payload, indent=2)

    metrics_path.write_text(metrics_json + "\n", encoding="utf-8")
    session_path.write_text(session_json + "\n", encoding="utf-8")

    # Compact exact-parity JS bundle for offline file:/// HTML apps
    bundle_content = (
        "// Auto-generated by engine/run_pipeline.py — exact parity with vwap_metrics.json & session_bars.json\n"
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


if __name__ == "__main__":
    m, s = run_pipeline()
    print_summary(m, s)
