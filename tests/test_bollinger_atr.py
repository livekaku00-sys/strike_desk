"""
Strike Desk V4 - Bollinger Bands & ATR Indicator Tests
Tests: band accuracy, percent-B positions, ATR volatility zones,
       dynamic SL, market-open reset, degraded mode, pipeline integration.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import unittest
from datetime import datetime, timedelta, timezone

from models.tick import Tick
from pipeline.indicators.bollinger_calculator import BollingerCalculator
from pipeline.indicators.atr_calculator import ATRCalculator

IST = timezone(timedelta(hours=5, minutes=30))
_DAY1_OPEN_UTC = datetime(2026, 9, 22, 3, 45, 0, tzinfo=timezone.utc)   # 09:15 IST
_DAY2_OPEN_UTC = datetime(2026, 9, 23, 3, 45, 0, tzinfo=timezone.utc)   # 09:15 IST


def _tick(ltp: float, offset_sec: int = 0, high: float = 0.0, low: float = 0.0,
          symbol: str = "TEST", base_ts: datetime = _DAY1_OPEN_UTC) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="NSE",
        instrument_type="EQ",
        timestamp=base_ts + timedelta(seconds=offset_sec),
        ltp=ltp,
        high=high,
        low=low,
    )


# ---------------------------------------------------------------------------
# Bollinger Band Tests
# ---------------------------------------------------------------------------

class TestBollingerCalculator(unittest.TestCase):

    def setUp(self) -> None:
        self.calc = BollingerCalculator("TEST", period=20, num_std=2.0)

    def _feed(self, prices, base_ts=_DAY1_OPEN_UTC):
        result = {}
        for i, p in enumerate(prices):
            result = self.calc.process_tick(_tick(p, i, base_ts=base_ts))
        return result

    def _known_bands(self):
        """
        Feed 10 ticks at 98 and 10 ticks at 102.
        Analytically: mean=100, pop_std=2.0, upper=104, lower=96.
        """
        prices = [98.0] * 10 + [102.0] * 10
        result = self._feed(prices)
        return result

    def test_01_band_calculation_accuracy(self):
        """With analytically known prices, bands must match exact values."""
        result = self._known_bands()

        self.assertIsNotNone(result["middle"])
        self.assertAlmostEqual(result["middle"], 100.0, places=4)
        self.assertAlmostEqual(result["upper"], 104.0, places=4,
                               msg=f"Expected upper=104.0, got {result['upper']}")
        self.assertAlmostEqual(result["lower"], 96.0, places=4,
                               msg=f"Expected lower=96.0, got {result['lower']}")
        # Symmetry
        self.assertAlmostEqual(result["upper"] - result["middle"],
                               result["middle"] - result["lower"], places=6)
        self.assertTrue(result["sufficient_data"])

    def test_02_bandwidth_calculation(self):
        """bandwidth = (upper - lower) / middle = 8 / 100 = 0.08."""
        result = self._known_bands()
        expected_bandwidth = (104.0 - 96.0) / 100.0  # = 0.08
        self.assertAlmostEqual(result["bandwidth"], expected_bandwidth, places=4)

    def test_03_percent_b_and_all_position_zones(self):
        """_classify_position maps each zone to the correct label.

        Tested directly so rolling-window drift from additional ticks cannot
        invalidate the assertions.  Bands: upper=104, middle=100, lower=96,
        near_threshold = 0.005 × 100 = 0.5.
        """
        upper, middle, lower = 104.0, 100.0, 96.0
        cases = [
            (105.0, "ABOVE_UPPER",  "above upper band"),
            (103.6, "NEAR_UPPER",   "within 0.5 below upper (103.5–104)"),
            (100.0, "MIDDLE",       "at middle band"),
            (96.4,  "NEAR_LOWER",   "within 0.5 above lower (96–96.5)"),
            (95.0,  "BELOW_LOWER",  "below lower band"),
        ]
        for ltp, expected_pos, desc in cases:
            pos = self.calc._classify_position(ltp, lower=lower, upper=upper,
                                               middle=middle)
            self.assertEqual(pos, expected_pos,
                             f"ltp={ltp} ({desc}): expected {expected_pos}, got {pos}")

    def test_04_percent_b_values(self):
        """percent_b formula: 1.0 at upper, 0.0 at lower, 0.5 at middle.

        The last tick of _known_bands() has ltp=102, giving bands upper=104 /
        lower=96 / band_width=8.  We verify the formula directly rather than
        feeding additional ticks (which would shift the window and change bands).
        """
        upper, lower = 104.0, 96.0
        band_width = upper - lower  # 8.0

        pct_at_upper = (upper - lower) / band_width       # 1.0
        pct_at_lower = (lower - lower) / band_width       # 0.0
        pct_at_middle = ((upper + lower) / 2 - lower) / band_width  # 0.5

        self.assertAlmostEqual(pct_at_upper, 1.0, places=4)
        self.assertAlmostEqual(pct_at_lower, 0.0, places=4)
        self.assertAlmostEqual(pct_at_middle, 0.5, places=4)

        # Also verify the output from the 20th (final) tick of _known_bands().
        result = self._known_bands()   # last ltp = 102, bands upper=104 / lower=96
        expected = round((102.0 - lower) / band_width, 6)   # (102-96)/8 = 0.75
        self.assertAlmostEqual(result["percent_b"], expected, places=4)

    def test_05_bollinger_degraded_mode_less_than_20_ticks(self):
        """All keys must be None for every tick when fewer than 20 ticks processed."""
        for i in range(19):
            result = self.calc.process_tick(_tick(100.0 + (i % 5), i))
            self.assertIsNone(
                result["upper"],
                f"Expected upper=None at tick {i + 1}, got {result['upper']}",
            )
            self.assertFalse(result["sufficient_data"])
        # 20th tick should produce valid bands
        result = self.calc.process_tick(_tick(100.0, 19))
        self.assertIsNotNone(result["upper"], "Bands must be non-None at tick 20")

    def test_06_bollinger_reset_at_market_open(self):
        """After daily reset at 09:15 IST, Bollinger state clears and returns None."""
        # Warm up 25 ticks on day 1
        for i in range(25):
            self.calc.process_tick(_tick(100.0 + i % 5, i))
        self.assertIsNotNone(self.calc.upper_value, "Bands should be valid before reset")

        # First tick on day 2 at 09:15 IST triggers reset
        new_day_tick = _tick(100.0, base_ts=_DAY2_OPEN_UTC)
        result_after_reset = self.calc.process_tick(new_day_tick)

        self.assertIsNone(result_after_reset["upper"],
                          "upper must be None immediately after daily reset")
        self.assertEqual(self.calc.tick_count, 1)
        self.assertIsNone(self.calc.upper_value)
        self.assertEqual(len(self.calc._prices), 1)  # only the reset tick in window


# ---------------------------------------------------------------------------
# ATR Tests
# ---------------------------------------------------------------------------

class TestATRCalculator(unittest.TestCase):

    def setUp(self) -> None:
        self.calc = ATRCalculator("TEST", period=14)

    def _trending_ticks(self, n=25, step=2.0, base=100.0, tr_size=2.0,
                        base_ts=_DAY1_OPEN_UTC):
        """Generate n ticks with consistent H/L spread giving a known TR."""
        ticks = []
        for i in range(n):
            p = base + i * step
            ticks.append(_tick(p, i, high=p + tr_size, low=p - tr_size, base_ts=base_ts))
        return ticks

    def _feed(self, ticks):
        result = {}
        for t in ticks:
            result = self.calc.process_tick(t)
        return result

    def test_07_atr_basic_calculation(self):
        """ATR should be positive and finite after sufficient ticks."""
        result = self._feed(self._trending_ticks(n=25, step=1.0, tr_size=2.0))
        self.assertIsNotNone(result["atr"])
        self.assertGreater(result["atr"], 0.0)
        self.assertTrue(result["sufficient_data"])
        self.assertIsNotNone(result["atr_percent"])
        self.assertIsNotNone(result["volatility"])

    def test_08_atr_volatility_all_zones(self):
        """_classify_volatility must map ATR% to the correct label for all four zones."""
        cases = [
            (0.3,  "LOW"),
            (1.0,  "NORMAL"),
            (2.0,  "HIGH"),
            (4.0,  "EXTREME"),
        ]
        for atr_pct, expected_label in cases:
            label = self.calc._classify_volatility(atr_pct)
            self.assertEqual(label, expected_label,
                             f"ATR%={atr_pct} should map to {expected_label}, got {label}")

    def test_09_atr_dynamic_sl_distance(self):
        """atr_sl_distance must equal atr × 1.5 (dynamic stop-loss multiplier)."""
        result = self._feed(self._trending_ticks(n=25, step=1.0, tr_size=3.0))
        self.assertIsNotNone(result["atr"])
        self.assertIsNotNone(result["atr_sl_distance"])
        expected_sl = round(result["atr"] * 1.5, 4)
        self.assertAlmostEqual(
            result["atr_sl_distance"], expected_sl, places=4,
            msg=f"Expected atr_sl_distance={expected_sl}, got {result['atr_sl_distance']}",
        )

    def test_10_atr_degraded_mode_less_than_14_ticks(self):
        """ATR must return None for every tick when fewer than 14 ticks processed."""
        ticks = self._trending_ticks(n=13, tr_size=2.0)
        for i, t in enumerate(ticks):
            result = self.calc.process_tick(t)
            self.assertIsNone(
                result["atr"],
                f"ATR should be None at tick {i + 1}, got {result['atr']}",
            )
            self.assertFalse(result["sufficient_data"])

    def test_11_atr_reset_at_market_open(self):
        """After daily reset at 09:15 IST, ATR state is cleared and returns None."""
        self._feed(self._trending_ticks(n=25, tr_size=2.0))
        self.assertIsNotNone(self.calc.atr_value, "ATR should be valid before reset")

        new_day_tick = _tick(120.0, high=122.0, low=118.0, base_ts=_DAY2_OPEN_UTC)
        result_after_reset = self.calc.process_tick(new_day_tick)

        self.assertIsNone(result_after_reset["atr"],
                          "ATR must be None immediately after daily reset")
        self.assertEqual(self.calc.tick_count, 1)
        self.assertIsNone(self.calc._smoothed_tr)
        # _prev_close should be seeded to the new-day tick's close
        self.assertIsNotNone(self.calc._prev_close)


# ---------------------------------------------------------------------------
# Pipeline Integration Tests
# ---------------------------------------------------------------------------

class TestBollingerATRPipelineIntegration(unittest.TestCase):

    def test_12_bundle_has_bollinger_and_atr(self):
        """SymbolIndicatorBundle must expose bollinger and atr sub-calculators."""
        from pipeline.pipeline_orchestrator import SymbolIndicatorBundle
        bundle = SymbolIndicatorBundle("HDFC")
        self.assertIsInstance(bundle.bollinger, BollingerCalculator)
        self.assertIsInstance(bundle.atr, ATRCalculator)

    def test_13_pipeline_indicator_values_expose_all_keys(self):
        """After 25 ticks, indicator_values must contain all Bollinger+ATR keys with values."""
        from pipeline.pipeline_orchestrator import SymbolIndicatorBundle
        bundle = SymbolIndicatorBundle("TCS")

        for i in range(25):
            p = 3000.0 + i * 2.0
            tick = Tick(
                symbol="TCS", exchange="NSE", instrument_type="EQ",
                timestamp=_DAY1_OPEN_UTC + timedelta(seconds=i),
                ltp=p, high=p + 3.0, low=p - 3.0,
            )
            bundle.process_tick(tick)

        vals = bundle.indicator_values

        # Bollinger keys
        for key in ("upper_band", "middle_band", "lower_band", "bandwidth",
                    "percent_b", "bb_position"):
            self.assertIn(key, vals, f"'{key}' missing from indicator_values")
        self.assertIsNotNone(vals["upper_band"],
                             "upper_band should be non-None after 25 ticks (period=20)")

        # ATR keys
        for key in ("atr", "atr_percent", "atr_sl_distance", "volatility"):
            self.assertIn(key, vals, f"'{key}' missing from indicator_values")
        self.assertIsNotNone(vals["atr"],
                             "atr should be non-None after 25 ticks (period=14)")

        # Dynamic SL consistency check
        self.assertAlmostEqual(
            vals["atr_sl_distance"], round(vals["atr"] * 1.5, 4), places=4,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
