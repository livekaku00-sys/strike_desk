"""
Strike Desk V4 — Phase 3 Prompt 2/4 Verification Suite: Price & Volume Indicators (VWAP & EMA9)
Verifies:
  a) VWAP basic calculation (reference values across 5 ticks)
  b) VWAP zero volume handling (graceful handling, unchanged VWAP, zero-volume start)
  c) VWAP daily reset (new trading day after 09:15 IST resets cumulative values)
  d) EMA9 basic calculation (first 9 ticks = SMA, subsequent ticks = EMA formula)
  e) EMA insufficient data (< 9 ticks -> ema=None, sufficient_data=False)
  f) EMA reset (reset() clears state, starts fresh)
  g) Integration with pipeline (register VWAP & EMA with pipeline_manager, end-to-end processing)
  h) Performance (1,000 ticks processed by both calculators in < 100ms)
  i) State persistence (get_state() returns all required telemetry & indicator fields)
  j) Edge cases (extreme prices, fractional volumes, zero prices)
"""
import asyncio
from datetime import date, datetime, time as dtime, timedelta, timezone
import math
import os
import sys
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.tick import Tick
from pipeline import (
    BaseIndicatorCalculator,
    MarketSession,
    PipelineManager,
    TickBuffer,
    TickConsumer,
    market_session,
    pipeline_manager,
)
from pipeline.indicators import (
    CompositeIndicatorCalculator,
    EMACalculator,
    VWAPCalculator,
)

IST = timezone(timedelta(hours=5, minutes=30))


def make_tick(
    symbol: str = "NIFTY",
    price: float = 100.0,
    volume: float = 10.0,
    dt: datetime = None,
) -> Tick:
    if dt is None:
        dt = datetime(2026, 9, 21, 9, 30, 0, tzinfo=IST)
    return Tick(
        symbol=symbol,
        exchange="NSE",
        instrument_type="EQUITY",
        timestamp=dt,
        ltp=price,
        volume=volume,
    )


class TestPriceVolumeIndicators(unittest.IsolatedAsyncioTestCase):
    """
    Verification Suite for VWAP and EMA Indicators.
    """

    def test_a_vwap_basic_calculation(self):
        """[a] VWAP basic calculation against known reference values."""
        calc = VWAPCalculator("NIFTY")
        base_time = datetime(2026, 9, 21, 9, 30, 0, tzinfo=IST)

        ticks_data = [
            (100.0, 10.0, 1000.0, 10.0, 100.0),
            (102.0, 20.0, 3040.0, 30.0, 3040.0 / 30.0),      # ~101.333
            (101.0, 15.0, 4555.0, 45.0, 4555.0 / 45.0),      # ~101.222
            (105.0, 25.0, 7180.0, 70.0, 7180.0 / 70.0),      # ~102.571
            (98.0, 30.0, 10120.0, 100.0, 10120.0 / 100.0),   # 101.2
        ]

        for i, (price, vol, exp_pv, exp_vol, exp_vwap) in enumerate(ticks_data, 1):
            tick = make_tick("NIFTY", price, vol, base_time + timedelta(seconds=i))
            res = calc.process_tick(tick)

            self.assertEqual(res["tick_count"], i)
            self.assertAlmostEqual(calc.cumulative_pv, exp_pv, places=4)
            self.assertAlmostEqual(calc.cumulative_volume, exp_vol, places=4)
            self.assertAlmostEqual(res["cumulative_volume"], exp_vol, places=4)
            self.assertAlmostEqual(res["vwap"], exp_vwap, places=4)
            self.assertAlmostEqual(calc.vwap, exp_vwap, places=4)

        print(f"✓ [a] VWAP basic calculation: Processed 5 ticks, final VWAP = {calc.vwap:.4f} (expected: 101.2000)")

    def test_b_vwap_zero_volume_handling(self):
        """[b] VWAP zero volume handling without division by zero."""
        # 1. Starting with zero volume
        calc = VWAPCalculator("NIFTY")
        t0 = make_tick("NIFTY", price=150.0, volume=0.0)
        res0 = calc.process_tick(t0)
        self.assertEqual(res0["vwap"], 0.0)
        self.assertEqual(res0["cumulative_volume"], 0.0)
        self.assertEqual(res0["tick_count"], 1)

        # 2. Add volume
        t1 = make_tick("NIFTY", price=100.0, volume=10.0, dt=datetime(2026, 9, 21, 9, 31, 0, tzinfo=IST))
        res1 = calc.process_tick(t1)
        self.assertEqual(res1["vwap"], 100.0)
        self.assertEqual(res1["cumulative_volume"], 10.0)

        # 3. Add zero-volume tick - VWAP and cumulative volume must remain unchanged
        t2 = make_tick("NIFTY", price=120.0, volume=0.0, dt=datetime(2026, 9, 21, 9, 31, 1, tzinfo=IST))
        res2 = calc.process_tick(t2)
        self.assertEqual(res2["vwap"], 100.0)
        self.assertEqual(res2["cumulative_volume"], 10.0)
        self.assertEqual(res2["tick_count"], 3)

        print("✓ [b] VWAP zero volume handling: Zero-volume tick preserved VWAP=100.0 without division error")

    def test_c_vwap_daily_reset(self):
        """[c] VWAP daily reset at market open (09:15 IST)."""
        calc = VWAPCalculator("NIFTY")

        # Day 1: 21-Sep-2026 09:20:00 IST
        day1_t1 = make_tick("NIFTY", price=100.0, volume=10.0, dt=datetime(2026, 9, 21, 9, 20, 0, tzinfo=IST))
        day1_t2 = make_tick("NIFTY", price=110.0, volume=10.0, dt=datetime(2026, 9, 21, 9, 25, 0, tzinfo=IST))
        calc.process_tick(day1_t1)
        res1 = calc.process_tick(day1_t2)

        self.assertEqual(calc.last_reset_date, date(2026, 9, 21))
        self.assertEqual(res1["cumulative_volume"], 20.0)
        self.assertAlmostEqual(res1["vwap"], 105.0)

        # Check should_reset:
        # Pre-market on Day 2 (09:05 IST) -> should NOT reset yet
        pre_market_d2 = datetime(2026, 9, 22, 9, 5, 0, tzinfo=IST)
        self.assertFalse(calc.should_reset(pre_market_d2))

        # At or after 09:15 IST on Day 2 -> should reset
        market_open_d2 = datetime(2026, 9, 22, 9, 15, 1, tzinfo=IST)
        self.assertTrue(calc.should_reset(market_open_d2))

        # Process tick on Day 2
        day2_t1 = make_tick("NIFTY", price=200.0, volume=5.0, dt=market_open_d2)
        res2 = calc.process_tick(day2_t1)

        self.assertEqual(calc.last_reset_date, date(2026, 9, 22))
        self.assertEqual(res2["tick_count"], 1)
        self.assertEqual(res2["cumulative_volume"], 5.0)
        self.assertAlmostEqual(res2["vwap"], 200.0)
        self.assertAlmostEqual(calc.cumulative_pv, 1000.0)

        print("✓ [c] VWAP daily reset: Day 2 tick (09:15:01 IST) reset cumulative volume and PV to fresh session")

    def test_d_ema9_basic_calculation(self):
        """[d] EMA9 basic calculation: SMA for first 9 periods, EMA smoothing for subsequent periods."""
        calc = EMACalculator(period=9)
        base_time = datetime(2026, 9, 21, 9, 30, 0, tzinfo=IST)

        prices = [10.0, 11.0, 12.0, 11.0, 13.0, 12.0, 14.0, 13.0, 15.0, 14.0]

        # Feed first 9 ticks
        for i in range(9):
            tick = make_tick("NIFTY", price=prices[i], dt=base_time + timedelta(seconds=i))
            res = calc.process_tick(tick)
            if i < 8:
                self.assertIsNone(res["ema"])
                self.assertFalse(res["sufficient_data"])

        # Check 9th tick (First EMA = SMA of first 9 prices)
        expected_sma = sum(prices[:9]) / 9.0  # 111.0 / 9.0 = 12.3333...
        self.assertTrue(res["sufficient_data"])
        self.assertAlmostEqual(res["ema"], expected_sma, places=4)
        self.assertAlmostEqual(calc.ema_value, expected_sma, places=4)

        # Check 10th tick (EMA formula: 14.0 * 0.2 + SMA * 0.8)
        tick10 = make_tick("NIFTY", price=prices[9], dt=base_time + timedelta(seconds=9))
        res10 = calc.process_tick(tick10)
        expected_ema10 = (14.0 * 0.2) + (expected_sma * 0.8)
        self.assertTrue(res10["sufficient_data"])
        self.assertAlmostEqual(res10["ema"], expected_ema10, places=4)
        self.assertAlmostEqual(calc.ema_value, expected_ema10, places=4)

        # Also test configurable period (e.g. period=3, alpha=0.5)
        ema3 = EMACalculator(period=3)
        for p in [10.0, 20.0, 30.0]:
            ema3.process_tick(make_tick("NIFTY", price=p))
        self.assertAlmostEqual(ema3.ema_value, 20.0)  # SMA(10,20,30) = 20.0
        res_next = ema3.process_tick(make_tick("NIFTY", price=40.0))
        # 40 * 0.5 + 20 * 0.5 = 30.0
        self.assertAlmostEqual(res_next["ema"], 30.0)

        print(f"✓ [d] EMA9 basic calculation: 9-tick SMA = {expected_sma:.4f}, 10-tick EMA = {expected_ema10:.4f}")

    def test_e_ema_insufficient_data(self):
        """[e] EMA insufficient data handling (< 9 ticks)."""
        calc = EMACalculator(period=9)
        base_time = datetime(2026, 9, 21, 9, 30, 0, tzinfo=IST)

        for i in range(1, 9):
            tick = make_tick("NIFTY", price=100.0 + i, dt=base_time + timedelta(seconds=i))
            res = calc.process_tick(tick)
            self.assertIsNone(res["ema"])
            self.assertFalse(res["sufficient_data"])
            self.assertEqual(res["period"], 9)
            self.assertEqual(len(calc.prices), i)

        # 9th tick completes the requirement
        tick9 = make_tick("NIFTY", price=109.0, dt=base_time + timedelta(seconds=9))
        res9 = calc.process_tick(tick9)
        self.assertIsNotNone(res9["ema"])
        self.assertTrue(res9["sufficient_data"])

        print("✓ [e] EMA insufficient data: Returned ema=None and sufficient_data=False for ticks 1-8")

    def test_f_ema_reset(self):
        """[f] EMA reset clears state and allows fresh restart."""
        calc = EMACalculator("BANKNIFTY", period=9)
        for i in range(12):
            calc.process_tick(make_tick("BANKNIFTY", price=40000.0 + i))

        self.assertIsNotNone(calc.ema_value)
        self.assertEqual(len(calc.prices), 9)

        # Call reset
        calc.reset()
        self.assertIsNone(calc.ema_value)
        self.assertEqual(len(calc.prices), 0)
        self.assertEqual(calc.tick_count, 0)
        self.assertEqual(calc.indicator_values, {})

        # Next tick starts fresh
        fresh_res = calc.process_tick(make_tick("BANKNIFTY", price=41000.0))
        self.assertIsNone(fresh_res["ema"])
        self.assertFalse(fresh_res["sufficient_data"])
        self.assertEqual(calc.tick_count, 1)
        self.assertEqual(len(calc.prices), 1)

        print("✓ [f] EMA reset: Cleared price history and EMA value; restarted session on next tick")

    async def test_g_pipeline_integration(self):
        """[g] Integration with pipeline: Register VWAP and EMA calculators with PipelineManager."""
        custom_buffer = TickBuffer(buffer_window_ms=10)
        custom_session = MarketSession(bypass_market_hours=True)
        custom_consumer = TickConsumer(buffer=custom_buffer, session_validator=custom_session)
        manager = PipelineManager(consumer=custom_consumer, buffer=custom_buffer, session_validator=custom_session)

        symbols = ["TCS", "RELIANCE", "INFY"]
        await manager.initialize(symbols)

        vwap_tcs = VWAPCalculator("TCS")
        ema_rel = EMACalculator("RELIANCE", period=9)
        comp_infy = CompositeIndicatorCalculator(
            "INFY",
            [VWAPCalculator("INFY"), EMACalculator("INFY", period=9)],
        )

        manager.register_indicator_calculator("TCS", vwap_tcs)
        manager.register_indicator_calculator("RELIANCE", ema_rel)
        manager.register_indicator_calculator("INFY", comp_infy)

        # Push ticks
        base_t = datetime(2026, 9, 21, 10, 0, 0, tzinfo=IST)
        # Push 2 ticks for TCS
        await custom_buffer.push(make_tick("TCS", price=3500.0, volume=10.0, dt=base_t))
        await custom_buffer.push(make_tick("TCS", price=3510.0, volume=10.0, dt=base_t + timedelta(seconds=1)))

        # Push 10 ticks for RELIANCE
        for i in range(10):
            await custom_buffer.push(make_tick("RELIANCE", price=2500.0 + i, volume=5.0, dt=base_t + timedelta(seconds=i)))

        # Push 10 ticks for INFY
        for i in range(10):
            await custom_buffer.push(make_tick("INFY", price=1500.0 + i, volume=10.0, dt=base_t + timedelta(seconds=i)))

        # Allow consumer loop and flush loop to process
        await asyncio.sleep(0.15)

        tcs_values = manager.get_indicator_values("TCS")
        rel_values = manager.get_indicator_values("RELIANCE")
        infy_values = manager.get_indicator_values("INFY")

        self.assertIn("vwap", tcs_values)
        self.assertAlmostEqual(tcs_values["vwap"], 3505.0)

        self.assertIn("ema", rel_values)
        self.assertTrue(rel_values["sufficient_data"])

        self.assertIn("vwap", infy_values)
        self.assertIn("ema", infy_values)
        self.assertTrue(infy_values["sufficient_data"])

        await manager.stop()
        print("✓ [g] Integration with pipeline: Successfully registered VWAP, EMA, and Composite with PipelineManager")

    def test_h_performance_benchmark(self):
        """[h] Performance: Feed 1000 ticks, verify both indicators compute in < 100ms."""
        vwap = VWAPCalculator("NIFTY")
        ema = EMACalculator("NIFTY", period=9)
        base_time = datetime(2026, 9, 21, 9, 15, 0, tzinfo=IST)

        ticks = [
            make_tick(
                "NIFTY",
                price=100.0 + (i % 50),
                volume=10.0 + (i % 20),
                dt=base_time + timedelta(milliseconds=i * 10),
            )
            for i in range(1000)
        ]

        t0 = time.perf_counter()
        for tick in ticks:
            vwap.process_tick(tick)
            ema.process_tick(tick)
        elapsed_sec = time.perf_counter() - t0
        elapsed_ms = elapsed_sec * 1000.0

        self.assertLess(elapsed_ms, 100.0, f"Benchmark failed: took {elapsed_ms:.2f}ms (> 100ms threshold)")
        self.assertEqual(vwap.tick_count, 1000)
        self.assertEqual(ema.tick_count, 1000)
        print(f"✓ [h] Performance: Processed 1,000 ticks in {elapsed_ms:.2f}ms (threshold: < 100ms)")

    def test_i_state_persistence(self):
        """[i] State persistence: Verify get_state() returns all required fields."""
        vwap = VWAPCalculator("NIFTY")
        ema = EMACalculator("NIFTY", period=9)

        base_time = datetime(2026, 9, 21, 9, 30, 0, tzinfo=IST)
        for i in range(10):
            t = make_tick("NIFTY", price=100.0 + i, volume=10.0, dt=base_time + timedelta(seconds=i))
            vwap.process_tick(t)
            ema.process_tick(t)

        vwap_state = vwap.get_state()
        ema_state = ema.get_state()

        # VWAP state assertions
        for key in ["symbol", "tick_count", "last_tick_timestamp", "last_ltp", "indicator_values", "cumulative_pv", "cumulative_volume", "vwap", "last_reset_date"]:
            self.assertIn(key, vwap_state, f"Missing key '{key}' in VWAP get_state()")
        self.assertEqual(vwap_state["tick_count"], 10)
        self.assertEqual(vwap_state["cumulative_volume"], 100.0)

        # EMA state assertions
        for key in ["symbol", "tick_count", "last_tick_timestamp", "last_ltp", "indicator_values", "period", "alpha", "prices", "ema_value", "ema", "sufficient_data"]:
            self.assertIn(key, ema_state, f"Missing key '{key}' in EMA get_state()")
        self.assertEqual(ema_state["period"], 9)
        self.assertTrue(ema_state["sufficient_data"])

        print("✓ [i] State persistence: Verified complete telemetry payload on VWAP and EMA get_state()")

    def test_j_edge_cases(self):
        """[j] Edge cases: Extreme prices, micro volumes, single tick calculation."""
        vwap = VWAPCalculator("EXTREME")
        ema = EMACalculator("EXTREME", period=5)

        # Micro-fractional volume & large price
        t_huge = make_tick("EXTREME", price=100_000_000.0, volume=0.000001)
        res_v = vwap.process_tick(t_huge)
        res_e = ema.process_tick(t_huge)

        self.assertAlmostEqual(res_v["vwap"], 100_000_000.0)
        self.assertFalse(math.isnan(res_v["vwap"]))
        self.assertFalse(math.isinf(res_v["vwap"]))

        # Micro price & large volume
        t_micro = make_tick("EXTREME", price=0.0001, volume=10_000_000.0)
        res_v2 = vwap.process_tick(t_micro)
        self.assertGreater(res_v2["vwap"], 0.0)
        self.assertFalse(math.isnan(res_v2["vwap"]))
        self.assertFalse(math.isinf(res_v2["vwap"]))

        # Check negative period error handling
        with self.assertRaises(ValueError):
            EMACalculator(period=0)

        print("✓ [j] Edge cases: Handled extreme values (1e8 price, 1e-6 volume) without numerical instability")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 3 PROMPT 2/4 PRICE & VOLUME INDICATORS")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPriceVolumeIndicators)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 10 PRICE & VOLUME INDICATOR TESTS (a-j) PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)