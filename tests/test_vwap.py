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
    compute_entry_signals,
    compute_rolling_vwap_and_dispersion,
    compute_session_indicators,
    get_13_strategy_configs,
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


if __name__ == "__main__":
    unittest.main()
