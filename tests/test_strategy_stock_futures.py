"""
Strike Desk V4 — Phase 4 Prompt 2/5 Verification Suite: Stock Futures Strategy
Verifies:
  a) Signal generation: All criteria -> A+, 3 criteria -> Tier B, 1 criterion -> None (Tier C)
  b) Confidence score calculation: Exact manual calculation match
  c) A+ Setup criteria: Score >= 85 AND ema9_crossover = True required
  d) Edge cases & Roll-over: Expiry < 3 days suppressed, missing EMA9, extreme price (1e8)
  e) Direction logic & Basis: Bullish + contango -> LONG, Bearish + backwardation -> SHORT
  f) Asset class correctness: asset_class == AssetClass.STOCK_FUTURES
  g) Signal expiry: expires_at == timestamp + 30s
  h) Duplicate handling: Duplicate suppressed within 60s
  i) Criteria checklist: Accurate boolean outputs for basis, OI, and EMA9
  j) Independence: Testable as a standalone module
"""
from datetime import datetime, timedelta, timezone
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal, SignalType
from strategies.stock_futures_strategy import StockFuturesStrategy

UTC = timezone.utc


class TestStockFuturesStrategy(unittest.TestCase):
    """Unit tests for StockFuturesStrategy."""

    def setUp(self):
        self.strategy = StockFuturesStrategy()
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

    def test_a_signal_generation_tiers(self):
        """[a] Signal generation: All criteria -> A+, 3 criteria -> Tier B, 1 criterion -> None."""
        ind_all = {
            "symbol": "RELIANCE_FUT",
            "price": 2510.0,
            "prev_price": 2495.0,
            "ema9": 2500.0,
            "prev_ema9": 2500.0,
            "vwap": 2490.0,
            "spot_price": 2500.0,
            "oi_change_pct": 5.0,
            "sentiment": 25.0,
        }
        sig_all = self.strategy.evaluate(ind_all, {"timestamp": self.t0, "days_to_expiry": 10.0})
        self.assertIsNotNone(sig_all)
        self.assertEqual(sig_all.confidence_tier, ConfidenceTier.A_PLUS)
        self.assertEqual(sig_all.confidence_score, 100.0)

        self.strategy.reset()
        ind_three = {
            "symbol": "RELIANCE_FUT",
            "price": 2510.0,
            "prev_price": 2495.0,
            "ema9": 2500.0,
            "prev_ema9": 2500.0,
            "vwap": 2490.0,
            "spot_price": 2500.0,
            "oi_change_pct": -2.0,
            "sentiment": -10.0,
        }
        sig_three = self.strategy.evaluate(ind_three, {"timestamp": self.t0, "days_to_expiry": 10.0})
        self.assertIsNotNone(sig_three)
        self.assertEqual(sig_three.confidence_tier, ConfidenceTier.B)
        self.assertEqual(sig_three.confidence_score, 65.0)

        self.strategy.reset()
        ind_one = {
            "symbol": "RELIANCE_FUT",
            "price": 2480.0,
            "prev_price": 2490.0,
            "ema9": 2500.0,
            "prev_ema9": 2500.0,
            "vwap": 2490.0,
            "spot_price": 2495.0,
            "oi_change_pct": -1.0,
            "sentiment": 20.0,
            "direction": Direction.LONG,
        }
        sig_one = self.strategy.evaluate(ind_one, {"timestamp": self.t0, "days_to_expiry": 10.0})
        self.assertIsNone(sig_one)
        print("✓ [a] Signal generation: Verified A+ (100.0), Tier B (65.0), and None for Tier C (15.0)")

    def test_b_confidence_score_calculation(self):
        """[b] Confidence score calculation: Exact manual calculation match."""
        ind = {
            "symbol": "TCS_FUT",
            "price": 3520.0,
            "prev_price": 3505.0,
            "ema9": 3510.0,
            "prev_ema9": 3510.0,
            "vwap": 3500.0,
            "spot_price": 3525.0,
            "oi_change_pct": 3.5,
            "sentiment": -2.0,
        }
        expected_score = 25.0 + 20.0 + 20.0  # 65.0
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0, "days_to_expiry": 15.0})
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.confidence_score, expected_score, places=2)
        self.assertEqual(sig.confidence_tier, ConfidenceTier.B)
        print(f"✓ [b] Confidence score calculation: Exact match at {sig.confidence_score:.2f} (expected: 65.00)")

    def test_c_a_plus_setup_criteria(self):
        """[c] A+ Setup criteria: Score >= 85 AND ema9_crossover = True."""
        ind_no_cross = {
            "symbol": "INFY_FUT",
            "price": 1500.0,
            "ema9": 1490.0,
            "ema9_crossover": False,
            "vwap_alignment": True,
            "basis_tracking": True,
            "oi_change": True,
            "sentiment_alignment": True,
            "direction": Direction.LONG,
        }
        sig = self.strategy.evaluate(ind_no_cross, {"timestamp": self.t0, "days_to_expiry": 10.0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.confidence_score, 75.0)
        self.assertEqual(sig.confidence_tier, ConfidenceTier.A)

        self.strategy.reset()
        ind_with_cross = {
            "symbol": "INFY_FUT",
            "price": 1500.0,
            "ema9": 1490.0,
            "ema9_crossover": True,
            "vwap_alignment": True,
            "basis_tracking": True,
            "oi_change": True,
            "sentiment_alignment": False,
            "direction": Direction.LONG,
        }
        sig_a_plus = self.strategy.evaluate(ind_with_cross, {"timestamp": self.t0, "days_to_expiry": 10.0})
        self.assertIsNotNone(sig_a_plus)
        self.assertEqual(sig_a_plus.confidence_score, 85.0)
        self.assertEqual(sig_a_plus.confidence_tier, ConfidenceTier.A_PLUS)
        print("✓ [c] A+ Setup criteria: Score=85.0 + crossover -> A+; Score=75.0 without crossover -> Tier A")

    def test_d_edge_cases_and_rollover(self):
        """[d] Edge cases & Roll-over: Expiry < 3 days suppressed, missing EMA9, extreme price."""
        ind_valid = {
            "symbol": "ROLLOVER_TEST",
            "price": 100.0,
            "ema9": 95.0,
            "ema9_crossover": True,
            "vwap_alignment": True,
            "basis_tracking": True,
            "oi_change": True,
            "direction": Direction.LONG,
        }
        sig_rollover = self.strategy.evaluate(ind_valid, {"days_to_expiry": 2.0})
        self.assertIsNone(sig_rollover, "Signal was not suppressed despite expiry < 3 days!")

        sig_ok = self.strategy.evaluate(ind_valid, {"days_to_expiry": 4.0})
        self.assertIsNotNone(sig_ok)

        self.assertIsNone(self.strategy.evaluate({"price": 100.0}, {}))

        ind_ext = {
            "symbol": "EXTREME_FUT",
            "price": 100_000_000.0,
            "ema9": 99_900_000.0,
            "ema9_crossover": True,
            "vwap_alignment": True,
            "basis_tracking": True,
            "direction": Direction.LONG,
        }
        sig_ext = self.strategy.evaluate(ind_ext, {"days_to_expiry": 10.0})
        self.assertIsNotNone(sig_ext)
        print("✓ [d] Edge cases & Roll-over: Expiry < 3d suppressed; missing EMA9 handled; extreme price handled")

    def test_e_direction_logic_and_basis(self):
        """[e] Direction logic & Basis: Contango favors LONG, Backwardation favors SHORT."""
        ind_bull = {
            "symbol": "BASIS_TEST",
            "price": 2510.0,
            "spot_price": 2500.0,
            "ema9": 2505.0,
            "prev_price": 2500.0,
            "prev_ema9": 2505.0,
            "vwap": 2495.0,
            "oi_change_pct": 2.0,
            "sentiment": 10.0,
        }
        sig_bull = self.strategy.evaluate(ind_bull, {"days_to_expiry": 15.0})
        self.assertIsNotNone(sig_bull)
        self.assertEqual(sig_bull.direction, Direction.LONG)
        self.assertEqual(sig_bull.signal_type, SignalType.EMA9_CROSSOVER_BULLISH)

        self.strategy.reset()
        ind_bear = {
            "symbol": "BASIS_TEST",
            "price": 2490.0,
            "spot_price": 2500.0,
            "ema9": 2495.0,
            "prev_price": 2500.0,
            "prev_ema9": 2495.0,
            "vwap": 2505.0,
            "oi_change_pct": 2.0,
            "sentiment": -10.0,
            "direction": Direction.SHORT,
        }
        sig_bear = self.strategy.evaluate(ind_bear, {"days_to_expiry": 15.0})
        self.assertIsNotNone(sig_bear)
        self.assertEqual(sig_bear.direction, Direction.SHORT)
        self.assertEqual(sig_bear.signal_type, SignalType.EMA9_CROSSOVER_BEARISH)
        print("✓ [e] Direction logic & Basis: Verified LONG for contango and SHORT for backwardation")

    def test_f_asset_class_correctness(self):
        """[f] Asset class correctness: Verify signal.asset_class == STOCK_FUTURES."""
        ind = {"symbol": "TEST_FUT", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "vwap_alignment": True, "basis_tracking": True}
        sig = self.strategy.evaluate(ind, {"days_to_expiry": 10.0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.asset_class, AssetClass.STOCK_FUTURES)
        self.assertEqual(self.strategy.asset_class, AssetClass.STOCK_FUTURES)
        print("✓ [f] Asset class correctness: Confirmed signal.asset_class is STOCK_FUTURES")

    def test_g_signal_expiry(self):
        """[g] Signal expiry: Verify signal.expires_at = timestamp + 30 seconds."""
        ind = {"symbol": "TEST_FUT", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "vwap_alignment": True, "basis_tracking": True}
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0, "days_to_expiry": 10.0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.expires_at, self.t0 + timedelta(seconds=30))
        print("✓ [g] Signal expiry: Validated exact 30-second expiry window (expires_at = t0 + 30s)")

    def test_h_duplicate_handling(self):
        """[h] Duplicate handling: Generate same signal twice within 60s -> second suppressed."""
        ind = {"symbol": "DUP_FUT", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "vwap_alignment": True, "basis_tracking": True}
        sig1 = self.strategy.evaluate(ind, {"timestamp": self.t0, "days_to_expiry": 10.0})
        self.assertIsNotNone(sig1)

        sig2 = self.strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=25), "days_to_expiry": 10.0})
        self.assertIsNone(sig2, "Duplicate futures signal was not suppressed!")

        sig3 = self.strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=65), "days_to_expiry": 10.0})
        self.assertIsNotNone(sig3)
        print("✓ [h] Duplicate handling: Suppressed duplicate at 25s; accepted fresh signal at 65s")

    def test_i_criteria_checklist(self):
        """[i] Criteria checklist: Verify get_criteria_checklist() booleans."""
        ind = {
            "price": 2505.0,
            "spot_price": 2500.0,
            "vwap": 2490.0,
            "ema9": 2500.0,
            "ema9_crossover": True,
            "oi_change_pct": 3.0,
            "sentiment": 10.0,
        }
        checklist = self.strategy.get_criteria_checklist(ind, {})
        for key in ["ema9_crossover", "vwap_alignment", "basis_tracking", "oi_change", "sentiment_alignment"]:
            self.assertIn(key, checklist)
            self.assertTrue(checklist[key])
        print("✓ [i] Criteria checklist: Verified all 5 discrete stock futures criteria evaluated to True")

    def test_j_independence(self):
        """[j] Independence: Verify strategy operates completely standalone."""
        isolated = StockFuturesStrategy(name="StandaloneFutures")
        self.assertEqual(isolated.name, "StandaloneFutures")
        self.assertEqual(isolated.asset_class, AssetClass.STOCK_FUTURES)
        self.assertIn("name", isolated.get_status())
        print("✓ [j] Independence: Verified StockFuturesStrategy operates completely standalone")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 4 PROMPT 2/5 STOCK FUTURES STRATEGY VERIFICATION")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestStockFuturesStrategy)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 10 STOCK FUTURES STRATEGY TESTS (a-j) PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)