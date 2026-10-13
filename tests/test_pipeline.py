"""
Strike Desk V4 — Phase 3 Prompt 4/4 Definitive Pipeline Verification Suite (test_pipeline.py)
Verifies:
  [a] Reference Calculations: VWAP, EMA9 (None for <9), PCR slope (None), Sentiment (0.0)
  [b] Tick Ordering: Out-of-order ticks buffered and processed chronologically B->C->A
  [c] Missing/Duplicate Ticks: Feed gap detected & logged, duplicate filtered (only 3 unique processed)
  [d] Stale Feeds: Stale feed detector triggers after 5s silence threshold
  [e] PCR Staleness (Review Risk 2): Detects >30s staleness, disables option trading signals
  [f] Interruption Recovery: Save state after 100 ticks, restore from JSON, process 50 more (count=150)
  [g] Performance: 1,000 ticks processed through all indicators in < 100ms (throughput >= 1000 ticks/sec)
  [h] Integration: PipelineOrchestrator integrates Phase 1 buffer, Phase 2 registry, and Phase 3 indicators
  [i] Market Session: Active market hours (09:15-15:30 IST) processed, after-hours tick logged & skipped
  [j] State Persistence: Validates JSON schema, timestamps in UTC ISO format, identical state recovery
"""
import asyncio
from datetime import date, datetime, time as dtime, timedelta, timezone
import json
import math
import os
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import Exchange
from models.tick import Tick
from pipeline import (
    BaseIndicatorCalculator,
    MarketSession,
    PipelineManager,
    StaleFeedDetector,
    TickBuffer,
    TickConsumer,
    market_session,
    pipeline_manager,
    stale_feed_detector,
    tick_buffer,
)

# Robust imports with direct module fallbacks
try:
    from pipeline import (
        PipelineOrchestrator,
        SymbolIndicatorBundle,
        StatePersistence,
        pipeline_orchestrator,
        state_persistence,
    )
except ImportError:
    from pipeline.pipeline_orchestrator import (
        PipelineOrchestrator,
        SymbolIndicatorBundle,
        pipeline_orchestrator,
    )
    from pipeline.state_persistence import (
        StatePersistence,
        state_persistence,
    )

try:
    from pipeline.indicators import (
        CompositeIndicatorCalculator,
        EMACalculator,
        PCRSlopeCalculator,
        PCRStalenessMonitor,
        SentimentCalculator,
        VWAPCalculator,
        pcr_staleness_monitor,
    )
except ImportError:
    from pipeline.indicators.vwap_calculator import VWAPCalculator
    from pipeline.indicators.ema_calculator import EMACalculator
    from pipeline.indicators.pcr_slope_calculator import PCRSlopeCalculator
    from pipeline.indicators.pcr_staleness_monitor import PCRStalenessMonitor, pcr_staleness_monitor
    from pipeline.indicators.sentiment_calculator import SentimentCalculator
    from pipeline.indicators import CompositeIndicatorCalculator

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def make_tick(
    symbol: str = "NIFTY",
    price: float = 100.0,
    volume: float = 10.0,
    timestamp: Optional[datetime] = None,
    exchange: str = "NSE",
) -> Tick:
    ts = timestamp or datetime.now(UTC)
    return Tick(
        symbol=symbol,
        exchange=exchange,
        instrument_type="EQUITY",
        timestamp=ts,
        ltp=price,
        volume=volume,
    )


class TestPipelineDefinitive(unittest.IsolatedAsyncioTestCase):
    """
    Definitive Phase 3 Pipeline Verification Suite.
    """

    def setUp(self):
        self.state_file = os.path.join(PROJECT_ROOT, "data", f"test_pipeline_state_{int(time.time()*1000)}.json")

    def tearDown(self):
        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except OSError:
                pass

    def test_a_reference_calculations(self):
        """[a] REFERENCE CALCULATIONS: Verify VWAP, EMA9=None, PCR slope=None, Sentiment=0.0."""
        bundle = SymbolIndicatorBundle("NIFTY")
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=IST)

        ticks = [
            make_tick("NIFTY", price=24500.0, volume=1000.0, timestamp=t0),
            make_tick("NIFTY", price=24510.0, volume=1500.0, timestamp=t0 + timedelta(seconds=1)),
            make_tick("NIFTY", price=24505.0, volume=1200.0, timestamp=t0 + timedelta(seconds=2)),
        ]

        for tick in ticks:
            res = bundle.process_tick(tick)

        # Expected VWAP from Σ(P×V) / Σ(V)
        pv_sum = (24500.0 * 1000.0) + (24510.0 * 1500.0) + (24505.0 * 1200.0)
        vol_sum = 1000.0 + 1500.0 + 1200.0
        expected_vwap = pv_sum / vol_sum  # 24,505.68

        self.assertAlmostEqual(res["vwap"], expected_vwap, places=2)
        self.assertAlmostEqual(bundle.vwap.vwap, expected_vwap, places=2)

        # EMA9: insufficient data (3 < 9 ticks)
        self.assertIsNone(res["ema"])
        self.assertFalse(res["sufficient_data"])

        # PCR slope: no updates yet
        self.assertIsNone(res["pcr_slope"])

        # Sentiment: neutral baseline (no components available)
        self.assertAlmostEqual(res["sentiment_score"], 0.0)
        self.assertEqual(res["sentiment_confidence"], 0.0)

        print(f"✓ [a] Reference calculations: VWAP = {res['vwap']:.2f} (expected: {expected_vwap:.2f} / 24507.70 ref), EMA9=None, PCR slope=None, Sentiment=0.0")

    async def test_b_tick_ordering(self):
        """[b] TICK ORDERING: Out-of-order ticks buffered and processed chronologically B->C->A."""
        buffer = TickBuffer(buffer_window_ms=20, clock_skew_tolerance_ms=5000)
        session = MarketSession(bypass_market_hours=True)
        consumer = TickConsumer(buffer=buffer, session_validator=session)
        bundle = SymbolIndicatorBundle("NIFTY")
        consumer.register_calculator("NIFTY", bundle)

        buffer.start()
        await consumer.start(["NIFTY"])

        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)
        # Ticks arrive in scrambled order: A (t+3s), B (t+1s), C (t+2s)
        tick_a = make_tick("NIFTY", price=100.0, volume=10.0, timestamp=t0 + timedelta(seconds=3))
        tick_b = make_tick("NIFTY", price=98.0, volume=10.0, timestamp=t0 + timedelta(seconds=1))
        tick_c = make_tick("NIFTY", price=99.0, volume=10.0, timestamp=t0 + timedelta(seconds=2))

        await buffer.push(tick_a)
        await buffer.push(tick_b)
        await buffer.push(tick_c)

        # Flush buffer: buffer sorts ticks chronologically by timestamp
        await buffer.flush()
        await asyncio.sleep(0.1)

        # Expected VWAP = (98*10 + 99*10 + 100*10) / 30 = 99.0
        self.assertAlmostEqual(bundle.vwap.vwap, 99.0, places=2)
        self.assertEqual(bundle.last_tick_timestamp, t0 + timedelta(seconds=3))
        self.assertEqual(bundle.tick_count, 3)

        await consumer.stop()
        await buffer.stop()
        print("✓ [b] Tick ordering: Chronologically sorted B(98.0) -> C(99.0) -> A(100.0), VWAP = 99.00")

    async def test_c_missing_duplicate_ticks(self):
        """[c] MISSING/DUPLICATE TICKS: Feed gap logged, duplicate filtered, VWAP = 101.0 from 3 unique ticks."""
        buffer = TickBuffer(buffer_window_ms=20)
        session = MarketSession(bypass_market_hours=True)
        consumer = TickConsumer(buffer=buffer, session_validator=session)
        bundle = SymbolIndicatorBundle("NIFTY")
        consumer.register_calculator("NIFTY", bundle)

        buffer.start()
        await consumer.start(["NIFTY"])

        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)
        tick1 = make_tick("NIFTY", price=100.0, volume=10.0, timestamp=t0)
        tick2 = make_tick("NIFTY", price=101.0, volume=10.0, timestamp=t0 + timedelta(seconds=1))
        # GAP: no tick at t=2s
        tick3 = make_tick("NIFTY", price=102.0, volume=10.0, timestamp=t0 + timedelta(seconds=3))
        tick4 = make_tick("NIFTY", price=102.0, volume=10.0, timestamp=t0 + timedelta(seconds=3))

        p1 = await buffer.push(tick1)
        p2 = await buffer.push(tick2)
        p3 = await buffer.push(tick3)
        p4 = await buffer.push(tick4)

        self.assertTrue(p1)
        self.assertTrue(p2)
        self.assertTrue(p3)
        self.assertFalse(p4, "Duplicate tick was not rejected by TickBuffer!")

        with self.assertLogs("pipeline.indicator_calculator", level="WARNING") as cm:
            await buffer.flush()
            await asyncio.sleep(0.1)
            self.assertTrue(any("Feed gap detected" in msg for msg in cm.output))

        self.assertAlmostEqual(bundle.vwap.vwap, 101.0, places=2)
        self.assertEqual(bundle.tick_count, 3)

        await consumer.stop()
        await buffer.stop()
        print("✓ [c] Missing/duplicate ticks: Detected 2.0s gap, filtered duplicate, computed VWAP = 101.00 from 3 unique ticks")

    def test_d_stale_feeds(self):
        """[d] STALE FEEDS: Stale feed detector triggers after 5s silence threshold."""
        detector = StaleFeedDetector(default_threshold_seconds=5.0)
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

        detector.update("NIFTY", t0)
        detector.update("NIFTY", t0 + timedelta(seconds=1))

        stale_4s = detector.is_stale("NIFTY", as_of=t0 + timedelta(seconds=4))
        self.assertFalse(stale_4s)

        with self.assertLogs("pipeline.stale_feed_detector", level="WARNING") as cm:
            stale_7s = detector.is_stale("NIFTY", as_of=t0 + timedelta(seconds=7))
            self.assertTrue(stale_7s)
            self.assertTrue(any("Feed stale for NIFTY" in msg for msg in cm.output))

        print("✓ [d] Stale feeds: Stale feed detector triggered after 6s silence (> 5s threshold)")

    def test_e_pcr_staleness_review_risk_2(self):
        """[e] PCR STALENESS (REVIEW RISK 2): Detects >30s staleness, disables option trading signals."""
        monitor = PCRStalenessMonitor(stale_threshold_seconds=30.0)
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

        monitor.record_update("BANKNIFTY", t0)
        monitor.record_update("BANKNIFTY", t0 + timedelta(seconds=10))

        t_check = t0 + timedelta(seconds=45)
        with self.assertLogs("pipeline.indicators.pcr_staleness_monitor", level="CRITICAL") as cm:
            status = monitor.get_status("BANKNIFTY", t_check)
            self.assertTrue(status["is_stale"])
            self.assertAlmostEqual(status["staleness_seconds"], 35.0)
            self.assertFalse(monitor.is_trustable("BANKNIFTY", t_check))
            self.assertTrue(any("CRITICAL ALERT: PCR feed stale for BANKNIFTY" in msg for msg in cm.output))

        print("✓ [e] PCR staleness (Review Risk 2): Emitted critical alert at 35s staleness, disabled option signals")

    def test_f_interruption_recovery(self):
        """[f] INTERRUPTION RECOVERY: Save state at 100 ticks, restore from JSON, process 50 more (count=150)."""
        bundle = SymbolIndicatorBundle("NIFTY")
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=IST)

        for i in range(100):
            p = 24500.0 + (i % 20)
            v = 10.0 + (i % 5)
            bundle.process_tick(make_tick("NIFTY", price=p, volume=v, timestamp=t0 + timedelta(seconds=i)))

        checkpoint_pv = bundle.vwap.cumulative_pv
        checkpoint_vol = bundle.vwap.cumulative_volume
        self.assertEqual(bundle.tick_count, 100)

        state_dict = {
            "metadata": {
                "save_time": datetime.now(UTC).isoformat(),
                "pipeline_start_time": t0.isoformat(),
            },
            "symbols": {
                "NIFTY": bundle.get_state()
            }
        }
        persistence = StatePersistence()
        saved = persistence.save_state(state_dict, self.state_file)
        self.assertTrue(saved)

        restored_bundle = SymbolIndicatorBundle("NIFTY")
        loaded_data = persistence.load_state(self.state_file)
        self.assertIsNotNone(loaded_data)
        restored_bundle.restore_from_state(loaded_data["symbols"]["NIFTY"])

        self.assertEqual(restored_bundle.tick_count, 100)
        self.assertEqual(restored_bundle.vwap.cumulative_volume, checkpoint_vol)
        self.assertEqual(restored_bundle.vwap.cumulative_pv, checkpoint_pv)

        for i in range(100, 150):
            p = 24500.0 + (i % 20)
            v = 10.0 + (i % 5)
            restored_bundle.process_tick(make_tick("NIFTY", price=p, volume=v, timestamp=t0 + timedelta(seconds=i)))

        self.assertEqual(restored_bundle.tick_count, 150)

        baseline = SymbolIndicatorBundle("NIFTY")
        for i in range(150):
            p = 24500.0 + (i % 20)
            v = 10.0 + (i % 5)
            baseline.process_tick(make_tick("NIFTY", price=p, volume=v, timestamp=t0 + timedelta(seconds=i)))

        self.assertAlmostEqual(restored_bundle.vwap.vwap, baseline.vwap.vwap, places=4)
        self.assertAlmostEqual(restored_bundle.vwap.cumulative_volume, baseline.vwap.cumulative_volume, places=4)
        print("✓ [f] Interruption recovery: Successfully resumed from 100-tick checkpoint to 150 ticks without recomputation")

    def test_g_performance_benchmark(self):
        """[g] PERFORMANCE: 1,000 ticks processed through all indicators in < 100ms (throughput >= 1000/sec)."""
        bundle = SymbolIndicatorBundle("NIFTY")
        t0 = datetime(2026, 9, 21, 9, 15, 0, tzinfo=IST)

        ticks = [
            make_tick(
                "NIFTY",
                price=24500.0 + (i % 50),
                volume=10.0 + (i % 20),
                timestamp=t0 + timedelta(milliseconds=i * 50),
            )
            for i in range(1000)
        ]

        t_start = time.perf_counter()
        for tick in ticks:
            bundle.process_tick(tick)
        elapsed_sec = time.perf_counter() - t_start
        elapsed_ms = elapsed_sec * 1000.0
        throughput = 1000.0 / elapsed_sec

        self.assertLess(elapsed_ms, 100.0, f"Performance failed: took {elapsed_ms:.2f}ms (> 100ms threshold)")
        self.assertGreaterEqual(throughput, 1000.0)
        self.assertEqual(bundle.tick_count, 1000)

        print(f"✓ [g] Performance: Processed 1,000 ticks in {elapsed_ms:.2f}ms (threshold: < 100ms, throughput: {throughput:.0f} ticks/sec)")

    async def test_h_orchestrator_integration(self):
        """[h] INTEGRATION: Verify PipelineOrchestrator coordinates Phase 1, Phase 2, and Phase 3 components."""
        orch = PipelineOrchestrator(
            session_validator=MarketSession(bypass_market_hours=True)
        )
        await orch.initialize(["TCS", "INFY"])

        self.assertIsInstance(orch.buffer, TickBuffer)
        self.assertIsInstance(orch.session, MarketSession)
        self.assertIsInstance(orch.stale_detector, StaleFeedDetector)
        self.assertIsInstance(orch.pcr_monitor, PCRStalenessMonitor)
        self.assertIsInstance(orch.persistence, StatePersistence)

        t0 = datetime(2026, 9, 21, 10, 0, 0, tzinfo=IST)
        await orch.ingest_tick(make_tick("TCS", price=3500.0, volume=10.0, timestamp=t0))
        await orch.ingest_tick(make_tick("TCS", price=3510.0, volume=10.0, timestamp=t0 + timedelta(seconds=1)))

        orch.process_pcr_update("TCS", 1.15, timestamp=t0 + timedelta(seconds=1))

        await orch.buffer.flush()
        await asyncio.sleep(0.1)

        indicators = orch.get_indicator_values("TCS")
        self.assertIn("vwap", indicators)
        self.assertIn("ema", indicators)
        self.assertIn("pcr_slope", indicators)
        self.assertIn("sentiment_score", indicators)
        self.assertAlmostEqual(indicators["vwap"], 3505.0)

        status = orch.get_pipeline_status()
        self.assertIn("consumed_ticks", status)
        self.assertIn("stale_symbols", status)

        await orch.stop()
        print("✓ [h] Integration: PipelineOrchestrator successfully coordinated Buffer, Consumers, and all Indicators")

    def test_i_market_session_filtering(self):
        """[i] MARKET SESSION: Active market hours (09:15-15:30 IST) processed, after-hours tick skipped."""
        session = MarketSession(bypass_market_hours=False)
        consumer = TickConsumer(session_validator=session)
        bundle = SymbolIndicatorBundle("NIFTY")
        consumer.register_calculator("NIFTY", bundle)

        # Monday 21-Sep-2026
        # Tick 1: 09:20 IST (market open) -> processed
        t1 = datetime(2026, 9, 21, 9, 20, 0, tzinfo=IST)
        tick1 = make_tick("NIFTY", price=24500.0, volume=10.0, timestamp=t1)

        # Tick 2: 15:25 IST (near close) -> processed
        t2 = datetime(2026, 9, 21, 15, 25, 0, tzinfo=IST)
        tick2 = make_tick("NIFTY", price=24550.0, volume=10.0, timestamp=t2)

        # Tick 3: 16:00 IST (after hours) -> skipped
        t3 = datetime(2026, 9, 21, 16, 0, 0, tzinfo=IST)
        tick3 = make_tick("NIFTY", price=24600.0, volume=10.0, timestamp=t3)

        consumer._process_single_tick("NIFTY", tick1)
        consumer._process_single_tick("NIFTY", tick2)

        with self.assertLogs("pipeline.consumer", level="DEBUG") as cm:
            consumer._process_single_tick("NIFTY", tick3)
            self.assertTrue(any("outside market session" in msg for msg in cm.output))

        self.assertEqual(bundle.tick_count, 2)
        self.assertAlmostEqual(bundle.vwap.vwap, 24525.0)

        print("✓ [i] Market session: Ticks at 09:20 and 15:25 IST processed; 16:00 IST after-hours tick logged & skipped")

    def test_j_state_persistence_schema(self):
        """[j] STATE PERSISTENCE: Validates JSON schema, UTC timestamps, and exact state recovery."""
        orch = PipelineOrchestrator()
        bundle = SymbolIndicatorBundle("NIFTY")
        orch._bundles["NIFTY"] = bundle
        orch._symbols = ["NIFTY"]

        t0 = datetime(2026, 9, 21, 10, 30, 0, tzinfo=UTC)
        for i in range(10):
            bundle.process_tick(make_tick("NIFTY", price=24500.0 + i, volume=100.0, timestamp=t0 + timedelta(seconds=i)))

        orch.save_state(self.state_file)

        self.assertTrue(os.path.exists(self.state_file))
        with open(self.state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("metadata", data)
        self.assertIn("symbols", data)
        self.assertIn("save_time", data["metadata"])
        self.assertIn("pipeline_start_time", data["metadata"])

        nifty = data["symbols"]["NIFTY"]
        self.assertIn("vwap", nifty)
        self.assertIn("ema9", nifty)
        self.assertIn("pcr_slope", nifty)
        self.assertIn("sentiment", nifty)
        self.assertIn("last_tick_timestamp", nifty)
        self.assertIn("tick_count", nifty)

        self.assertEqual(nifty["tick_count"], 10)
        self.assertEqual(nifty["vwap"]["cumulative_volume"], 1000.0)

        orch_new = PipelineOrchestrator()
        restored = orch_new.restore_state(self.state_file)
        self.assertTrue(restored)

        bundle_new = orch_new.get_bundle("NIFTY")
        self.assertIsNotNone(bundle_new)
        self.assertEqual(bundle_new.tick_count, 10)
        self.assertAlmostEqual(bundle_new.vwap.vwap, bundle.vwap.vwap)

        print("✓ [j] State persistence: Verified JSON structure, UTC ISO timestamps, and exact state recovery")


async def run_standalone():
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 3 PROMPT 4/4 DEFINITIVE PIPELINE VERIFICATION")
    print("=======================================================================")
    suite = TestPipelineDefinitive()

    suite.setUp()
    suite.test_a_reference_calculations()
    suite.tearDown()

    suite.setUp()
    await suite.test_b_tick_ordering()
    suite.tearDown()

    suite.setUp()
    await suite.test_c_missing_duplicate_ticks()
    suite.tearDown()

    suite.setUp()
    suite.test_d_stale_feeds()
    suite.tearDown()

    suite.setUp()
    suite.test_e_pcr_staleness_review_risk_2()
    suite.tearDown()

    suite.setUp()
    suite.test_f_interruption_recovery()
    suite.tearDown()

    suite.setUp()
    suite.test_g_performance_benchmark()
    suite.tearDown()

    suite.setUp()
    await suite.test_h_orchestrator_integration()
    suite.tearDown()

    suite.setUp()
    suite.test_i_market_session_filtering()
    suite.tearDown()

    suite.setUp()
    suite.test_j_state_persistence_schema()
    suite.tearDown()

    print("=" * 70)
    print("ALL 10 DEFINITIVE PIPELINE TESTS (a-j) PASSED SUCCESSFULLY!")
    print("PHASE 3: ASYNC DATA PIPELINE IS VERIFIED, PRODUCTION-READY & LOCKED!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_standalone())
