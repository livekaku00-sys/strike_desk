import asyncio
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import Exchange
from models.tick import Tick
from pipeline import (
    BaseIndicatorCalculator,
    DefaultIndicatorCalculator,
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

IST = timezone(timedelta(hours=5, minutes=30))


class TestPipelineArchitecture(unittest.IsolatedAsyncioTestCase):
    """
    Phase 3 Prompt 1/4 Verification Suite: Core Pipeline Architecture & Ingestion.
    """

    def setUp(self):
        self.buffer = TickBuffer(buffer_window_ms=10)
        self.session = MarketSession(bypass_market_hours=True)
        self.stale_detector = StaleFeedDetector(default_threshold_seconds=5.0)
        self.consumer = TickConsumer(
            buffer=self.buffer,
            session_validator=self.session,
            stale_detector=self.stale_detector,
        )
        self.manager = PipelineManager(
            consumer=self.consumer,
            buffer=self.buffer,
            session_validator=self.session,
            stale_detector=self.stale_detector,
        )

    async def asyncTearDown(self):
        await self.consumer.stop()
        await self.buffer.stop()

    async def test_a_tick_consumer(self):
        """
        Verify tick consumer subscribes to buffer queues, ingests ticks,
        and computes baseline indicators.
        """
        await self.consumer.start(["RELIANCE"])
        now = datetime.now(timezone.utc)

        t1 = Tick(symbol="RELIANCE", exchange="NSE", instrument_type="EQ", timestamp=now, ltp=2500.0, volume=100.0)
        t2 = Tick(symbol="RELIANCE", exchange="NSE", instrument_type="EQ", timestamp=now + timedelta(milliseconds=50), ltp=2501.5, volume=150.0)
        t3 = Tick(symbol="RELIANCE", exchange="NSE", instrument_type="EQ", timestamp=now + timedelta(milliseconds=100), ltp=2502.0, volume=200.0)

        await self.buffer.push(t1)
        await self.buffer.push(t2)
        await self.buffer.push(t3)
        await self.buffer.flush()

        # Allow consumer task to process queue
        await asyncio.sleep(0.05)

        self.assertEqual(self.consumer.get_consumed_count(), 3)
        self.assertEqual(self.consumer.get_last_tick_time("RELIANCE"), t3.timestamp)
        print("✓ [a] Tick consumer: Subscribed to buffer queue and consumed 3 ticks")

    def test_b_market_session_validation(self):
        """
        Verify market session logic for open hours, closed hours, weekends, and holidays.
        """
        session = MarketSession(bypass_market_hours=False)

        # 1. Active trading session: Monday 10:30 AM IST
        mon_open_ist = datetime(2026, 9, 21, 10, 30, tzinfo=IST)
        self.assertTrue(session.is_market_open(Exchange.NSE, mon_open_ist))

        # 2. Pre-market / closed: Monday 08:30 AM IST
        mon_early_ist = datetime(2026, 9, 21, 8, 30, tzinfo=IST)
        self.assertFalse(session.is_market_open(Exchange.NSE, mon_early_ist))

        # 3. Post-market / closed: Monday 16:00 PM IST
        mon_late_ist = datetime(2026, 9, 21, 16, 0, tzinfo=IST)
        self.assertFalse(session.is_market_open(Exchange.NSE, mon_late_ist))

        # 4. Weekend: Saturday 11:00 AM IST
        sat_ist = datetime(2026, 9, 26, 11, 0, tzinfo=IST)
        self.assertFalse(session.is_market_open(Exchange.NSE, sat_ist))

        # 5. Holiday: Republic Day 2026-01-26 10:30 AM IST
        holiday_ist = datetime(2026, 1, 26, 10, 30, tzinfo=IST)
        self.assertFalse(session.is_market_open(Exchange.NSE, holiday_ist))
        print("✓ [b] Market session validation: Correctly validated active hours, pre/post market, weekends, holidays")

    def test_c_stale_feed_detection(self):
        """
        Verify feed marked stale when no ticks received within threshold.
        """
        t0 = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
        self.stale_detector.update("INFY", t0)

        # Within 5s threshold -> not stale
        self.assertFalse(self.stale_detector.is_stale("INFY", threshold_seconds=5.0, as_of=t0 + timedelta(seconds=3)))

        # Beyond 5s threshold -> stale
        self.assertTrue(self.stale_detector.is_stale("INFY", threshold_seconds=5.0, as_of=t0 + timedelta(seconds=6)))
        self.assertIn("INFY", self.stale_detector.get_stale_symbols(as_of=t0 + timedelta(seconds=6)))
        print("✓ [c] Stale feed detection: Triggered stale status after 6s silence (> 5s threshold)")

    def test_d_stale_feed_recovery(self):
        """
        Verify symbol recovers from stale status upon receiving new ticks.
        """
        t0 = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
        self.stale_detector.update("TCS", t0)
        t_stale = t0 + timedelta(seconds=10)
        self.assertTrue(self.stale_detector.is_stale("TCS", threshold_seconds=5.0, as_of=t_stale))

        # New tick arrives at t0 + 11s -> recovered
        t_recovery = t0 + timedelta(seconds=11)
        self.stale_detector.update("TCS", t_recovery)
        self.assertFalse(self.stale_detector.is_stale("TCS", threshold_seconds=5.0, as_of=t_recovery))
        self.assertNotIn("TCS", self.stale_detector.get_stale_symbols(as_of=t_recovery))
        print("✓ [d] Stale feed recovery: Verified automatic recovery upon fresh tick arrival")

    async def test_e_pipeline_lifecycle(self):
        """
        Verify start/pause/resume/stop lifecycle controls.
        """
        await self.manager.initialize(["SBIN"])
        self.assertTrue(self.manager.consumer.is_running)
        self.assertFalse(self.manager.consumer.is_paused)

        self.manager.pause()
        self.assertTrue(self.manager.consumer.is_paused)

        # Ingest during pause -> should be ignored or skipped
        t_paused = Tick(symbol="SBIN", exchange="NSE", instrument_type="EQ", timestamp=datetime.now(timezone.utc), ltp=800.0, volume=100.0)
        await self.buffer.push(t_paused)
        await self.buffer.flush()
        await asyncio.sleep(0.02)
        self.assertEqual(self.consumer.get_consumed_count(), 0)

        self.manager.resume()
        self.assertFalse(self.manager.consumer.is_paused)

        t_resumed = Tick(symbol="SBIN", exchange="NSE", instrument_type="EQ", timestamp=datetime.now(timezone.utc), ltp=801.0, volume=100.0)
        await self.buffer.push(t_resumed)
        await self.buffer.flush()
        await asyncio.sleep(0.02)
        self.assertEqual(self.consumer.get_consumed_count(), 1)

        await self.manager.stop()
        self.assertFalse(self.manager.consumer.is_running)
        print("✓ [e] Pipeline lifecycle: Verified start -> pause -> resume -> stop transitions")

    async def test_f_indicator_calculator_registration(self):
        """
        Verify registering custom indicator calculators and pulling indicator values.
        """
        class CustomIndicatorCalculator(BaseIndicatorCalculator):
            def calculate_indicators(self, tick: Tick):
                return {
                    "price_doubled": tick.ltp * 2,
                    "tick_seq": self.tick_count,
                }

        calc = CustomIndicatorCalculator("HDFCBANK")
        self.manager.register_indicator_calculator("HDFCBANK", calc)
        await self.consumer.start(["HDFCBANK"])

        tick = Tick(symbol="HDFCBANK", exchange="NSE", instrument_type="EQ", timestamp=datetime.now(timezone.utc), ltp=1650.0, volume=500.0)
        await self.buffer.push(tick)
        await self.buffer.flush()
        await asyncio.sleep(0.02)

        vals = self.manager.get_indicator_values("HDFCBANK")
        self.assertEqual(vals["price_doubled"], 3300.0)
        self.assertEqual(vals["tick_seq"], 1)
        print("✓ [f] Indicator calculator registration: Dispatched tick and extracted custom indicator output")

    async def test_g_tick_ordering(self):
        """
        Verify buffer sorts out-of-order ticks chronologically before delivery to consumer.
        """
        await self.consumer.start(["NIFTY"])
        now = datetime.now(timezone.utc)
        t_early = now
        t_late = now + timedelta(milliseconds=100)

        tick1 = Tick(symbol="NIFTY", exchange="NSE", instrument_type="IDX_FUT", timestamp=t_late, ltp=24500.0, volume=50.0)
        tick2 = Tick(symbol="NIFTY", exchange="NSE", instrument_type="IDX_FUT", timestamp=t_early, ltp=24495.0, volume=60.0)

        # Push in reverse order (late first, early second)
        await self.buffer.push(tick1)
        await self.buffer.push(tick2)
        await self.buffer.flush()
        await asyncio.sleep(0.02)

        calc = self.consumer.get_calculator("NIFTY")
        self.assertIsNotNone(calc)
        # Final processed tick should be t_late because flush sorted them chronologically
        self.assertEqual(calc.last_tick_timestamp, t_late)
        self.assertEqual(calc.last_ltp, 24500.0)
        print("✓ [g] Tick ordering: Ensured chronological ordering prior to indicator computation")

    def test_h_missing_tick_detection(self):
        """
        Verify gap detection warning emitted when time delta between ticks > 1.0s.
        """
        calc = DefaultIndicatorCalculator("AXISBANK")
        now = datetime.now(timezone.utc)
        t1 = Tick(symbol="AXISBANK", exchange="NSE", instrument_type="EQ", timestamp=now, ltp=1100.0, volume=10.0)
        t2 = Tick(symbol="AXISBANK", exchange="NSE", instrument_type="EQ", timestamp=now + timedelta(seconds=2.5), ltp=1102.0, volume=20.0)

        calc.process_tick(t1)
        with self.assertLogs("pipeline.indicator_calculator", level="WARNING") as cm:
            calc.process_tick(t2)
            self.assertTrue(any("Feed gap detected" in msg for msg in cm.output))
        print("✓ [h] Missing tick detection: Successfully detected and logged 2.5s gap warning")

    def test_i_pipeline_status(self):
        """
        Verify get_status returns complete telemetry dictionary.
        """
        status = self.manager.get_status()
        self.assertIn("is_running", status)
        self.assertIn("is_paused", status)
        self.assertIn("monitored_symbols", status)
        self.assertIn("consumed_ticks", status)
        self.assertIn("stale_symbols", status)
        self.assertIn("stale_count", status)
        print("✓ [i] Pipeline status: Verified complete telemetry payload")

    def test_j_performance(self):
        """
        Performance stress test: Verify 1,000 ticks processed in < 1.0 second.
        """
        calc = DefaultIndicatorCalculator("STRESS_TEST")
        now = datetime.now(timezone.utc)

        t_start = time.perf_counter()
        for i in range(1000):
            t = Tick(
                symbol="STRESS_TEST",
                exchange="NSE",
                instrument_type="EQ",
                timestamp=now + timedelta(milliseconds=i),
                ltp=100.0 + (i * 0.05),
                volume=1000.0 + i,
            )
            calc.process_tick(t)
        t_elapsed = time.perf_counter() - t_start

        self.assertEqual(calc.tick_count, 1000)
        self.assertLess(t_elapsed, 1.0, f"Processing took {t_elapsed:.4f}s, expected < 1.0s")
        print(f"✓ [j] Performance: Processed 1,000 ticks in {t_elapsed * 1000:.2f}ms (threshold < 1,000ms)")


async def run_standalone():
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 3 PROMPT 1/4 CORE PIPELINE ARCHITECTURE")
    print("=" * 70)

    suite = TestPipelineArchitecture()

    suite.setUp()
    await suite.test_a_tick_consumer()
    await suite.asyncTearDown()

    suite.setUp()
    suite.test_b_market_session_validation()
    await suite.asyncTearDown()

    suite.setUp()
    suite.test_c_stale_feed_detection()
    await suite.asyncTearDown()

    suite.setUp()
    suite.test_d_stale_feed_recovery()
    await suite.asyncTearDown()

    suite.setUp()
    await suite.test_e_pipeline_lifecycle()
    await suite.asyncTearDown()

    suite.setUp()
    await suite.test_f_indicator_calculator_registration()
    await suite.asyncTearDown()

    suite.setUp()
    await suite.test_g_tick_ordering()
    await suite.asyncTearDown()

    suite.setUp()
    suite.test_h_missing_tick_detection()
    await suite.asyncTearDown()

    suite.setUp()
    suite.test_i_pipeline_status()
    await suite.asyncTearDown()

    suite.setUp()
    suite.test_j_performance()
    await suite.asyncTearDown()

    print("=" * 70)
    print("ALL 10 PIPELINE ARCHITECTURE VERIFICATIONS (a-j) PASSED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_standalone())
