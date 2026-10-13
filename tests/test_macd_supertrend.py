"""
Strike Desk V4 - MACD & Supertrend Indicator Tests
Tests: MACD crossover detection, histogram sign change, Supertrend direction flip,
       confidence boost on dual alignment, degraded mode, reset at market open,
       pipeline integration.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timedelta, timezone

from models.tick import Tick
from pipeline.indicators.macd_calculator import MACDCalculator
from pipeline.indicators.supertrend_calculator import SupertrendCalculator

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
        high=high if high > 0.0 else ltp,
        low=low if low > 0.0 else ltp,
    )


# ---------------------------------------------------------------------------
# MACD Calculator Tests
# ---------------------------------------------------------------------------

class TestMACDCalculator(unittest.TestCase):

    def setUp(self) -> None:
        self.calc = MACDCalculator("TEST", fast=12, slow=26, signal=9)

    def _feed(self, prices, base_ts=_DAY1_OPEN_UTC):
        result = {}
        for i, p in enumerate(prices):
            result = self.calc.process_tick(_tick(p, i, base_ts=base_ts))
        return result

    def test_01_macd_degraded_less_than_26_ticks(self):
        """macd_line must be None for every tick when fewer than 26 ticks processed."""
        for i in range(25):
            result = self.calc.process_tick(_tick(100.0, i))
            self.assertIsNone(
                result["macd_line"],
                f"Expected macd_line=None at tick {i + 1}, got {result['macd_line']}",
            )
            self.assertFalse(result["sufficient_data"])
        # 26th tick: slow EMA seeds, macd_line becomes available
        result = self.calc.process_tick(_tick(100.0, 25))
        self.assertIsNotNone(result["macd_line"], "macd_line must be non-None at tick 26")

    def test_02_signal_line_available_after_34_ticks(self):
        """signal_line requires 9 MACD values; first available at tick 34."""
        prices = [100.0] * 34
        result = self._feed(prices)
        self.assertIsNotNone(result["signal_line"],
                             "signal_line must be non-None at tick 34 (26 slow seed + 8 signal seed)")
        self.assertTrue(result["sufficient_data"])

    def test_03_bullish_crossover_detected(self):
        """MACD line crossing above signal line must produce BULLISH_CROSS."""
        # Phase 1: 40 ticks at falling price → MACD goes negative
        descending = [200.0 - i * 0.5 for i in range(40)]
        self._feed(descending)
        # Phase 2: reset fresh for clean test
        self.calc.reset()
        self.calc.last_reset_date = _DAY1_OPEN_UTC.astimezone(IST).date()

        # Constant price then sharp upward spike to force MACD to cross signal
        constant = [100.0] * 40
        for i, p in enumerate(constant):
            self.calc.process_tick(_tick(p, i))

        # Now feed sharp upward price to force fast EMA above slow EMA faster
        crossover_detected = False
        prev_histogram = None
        for i, spike in enumerate([120.0] * 20, start=40):
            result = self.calc.process_tick(_tick(spike, i))
            if result.get("crossover") == "BULLISH_CROSS":
                crossover_detected = True
                break
            if result.get("histogram") is not None:
                if prev_histogram is not None and prev_histogram <= 0 and result["histogram"] > 0:
                    crossover_detected = True
                    break
                prev_histogram = result["histogram"]

        self.assertTrue(crossover_detected,
                        "Expected BULLISH_CROSS when price jumps from 100 to 120 after constant phase")

    def test_04_bearish_crossover_detected(self):
        """MACD line crossing below signal line must produce BEARISH_CROSS."""
        # Feed rising prices then sudden drop
        rising = [100.0 + i * 0.5 for i in range(40)]
        self._feed(rising)

        bearish_detected = False
        prev_histogram = None
        for i, drop in enumerate([70.0] * 20, start=40):
            result = self.calc.process_tick(_tick(drop, i))
            if result.get("crossover") == "BEARISH_CROSS":
                bearish_detected = True
                break
            if result.get("histogram") is not None:
                if prev_histogram is not None and prev_histogram >= 0 and result["histogram"] < 0:
                    bearish_detected = True
                    break
                prev_histogram = result["histogram"]

        self.assertTrue(bearish_detected,
                        "Expected BEARISH_CROSS when price drops sharply after rising phase")

    def test_05_histogram_equals_macd_minus_signal(self):
        """histogram must equal macd_line - signal_line exactly."""
        prices = [100.0 + i * 0.3 for i in range(50)]
        result = self._feed(prices)
        if result["sufficient_data"]:
            self.assertIsNotNone(result["histogram"])
            expected = round(result["macd_line"] - result["signal_line"], 4)
            self.assertAlmostEqual(result["histogram"], expected, places=4)

    def test_06_trend_positive_when_macd_above_zero(self):
        """trend must be BULLISH when macd_line > 0, BEARISH when < 0."""
        # Rising prices → fast EMA rises faster → MACD positive → BULLISH
        rising = [100.0 + i * 2.0 for i in range(50)]
        result = self._feed(rising)
        if result["macd_line"] is not None:
            if result["macd_line"] > 0:
                self.assertEqual(result["trend"], "BULLISH")
            elif result["macd_line"] < 0:
                self.assertEqual(result["trend"], "BEARISH")

    def test_07_macd_reset_at_market_open(self):
        """After daily reset at 09:15 IST, MACD state clears and returns None."""
        # Warm up with 40 ticks on day 1
        for i in range(40):
            self.calc.process_tick(_tick(100.0 + i * 0.5, i))
        self.assertIsNotNone(self.calc.macd_line, "macd_line should be valid before reset")

        # First tick on day 2 triggers reset
        new_day_tick = _tick(120.0, base_ts=_DAY2_OPEN_UTC)
        result = self.calc.process_tick(new_day_tick)

        self.assertIsNone(result["macd_line"], "macd_line must be None after daily reset")
        self.assertEqual(self.calc.tick_count, 1)
        self.assertIsNone(self.calc._fast_ema)
        self.assertIsNone(self.calc._slow_ema)


# ---------------------------------------------------------------------------
# Supertrend Calculator Tests
# ---------------------------------------------------------------------------

class TestSupertrendCalculator(unittest.TestCase):

    def setUp(self) -> None:
        self.calc = SupertrendCalculator("TEST", period=10, multiplier=3.0)

    def _trending_ticks(self, n=20, base=100.0, step=1.0, tr_size=2.0,
                        base_ts=_DAY1_OPEN_UTC):
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

    def test_08_supertrend_up_when_rising(self):
        """After enough ticks with rising prices, direction should be UP."""
        result = self._feed(self._trending_ticks(n=25, step=2.0, tr_size=3.0))
        self.assertIsNotNone(result["supertrend"])
        self.assertEqual(result["direction"], "UP")
        self.assertEqual(result["signal"], "BUY")
        self.assertTrue(result["sufficient_data"])

    def test_09_supertrend_direction_flip_trend_changed(self):
        """When price drops sharply through the supertrend line, trend_changed must be True."""
        # Build UP trend with strong rising prices
        rising = self._trending_ticks(n=30, step=3.0, tr_size=2.0)
        for t in rising:
            result = self.calc.process_tick(t)

        # Confirm we're in UP direction
        self.assertIsNotNone(result["supertrend"])
        self.assertEqual(result["direction"], "UP", "Must establish UP trend first")

        # Now drop sharply below the supertrend line
        st_val = result["supertrend"]  # current supertrend line (= lower_band when UP)
        crash_price = st_val - 20.0   # well below the supertrend line
        trend_changed_tick = _tick(crash_price, 31, high=crash_price + 1.0, low=crash_price - 1.0)
        result = self.calc.process_tick(trend_changed_tick)

        # Direction should flip to DOWN with trend_changed = True
        self.assertEqual(result["direction"], "DOWN",
                         f"Expected direction=DOWN after crash, got {result['direction']}")
        self.assertTrue(result["trend_changed"],
                        "trend_changed must be True on the flip tick")

    def test_10_trend_changed_false_when_steady(self):
        """trend_changed must be False when direction is maintained."""
        result = self._feed(self._trending_ticks(n=25, step=2.0, tr_size=2.0))
        # Steady UP trend — last tick should not have changed direction
        self.assertFalse(result["trend_changed"],
                         "trend_changed should be False when trend continues")

    def test_11_supertrend_degraded_less_than_period_ticks(self):
        """Supertrend must return None until ATR is ready (ATR seeds at tick period)."""
        # ATR(period=10) seeds at tick 10: prev_close seeded at tick 1,
        # then interval_count reaches period-1=9 at tick 10 → ATR ready.
        # So ticks 1-9 must all return None; tick 10 is the first valid output.
        ticks = self._trending_ticks(n=9, step=1.0, tr_size=2.0)
        for i, t in enumerate(ticks):
            result = self.calc.process_tick(t)
            self.assertIsNone(
                result["supertrend"],
                f"supertrend should be None at tick {i + 1}, got {result['supertrend']}",
            )
            self.assertFalse(result["sufficient_data"])
        # Tick 10 must produce a valid supertrend
        tick10 = _tick(109.0, 9, high=111.0, low=107.0)
        result = self.calc.process_tick(tick10)
        self.assertIsNotNone(result["supertrend"],
                             "supertrend should be non-None at tick 10 (ATR seeds)")

    def test_12_supertrend_reset_at_market_open(self):
        """After daily reset at 09:15 IST, Supertrend state clears and returns None."""
        # Warm up on day 1
        self._feed(self._trending_ticks(n=25, step=2.0))
        self.assertIsNotNone(self.calc.supertrend_value, "supertrend should be valid before reset")

        # Day 2 first tick triggers reset
        new_day_tick = _tick(130.0, high=132.0, low=128.0, base_ts=_DAY2_OPEN_UTC)
        result = self.calc.process_tick(new_day_tick)

        self.assertIsNone(result["supertrend"],
                          "supertrend must be None immediately after daily reset")
        self.assertEqual(self.calc.tick_count, 1)
        self.assertIsNone(self.calc.supertrend_value)
        self.assertIsNone(self.calc._prev_direction)


# ---------------------------------------------------------------------------
# Confidence Boost Tests
# ---------------------------------------------------------------------------

class TestConfidenceBoost(unittest.TestCase):
    """Verify the +5 boost when MACD crossover and Supertrend direction-change align."""

    def _make_indicators_long(self, macd_cross: str = "NO_CROSS",
                               st_changed: bool = False) -> dict:
        """
        Returns indicator dict for a LONG signal at StockCashStrategy.
        Base score: ema9_crossover=True (30 pts) + volume_surge=True (20 pts) = 50.
        """
        return {
            "price": 105.0,
            "ltp": 105.0,
            "ema9": 100.0,
            "vwap": 100.0,
            # Force explicit criterion values (override auto-computation)
            "ema9_crossover": True,        # 30 pts
            "vwap_alignment": False,       # 0 pts
            "volume_surge": True,          # 20 pts
            "pcr_alignment": False,        # 0 pts
            "sentiment_alignment": False,  # 0 pts
            # macd_crossover criterion: False (macd_line/signal_line not set → False)
            # supertrend_direction criterion: False (no st_dir set)
            # boost trigger fields:
            "macd_crossover": macd_cross,
            "supertrend_trend_changed": st_changed,
            "direction": "LONG",
            "symbol": "TESTSTOCK",
            "timestamp": _DAY1_OPEN_UTC,
        }

    def test_13_confidence_boost_long_when_macd_and_supertrend_align(self):
        """
        With base score = 50 (below 55 threshold), adding BULLISH_CROSS + trend_changed
        should boost to 55 and generate a signal.
        """
        from strategies.stock_cash_strategy import StockCashStrategy

        strategy = StockCashStrategy()
        market_state = {"direction": "LONG", "symbol": "TESTSTOCK",
                        "timestamp": _DAY1_OPEN_UTC}

        # Without boost: score = 50, no signal
        ind_no_boost = self._make_indicators_long(macd_cross="NO_CROSS", st_changed=False)
        signal_no_boost = strategy.evaluate(ind_no_boost, market_state)
        self.assertIsNone(signal_no_boost,
                          "Score 50 (below 55) should produce no signal without boost")

        # With boost: score = 50 + 5 = 55, signal created
        ind_boost = self._make_indicators_long(macd_cross="BULLISH_CROSS", st_changed=True)
        signal_boost = strategy.evaluate(ind_boost, market_state)
        self.assertIsNotNone(signal_boost,
                             "Score 50 + 5 boost = 55 should produce a signal")
        self.assertAlmostEqual(signal_boost.confidence_score, 55.0, places=1)

    def test_14_no_boost_when_only_supertrend_changes(self):
        """Boost must NOT apply when trend_changed=True but no MACD crossover."""
        from strategies.stock_cash_strategy import StockCashStrategy

        strategy = StockCashStrategy()
        market_state = {"direction": "LONG", "symbol": "TESTSTOCK",
                        "timestamp": _DAY1_OPEN_UTC}

        ind = self._make_indicators_long(macd_cross="NO_CROSS", st_changed=True)
        signal = strategy.evaluate(ind, market_state)
        self.assertIsNone(signal,
                          "No boost when trend_changed but macd_crossover is NO_CROSS")

    def test_15_no_boost_on_direction_mismatch(self):
        """Boost must NOT apply when crossover direction is opposite to trade direction."""
        from strategies.stock_cash_strategy import StockCashStrategy

        strategy = StockCashStrategy()
        market_state = {"direction": "LONG", "symbol": "TESTSTOCK",
                        "timestamp": _DAY1_OPEN_UTC}

        # LONG trade but BEARISH_CROSS — misaligned, no boost
        ind = self._make_indicators_long(macd_cross="BEARISH_CROSS", st_changed=True)
        signal = strategy.evaluate(ind, market_state)
        self.assertIsNone(signal,
                          "No boost when BEARISH_CROSS for a LONG trade direction")


# ---------------------------------------------------------------------------
# Pipeline Integration Tests
# ---------------------------------------------------------------------------

class TestMACDSupertrendPipelineIntegration(unittest.TestCase):

    def test_16_bundle_has_macd_and_supertrend(self):
        """SymbolIndicatorBundle must expose macd and supertrend sub-calculators."""
        from pipeline.pipeline_orchestrator import SymbolIndicatorBundle

        bundle = SymbolIndicatorBundle("HDFC")
        self.assertIsInstance(bundle.macd, MACDCalculator)
        self.assertIsInstance(bundle.supertrend, SupertrendCalculator)

    def test_17_pipeline_indicator_values_expose_all_macd_supertrend_keys(self):
        """After 50 ticks, indicator_values must contain all MACD+Supertrend keys."""
        from pipeline.pipeline_orchestrator import SymbolIndicatorBundle

        bundle = SymbolIndicatorBundle("RELIANCE")
        for i in range(50):
            p = 2000.0 + i * 5.0
            tick = Tick(
                symbol="RELIANCE", exchange="NSE", instrument_type="EQ",
                timestamp=_DAY1_OPEN_UTC + timedelta(seconds=i),
                ltp=p, high=p + 10.0, low=p - 10.0,
            )
            bundle.process_tick(tick)

        vals = bundle.indicator_values

        # MACD keys
        for key in ("macd_line", "signal_line", "histogram", "macd_crossover", "macd_trend"):
            self.assertIn(key, vals, f"'{key}' missing from indicator_values")

        # At tick 50: slow EMA (26 ticks) seeded at tick 26, signal seeded at tick 34
        # So at tick 50, macd_line and signal_line should be non-None
        self.assertIsNotNone(vals["macd_line"],
                             "macd_line should be non-None after 50 ticks (slow period=26)")
        self.assertIsNotNone(vals["signal_line"],
                             "signal_line should be non-None after 50 ticks (34 ticks needed)")

        # Supertrend keys
        for key in ("supertrend", "supertrend_direction", "supertrend_signal",
                    "supertrend_trend_changed"):
            self.assertIn(key, vals, f"'{key}' missing from indicator_values")

        self.assertIsNotNone(vals["supertrend"],
                             "supertrend should be non-None after 50 ticks (period=10)")

        # Histogram consistency
        if vals["macd_line"] is not None and vals["signal_line"] is not None:
            expected_hist = round(vals["macd_line"] - vals["signal_line"], 4)
            self.assertAlmostEqual(vals["histogram"], expected_hist, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
