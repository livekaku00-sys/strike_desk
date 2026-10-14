"""
S08 — Feature-Data Validity integration tests.

Verifies:
  - Tick.ltp_valid is True only for finite, positive LTP.
  - Tick.ltp_valid is derived in __post_init__; the caller never sets it manually.
  - All three normalizers (Fyers, Dhan, AngelOne) produce ltp_valid=False for
    NaN, Inf, and zero LTP payloads, and ltp_valid=True for a normal price.
  - _safe_float returns 0.0 for NaN, Inf, -Inf, and None inputs.
  - BaseIndicatorCalculator.process_tick skips invalid-LTP ticks and returns
    unchanged indicator_values without modifying tick_count or last_ltp.
  - A valid tick after an invalid tick is processed normally.
  - EMACalculator (via process_tick) inherits the gate and does not update
    its internal prices list on an invalid tick.
"""
import math
import sys
import os
import unittest
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.tick import Tick
from utils.tick_normalizer import TickNormalizer, _safe_float
from pipeline.indicator_calculator import DefaultIndicatorCalculator
from pipeline.indicators.ema_calculator import EMACalculator


# ── helpers ─────────────────────────────────────────────────────────────────

def _ts() -> int:
    return 1726740000


def _tick(ltp: float, symbol: str = "NIFTY_FUT") -> Tick:
    return Tick(
        symbol=symbol, exchange="NSE", instrument_type="FUT",
        timestamp=datetime.fromtimestamp(_ts(), tz=timezone.utc),
        ltp=ltp,
    )


def _fyers_raw(ltp, symbol: str = "NSE:BANKNIFTY24SEPFUT") -> dict:
    return {"symbol": symbol, "ltp": ltp, "exch_feed_time": _ts()}


def _dhan_raw(ltp) -> dict:
    return {"symbol": "BANKNIFTY_FUT", "LTP": ltp, "LTT": _ts()}


def _angelone_raw(ltp_paise) -> dict:
    return {"token": "NIFTY_FUT", "last_traded_price": ltp_paise}


# ── _safe_float ───────────────────────────────────────────────────────────────

class TestSafeFloat(unittest.TestCase):
    def test_a_nan_returns_fallback(self):
        self.assertEqual(_safe_float(float("nan")), 0.0)

    def test_b_pos_inf_returns_fallback(self):
        self.assertEqual(_safe_float(float("inf")), 0.0)

    def test_c_neg_inf_returns_fallback(self):
        self.assertEqual(_safe_float(float("-inf")), 0.0)

    def test_d_none_returns_fallback(self):
        self.assertEqual(_safe_float(None), 0.0)

    def test_e_custom_fallback(self):
        self.assertEqual(_safe_float(float("nan"), fallback=-1.0), -1.0)

    def test_f_normal_value_passes_through(self):
        self.assertAlmostEqual(_safe_float(24500.75), 24500.75)

    def test_g_zero_passes_through(self):
        self.assertEqual(_safe_float(0.0), 0.0)

    def test_h_string_number_converted(self):
        self.assertAlmostEqual(_safe_float("123.45"), 123.45)

    def test_i_bad_string_returns_fallback(self):
        self.assertEqual(_safe_float("abc"), 0.0)


# ── Tick.ltp_valid ────────────────────────────────────────────────────────────

class TestTickLtpValid(unittest.TestCase):
    def test_a_positive_ltp_valid_true(self):
        self.assertTrue(_tick(24500.0).ltp_valid)

    def test_b_zero_ltp_valid_false(self):
        self.assertFalse(_tick(0.0).ltp_valid)

    def test_c_nan_ltp_valid_false(self):
        self.assertFalse(_tick(float("nan")).ltp_valid)

    def test_d_inf_ltp_valid_false(self):
        self.assertFalse(_tick(float("inf")).ltp_valid)

    def test_e_neg_inf_ltp_valid_false(self):
        self.assertFalse(_tick(float("-inf")).ltp_valid)

    def test_f_negative_price_valid_false(self):
        self.assertFalse(_tick(-1.0).ltp_valid)

    def test_g_to_dict_includes_ltp_valid(self):
        d = _tick(100.0).to_dict()
        self.assertIn("ltp_valid", d)
        self.assertTrue(d["ltp_valid"])

    def test_h_to_dict_ltp_valid_false_for_zero(self):
        d = _tick(0.0).to_dict()
        self.assertFalse(d["ltp_valid"])


# ── Fyers normalizer ltp_valid ────────────────────────────────────────────────

class TestFyersNormalizerLtpValid(unittest.TestCase):
    def test_a_normal_price_ltp_valid_true(self):
        tick = TickNormalizer.normalize_fyers(_fyers_raw(24500.0))
        self.assertTrue(tick.ltp_valid)
        self.assertAlmostEqual(tick.ltp, 24500.0)

    def test_b_zero_ltp_valid_false(self):
        tick = TickNormalizer.normalize_fyers(_fyers_raw(0.0))
        self.assertFalse(tick.ltp_valid)

    def test_c_nan_ltp_clamped_to_zero_valid_false(self):
        tick = TickNormalizer.normalize_fyers(_fyers_raw(float("nan")))
        self.assertFalse(tick.ltp_valid)
        self.assertEqual(tick.ltp, 0.0)

    def test_d_inf_ltp_clamped_to_zero_valid_false(self):
        tick = TickNormalizer.normalize_fyers(_fyers_raw(float("inf")))
        self.assertFalse(tick.ltp_valid)
        self.assertEqual(tick.ltp, 0.0)

    def test_e_missing_ltp_key_valid_false(self):
        raw = {"symbol": "NSE:BANKNIFTY24SEPFUT", "exch_feed_time": _ts()}
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.ltp_valid)
        self.assertEqual(tick.ltp, 0.0)

    def test_f_index_normal_price_ltp_valid_true(self):
        raw = {"symbol": "NSE:NIFTY50-INDEX", "ltp": 24500.0, "exch_feed_time": _ts()}
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertTrue(tick.ltp_valid)
        self.assertFalse(tick.bid_ask_valid)


# ── Dhan normalizer ltp_valid ─────────────────────────────────────────────────

class TestDhanNormalizerLtpValid(unittest.TestCase):
    def test_a_normal_price_ltp_valid_true(self):
        tick = TickNormalizer.normalize_dhan(_dhan_raw(51200.0))
        self.assertTrue(tick.ltp_valid)

    def test_b_nan_ltp_clamped_valid_false(self):
        tick = TickNormalizer.normalize_dhan(_dhan_raw(float("nan")))
        self.assertFalse(tick.ltp_valid)
        self.assertEqual(tick.ltp, 0.0)

    def test_c_inf_ltp_clamped_valid_false(self):
        tick = TickNormalizer.normalize_dhan(_dhan_raw(float("inf")))
        self.assertFalse(tick.ltp_valid)
        self.assertEqual(tick.ltp, 0.0)

    def test_d_zero_ltp_valid_false(self):
        tick = TickNormalizer.normalize_dhan(_dhan_raw(0.0))
        self.assertFalse(tick.ltp_valid)

    def test_e_missing_ltp_key_valid_false(self):
        raw = {"symbol": "NIFTY_FUT"}
        tick = TickNormalizer.normalize_dhan(raw)
        self.assertFalse(tick.ltp_valid)


# ── AngelOne normalizer ltp_valid ─────────────────────────────────────────────

class TestAngelOneNormalizerLtpValid(unittest.TestCase):
    def test_a_normal_paise_price_ltp_valid_true(self):
        tick = TickNormalizer.normalize_angelone(_angelone_raw(2450000))  # 24500.0 rupees
        self.assertTrue(tick.ltp_valid)
        self.assertAlmostEqual(tick.ltp, 24500.0)

    def test_b_zero_paise_valid_false(self):
        tick = TickNormalizer.normalize_angelone(_angelone_raw(0))
        self.assertFalse(tick.ltp_valid)

    def test_c_nan_ltp_clamped_valid_false(self):
        tick = TickNormalizer.normalize_angelone({"token": "NIFTY_FUT", "last_traded_price": float("nan")})
        self.assertFalse(tick.ltp_valid)
        self.assertEqual(tick.ltp, 0.0)

    def test_d_inf_ltp_clamped_valid_false(self):
        tick = TickNormalizer.normalize_angelone({"token": "NIFTY_FUT", "last_traded_price": float("inf")})
        self.assertFalse(tick.ltp_valid)
        self.assertEqual(tick.ltp, 0.0)

    def test_e_missing_ltp_key_valid_false(self):
        tick = TickNormalizer.normalize_angelone({"token": "NIFTY_FUT"})
        self.assertFalse(tick.ltp_valid)


# ── BaseIndicatorCalculator LTP gate ─────────────────────────────────────────

class TestIndicatorLtpGate(unittest.TestCase):
    def test_a_invalid_tick_skipped_indicator_values_unchanged(self):
        calc = DefaultIndicatorCalculator("TEST")
        # Process one valid tick first to establish baseline
        valid = _tick(100.0)
        calc.process_tick(valid)
        before_values = dict(calc.indicator_values)
        before_count = calc.tick_count

        # Now process an invalid-LTP tick
        invalid = _tick(float("nan"))
        result = calc.process_tick(invalid)

        self.assertEqual(calc.tick_count, before_count)
        self.assertEqual(result, before_values)
        self.assertEqual(calc.last_ltp, 100.0)

    def test_b_invalid_tick_does_not_update_last_ltp(self):
        calc = DefaultIndicatorCalculator("TEST")
        calc.process_tick(_tick(100.0))
        calc.process_tick(_tick(float("inf")))
        self.assertAlmostEqual(calc.last_ltp, 100.0)

    def test_c_valid_tick_after_invalid_processed_normally(self):
        calc = DefaultIndicatorCalculator("TEST")
        calc.process_tick(_tick(float("nan")))
        self.assertEqual(calc.tick_count, 0)

        calc.process_tick(_tick(200.0))
        self.assertEqual(calc.tick_count, 1)
        self.assertAlmostEqual(calc.last_ltp, 200.0)

    def test_d_zero_ltp_tick_skipped(self):
        calc = DefaultIndicatorCalculator("TEST")
        calc.process_tick(_tick(0.0))
        self.assertEqual(calc.tick_count, 0)

    def test_e_neg_inf_ltp_skipped(self):
        calc = DefaultIndicatorCalculator("TEST")
        calc.process_tick(_tick(float("-inf")))
        self.assertEqual(calc.tick_count, 0)

    def test_f_empty_indicator_values_returned_before_any_valid_tick(self):
        calc = DefaultIndicatorCalculator("TEST")
        result = calc.process_tick(_tick(float("nan")))
        self.assertIsInstance(result, dict)


# ── EMACalculator inherits the gate ──────────────────────────────────────────

class TestEMACalculatorLtpGate(unittest.TestCase):
    def test_a_nan_ltp_not_appended_to_prices(self):
        calc = EMACalculator(period=3)
        calc.process_tick(_tick(float("nan")))
        self.assertEqual(len(calc.prices), 0)

    def test_b_inf_ltp_not_appended_to_prices(self):
        calc = EMACalculator(period=3)
        calc.process_tick(_tick(float("inf")))
        self.assertEqual(len(calc.prices), 0)

    def test_c_valid_ticks_still_compute_ema(self):
        calc = EMACalculator(period=3)
        for price in [100.0, 102.0, 104.0]:
            calc.process_tick(_tick(price))
        self.assertIsNotNone(calc.ema_value)

    def test_d_invalid_tick_between_valid_ticks_does_not_break_ema(self):
        calc = EMACalculator(period=3)
        calc.process_tick(_tick(100.0))
        calc.process_tick(_tick(float("nan")))
        calc.process_tick(_tick(102.0))
        calc.process_tick(_tick(104.0))
        # 3 valid ticks should produce a valid EMA
        self.assertIsNotNone(calc.ema_value)
        self.assertTrue(math.isfinite(calc.ema_value))


if __name__ == "__main__":
    unittest.main()
