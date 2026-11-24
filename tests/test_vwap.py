"""Comprehensive unit test suite for the Blended 5-20m VWAP engine and data exports."""

from __future__ import annotations

import json
import math
import re
import unittest
from pathlib import Path
import numpy as np

from engine.simulator import (
    MarketConfig,
    SessionData,
    compute_side_cost_bps,
    intraday_volume_profile,
    simulate_market,
)
from engine.blended_vwap import (
    ALL_HORIZONS,
    BLEND_HORIZONS,
    STATIC_WEIGHTS,
    backtest_session,
    backtest_session_turbo,
    compute_entry_signals,
    compute_rolling_vwap_and_dispersion,
    compute_session_indicators,
    get_13_strategy_configs,
)
from engine.research_lab import (
    ALPHA_DECAY_HORIZONS,
    backtest_session_ou_spectral,
    compute_ou_spectral_entry_signals,
    compute_research_indicators,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


class TestBlendedVwapEngine(unittest.TestCase):
    """Unit and integration tests for simulator, causal VWAP math, and outputs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = MarketConfig(seed=42)
        cls.sessions = simulate_market(cls.cfg)
        cls.sample_session = cls.sessions[0]
        cls.sample_ind = compute_session_indicators(cls.sample_session)
        cls.strategies = get_13_strategy_configs()

    def test_01_deterministic_reproducibility(self) -> None:
        """Simulating twice with seed=42 yields bit-for-bit identical OHLCV bars."""
        cfg_small = MarketConfig(seed=42, sessions=5)
        run_a = simulate_market(cfg_small)
        run_b = simulate_market(cfg_small)
        self.assertEqual(len(run_a), len(run_b))
        for sa, sb in zip(run_a, run_b):
            np.testing.assert_array_equal(sa.open, sb.open)
            np.testing.assert_array_equal(sa.high, sb.high)
            np.testing.assert_array_equal(sa.low, sb.low)
            np.testing.assert_array_equal(sa.close, sb.close)
            np.testing.assert_array_equal(sa.volume, sb.volume)

    def test_02_ohlc_and_u_shaped_volume_invariants(self) -> None:
        """OHLC bars obey High >= max(O,C), Low <= min(O,C), and volume is U-shaped."""
        self.assertEqual(len(self.sessions), 60)
        for sess in self.sessions[:10]:
            self.assertEqual(len(sess.close), 390)
            self.assertTrue(np.all(sess.high >= np.maximum(sess.open, sess.close)))
            self.assertTrue(np.all(sess.low <= np.minimum(sess.open, sess.close)))
            self.assertTrue(np.all(sess.volume > 0))

        u_prof = intraday_volume_profile(390)
        self.assertAlmostEqual(float(np.mean(u_prof)), 1.0, places=6)
        open_avg = float(np.mean(u_prof[:20]))
        midday_avg = float(np.mean(u_prof[150:240]))
        close_avg = float(np.mean(u_prof[-20:]))
        self.assertGreater(open_avg, midday_avg * 2.0)
        self.assertGreater(close_avg, midday_avg * 1.8)

    def test_03_exact_rolling_vwap_and_dispersion_formula(self) -> None:
        """Verify rolling VWAP and volume-weighted dispersion against naive formula."""
        tp = np.array([100.0, 100.5, 101.0, 100.2, 99.8, 100.4, 101.2, 100.9], dtype=np.float64)
        vol = np.array([1000.0, 2500.0, 1500.0, 4000.0, 3000.0, 2000.0, 5000.0, 1200.0], dtype=np.float64)

        for h in (5, 6, 8):
            vw, sg = compute_rolling_vwap_and_dispersion(tp, vol, h)
            for t in range(len(tp)):
                lo = max(0, t - h + 1)
                p_slice = tp[lo : t + 1]
                v_slice = vol[lo : t + 1]
                expected_vw = float(np.sum(p_slice * v_slice) / np.sum(v_slice))
                expected_sg = float(
                    math.sqrt(np.sum(v_slice * (p_slice - expected_vw) ** 2) / np.sum(v_slice))
                )
                self.assertAlmostEqual(float(vw[t]), expected_vw, places=12)
                self.assertAlmostEqual(float(sg[t]), expected_sg, places=12)

        # Also check all 9 required horizons are computed on simulated sessions
        self.assertEqual(tuple(sorted(self.sample_ind.vwap_by_h.keys())), ALL_HORIZONS)

    def test_04_weight_normalization_and_adaptive_regime_tilt(self) -> None:
        """All 3 blended models satisfy sum(w_h) == 1 and w_h >= 0 at every bar."""
        for weights in (
            self.sample_ind.weights_static,
            self.sample_ind.weights_vol,
            self.sample_ind.weights_adaptive,
        ):
            self.assertEqual(weights.shape, (390, 4))
            self.assertTrue(np.all(weights >= 0.0))
            self.assertTrue(np.all(np.isfinite(weights)))
            row_sums = np.sum(weights, axis=1)
            np.testing.assert_allclose(row_sums, np.ones(390), atol=1e-12)

        # Exact static weights check
        np.testing.assert_allclose(self.sample_ind.weights_static[0], STATIC_WEIGHTS, atol=1e-12)

        # Adaptive blend tilts toward 5m during open surge and toward 20m during midday lull
        all_w = np.stack([compute_session_indicators(s).weights_adaptive for s in self.sessions[:20]], axis=0)
        open_w5 = float(np.mean(all_w[:, :20, 0]))
        midday_w5 = float(np.mean(all_w[:, 150:240, 0]))
        open_w20 = float(np.mean(all_w[:, :20, 3]))
        midday_w20 = float(np.mean(all_w[:, 150:240, 3]))
        self.assertGreater(open_w5, midday_w5 + 0.10)
        self.assertGreater(midday_w20, open_w20 + 0.08)

    def test_05_strict_causality_zero_lookahead(self) -> None:
        """Perturbing bars > t0 must NOT change any VWAP, weight, z-score, or signal at <= t0."""
        t0 = 180
        orig = self.sample_session
        ind_orig = self.sample_ind

        # Heavily perturb all future bars t > t0
        pert = SessionData(
            session_id=orig.session_id,
            times=list(orig.times),
            open=orig.open.copy(),
            high=orig.high.copy(),
            low=orig.low.copy(),
            close=orig.close.copy(),
            volume=orig.volume.copy(),
            rel_vol=orig.rel_vol.copy(),
            vol_regime=orig.vol_regime.copy(),
            transient_impact_bps=orig.transient_impact_bps.copy(),
        )
        pert.open[t0 + 1 :] *= 1.35
        pert.high[t0 + 1 :] *= 1.42
        pert.low[t0 + 1 :] *= 0.72
        pert.close[t0 + 1 :] *= 1.28
        pert.volume[t0 + 1 :] *= 7.5
        pert.rel_vol[t0 + 1 :] *= 7.5

        ind_pert = compute_session_indicators(pert)

        # Every indicator for indices 0..t0 must match identically
        for h in ALL_HORIZONS:
            np.testing.assert_allclose(ind_orig.vwap_by_h[h][: t0 + 1], ind_pert.vwap_by_h[h][: t0 + 1], atol=1e-13)
            np.testing.assert_allclose(ind_orig.sigma_by_h[h][: t0 + 1], ind_pert.sigma_by_h[h][: t0 + 1], atol=1e-13)
            np.testing.assert_allclose(ind_orig.z_by_h[h][: t0 + 1], ind_pert.z_by_h[h][: t0 + 1], atol=1e-13)

        np.testing.assert_allclose(ind_orig.weights_vol[: t0 + 1], ind_pert.weights_vol[: t0 + 1], atol=1e-13)
        np.testing.assert_allclose(ind_orig.weights_adaptive[: t0 + 1], ind_pert.weights_adaptive[: t0 + 1], atol=1e-13)
        np.testing.assert_allclose(ind_orig.vwap_adaptive[: t0 + 1], ind_pert.vwap_adaptive[: t0 + 1], atol=1e-13)
        np.testing.assert_allclose(ind_orig.sigma_adaptive[: t0 + 1], ind_pert.sigma_adaptive[: t0 + 1], atol=1e-13)
        np.testing.assert_allclose(ind_orig.z_adaptive[: t0 + 1], ind_pert.z_adaptive[: t0 + 1], atol=1e-13)
        np.testing.assert_allclose(ind_orig.causal_bar_sigma[: t0 + 1], ind_pert.causal_bar_sigma[: t0 + 1], atol=1e-13)

        # Every strategy's causal entry signals for indices 0..t0 must match identically
        for strat in self.strategies:
            sig_orig = compute_entry_signals(orig, ind_orig, strat)
            sig_pert = compute_entry_signals(pert, ind_pert, strat)
            np.testing.assert_array_equal(sig_orig[: t0 + 1], sig_pert[: t0 + 1])

    def test_06_positive_transaction_cost_model(self) -> None:
        """Spread + square-root impact is positive and monotonic in q, sigma, and 1/V."""
        c_base = compute_side_cost_bps(0.0006, 50_000.0, 1_200.0, spread_bps=0.60, impact_coeff=0.42)
        c_big_q = compute_side_cost_bps(0.0006, 50_000.0, 4_800.0, spread_bps=0.60, impact_coeff=0.42)
        c_hi_vol = compute_side_cost_bps(0.0012, 50_000.0, 1_200.0, spread_bps=0.60, impact_coeff=0.42)
        c_deep_v = compute_side_cost_bps(0.0006, 200_000.0, 1_200.0, spread_bps=0.60, impact_coeff=0.42)

        self.assertGreater(c_base, 0.30)  # strictly above half-spread
        self.assertGreater(c_big_q, c_base)
        self.assertGreater(c_hi_vol, c_base)
        self.assertLess(c_deep_v, c_base)

        # Backtested flagship trades all incur positive costs
        flagship = next(s for s in self.strategies if s.is_flagship)
        trades = backtest_session(self.sample_session, self.sample_ind, flagship, self.cfg)
        self.assertGreater(len(trades), 0)
        for tr in trades:
            self.assertGreater(tr.cost_bps, 0.60)
            self.assertAlmostEqual(tr.gross_bps - tr.cost_bps, tr.net_bps, places=8)
            self.assertGreaterEqual(tr.hold_min, 5)
            self.assertLessEqual(tr.hold_min, 20)

    def test_07_exported_metrics_regimes_and_bundle_parity(self) -> None:
        """Verify exported JSON files and bundle.js meet every schema and economic invariant."""
        metrics_path = DATA_DIR / "vwap_metrics.json"
        session_path = DATA_DIR / "session_bars.json"
        bundle_path = DATA_DIR / "bundle.js"

        self.assertTrue(metrics_path.exists(), "data/vwap_metrics.json missing")
        self.assertTrue(session_path.exists(), "data/session_bars.json missing")
        self.assertTrue(bundle_path.exists(), "data/bundle.js missing")

        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        session = json.loads(session_path.read_text(encoding="utf-8"))

        # Meta invariants
        self.assertEqual(metrics["meta"]["seed"], 42)
        self.assertEqual(metrics["meta"]["sessions"], 60)
        self.assertEqual(metrics["meta"]["bars_per_session"], 390)
        self.assertEqual(metrics["meta"]["total_bars"], 23400)

        # 13 configs sorted descending by net_sharpe
        configs = metrics["configs"]
        self.assertEqual(len(configs), 13)
        sharpes = [c["net_sharpe"] for c in configs]
        self.assertEqual(sharpes, sorted(sharpes, reverse=True))

        # Regime bucket counts and avg_hold_min bounds
        bounds = {"micro": (5.0, 8.0), "core": (9.0, 14.0), "anchor": (15.0, 20.0)}
        counts = {"micro": 0, "core": 0, "anchor": 0}
        coords = []
        for c in configs:
            self.assertTrue(math.isfinite(c["net_sharpe"]))
            self.assertTrue(math.isfinite(c["avg_hold_min"]))
            self.assertGreater(c["net_sharpe"], 0.0, f"{c['id']} net_sharpe must be positive")
            self.assertGreater(c["gross_sharpe"], c["net_sharpe"], f"{c['id']} cost must reduce Sharpe")
            self.assertGreater(c["avg_cost_bps"], 0.60)
            self.assertGreater(c["vwap_slippage_saved_bps"], 0.0)
            lo, hi = bounds[c["regime"]]
            self.assertGreaterEqual(c["avg_hold_min"], lo, f"{c['id']} hold below regime min")
            self.assertLessEqual(c["avg_hold_min"], hi, f"{c['id']} hold above regime max")
            counts[c["regime"]] += 1
            coords.append((c["avg_hold_min"], c["net_sharpe"]))

        self.assertEqual(counts, {"micro": 4, "core": 5, "anchor": 4})

        # Ensure no degenerate overlapping (avg_hold_min, net_sharpe) pairs
        for i in range(len(coords)):
            for j in range(i + 1, len(coords)):
                dx = (coords[i][0] - coords[j][0]) / 15.0
                dy = (coords[i][1] - coords[j][1]) / 3.0
                dist = math.hypot(dx, dy)
                self.assertGreater(dist, 0.04, f"Configs {i} and {j} clumped: {coords[i]} vs {coords[j]}")

        # Blended 5-20m outperforms single-horizon peers on net Sharpe & slippage saved
        blended = [c for c in configs if c["is_blended"]]
        singles = [c for c in configs if not c["is_blended"]]
        flagship = next(c for c in configs if c["is_flagship"])
        self.assertEqual(len(blended), 3)
        self.assertEqual(flagship["id"], "adaptive_blend_5_20")
        self.assertEqual(configs[0]["id"], "adaptive_blend_5_20")
        self.assertGreater(
            min(c["net_sharpe"] for c in blended),
            max(c["net_sharpe"] for c in singles),
            "All 3 blended 5-20m models should beat isolated single-horizon peers on net Sharpe",
        )
        self.assertGreater(
            float(np.mean([c["vwap_slippage_saved_bps"] for c in blended])),
            float(np.mean([c["vwap_slippage_saved_bps"] for c in singles])),
        )

        # Weight schedule (39 points) & duration histogram (5..20)
        ws = metrics["weight_schedule_intraday"]
        self.assertEqual(len(ws), 39)
        for pt in ws:
            self.assertAlmostEqual(pt["w5"] + pt["w10"] + pt["w15"] + pt["w20"], 1.0, places=3)

        dh = metrics["duration_histogram"]
        self.assertEqual([d["hold_min"] for d in dh], list(range(5, 21)))
        self.assertGreater(sum(d["count"] for d in dh), 0)

        # Session bars export checks
        self.assertEqual(len(session["bars"]), 390)
        self.assertGreater(len(session["trades"]), 0)
        sw = session["spotlight_window"]
        self.assertGreaterEqual(sw["start_idx"], 0)
        self.assertLess(sw["end_idx"], 390)
        self.assertGreaterEqual(sw["end_idx"] - sw["start_idx"], 60)

        # Verify bundle.js exact data parity with JSON files
        bundle_txt = bundle_path.read_text(encoding="utf-8")
        self.assertIn("window.VWAP_DATA =", bundle_txt)
        m_match = re.search(r"metrics:\s*(\{.*\}),\s*\n\s*session:\s*(\{.*\})\s*\n\};", bundle_txt, re.S)
        self.assertIsNotNone(m_match, "bundle.js structure did not match expected format")
        bundle_metrics = json.loads(m_match.group(1))
        bundle_session = json.loads(m_match.group(2))
        self.assertEqual(bundle_metrics, metrics)
        self.assertEqual(bundle_session, session)

    def test_08_turbo_alpha_profitability_and_speed_invariants(self) -> None:
        """Verify Turbo 10x Alpha overlay is strictly causal, positive-cost, and >10x profitable + >10x faster."""
        metrics = json.loads((DATA_DIR / "vwap_metrics.json").read_text(encoding="utf-8"))
        tb = metrics["turbo_benchmark"]
        self.assertGreaterEqual(tb["profit_multiple"], 10.0)
        self.assertGreater(tb["turbo_net_sharpe"], tb["base_net_sharpe"])
        self.assertGreater(tb["turbo_avg_cost_bps"], 0.25)
        self.assertLess(tb["turbo_avg_cost_bps"], tb["base_avg_cost_bps"])
        self.assertGreaterEqual(tb["engine_speedup_x"], 10.0)

        # Verify causal zero-lookahead on backtest_session_turbo: perturbing bars > t0 does not alter earlier trades
        t0 = 180
        orig = self.sample_session
        pert = SessionData(
            session_id=orig.session_id,
            times=list(orig.times),
            open=orig.open.copy(),
            high=orig.high.copy(),
            low=orig.low.copy(),
            close=orig.close.copy(),
            volume=orig.volume.copy(),
            rel_vol=orig.rel_vol.copy(),
            vol_regime=orig.vol_regime.copy(),
            transient_impact_bps=orig.transient_impact_bps.copy(),
        )
        pert.open[t0 + 1 :] *= 1.35
        pert.high[t0 + 1 :] *= 1.42
        pert.low[t0 + 1 :] *= 0.72
        pert.close[t0 + 1 :] *= 1.28
        pert.volume[t0 + 1 :] *= 7.5
        pert.rel_vol[t0 + 1 :] *= 7.5
        flagship = next(s for s in self.strategies if s.is_flagship)
        _, _, rows_o = backtest_session_turbo(orig, self.sample_ind, flagship, self.cfg)
        _, _, rows_p = backtest_session_turbo(pert, compute_session_indicators(pert), flagship, self.cfg)
        # First trades finishing well before t0 must match identically
        self.assertGreater(len(rows_o), 0)
        self.assertAlmostEqual(rows_o[0]["scaled_net_bps"], rows_p[0]["scaled_net_bps"], places=10)

    def test_09_research_lab_causality_and_invariants(self) -> None:
        """Verify Phase 2 OU-Spectral, Curvature, VPIN, Hurst causality, invariants, and JSON/bundle parity."""
        t0 = 180
        orig = self.sample_session
        ind_orig = self.sample_ind
        res_orig = compute_research_indicators(orig, ind_orig)

        # Mathematical bounds & weight normalization on all 390 bars
        self.assertEqual(res_orig.weights_ou.shape, (390, 4))
        self.assertTrue(np.all(np.isfinite(res_orig.weights_ou)))
        self.assertTrue(np.all(res_orig.weights_ou > 0.0))
        np.testing.assert_allclose(np.sum(res_orig.weights_ou, axis=1), np.ones(390), atol=1e-12)

        self.assertTrue(np.all(np.isfinite(res_orig.tau_half)))
        self.assertTrue(np.all((res_orig.tau_half >= 4.5) & (res_orig.tau_half <= 22.0)))
        self.assertTrue(np.all((res_orig.vpin >= 0.0) & (res_orig.vpin <= 1.0)))
        self.assertTrue(np.all((res_orig.signed_ofi >= -1.0) & (res_orig.signed_ofi <= 1.0)))
        self.assertTrue(np.all((res_orig.hurst > 0.0) & (res_orig.hurst < 1.0)))
        self.assertTrue(np.all(np.isfinite(res_orig.curvature)))
        self.assertTrue(np.all(np.isfinite(res_orig.d_curvature)))

        # Strict zero-lookahead causality: heavily perturb bars t > t0
        pert = SessionData(
            session_id=orig.session_id,
            times=list(orig.times),
            open=orig.open.copy(),
            high=orig.high.copy(),
            low=orig.low.copy(),
            close=orig.close.copy(),
            volume=orig.volume.copy(),
            rel_vol=orig.rel_vol.copy(),
            vol_regime=orig.vol_regime.copy(),
            transient_impact_bps=orig.transient_impact_bps.copy(),
        )
        pert.open[t0 + 1 :] *= 1.38
        pert.high[t0 + 1 :] *= 1.45
        pert.low[t0 + 1 :] *= 0.68
        pert.close[t0 + 1 :] *= 1.31
        pert.volume[t0 + 1 :] *= 8.0
        pert.rel_vol[t0 + 1 :] *= 8.0

        ind_pert = compute_session_indicators(pert)
        res_pert = compute_research_indicators(pert, ind_pert)

        for field_name in (
            "tau_half",
            "weights_ou",
            "vwap_ou",
            "sigma_ou",
            "z_ou",
            "level",
            "slope",
            "curvature",
            "d_curvature",
            "vpin",
            "signed_ofi",
            "hurst",
        ):
            np.testing.assert_allclose(
                getattr(res_orig, field_name)[: t0 + 1],
                getattr(res_pert, field_name)[: t0 + 1],
                atol=1e-13,
                err_msg=f"Lookahead leakage detected in {field_name}",
            )
        np.testing.assert_array_equal(res_orig.toxic_flag[: t0 + 1], res_pert.toxic_flag[: t0 + 1])

        sig_o, _ = compute_ou_spectral_entry_signals(orig, ind_orig, res_orig, market_cfg=self.cfg)
        sig_p, _ = compute_ou_spectral_entry_signals(pert, ind_pert, res_pert, market_cfg=self.cfg)
        np.testing.assert_array_equal(sig_o[: t0 + 1], sig_p[: t0 + 1])

        # Early OU-Spectral trades (both 1x and Turbo) finishing before t0 match identically
        for turbo_flag in (False, True):
            _, _, tr_o, _ = backtest_session_ou_spectral(orig, ind_orig, res_orig, self.cfg, turbo_mode=turbo_flag)
            _, _, tr_p, _ = backtest_session_ou_spectral(pert, ind_pert, res_pert, self.cfg, turbo_mode=turbo_flag)
            self.assertGreater(len(tr_o), 0)
            self.assertAlmostEqual(tr_o[0]["scaled_net_bps"], tr_p[0]["scaled_net_bps"], places=10)

        # Standalone research_lab.json, embedded vwap_metrics.json["research_lab"], and bundle.js parity
        research_path = DATA_DIR / "research_lab.json"
        metrics_path = DATA_DIR / "vwap_metrics.json"
        session_path = DATA_DIR / "session_bars.json"
        self.assertTrue(research_path.exists(), "data/research_lab.json missing")

        rl = json.loads(research_path.read_text(encoding="utf-8"))
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        session = json.loads(session_path.read_text(encoding="utf-8"))
        self.assertEqual(metrics["research_lab"], rl)

        # Telemetry invariants: volume-clock half-life expands during midday lull vs open surge
        tel = rl["telemetry"]
        self.assertGreater(tel["tau_half_midday_min"], tel["tau_half_open_min"] + 2.0)
        self.assertGreater(tel["tau_half_midday_min"], tel["tau_half_close_min"] + 2.0)
        self.assertTrue(5 <= tel["peak_ir_horizon_min"] <= 20)

        # Walk-Forward & DSR (M=16) invariants
        wf = rl["walk_forward_dsr"]
        self.assertEqual(wf["split"]["trials_m"], 16)
        self.assertEqual(len(wf["models"]), 6)
        model_map = {m["id"]: m for m in wf["models"]}
        for m in wf["models"]:
            self.assertGreaterEqual(m["psr_pct"], 0.0)
            self.assertLessEqual(m["psr_pct"], 100.0)
            self.assertGreaterEqual(m["dsr_pct"], 0.0)
            self.assertLessEqual(m["dsr_pct"], 100.0)

        ou_1x = model_map["ou_spectral_vpin_1x"]
        ou_tb = model_map["ou_spectral_vpin_turbo"]
        adapt = model_map["adaptive_blend_5_20"]
        self.assertGreater(ou_1x["oos_sharpe"], adapt["oos_sharpe"])
        self.assertGreater(ou_1x["full_sharpe"], adapt["full_sharpe"])
        self.assertGreater(ou_tb["full_sharpe"], ou_1x["full_sharpe"])
        self.assertGreater(ou_tb["dsr_pct"], 90.0)

        # 4 Adversarial Stress Scenarios & VPIN+Hurst protection in momentum_meltup
        stress = {s["id"]: s for s in rl["stress_tests"]}
        self.assertEqual(
            set(stress.keys()),
            {"flash_crash", "momentum_meltup", "cost_shock_2x", "latency_lag_1m"},
        )
        mm = stress["momentum_meltup"]
        self.assertGreater(mm["toxic_vetoes"], 100)
        self.assertGreater(mm["models"]["ou_spectral_1x"]["net_sharpe"], mm["models"]["single_8m"]["net_sharpe"])
        self.assertGreater(mm["models"]["ou_spectral_1x"]["net_sharpe"], mm["models"]["adaptive_1x"]["net_sharpe"])
        self.assertGreater(mm["models"]["ou_spectral_turbo"]["net_sharpe"], mm["models"]["adaptive_1x"]["net_sharpe"])

        # 12-point Alpha Decay Curve (1..30m)
        decay = rl["alpha_decay_curve"]
        self.assertEqual(tuple(pt["horizon_min"] for pt in decay), ALPHA_DECAY_HORIZONS)
        sub5 = [pt["ou_spectral_vpin_net_bps"] for pt in decay if pt["horizon_min"] < 5]
        sweet = [pt["ou_spectral_vpin_net_bps"] for pt in decay if pt["in_sweet_spot"]]
        self.assertTrue(all(x < 0.0 for x in sub5), "Sub-5m friction should yield negative net edge")
        self.assertTrue(all(x > 0.0 for x in sweet), "All 5-20m sweet-spot horizons should show positive net edge")

        # Enriched session_bars.json schema check (all 10 required new fields + OU weights)
        required_new_fields = (
            "vwap6",
            "vwap8",
            "vwap12",
            "vwap16",
            "vwap18",
            "vwap_ou",
            "tau_half",
            "curvature",
            "vpin",
            "hurst",
        )
        for bar in session["bars"]:
            for fld in required_new_fields:
                self.assertIn(fld, bar, f"Missing enriched bar field: {fld}")
                self.assertTrue(math.isfinite(bar[fld]), f"Non-finite enriched field: {fld}")
            self.assertAlmostEqual(
                bar["ou_w5"] + bar["ou_w10"] + bar["ou_w15"] + bar["ou_w20"],
                1.0,
                places=3,
            )


if __name__ == "__main__":
    unittest.main()
