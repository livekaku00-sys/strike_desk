"""
Strike Desk V4 - RSI & ADX Indicator Tests
Tests: oversold/overbought, trend strength, degraded mode, market-open reset,
       and pipeline_orchestrator integration.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timedelta, timezone

from models.tick import Tick
from pipeline.indicators.rsi_calculator import RSICalculator
from pipeline.indicators.adx_calculator import ADXCalculator

# IST = UTC+05:30  →  09:15 IST = 03:45 UTC
IST = timezone(timedelta(hours=5, minutes=30))
_DAY1_OPEN_UTC = datetime(2026, 9, 22, 3, 45, 0, tzinfo=timezone.utc)   # 09:15 IST
_DAY2_OPEN_UTC = datetime(2026, 9, 23, 3, 45, 0, tzinfo=timezone.utc)   # 09:15 IST next day


def _tick(ltp: float, offset_sec: int = 0, high: float = 0.0, low: float = 0.0,
          base_ts: datetime = _DAY1_OPEN_UTC) -> Tick:
    return Tick(
        symbol="TEST",
        exchange="NSE",
        instrument_type="EQ",
        timestamp=base_ts + timedelta(seconds=offset_sec),
        ltp=ltp,
        high=high,
        low=low,
    )


# ---------------------------------------------------------------------------
# RSI Tests
# ---------------------------------------------------------------------------

class TestRSICalculator(unittest.TestCase):

    def setUp(self) -> None:
        self.calc = RSICalculator("TEST", period=14)

    def _feed(self, prices, base_ts=_DAY1_OPEN_UTC):
        result = {}
        for i, p in enumerate(prices):
            result = self.calc.process_tick(_tick(p, i, base_ts=base_ts))
        return result

    def test_01_rsi_oversold_detection(self):
        """Steadily declining prices produce RSI < 30 (OVERSOLD zone)."""
        # 20 ticks: 100 → 81 (all down moves, avg_gain=0)
        prices = [100.0 - i for i in range(20)]
        result = self._feed(prices)
        self.assertIsNotNone(result["rsi"], "RSI should be computed after 20 ticks")
        self.assertLess(result["rsi"], 30.0, f"Expected RSI < 30, got {result['rsi']}")
        self.assertEqual(result["zone"], "OVERSOLD")
        self.assertFalse(result["sufficient_data"] is False)

    def test_02_rsi_overbought_detection(self):
        """Steadily rising prices produce RSI > 70 (OVERBOUGHT zone)."""
        # 20 ticks: 80 → 99 (all up moves, avg_loss=0)
        prices = [80.0 + i for i in range(20)]
        result = self._feed(prices)
        self.assertIsNotNone(result["rsi"])
        self.assertGreater(result["rsi"], 70.0, f"Expected RSI > 70, got {result['rsi']}")
        self.assertEqual(result["zone"], "OVERBOUGHT")

    def test_03_rsi_neutral_zone(self):
        """Perfectly alternating +1/-1 changes produce RSI=50 (NEUTRAL zone)."""
        # 100, 101, 100, 101, ... → avg_gain == avg_loss → RS=1 → RSI=50
        prices = [100.0 + (i % 2) for i in range(25)]
        result = self._feed(prices)
        self.assertIsNotNone(result["rsi"])
        self.assertEqual(result["zone"], "NEUTRAL",
                         f"Expected NEUTRAL, got {result['zone']} (RSI={result['rsi']:.2f})")

    def test_04_rsi_degraded_mode_less_than_14_ticks(self):
        """RSI must return None for every tick when fewer than 14 ticks have been processed."""
        prices = [100.0 + (i % 5) for i in range(13)]  # 13 ticks
        for i, p in enumerate(prices):
            result = self.calc.process_tick(_tick(p, i))
            self.assertIsNone(
                result["rsi"],
                f"RSI should be None at tick {i + 1}, got {result['rsi']}",
            )
            self.assertFalse(result["sufficient_data"])

    def test_05_rsi_reset_at_market_open(self):
        """After daily reset at 09:15 IST, RSI state is cleared and returns None."""
        # Warm up with 20 ticks on day 1
        prices = [100.0 - i for i in range(20)]
        result = self._feed(prices)
        self.assertIsNotNone(result["rsi"], "RSI should be valid before reset")

        # First tick on next day at 09:15 IST triggers reset
        new_day_tick = _tick(90.0, base_ts=_DAY2_OPEN_UTC)
        result_after_reset = self.calc.process_tick(new_day_tick)

        self.assertIsNone(
            result_after_reset["rsi"],
            "RSI should be None immediately after daily reset (only 1 tick available)",
        )
        self.assertEqual(self.calc.tick_count, 1, "tick_count should reset to 1")
        self.assertIsNone(self.calc._avg_gain)
        self.assertIsNone(self.calc._avg_loss)


# ---------------------------------------------------------------------------
# ADX Tests
# ---------------------------------------------------------------------------

class TestADXCalculator(unittest.TestCase):

    def setUp(self) -> None:
        self.calc = ADXCalculator("TEST", period=14)

    def _feed(self, ticks):
        result = {}
        for t in ticks:
            result = self.calc.process_tick(t)
        return result

    def _trending_ticks(self, n=25, step=2.0, base=100.0, base_ts=_DAY1_OPEN_UTC):
        """Generate n ticks with a consistent uptrend and clear H/L."""
        ticks = []
        for i in range(n):
            p = base + i * step
            ticks.append(_tick(p, i, high=p + 1.0, low=p - 1.0, base_ts=base_ts))
        return ticks

    def _oscillating_ticks(self, n=30, base=100.0, amplitude=0.5, base_ts=_DAY1_OPEN_UTC):
        """Generate n ticks that zigzag with very small moves (low ADX)."""
        ticks = []
        for i in range(n):
            p = base + amplitude if i % 2 == 0 else base - amplitude
            ticks.append(_tick(p, i, high=p + 0.2, low=p - 0.2, base_ts=base_ts))
        return ticks

    def test_06_adx_strong_trend_detection(self):
        """Consistent directional movement produces STRONG or VERY_STRONG ADX."""
        result = self._feed(self._trending_ticks(n=30, step=2.0))
        self.assertIsNotNone(result["adx"], "ADX should be computed after 30 ticks")
        self.assertIn(
            result["trend_strength"], ["STRONG", "VERY_STRONG"],
            f"Expected STRONG/VERY_STRONG, got {result['trend_strength']} (ADX={result['adx']:.2f})",
        )
        self.assertTrue(result["sufficient_data"])

    def test_07_adx_weak_trend_detection(self):
        """Oscillating prices produce WEAK or MODERATE ADX (< 25)."""
        result = self._feed(self._oscillating_ticks(n=30))
        self.assertIsNotNone(result["adx"])
        self.assertLess(
            result["adx"], 25.0,
            f"Expected ADX < 25 (no trend), got {result['adx']:.2f}",
        )

    def test_08_adx_trend_strength_classification(self):
        """All four trend-strength zones map to the correct label."""
        thresholds = [
            (10.0, "WEAK"),
            (22.0, "MODERATE"),
            (35.0, "STRONG"),
            (50.0, "VERY_STRONG"),
        ]
        for adx_val, expected_label in thresholds:
            self.calc.adx_value = adx_val
            label = self.calc._classify_trend(adx_val)
            self.assertEqual(label, expected_label,
                             f"ADX={adx_val} should map to {expected_label}, got {label}")

    def test_09_adx_degraded_mode_less_than_14_ticks(self):
        """ADX must return None for every tick when fewer than 14 ticks processed."""
        ticks = self._trending_ticks(n=13)
        for i, t in enumerate(ticks):
            result = self.calc.process_tick(t)
            self.assertIsNone(
                result["adx"],
                f"ADX should be None at tick {i + 1}, got {result['adx']}",
            )
            self.assertFalse(result["sufficient_data"])

    def test_10_adx_reset_at_market_open(self):
        """After daily reset at 09:15 IST, ADX state is cleared and returns None."""
        # Warm up 25 ticks on day 1
        self._feed(self._trending_ticks(n=25))
        self.assertIsNotNone(self.calc.adx_value, "ADX should be valid before reset")

        # First tick on day 2 at 09:15 IST triggers reset
        new_day_tick = _tick(150.0, high=151.0, low=149.0, base_ts=_DAY2_OPEN_UTC)
        result_after_reset = self.calc.process_tick(new_day_tick)

        self.assertIsNone(
            result_after_reset["adx"],
            "ADX should be None immediately after daily reset",
        )
        self.assertEqual(self.calc.tick_count, 1)
        self.assertIsNone(self.calc._smoothed_tr)
        # _prev_close is correctly seeded to the new day's first tick price
        self.assertIsNotNone(self.calc._prev_close)


# ---------------------------------------------------------------------------
# Pipeline Orchestrator Integration Tests
# ---------------------------------------------------------------------------

class TestRSIADXPipelineIntegration(unittest.TestCase):

    def test_11_symbol_indicator_bundle_has_rsi_adx(self):
        """SymbolIndicatorBundle must instantiate RSI and ADX calculators."""
        from pipeline.pipeline_orchestrator import SymbolIndicatorBundle
        bundle = SymbolIndicatorBundle("INFY")
        self.assertIsInstance(bundle.rsi, RSICalculator,
                              "bundle.rsi must be an RSICalculator")
        self.assertIsInstance(bundle.adx, ADXCalculator,
                              "bundle.adx must be an ADXCalculator")

    def test_12_pipeline_indicator_values_expose_rsi_adx_keys(self):
        """After processing ticks, indicator_values dict must contain 'rsi' and 'adx' keys."""
        from pipeline.pipeline_orchestrator import SymbolIndicatorBundle
        bundle = SymbolIndicatorBundle("RELIANCE")

        for i in range(20):
            p = 100.0 + i
            t = _tick(p, i, high=p + 0.5, low=p - 0.5)
            t = Tick(symbol="RELIANCE", exchange="NSE", instrument_type="EQ",
                     timestamp=_DAY1_OPEN_UTC + timedelta(seconds=i),
                     ltp=p, high=p + 0.5, low=p - 0.5)
            bundle.process_tick(t)

        vals = bundle.indicator_values
        self.assertIn("rsi", vals, "'rsi' key missing from indicator_values")
        self.assertIn("rsi_zone", vals, "'rsi_zone' key missing from indicator_values")
        self.assertIn("adx", vals, "'adx' key missing from indicator_values")
        self.assertIn("plus_di", vals, "'plus_di' key missing from indicator_values")
        self.assertIn("minus_di", vals, "'minus_di' key missing from indicator_values")
        self.assertIn("adx_trend_strength", vals, "'adx_trend_strength' key missing")
        # RSI should have a value after 20 ticks (≥ 14)
        self.assertIsNotNone(vals["rsi"], "RSI should be non-None after 20 ticks")
        # ADX should have a value after 20 ticks (≥ 14)
        self.assertIsNotNone(vals["adx"], "ADX should be non-None after 20 ticks")


if __name__ == "__main__":
    unittest.main(verbosity=2)
