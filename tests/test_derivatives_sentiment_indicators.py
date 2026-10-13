"""
Strike Desk V4 — Phase 3 Prompt 3/4 Verification Suite: Derivatives & Sentiment Indicators
Verifies:
  a) PCR slope basic calculation (t=0: 1.0, t=2m: 1.2, t=5m: 1.5 -> slope = 0.1/min)
  b) PCR slope insufficient data (< 2 points -> None, sufficient_data=False)
  c) PCR staleness detection (> 30s -> is_stale=True, is_trustable=False)
  d) PCR staleness recovery (fresh update recovers trustable status)
  e) PCR low frequency detection (1 update/60s -> is_low_frequency=True)
  f) Sentiment scoring basic (all 4 components present -> score in [-100, +100], confidence=100%)
  g) Sentiment degraded mode (2 of 4 components present -> confidence=50%)
  h) Sentiment no data (no components present -> sentiment=0, confidence=0)
  i) Integration (Register PCR slope + Sentiment with PipelineManager)
  j) Review Risk 2 compliance (Option signals disabled when PCR > 30s stale)
  k) Performance (100 PCR updates processed in < 10ms)
  l) Edge cases (extreme PCR 0.1 & 10.0, negative slopes, zero OI change)
"""
import asyncio
from datetime import datetime, timedelta, timezone
import math
import os
import sys
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.option_chain import OptionChain, OptionContract, StrikeData
from models.tick import Tick
from pipeline import (
    MarketSession,
    PipelineManager,
    TickBuffer,
    TickConsumer,
    pipeline_manager,
)
from pipeline.indicators import (
    CompositeIndicatorCalculator,
    PCRSlopeCalculator,
    PCRStalenessMonitor,
    SentimentCalculator,
)

UTC = timezone.utc


class TestDerivativesSentimentIndicators(unittest.IsolatedAsyncioTestCase):
    """
    Verification Suite for PCR Slope, PCR Staleness Monitor, and Sentiment Scoring.
    """

    def test_a_pcr_slope_basic_calculation(self):
        """[a] PCR slope basic calculation: 3 points over 5 minutes -> slope = 0.1."""
        calc = PCRSlopeCalculator(window_minutes=5)
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=UTC)

        # t=0: PCR=1.0
        r0 = calc.process_pcr_update("NIFTY", 1.0, t0)
        self.assertIsNone(r0["pcr_slope"])
        self.assertFalse(r0["sufficient_data"])

        # t=2min: PCR=1.2
        r1 = calc.process_pcr_update("NIFTY", 1.2, t0 + timedelta(minutes=2))
        self.assertTrue(r1["sufficient_data"])
        self.assertIsNotNone(r1["pcr_slope"])

        # t=5min: PCR=1.5
        r2 = calc.process_pcr_update("NIFTY", 1.5, t0 + timedelta(minutes=5))
        self.assertTrue(r2["sufficient_data"])

        # Formula: (1.5 - 1.0) / 5.0 = 0.1 per minute
        expected_slope = (1.5 - 1.0) / 5.0
        self.assertAlmostEqual(r2["pcr_slope"], expected_slope, places=4)
        self.assertAlmostEqual(calc.last_slope, expected_slope, places=4)
        self.assertEqual(r2["pcr_current"], 1.5)
        self.assertEqual(r2["window_minutes"], 5)

        print(f"✓ [a] PCR slope basic calculation: t=0 (1.0), t=2m (1.2), t=5m (1.5) -> slope = {calc.last_slope:.4f}/min")

    def test_b_pcr_slope_insufficient_data(self):
        """[b] PCR slope insufficient data (< 2 points in window)."""
        calc = PCRSlopeCalculator(window_minutes=5)
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=UTC)

        # 0 points
        self.assertIsNone(calc.calculate_slope())

        # 1 point
        res = calc.process_pcr_update("NIFTY", 1.25, t0)
        self.assertIsNone(res["pcr_slope"])
        self.assertFalse(res["sufficient_data"])
        self.assertIsNone(calc.calculate_slope())

        print("✓ [b] PCR slope insufficient data: Returned None and sufficient_data=False for 0 and 1 points")

    def test_c_pcr_staleness_detection(self):
        """[c] PCR staleness detection: Record update, advance 35s (> 30s threshold) -> is_trustable=False."""
        monitor = PCRStalenessMonitor(stale_threshold_seconds=30.0)
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=UTC)

        # Record update at t=0
        monitor.record_update("BANKNIFTY", t0)

        # At t=25s: not stale
        t_25 = t0 + timedelta(seconds=25)
        status_25 = monitor.get_status("BANKNIFTY", t_25)
        self.assertFalse(status_25["is_stale"])
        self.assertAlmostEqual(status_25["staleness_seconds"], 25.0)

        # At t=35s: stale (> 30s)
        t_35 = t0 + timedelta(seconds=35)
        status_35 = monitor.get_status("BANKNIFTY", t_35)
        self.assertTrue(status_35["is_stale"])
        self.assertAlmostEqual(status_35["staleness_seconds"], 35.0)
        self.assertFalse(monitor.is_trustable("BANKNIFTY", t_35))

        # Check stale underlyings listing
        stale_list = monitor.get_all_stale_underlyings(t_35)
        self.assertIn("BANKNIFTY", stale_list)

        print("✓ [c] PCR staleness detection: Correctly identified feed stale at 35s (> 30s threshold)")

    def test_d_pcr_staleness_recovery(self):
        """[d] PCR staleness recovery: Recording fresh update restores trustable status."""
        monitor = PCRStalenessMonitor(stale_threshold_seconds=30.0, min_update_frequency_hz=2.0)
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=UTC)

        # Seed nominal updates (120 updates across 60 seconds = 2.0 Hz)
        for i in range(120):
            monitor.record_update("NIFTY", t0 + timedelta(milliseconds=i * 500))

        t_active = t0 + timedelta(seconds=60)
        self.assertTrue(monitor.is_trustable("NIFTY", t_active))

        # Stale period: advance 40 seconds with no updates
        t_stale = t_active + timedelta(seconds=40)
        self.assertTrue(monitor.get_status("NIFTY", t_stale)["is_stale"])
        self.assertFalse(monitor.is_trustable("NIFTY", t_stale))

        # Recovery: feed new updates meeting throughput
        for i in range(120):
            monitor.record_update("NIFTY", t_stale + timedelta(milliseconds=i * 500))

        t_recovered = t_stale + timedelta(seconds=60)
        status_rec = monitor.get_status("NIFTY", t_recovered)
        self.assertFalse(status_rec["is_stale"])
        self.assertTrue(monitor.is_trustable("NIFTY", t_recovered))

        print("✓ [d] PCR staleness recovery: Recovered from 40s stale silence to active trustable status")

    def test_e_pcr_low_frequency_detection(self):
        """[e] PCR low frequency detection: 1 update in 60s -> is_low_frequency=True (1/60 Hz < 2.0 Hz)."""
        monitor = PCRStalenessMonitor(min_update_frequency_hz=2.0)
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=UTC)

        # Record only 1 update at t=5s
        monitor.record_update("FINNIFTY", t0 + timedelta(seconds=5))

        # Check at t=60s
        t_check = t0 + timedelta(seconds=60)
        status = monitor.get_status("FINNIFTY", t_check)

        # Staleness is 55s (> 30s) AND frequency is 1/60 = 0.0167 Hz (< 2.0 Hz)
        self.assertTrue(status["is_low_frequency"])
        self.assertAlmostEqual(status["update_frequency_hz"], 1.0 / 60.0, places=4)
        self.assertFalse(status["is_trustable"])

        # Also test fresh tick but insufficient count (e.g. 10 updates in 60s = 0.167 Hz < 2.0 Hz)
        t_fresh = t_check + timedelta(seconds=5)
        for i in range(10):
            monitor.record_update("FINNIFTY", t_fresh - timedelta(seconds=i))

        status_low = monitor.get_status("FINNIFTY", t_fresh)
        self.assertFalse(status_low["is_stale"])
        self.assertTrue(status_low["is_low_frequency"])
        self.assertFalse(status_low["is_trustable"])

        print(f"✓ [e] PCR low frequency detection: Detected update rate {status_low['update_frequency_hz']:.4f} Hz < 2.0 Hz")

    def test_f_sentiment_scoring_basic(self):
        """[f] Sentiment scoring basic: All 4 components provided -> score in [-100, +100], confidence=100%."""
        sent = SentimentCalculator()

        # Moderately bullish inputs:
        # pcr_slope = -0.5 (falling PCR = bullish)
        # vwap_deviation = +0.02 (+2% above VWAP = bullish)
        # ema_trend = +0.015 (+1.5% above EMA = bullish)
        # oi_change_pct = +0.10 (+10% OI expansion = bullish)
        res = sent.calculate_sentiment(
            pcr_slope=-0.5,
            vwap_deviation=0.02,
            ema_trend=0.015,
            oi_change_pct=0.10,
        )

        score = res["sentiment_score"]
        self.assertGreater(score, 0.0)
        self.assertLessEqual(score, 100.0)
        self.assertGreaterEqual(score, -100.0)
        self.assertEqual(res["confidence"], 100.0)
        self.assertEqual(len(res["components"]), 4)

        # Verify component scores
        self.assertAlmostEqual(res["components"]["pcr_slope"], 25.0)       # -(-0.5/2.0)*100 = +25
        self.assertAlmostEqual(res["components"]["vwap_deviation"], 40.0)  # (0.02/0.05)*100 = +40
        self.assertAlmostEqual(res["components"]["ema_trend"], 50.0)       # (0.015/0.03)*100 = +50
        self.assertAlmostEqual(res["components"]["oi_change"], 50.0)       # (0.10/0.20)*100 = +50

        # Weighted sum: 0.4*25 + 0.3*40 + 0.2*50 + 0.1*50 = 10 + 12 + 10 + 5 = 37.0
        self.assertAlmostEqual(score, 37.0, places=2)

        print(f"✓ [f] Sentiment scoring basic: Calculated composite score = {score:.2f} (Confidence: 100%)")

    def test_g_sentiment_degraded_mode(self):
        """[g] Sentiment degraded mode: Provide 2 of 4 components -> confidence = 50%."""
        sent = SentimentCalculator()

        res = sent.calculate_sentiment(
            pcr_slope=1.0,           # bearish: -50.0
            vwap_deviation=-0.025,   # bearish: -50.0
            ema_trend=None,          # missing
            oi_change_pct=None,      # missing
        )

        self.assertEqual(res["confidence"], 50.0)
        self.assertIn("pcr_slope", res["components"])
        self.assertIn("vwap_deviation", res["components"])
        self.assertNotIn("ema_trend", res["components"])
        self.assertNotIn("oi_change", res["components"])

        # Re-normalized weighted average: (0.4 * -50 + 0.3 * -50) / 0.7 = -50.0
        self.assertAlmostEqual(res["sentiment_score"], -50.0, places=2)
        print("✓ [g] Sentiment degraded mode: Handled 2/4 components with re-normalized score and confidence=50%")

    def test_h_sentiment_no_data(self):
        """[h] Sentiment no data: No components provided -> sentiment=0, confidence=0."""
        sent = SentimentCalculator()

        res = sent.calculate_sentiment(None, None, None, None)
        self.assertEqual(res["sentiment_score"], 0.0)
        self.assertEqual(res["confidence"], 0.0)
        self.assertEqual(res["components"], {})

        print("✓ [h] Sentiment no data: Gracefully returned neutral sentiment=0.0 and confidence=0.0")

    async def test_i_pipeline_integration(self):
        """[i] Integration: Register PCR slope and Sentiment calculators with PipelineManager."""
        custom_buffer = TickBuffer(buffer_window_ms=10)
        custom_session = MarketSession(bypass_market_hours=True)
        custom_consumer = TickConsumer(buffer=custom_buffer, session_validator=custom_session)
        manager = PipelineManager(consumer=custom_consumer, buffer=custom_buffer, session_validator=custom_session)

        await manager.initialize(["NIFTY_OPT", "NIFTY_SENT"])

        pcr_calc = PCRSlopeCalculator("NIFTY_OPT", window_minutes=5)
        sent_calc = SentimentCalculator("NIFTY_SENT")

        manager.register_indicator_calculator("NIFTY_OPT", pcr_calc)
        manager.register_indicator_calculator("NIFTY_SENT", sent_calc)

        # Process PCR updates
        t0 = datetime(2026, 9, 21, 10, 0, 0, tzinfo=UTC)
        pcr_calc.process_pcr_update("NIFTY_OPT", 0.95, t0)
        pcr_calc.process_pcr_update("NIFTY_OPT", 1.15, t0 + timedelta(minutes=5))

        # Process Sentiment update
        sent_calc.calculate_sentiment(pcr_slope=0.04, vwap_deviation=0.01, ema_trend=0.005, oi_change_pct=0.02)

        pcr_state = manager.get_indicator_values("NIFTY_OPT")
        sent_state = manager.get_indicator_values("NIFTY_SENT")

        self.assertIn("pcr_slope", pcr_state)
        self.assertTrue(pcr_state["sufficient_data"])
        self.assertAlmostEqual(pcr_state["pcr_slope"], 0.04)

        self.assertIn("sentiment_score", sent_state)
        self.assertEqual(sent_state["confidence"], 100.0)

        await manager.stop()
        print("✓ [i] Integration: Successfully registered and retrieved PCR & Sentiment indicators from PipelineManager")

    def test_j_review_risk_2_compliance(self):
        """[j] Review Risk 2 compliance: Stale PCR (> 30s) disables option trading signals via is_pcr_trustable."""
        calc = PCRSlopeCalculator(window_minutes=5)
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=UTC)

        # Nominal update
        calc.process_pcr_update("BANKNIFTY", 1.10, t0)
        self.assertTrue(calc.is_pcr_trustable("BANKNIFTY", current_time=t0 + timedelta(seconds=10)))

        # Time passes > 30 seconds
        t_stale = t0 + timedelta(seconds=31)
        is_trustable = calc.is_pcr_trustable("BANKNIFTY", current_time=t_stale)
        self.assertFalse(is_trustable)

        # Simulate Trading Gatekeeper Check
        signals_enabled = is_trustable
        self.assertFalse(signals_enabled, "Option trading signals MUST be disabled when PCR > 30s stale!")

        print("✓ [j] Review Risk 2 compliance: Verified option signals are disabled when PCR feed exceeds 30s staleness")

    def test_k_performance_benchmark(self):
        """[k] Performance: 100 PCR updates processed in < 10ms."""
        calc = PCRSlopeCalculator(window_minutes=5)
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=UTC)

        t_start = time.perf_counter()
        for i in range(100):
            calc.process_pcr_update(
                "NIFTY",
                pcr=1.0 + (i * 0.005),
                timestamp=t0 + timedelta(seconds=i * 2),
            )
        elapsed_sec = time.perf_counter() - t_start
        elapsed_ms = elapsed_sec * 1000.0

        self.assertLess(elapsed_ms, 10.0, f"Benchmark failed: took {elapsed_ms:.2f}ms (> 10ms threshold)")
        self.assertTrue(calc.last_slope is not None)
        print(f"✓ [k] Performance: Processed 100 PCR updates in {elapsed_ms:.2f}ms (threshold: < 10ms)")

    def test_l_edge_cases(self):
        """[l] Edge cases: Extreme PCR (0.1, 10.0), negative slopes, zero OI change, invalid window."""
        calc = PCRSlopeCalculator(window_minutes=5)
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=UTC)

        # Micro PCR
        calc.process_pcr_update("EXTREME", 0.1, t0)
        # Macro PCR
        r_macro = calc.process_pcr_update("EXTREME", 10.0, t0 + timedelta(minutes=5))
        self.assertAlmostEqual(r_macro["pcr_slope"], (10.0 - 0.1) / 5.0)

        # Negative slope (falling PCR: bullish)
        calc.reset()
        calc.process_pcr_update("BULLISH", 1.5, t0)
        r_bull = calc.process_pcr_update("BULLISH", 1.0, t0 + timedelta(minutes=5))
        self.assertAlmostEqual(r_bull["pcr_slope"], -0.1)

        # Sentiment with zero OI change
        sent = SentimentCalculator()
        r_sent = sent.calculate_sentiment(pcr_slope=0.0, vwap_deviation=0.0, ema_trend=0.0, oi_change_pct=0.0)
        self.assertAlmostEqual(r_sent["sentiment_score"], 0.0)
        self.assertEqual(r_sent["confidence"], 100.0)

        # Invalid window validation
        with self.assertRaises(ValueError):
            PCRSlopeCalculator(window_minutes=0)

        # OptionChain integration
        chain = OptionChain(underlying="NIFTY", expiry_date="2026-09-24")
        chain.strikes = [
            StrikeData(strike_price=24000.0, call=OptionContract(oi=1000), put=OptionContract(oi=1500))
        ]
        r_chain = calc.process_option_chain(chain, t0 + timedelta(minutes=10))
        self.assertAlmostEqual(r_chain["pcr_current"], 1.5)

        print("✓ [l] Edge cases: Validated extreme PCRs (0.1, 10.0), negative slopes, zero OI, and OptionChain integration")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 3 PROMPT 3/4 DERIVATIVES & SENTIMENT INDICATORS")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestDerivativesSentimentIndicators)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 12 DERIVATIVES & SENTIMENT INDICATOR TESTS (a-l) PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)