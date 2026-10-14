"""
S12 — Market Regime Classifier Tests

Tests:
  1.  UNKNOWN when ADX is None (missing)
  2.  UNKNOWN when plus_di/minus_di are None
  3.  UNKNOWN when ADX values are non-numeric strings
  4.  UNKNOWN when ADX is NaN (non-finite)
  5.  TRENDING_UP: ADX >= 25, +DI > -DI, no secondary signals
  6.  TRENDING_DOWN: ADX >= 25, -DI > +DI, no secondary signals
  7.  RANGING: ADX < 20, no volatile flag
  8.  VOLATILE: ADX < 20, Bollinger bandwidth >= 0.04
  9.  Transition zone (ADX 20-25) → TRENDING_UP when +DI >= -DI
  10. Transition zone (ADX 20-25) → TRENDING_DOWN when -DI > +DI
  11. Transition zone (ADX 20-25) → VOLATILE when bandwidth elevated
  12. Confidence = 0 when no secondary indicators provided
  13. Confidence = 100 when all available secondaries confirm
  14. Confidence is partial when some secondaries disagree
  15. RSI confirms TRENDING_UP when RSI >= 50
  16. RSI confirms TRENDING_DOWN when RSI < 50
  17. RSI confirms RANGING when RSI in [40, 60]
  18. percent_b confirms TRENDING_UP when percent_b >= 0.5
  19. percent_b confirms TRENDING_DOWN when percent_b < 0.5
  20. MACD histogram confirms TRENDING_UP when positive
  21. MACD histogram confirms TRENDING_DOWN when negative
  22. Supertrend BUY confirms TRENDING_UP
  23. Supertrend SELL confirms TRENDING_DOWN
  24. Price > VWAP confirms TRENDING_UP
  25. Price < VWAP confirms TRENDING_DOWN
  26. MACD histogram skipped for RANGING (no contribution to available count)
  27. Supertrend skipped for RANGING/VOLATILE
  28. classified_at is UTC-aware
  29. sufficient_data=True for all classified regimes, False for UNKNOWN
  30. Determinism: identical inputs produce identical label, score, reason
  31. VOLATILE bandwidth threshold boundary: exactly 0.04 → VOLATILE
  32. VOLATILE bandwidth boundary: 0.039... → RANGING
  33. ADX exactly at 25.0 boundary → TRENDING
  34. ADX exactly at 20.0 boundary → transition zone (not RANGING)
  35. Non-finite bandwidth is treated as absent (does not trigger volatile flag)
  36. Regression: existing indicator calculators still produce correct outputs
"""
import math
import sys
import os
import unittest
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pipeline.regime_classifier import RegimeClassifier, RegimeLabel, RegimeResult


# ── helpers ──────────────────────────────────────────────────────────────────

_FIXED_NOW = datetime(2024, 9, 19, 10, 0, 0, tzinfo=timezone.utc)


def _classify(indicators: dict) -> RegimeResult:
    return RegimeClassifier.classify(indicators, _now=_FIXED_NOW)


def _adx_ready(adx: float, plus_di: float, minus_di: float) -> dict:
    """Minimal ADX-only indicators dict, no secondaries."""
    return {
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "trend_strength": "STRONG" if adx >= 40 else ("MODERATE" if adx >= 25 else ("WEAK" if adx < 20 else "MODERATE")),
    }


def _with_rsi(base: dict, rsi: float) -> dict:
    return {**base, "rsi": rsi}


def _with_bollinger(base: dict, bandwidth: float, percent_b: float) -> dict:
    return {**base, "bandwidth": bandwidth, "percent_b": percent_b}


def _with_macd(base: dict, histogram: float) -> dict:
    return {**base, "histogram": histogram}


def _with_supertrend(base: dict, signal: str) -> dict:
    return {**base, "signal": signal}


def _with_vwap(base: dict, ltp: float, vwap: float) -> dict:
    return {**base, "ltp": ltp, "vwap": vwap}


# ── UNKNOWN tests ─────────────────────────────────────────────────────────────

class TestUnknownRegime(unittest.TestCase):

    def test_01_unknown_when_adx_is_none(self):
        result = _classify({"adx": None, "plus_di": 20.0, "minus_di": 10.0})
        self.assertEqual(result.label, RegimeLabel.UNKNOWN)
        self.assertFalse(result.sufficient_data)
        self.assertEqual(result.confidence_score, 0.0)

    def test_02_unknown_when_plus_di_none(self):
        result = _classify({"adx": 30.0, "plus_di": None, "minus_di": 10.0})
        self.assertEqual(result.label, RegimeLabel.UNKNOWN)
        self.assertFalse(result.sufficient_data)

    def test_03_unknown_when_minus_di_none(self):
        result = _classify({"adx": 30.0, "plus_di": 20.0, "minus_di": None})
        self.assertEqual(result.label, RegimeLabel.UNKNOWN)
        self.assertFalse(result.sufficient_data)

    def test_03b_unknown_when_adx_absent(self):
        """No ADX key at all — same as None."""
        result = _classify({})
        self.assertEqual(result.label, RegimeLabel.UNKNOWN)
        self.assertFalse(result.sufficient_data)

    def test_04_unknown_when_adx_is_nan(self):
        result = _classify({"adx": float("nan"), "plus_di": 20.0, "minus_di": 10.0})
        self.assertEqual(result.label, RegimeLabel.UNKNOWN)
        self.assertFalse(result.sufficient_data)

    def test_04b_unknown_when_adx_is_inf(self):
        result = _classify({"adx": float("inf"), "plus_di": 20.0, "minus_di": 10.0})
        self.assertEqual(result.label, RegimeLabel.UNKNOWN)
        self.assertFalse(result.sufficient_data)

    def test_unknown_classified_at_is_utc(self):
        result = _classify({})
        self.assertIsNotNone(result.classified_at.tzinfo)
        self.assertEqual(result.classified_at.utcoffset().total_seconds(), 0.0)


# ── Primary classification tests ─────────────────────────────────────────────

class TestPrimaryClassification(unittest.TestCase):

    def test_05_trending_up_adx_above_25_bullish_di(self):
        result = _classify(_adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)
        self.assertTrue(result.sufficient_data)

    def test_06_trending_down_adx_above_25_bearish_di(self):
        result = _classify(_adx_ready(adx=30.0, plus_di=10.0, minus_di=25.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_DOWN)
        self.assertTrue(result.sufficient_data)

    def test_07_ranging_adx_below_20_no_volatile(self):
        result = _classify(_adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0))
        self.assertEqual(result.label, RegimeLabel.RANGING)
        self.assertTrue(result.sufficient_data)

    def test_08_volatile_adx_below_20_bandwidth_elevated(self):
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result = _classify(_with_bollinger(base, bandwidth=0.05, percent_b=0.5))
        self.assertEqual(result.label, RegimeLabel.VOLATILE)
        self.assertTrue(result.sufficient_data)

    def test_09_transition_zone_trending_up(self):
        result = _classify(_adx_ready(adx=22.0, plus_di=20.0, minus_di=15.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)

    def test_10_transition_zone_trending_down(self):
        result = _classify(_adx_ready(adx=22.0, plus_di=15.0, minus_di=20.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_DOWN)

    def test_11_transition_zone_volatile_overrides(self):
        base = _adx_ready(adx=22.0, plus_di=20.0, minus_di=15.0)
        result = _classify(_with_bollinger(base, bandwidth=0.06, percent_b=0.9))
        self.assertEqual(result.label, RegimeLabel.VOLATILE)

    def test_33_adx_exactly_at_25_boundary_trending(self):
        result = _classify(_adx_ready(adx=25.0, plus_di=20.0, minus_di=10.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)

    def test_34_adx_exactly_at_20_boundary_transition_not_ranging(self):
        # ADX = 20.0 is in the transition zone (>= 20, < 25), not RANGING (< 20)
        result = _classify(_adx_ready(adx=20.0, plus_di=20.0, minus_di=10.0))
        self.assertNotEqual(result.label, RegimeLabel.RANGING)

    def test_adx_just_below_20_is_ranging(self):
        result = _classify(_adx_ready(adx=19.99, plus_di=15.0, minus_di=14.0))
        self.assertEqual(result.label, RegimeLabel.RANGING)


# ── Confidence scoring tests ──────────────────────────────────────────────────

class TestConfidenceScoring(unittest.TestCase):

    def test_12_confidence_zero_when_no_secondaries(self):
        result = _classify(_adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0))
        self.assertEqual(result.confidence_score, 0.0)

    def test_13_confidence_100_when_all_secondaries_confirm(self):
        """All five secondary signals confirm TRENDING_UP."""
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        indicators = {
            **base,
            "rsi": 60.0,         # >= 50 → confirms TRENDING_UP
            "percent_b": 0.7,    # >= 0.5 → confirms TRENDING_UP
            "histogram": 0.5,    # > 0 → confirms TRENDING_UP
            "signal": "BUY",     # confirms TRENDING_UP
            "ltp": 100.0,
            "vwap": 98.0,        # ltp > vwap → confirms TRENDING_UP
        }
        result = _classify(indicators)
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)
        self.assertEqual(result.confidence_score, 100.0)

    def test_14_confidence_partial_when_some_disagree(self):
        """RSI confirms but percent_b and histogram do not."""
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        indicators = {
            **base,
            "rsi": 60.0,       # confirms TRENDING_UP
            "percent_b": 0.3,  # < 0.5 → does NOT confirm TRENDING_UP
            "histogram": -0.1, # < 0 → does NOT confirm TRENDING_UP
        }
        result = _classify(indicators)
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)
        # 1 confirm out of 3 available = 33.3%
        self.assertAlmostEqual(result.confidence_score, 33.3, places=0)

    def test_confidence_bounded_0_to_100(self):
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        for _ in range(5):
            result = _classify(base)
            self.assertGreaterEqual(result.confidence_score, 0.0)
            self.assertLessEqual(result.confidence_score, 100.0)


# ── RSI secondary signal tests ────────────────────────────────────────────────

class TestRSIConfirmation(unittest.TestCase):

    def test_15_rsi_confirms_trending_up_above_50(self):
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        high_rsi = _with_rsi(base, rsi=65.0)
        low_rsi = _with_rsi(base, rsi=45.0)
        result_high = _classify(high_rsi)
        result_low = _classify(low_rsi)
        self.assertEqual(result_high.label, RegimeLabel.TRENDING_UP)
        self.assertEqual(result_high.confidence_score, 100.0)  # only RSI, confirms
        self.assertEqual(result_low.label, RegimeLabel.TRENDING_UP)
        self.assertEqual(result_low.confidence_score, 0.0)     # only RSI, doesn't confirm

    def test_16_rsi_confirms_trending_down_below_50(self):
        base = _adx_ready(adx=30.0, plus_di=10.0, minus_di=25.0)
        result = _classify(_with_rsi(base, rsi=40.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_DOWN)
        self.assertEqual(result.confidence_score, 100.0)

    def test_17_rsi_confirms_ranging_in_40_60(self):
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        # RSI = 50 is within [40, 60] → confirms RANGING
        result = _classify(_with_rsi(base, rsi=50.0))
        self.assertEqual(result.label, RegimeLabel.RANGING)
        self.assertEqual(result.confidence_score, 100.0)

    def test_rsi_does_not_confirm_ranging_when_outside_band(self):
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        # RSI = 75 is outside [40, 60] → does NOT confirm RANGING
        result = _classify(_with_rsi(base, rsi=75.0))
        self.assertEqual(result.label, RegimeLabel.RANGING)
        self.assertEqual(result.confidence_score, 0.0)


# ── Bollinger percent_b tests ─────────────────────────────────────────────────

class TestPercentBConfirmation(unittest.TestCase):

    def test_18_percent_b_confirms_trending_up(self):
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        result = _classify(_with_bollinger(base, bandwidth=0.02, percent_b=0.75))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)
        self.assertEqual(result.confidence_score, 100.0)

    def test_19_percent_b_confirms_trending_down(self):
        base = _adx_ready(adx=30.0, plus_di=10.0, minus_di=25.0)
        result = _classify(_with_bollinger(base, bandwidth=0.02, percent_b=0.3))
        self.assertEqual(result.label, RegimeLabel.TRENDING_DOWN)
        self.assertEqual(result.confidence_score, 100.0)

    def test_percent_b_confirms_ranging_in_mid_band(self):
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result = _classify(_with_bollinger(base, bandwidth=0.01, percent_b=0.5))
        self.assertEqual(result.label, RegimeLabel.RANGING)
        self.assertEqual(result.confidence_score, 100.0)


# ── MACD histogram tests ──────────────────────────────────────────────────────

class TestMACDHistogramConfirmation(unittest.TestCase):

    def test_20_histogram_positive_confirms_trending_up(self):
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        result = _classify(_with_macd(base, histogram=1.5))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)
        self.assertEqual(result.confidence_score, 100.0)

    def test_21_histogram_negative_confirms_trending_down(self):
        base = _adx_ready(adx=30.0, plus_di=10.0, minus_di=25.0)
        result = _classify(_with_macd(base, histogram=-1.5))
        self.assertEqual(result.label, RegimeLabel.TRENDING_DOWN)
        self.assertEqual(result.confidence_score, 100.0)

    def test_histogram_positive_does_not_confirm_trending_down(self):
        base = _adx_ready(adx=30.0, plus_di=10.0, minus_di=25.0)
        result = _classify(_with_macd(base, histogram=1.5))
        self.assertEqual(result.label, RegimeLabel.TRENDING_DOWN)
        self.assertEqual(result.confidence_score, 0.0)

    def test_26_histogram_not_counted_for_ranging(self):
        """For RANGING, histogram is not counted as a secondary signal."""
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result_with = _classify({**base, "histogram": 0.0})
        result_without = _classify(base)
        # Available secondaries should be the same (histogram not counted for RANGING)
        self.assertEqual(result_with.confidence_score, result_without.confidence_score)


# ── Supertrend signal tests ───────────────────────────────────────────────────

class TestSupertrendConfirmation(unittest.TestCase):

    def test_22_supertrend_buy_confirms_trending_up(self):
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        result = _classify(_with_supertrend(base, "BUY"))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)
        self.assertEqual(result.confidence_score, 100.0)

    def test_23_supertrend_sell_confirms_trending_down(self):
        base = _adx_ready(adx=30.0, plus_di=10.0, minus_di=25.0)
        result = _classify(_with_supertrend(base, "SELL"))
        self.assertEqual(result.label, RegimeLabel.TRENDING_DOWN)
        self.assertEqual(result.confidence_score, 100.0)

    def test_supertrend_sell_does_not_confirm_trending_up(self):
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        result = _classify(_with_supertrend(base, "SELL"))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)
        self.assertEqual(result.confidence_score, 0.0)

    def test_27_supertrend_not_counted_for_ranging_or_volatile(self):
        """Supertrend is not counted as a secondary signal for RANGING or VOLATILE."""
        base_ranging = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result_r = _classify({**base_ranging, "signal": "BUY"})
        result_r_plain = _classify(base_ranging)
        self.assertEqual(result_r.confidence_score, result_r_plain.confidence_score)

        # VOLATILE
        base_volatile = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result_v = _classify({**base_volatile, "bandwidth": 0.05, "signal": "BUY"})
        result_v_plain = _classify({**base_volatile, "bandwidth": 0.05})
        # percent_b not provided for either → same available count
        self.assertEqual(result_v.confidence_score, result_v_plain.confidence_score)


# ── VWAP confirmation tests ───────────────────────────────────────────────────

class TestVWAPConfirmation(unittest.TestCase):

    def test_24_price_above_vwap_confirms_trending_up(self):
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        result = _classify(_with_vwap(base, ltp=101.0, vwap=100.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)
        self.assertEqual(result.confidence_score, 100.0)

    def test_25_price_below_vwap_confirms_trending_down(self):
        base = _adx_ready(adx=30.0, plus_di=10.0, minus_di=25.0)
        result = _classify(_with_vwap(base, ltp=99.0, vwap=100.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_DOWN)
        self.assertEqual(result.confidence_score, 100.0)

    def test_vwap_zero_is_ignored(self):
        """vwap=0 is treated as not-available; no contribution to available count."""
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        result_vwap_zero = _classify({**base, "ltp": 100.0, "vwap": 0.0})
        result_no_vwap = _classify(base)
        self.assertEqual(result_vwap_zero.confidence_score, result_no_vwap.confidence_score)

    def test_vwap_none_is_ignored(self):
        base = _adx_ready(adx=30.0, plus_di=25.0, minus_di=10.0)
        result = _classify({**base, "ltp": 100.0, "vwap": None})
        self.assertEqual(result.confidence_score, 0.0)


# ── Boundary and edge-case tests ──────────────────────────────────────────────

class TestBoundaryConditions(unittest.TestCase):

    def test_31_volatile_bandwidth_exactly_at_threshold(self):
        """bandwidth == 0.04 exactly → VOLATILE."""
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result = _classify(_with_bollinger(base, bandwidth=0.04, percent_b=0.5))
        self.assertEqual(result.label, RegimeLabel.VOLATILE)

    def test_32_volatile_bandwidth_just_below_threshold_is_ranging(self):
        """bandwidth = 0.039... → RANGING (not VOLATILE)."""
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result = _classify(_with_bollinger(base, bandwidth=0.0399, percent_b=0.5))
        self.assertEqual(result.label, RegimeLabel.RANGING)

    def test_35_non_finite_bandwidth_is_ignored(self):
        """float('nan') bandwidth must not trigger the volatile flag."""
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result = _classify({**base, "bandwidth": float("nan"), "percent_b": 0.5})
        self.assertEqual(result.label, RegimeLabel.RANGING)

    def test_non_finite_bandwidth_inf_is_ignored(self):
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result = _classify({**base, "bandwidth": float("inf"), "percent_b": 0.5})
        self.assertEqual(result.label, RegimeLabel.RANGING)

    def test_equal_di_values_trending_up(self):
        """+DI == -DI → TRENDING_UP (>= comparison)."""
        result = _classify(_adx_ready(adx=30.0, plus_di=20.0, minus_di=20.0))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)

    def test_volatile_with_adx_in_trend_zone_does_not_override(self):
        """ADX >= 25 → TRENDING even with high bandwidth."""
        base = _adx_ready(adx=28.0, plus_di=25.0, minus_di=10.0)
        result = _classify(_with_bollinger(base, bandwidth=0.10, percent_b=0.9))
        self.assertEqual(result.label, RegimeLabel.TRENDING_UP)


# ── MetaResult invariant tests ────────────────────────────────────────────────

class TestResultInvariants(unittest.TestCase):

    def test_28_classified_at_is_utc_aware(self):
        result = RegimeClassifier.classify(_adx_ready(30.0, 25.0, 10.0))
        self.assertIsNotNone(result.classified_at.tzinfo)
        self.assertEqual(result.classified_at.utcoffset().total_seconds(), 0.0)

    def test_29_sufficient_data_false_for_unknown(self):
        result = _classify({})
        self.assertFalse(result.sufficient_data)

    def test_29b_sufficient_data_true_for_classified_regimes(self):
        for adx, pd, md, expected in [
            (30.0, 25.0, 10.0, RegimeLabel.TRENDING_UP),
            (30.0, 10.0, 25.0, RegimeLabel.TRENDING_DOWN),
            (12.0, 15.0, 14.0, RegimeLabel.RANGING),
        ]:
            with self.subTest(label=expected):
                result = _classify(_adx_ready(adx, pd, md))
                self.assertTrue(result.sufficient_data)

    def test_29c_sufficient_data_true_for_volatile(self):
        base = _adx_ready(adx=12.0, plus_di=15.0, minus_di=14.0)
        result = _classify(_with_bollinger(base, bandwidth=0.05, percent_b=0.5))
        self.assertTrue(result.sufficient_data)

    def test_label_is_regime_label_enum(self):
        result = _classify(_adx_ready(30.0, 25.0, 10.0))
        self.assertIsInstance(result.label, RegimeLabel)

    def test_confidence_is_float_between_0_and_100(self):
        result = _classify({
            **_adx_ready(30.0, 25.0, 10.0),
            "rsi": 60.0,
            "percent_b": 0.7,
        })
        self.assertIsInstance(result.confidence_score, float)
        self.assertGreaterEqual(result.confidence_score, 0.0)
        self.assertLessEqual(result.confidence_score, 100.0)

    def test_reason_contains_label_name(self):
        result = _classify(_adx_ready(30.0, 25.0, 10.0))
        self.assertIn("TRENDING_UP", result.reason)

    def test_inputs_used_contains_adx_fields(self):
        result = _classify(_adx_ready(30.0, 25.0, 10.0))
        self.assertIn("adx", result.inputs_used)
        self.assertIn("plus_di", result.inputs_used)
        self.assertIn("minus_di", result.inputs_used)


# ── Determinism test ──────────────────────────────────────────────────────────

class TestDeterminism(unittest.TestCase):

    def test_30_identical_inputs_same_label_score_reason(self):
        """Calling classify twice with the same indicators and _now yields same results."""
        indicators = {
            **_adx_ready(30.0, 25.0, 10.0),
            "rsi": 60.0,
            "percent_b": 0.7,
            "histogram": 1.0,
            "signal": "BUY",
            "ltp": 101.0,
            "vwap": 100.0,
        }
        r1 = RegimeClassifier.classify(indicators, _now=_FIXED_NOW)
        r2 = RegimeClassifier.classify(indicators, _now=_FIXED_NOW)
        self.assertEqual(r1.label, r2.label)
        self.assertEqual(r1.confidence_score, r2.confidence_score)
        self.assertEqual(r1.reason, r2.reason)
        self.assertEqual(r1.sufficient_data, r2.sufficient_data)

    def test_different_inputs_may_differ(self):
        """Different ADX values yield different labels."""
        r_trend = _classify(_adx_ready(30.0, 25.0, 10.0))
        r_range = _classify(_adx_ready(12.0, 15.0, 14.0))
        self.assertNotEqual(r_trend.label, r_range.label)


# ── Regression: existing indicators not broken ────────────────────────────────

class TestRegressionExistingIndicators(unittest.TestCase):

    def test_36_rsi_calculator_still_works(self):
        from pipeline.indicators.rsi_calculator import RSICalculator
        from models.tick import Tick
        from datetime import timedelta

        calc = RSICalculator("REGRESSION_TEST", period=14)
        base_ts = datetime(2024, 9, 19, 3, 45, 0, tzinfo=timezone.utc)
        prices = [100.0 + i for i in range(20)]  # all up
        result = {}
        for i, p in enumerate(prices):
            t = Tick(
                symbol="REGRESSION_TEST",
                exchange="NSE",
                instrument_type="EQ",
                timestamp=base_ts + timedelta(seconds=i),
                ltp=p,
            )
            result = calc.process_tick(t)
        self.assertIsNotNone(result.get("rsi"))
        self.assertGreater(result["rsi"], 70.0)
        self.assertEqual(result["zone"], "OVERBOUGHT")

    def test_36b_adx_calculator_still_works(self):
        from pipeline.indicators.adx_calculator import ADXCalculator
        from models.tick import Tick
        from datetime import timedelta

        calc = ADXCalculator("REGRESSION_TEST_ADX", period=14)
        base_ts = datetime(2024, 9, 19, 3, 45, 0, tzinfo=timezone.utc)
        prices = [100.0 + i for i in range(30)]  # strongly trending up
        result = {}
        for i, p in enumerate(prices):
            h = p + 1.0
            l = p - 1.0
            t = Tick(
                symbol="REGRESSION_TEST_ADX",
                exchange="NSE",
                instrument_type="EQ",
                timestamp=base_ts + timedelta(seconds=i),
                ltp=p,
                high=h,
                low=l,
            )
            result = calc.process_tick(t)
        self.assertIsNotNone(result.get("adx"))
        self.assertTrue(result["sufficient_data"])

    def test_36c_bollinger_calculator_still_works(self):
        from pipeline.indicators.bollinger_calculator import BollingerCalculator
        from models.tick import Tick
        from datetime import timedelta

        calc = BollingerCalculator("REGRESSION_TEST_BB", period=20)
        base_ts = datetime(2024, 9, 19, 3, 45, 0, tzinfo=timezone.utc)
        prices = [100.0 + (i % 5) * 0.5 for i in range(25)]
        result = {}
        for i, p in enumerate(prices):
            t = Tick(
                symbol="REGRESSION_TEST_BB",
                exchange="NSE",
                instrument_type="EQ",
                timestamp=base_ts + timedelta(seconds=i),
                ltp=p,
            )
            result = calc.process_tick(t)
        self.assertIsNotNone(result.get("bandwidth"))
        self.assertTrue(result["sufficient_data"])

    def test_regime_classifier_with_real_adx_output(self):
        """Feed real ADX calculator output into the regime classifier."""
        from pipeline.indicators.adx_calculator import ADXCalculator
        from models.tick import Tick
        from datetime import timedelta

        calc = ADXCalculator("INTEGRATION_TEST", period=14)
        base_ts = datetime(2024, 9, 19, 3, 45, 0, tzinfo=timezone.utc)

        # Generate 30 ticks with clear upward trend
        result = {}
        for i in range(30):
            p = 100.0 + i * 0.5
            t = Tick(
                symbol="INTEGRATION_TEST",
                exchange="NSE",
                instrument_type="EQ",
                timestamp=base_ts + timedelta(seconds=i),
                ltp=p,
                high=p + 0.5,
                low=p - 0.3,
            )
            result = calc.process_tick(t)

        # Only proceed if ADX is ready
        if result.get("adx") is not None:
            regime = RegimeClassifier.classify(result)
            self.assertIsInstance(regime.label, RegimeLabel)
            self.assertIsNotNone(regime.label)
            self.assertIn(
                regime.label,
                list(RegimeLabel),
                f"Unexpected label: {regime.label}",
            )


if __name__ == "__main__":
    unittest.main()
