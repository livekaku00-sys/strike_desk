"""
Strike Desk V4 — Phase 4 Prompt 2/5 Verification Suite: Stock Cash Strategy
Verifies:
  a) Signal generation: All criteria -> A+, 3 criteria -> Tier B, 1 criterion -> None (Tier C)
  b) Confidence score calculation: Exact manual calculation match
  c) A+ Setup criteria: Score >= 85 AND ema9_crossover = True required
  d) Edge cases: Missing EMA9, extreme price (1e8), zero volume
  e) Direction logic: Bullish -> LONG, Bearish -> SHORT
  f) Asset class correctness: asset_class == AssetClass.STOCK_CASH
  g) Signal expiry: expires_at == timestamp + 30s
  h) Duplicate handling: Duplicate suppressed within 60s
  i) Criteria checklist: Accurate boolean outputs
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
from strategies.stock_cash_strategy import StockCashStrategy

UTC = timezone.utc


class TestStockCashStrategy(unittest.TestCase):
    """Unit tests for StockCashStrategy."""

    def setUp(self):
        self.strategy = StockCashStrategy()
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

    def test_a_signal_generation_tiers(self):
        """[a] Signal generation: All criteria -> A+, 3 criteria -> Tier B, 1 criterion -> None."""
        ind_all = {
            "symbol": "TCS",
            "price": 3500.0,
            "prev_price": 3490.0,
            "ema9": 3495.0,
            "prev_ema9": 3495.0,
            "vwap": 3480.0,
            "volume": 50000.0,
            "avg_volume": 20000.0,
            "pcr_slope": -0.05,
            "sentiment": 30.0,
        }
        sig_all = self.strategy.evaluate(ind_all, {"timestamp": self.t0})
        self.assertIsNotNone(sig_all)
        self.assertEqual(sig_all.confidence_tier, ConfidenceTier.A_PLUS)
        self.assertEqual(sig_all.confidence_score, 100.0)

        self.strategy.reset()
        ind_three = {
            "symbol": "TCS",
            "price": 3500.0,
            "prev_price": 3490.0,
            "ema9": 3495.0,
            "prev_ema9": 3495.0,
            "vwap": 3480.0,
            "volume": 10000.0,
            "avg_volume": 20000.0,
            "pcr_slope": 0.05,
            "sentiment": 15.0,
        }
        sig_three = self.strategy.evaluate(ind_three, {"timestamp": self.t0})
        self.assertIsNotNone(sig_three)
        self.assertEqual(sig_three.confidence_tier, ConfidenceTier.B)
        self.assertEqual(sig_three.confidence_score, 65.0)

        self.strategy.reset()
        ind_one = {
            "symbol": "TCS",
            "price": 3450.0,
            "prev_price": 3460.0,
            "ema9": 3490.0,
            "prev_ema9": 3490.0,
            "vwap": 3480.0,
            "volume": 5000.0,
            "avg_volume": 20000.0,
            "pcr_slope": 0.05,
            "sentiment": 15.0,
            "direction": Direction.LONG,
        }
        sig_one = self.strategy.evaluate(ind_one, {"timestamp": self.t0})
        self.assertIsNone(sig_one)
        print("✓ [a] Signal generation: Verified A+ (100.0), Tier B (65.0), and None for Tier C (10.0)")

    def test_b_confidence_score_calculation(self):
        """[b] Confidence score calculation: Exact manual calculation match."""
        ind = {
            "symbol": "INFY",
            "price": 1520.0,
            "prev_price": 1510.0,
            "ema9": 1515.0,
            "prev_ema9": 1515.0,
            "vwap": 1510.0,
            "volume": 1000.0,
            "avg_volume": 2000.0,
            "pcr_slope": -0.02,
            "sentiment": -5.0,
        }
        checklist = self.strategy.get_criteria_checklist(ind, {})
        self.assertTrue(checklist["ema9_crossover"])
        self.assertTrue(checklist["vwap_alignment"])
        self.assertFalse(checklist["volume_surge"])
        self.assertTrue(checklist["pcr_alignment"])
        self.assertFalse(checklist["sentiment_alignment"])

        expected_score = 30.0 + 25.0 + 15.0  # 70.0
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.confidence_score, expected_score, places=2)
        self.assertEqual(sig.confidence_tier, ConfidenceTier.A)
        print(f"✓ [b] Confidence score calculation: Exact match at {sig.confidence_score:.2f} (expected: 70.00)")

    def test_c_a_plus_setup_criteria(self):
        """[c] A+ Setup criteria: Score >= 85 AND ema9_crossover = True."""
        ind_no_cross = {
            "symbol": "RELIANCE",
            "price": 2500.0,
            "ema9_crossover": False,
            "vwap_alignment": True,
            "volume_surge": True,
            "pcr_alignment": True,
            "sentiment_alignment": True,
            "direction": Direction.LONG,
            "ema9": 2490.0,
        }
        sig = self.strategy.evaluate(ind_no_cross, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.confidence_score, 70.0)
        self.assertEqual(sig.confidence_tier, ConfidenceTier.A)
        self.assertNotEqual(sig.confidence_tier, ConfidenceTier.A_PLUS)

        self.strategy.reset()
        ind_with_cross = {
            "symbol": "RELIANCE",
            "price": 2500.0,
            "ema9_crossover": True,
            "vwap_alignment": True,
            "volume_surge": True,
            "pcr_alignment": True,
            "sentiment_alignment": False,
            "direction": Direction.LONG,
            "ema9": 2490.0,
        }
        sig_a_plus = self.strategy.evaluate(ind_with_cross, {"timestamp": self.t0})
        self.assertIsNotNone(sig_a_plus)
        self.assertEqual(sig_a_plus.confidence_score, 90.0)
        self.assertEqual(sig_a_plus.confidence_tier, ConfidenceTier.A_PLUS)
        print("✓ [c] A+ Setup criteria: Score=90.0 + crossover -> A+; Score=70.0 without crossover -> Tier A")

    def test_d_edge_cases(self):
        """[d] Edge cases: Missing EMA9, extreme price (1e8), zero volume."""
        ind_missing = {"symbol": "EDGE", "price": 100.0}
        self.assertIsNone(self.strategy.evaluate(ind_missing, {}))

        ind_extreme = {
            "symbol": "EXTREME",
            "price": 100_000_000.0,
            "ema9": 99_990_000.0,
            "vwap": 99_000_000.0,
            "ema9_crossover": True,
            "volume_surge": True,
            "direction": Direction.LONG,
        }
        sig_ext = self.strategy.evaluate(ind_extreme, {"timestamp": self.t0})
        self.assertIsNotNone(sig_ext)
        self.assertAlmostEqual(sig_ext.entry_price, 100_000_000.0)

        checklist = self.strategy.get_criteria_checklist({"volume": 0.0, "avg_volume": 1000.0}, {})
        self.assertFalse(checklist["volume_surge"])
        print("✓ [d] Edge cases: Gracefully handled missing EMA9, extreme 1e8 price, and zero volume")

    def test_e_direction_logic(self):
        """[e] Direction logic: Bullish setup -> LONG, Bearish setup -> SHORT."""
        ind_bull = {
            "symbol": "DIR_TEST",
            "price": 500.0,
            "prev_price": 490.0,
            "ema9": 495.0,
            "prev_ema9": 495.0,
            "vwap": 480.0,
            "volume_surge": True,
            "pcr_slope": -0.05,
            "sentiment": 10.0,
        }
        sig_bull = self.strategy.evaluate(ind_bull, {"timestamp": self.t0})
        self.assertIsNotNone(sig_bull)
        self.assertEqual(sig_bull.direction, Direction.LONG)
        self.assertEqual(sig_bull.signal_type, SignalType.EMA9_CROSSOVER_BULLISH)

        self.strategy.reset()
        ind_bear = {
            "symbol": "DIR_TEST",
            "price": 480.0,
            "prev_price": 495.0,
            "ema9": 490.0,
            "prev_ema9": 490.0,
            "vwap": 500.0,
            "volume_surge": True,
            "pcr_slope": 0.05,
            "sentiment": -10.0,
            "direction": Direction.SHORT,
        }
        sig_bear = self.strategy.evaluate(ind_bear, {"timestamp": self.t0})
        self.assertIsNotNone(sig_bear)
        self.assertEqual(sig_bear.direction, Direction.SHORT)
        self.assertEqual(sig_bear.signal_type, SignalType.EMA9_CROSSOVER_BEARISH)
        print("✓ [e] Direction logic: Correctly generated LONG for bullish and SHORT for bearish setup")

    def test_f_asset_class_correctness(self):
        """[f] Asset class correctness: Verify signal.asset_class == STOCK_CASH."""
        ind = {"symbol": "TEST", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "vwap_alignment": True}
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.asset_class, AssetClass.STOCK_CASH)
        self.assertEqual(self.strategy.asset_class, AssetClass.STOCK_CASH)
        print("✓ [f] Asset class correctness: Confirmed signal.asset_class is STOCK_CASH")

    def test_g_signal_expiry(self):
        """[g] Signal expiry: Verify signal.expires_at = timestamp + 30 seconds."""
        ind = {"symbol": "TEST", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "vwap_alignment": True}
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.expires_at, self.t0 + timedelta(seconds=30))
        print("✓ [g] Signal expiry: Validated exact 30-second expiry window (expires_at = t0 + 30s)")

    def test_h_duplicate_handling(self):
        """[h] Duplicate handling: Generate same signal twice within 60s -> second suppressed."""
        ind = {"symbol": "DUP_TEST", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "vwap_alignment": True}
        sig1 = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig1)

        sig2 = self.strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=20)})
        self.assertIsNone(sig2, "Duplicate signal was not suppressed!")

        sig3 = self.strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=65)})
        self.assertIsNotNone(sig3)
        print("✓ [h] Duplicate handling: Suppressed duplicate at 20s; accepted fresh signal at 65s")

    def test_i_criteria_checklist(self):
        """[i] Criteria checklist: Verify get_criteria_checklist() outputs expected booleans."""
        ind = {
            "price": 200.0,
            "vwap": 195.0,
            "ema9": 198.0,
            "ema9_crossover": True,
            "volume": 2000.0,
            "avg_volume": 1000.0,
            "pcr_slope": -0.01,
            "sentiment": 5.0,
        }
        checklist = self.strategy.get_criteria_checklist(ind, {})
        for key in ["ema9_crossover", "vwap_alignment", "volume_surge", "pcr_alignment", "sentiment_alignment"]:
            self.assertIn(key, checklist)
            self.assertTrue(checklist[key])
        print("✓ [i] Criteria checklist: Verified all 5 discrete criteria evaluated to True")

    def test_j_independence(self):
        """[j] Independence: Verify strategy can be tested without other strategies loaded."""
        isolated = StockCashStrategy(name="StandaloneCash")
        self.assertEqual(isolated.name, "StandaloneCash")
        self.assertEqual(isolated.asset_class, AssetClass.STOCK_CASH)
        res = isolated.get_status()
        self.assertIn("name", res)
        self.assertIn("asset_class", res)
        print("✓ [j] Independence: Verified StockCashStrategy operates completely standalone")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 4 PROMPT 2/5 STOCK CASH STRATEGY VERIFICATION")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestStockCashStrategy)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 10 STOCK CASH STRATEGY TESTS (a-j) PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)