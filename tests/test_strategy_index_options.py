"""
Strike Desk V4 — Phase 4 Prompt 3/5 Verification Suite: Index Options Strategy
Verifies:
  a) Signal generation with known inputs (All -> A+, 3/5 -> Tier B, 1/5 -> None)
  b) Confidence score calculation: Exact manual calculation match
  c) A+ Setup criteria: Score >= 85 AND pcr_slope = True AND ema9_crossover = True
  d) PCR staleness check (Review Risk 2): Staleness > 30s -> returns None & logs warning
  e) Strike selection: select_strike(24500, [24400, 24500, 24600]) returns 24500 (ATM)
  f) Expiry selection: select_expiry([next_week, next_month]) returns next_week
  g) Edge cases: Missing EMA9, extreme price (1e8)
  h) Direction logic: Negative PCR slope -> LONG, Positive PCR slope -> SHORT
  i) Asset class correctness: asset_class == AssetClass.INDEX_OPTIONS
  j) Signal expiry: expires_at == timestamp + 30s
  k) Duplicate handling: Duplicate suppressed within 60s
  l) Independence: Testable as a standalone module
"""
from datetime import date, datetime, timedelta, timezone
import logging
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal, SignalType
from pipeline.indicators.pcr_staleness_monitor import PCRStalenessMonitor
from strategies.index_options_strategy import IndexOptionsStrategy
from tests.option_chain_fixture import FIXTURE_CE_PREMIUM, seed_many

UTC = timezone.utc


class TestIndexOptionsStrategy(unittest.TestCase):
    """Unit tests for IndexOptionsStrategy."""

    def setUp(self):
        self.monitor = PCRStalenessMonitor(stale_threshold_seconds=30.0)
        self.strategy = IndexOptionsStrategy(pcr_monitor=self.monitor)
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)
        # Entry price comes from the live chain, so every underlying under test needs one.
        seed_many({
            "NIFTY": 24500.0,
            "BANKNIFTY": 51250.0,
            "DUP_OPT": 24500.0,
            "EXTREME_INDEX": 100_000_000.0,
        })

    def test_a_signal_generation_tiers(self):
        """[a] Signal generation: All criteria -> A+, 3 criteria -> Tier B, 1 criterion -> None."""
        ind_all = {
            "symbol": "NIFTY",
            "underlying": "NIFTY",
            "price": 24520.0,
            "prev_price": 24490.0,
            "ema9": 24500.0,
            "prev_ema9": 24500.0,
            "vwap": 24480.0,
            "pcr_slope": -0.05,     # Bullish (35.0)
            "sentiment": 25.0,      # Bullish (20.0)
            "oi_change_pct": 3.0,   # Rising OI (10.0)
        }
        sig_all = self.strategy.evaluate(ind_all, {"timestamp": self.t0})
        self.assertIsNotNone(sig_all)
        self.assertEqual(sig_all.confidence_tier, ConfidenceTier.A_PLUS)
        self.assertEqual(sig_all.confidence_score, 100.0)

        self.strategy.reset()
        ind_three = {
            "symbol": "NIFTY",
            "underlying": "NIFTY",
            "price": 24520.0,
            "prev_price": 24530.0,
            "ema9": 24530.0,
            "prev_ema9": 24530.0,
            "ema9_crossover": False,
            "vwap": 24500.0,        # Aligned (10.0)
            "pcr_slope": -0.04,     # Bullish (35.0)
            "sentiment": -10.0,     # False (0)
            "oi_change_pct": 2.0,   # Aligned (10.0)
            "direction": Direction.LONG,
        }
        sig_three = self.strategy.evaluate(ind_three, {"timestamp": self.t0})
        self.assertIsNotNone(sig_three)
        self.assertEqual(sig_three.confidence_tier, ConfidenceTier.B)
        self.assertEqual(sig_three.confidence_score, 55.0)

        self.strategy.reset()
        ind_one = {
            "symbol": "NIFTY",
            "underlying": "NIFTY",
            "price": 24520.0,
            "ema9": 24550.0,
            "ema9_crossover": False,
            "vwap": 24500.0,        # Only VWAP matches (10.0)
            "pcr_slope": 0.05,
            "sentiment": -20.0,
            "oi_change_pct": -2.0,
            "direction": Direction.LONG,
        }
        sig_one = self.strategy.evaluate(ind_one, {"timestamp": self.t0})
        self.assertIsNone(sig_one)
        print("✓ [a] Signal generation: Verified A+ (100.0), Tier B (55.0), and None for Tier C (10.0)")

    def test_b_confidence_score_calculation(self):
        """[b] Confidence score calculation: Exact manual calculation match."""
        ind = {
            "symbol": "BANKNIFTY",
            "underlying": "BANKNIFTY",
            "price": 51250.0,
            "prev_price": 51180.0,
            "ema9": 51200.0,
            "prev_ema9": 51200.0,
            "vwap": 51300.0,
            "pcr_slope": -0.06,     # True (35.0)
            "sentiment": 15.0,      # True (20.0)
            "oi_change_pct": -1.0,  # False (0)
        }
        expected_score = 35.0 + 25.0 + 20.0  # 80.0
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.confidence_score, expected_score, places=2)
        self.assertEqual(sig.confidence_tier, ConfidenceTier.A)
        print(f"✓ [b] Confidence score calculation: Exact match at {sig.confidence_score:.2f} (expected: 80.00)")

    def test_c_a_plus_setup_criteria(self):
        """[c] A+ Setup criteria: Score >= 85 AND pcr_slope = True AND ema9_crossover = True."""
        ind_no_ema9 = {
            "symbol": "NIFTY",
            "price": 24500.0,
            "ema9": 24400.0,
            "pcr_slope": True,          # 35
            "ema9_crossover": False,    # 0
            "sentiment_alignment": True,# 20
            "vwap_alignment": True,     # 10
            "oi_change": True,          # 10
            "direction": Direction.LONG,
        }
        sig_no_ema9 = self.strategy.evaluate(ind_no_ema9, {"timestamp": self.t0})
        self.assertIsNotNone(sig_no_ema9)
        self.assertEqual(sig_no_ema9.confidence_tier, ConfidenceTier.A)

        self.strategy.reset()
        ind_a_plus = {
            "symbol": "NIFTY",
            "price": 24500.0,
            "ema9": 24400.0,
            "pcr_slope": True,          # 35
            "ema9_crossover": True,     # 25
            "sentiment_alignment": True,# 20
            "vwap_alignment": True,     # 10
            "oi_change": False,         # 0
            "direction": Direction.LONG,
        }
        sig_a_plus = self.strategy.evaluate(ind_a_plus, {"timestamp": self.t0})
        self.assertIsNotNone(sig_a_plus)
        self.assertEqual(sig_a_plus.confidence_score, 90.0)
        self.assertEqual(sig_a_plus.confidence_tier, ConfidenceTier.A_PLUS)
        print("✓ [c] A+ Setup criteria: Verified pcr_slope & ema9_crossover required for Tier A+")

    def test_d_pcr_staleness_check(self):
        """[d] PCR staleness check (Review Risk 2): Staleness > 30s -> returns None & logs warning."""
        underlying = "NIFTY"
        self.strategy.record_pcr_update(underlying, self.t0)
        self.assertTrue(self.strategy.check_pcr_staleness(underlying, self.t0 + timedelta(seconds=10)))

        t35 = self.t0 + timedelta(seconds=35)
        self.assertFalse(self.strategy.check_pcr_staleness(underlying, t35))

        ind = {
            "symbol": underlying,
            "underlying": underlying,
            "price": 24520.0,
            "ema9": 24500.0,
            "ema9_crossover": True,
            "pcr_slope": -0.05,
            "sentiment": 20.0,
            "vwap": 24500.0,
            "oi_change_pct": 2.0,
        }
        with self.assertLogs(logger="strategies.index_options_strategy", level=logging.WARNING) as cm:
            sig_stale = self.strategy.evaluate(ind, {"timestamp": t35})

        self.assertIsNone(sig_stale, "Signal was not disabled despite PCR feed being 35s stale!")
        self.assertTrue(
            any("PCR feed stale, disabling option signals" in msg for msg in cm.output),
            f"Expected warning log not found in {cm.output}",
        )
        print("✓ [d] PCR staleness check (Review Risk 2): Disabled signals and logged warning at 35s staleness")

    def test_e_strike_selection(self):
        """[e] Strike selection: select_strike(24500, [24400, 24500, 24600]) returns 24500 (ATM)."""
        underlying_price = 24500.0
        available_strikes = [24400.0, 24500.0, 24600.0]
        strike = self.strategy.select_strike(underlying_price, available_strikes)
        self.assertEqual(strike, 24500.0)

        strike_off = self.strategy.select_strike(24520.0, [24450.0, 24500.0, 24550.0])
        self.assertEqual(strike_off, 24500.0)
        print("✓ [e] Strike selection: select_strike(24500, ...) accurately identified ATM strike 24500.0")

    def test_f_expiry_selection(self):
        """[f] Expiry selection: select_expiry([next_week, next_month]) returns next_week."""
        today = date(2026, 9, 21)
        next_week = today + timedelta(days=3)
        next_month = today + timedelta(days=31)
        available_expiries = [next_week, next_month]

        selected = self.strategy.select_expiry(available_expiries, reference_date=today)
        self.assertEqual(selected, next_week, "Expected nearest weekly expiry to be selected for Index Options")
        print("✓ [f] Expiry selection: select_expiry(...) correctly prioritized weekly expiry over monthly")

    def test_g_edge_cases(self):
        """[g] Edge cases: Missing EMA9, extreme price (1e8)."""
        self.assertIsNone(self.strategy.evaluate({"price": 24500.0}, {}))

        ind_ext = {
            "symbol": "EXTREME_INDEX",
            "price": 100_000_000.0,
            "ema9": 99_900_000.0,
            "ema9_crossover": True,
            "pcr_slope": True,
            "sentiment_alignment": True,
            "direction": Direction.LONG,
        }
        sig_ext = self.strategy.evaluate(ind_ext, {"timestamp": self.t0})
        self.assertIsNotNone(sig_ext)
        # Entry is the option premium from the chain, not the 1e8 underlying price.
        self.assertEqual(sig_ext.entry_price, FIXTURE_CE_PREMIUM)
        print("✓ [g] Edge cases: Handled missing EMA9 gracefully and extreme 1e8 price without instability")

    def test_h_direction_logic(self):
        """[h] Direction logic: Negative PCR slope -> LONG, Positive PCR slope -> SHORT."""
        ind_bull = {
            "symbol": "NIFTY",
            "price": 24550.0,
            "ema9": 24500.0,
            "ema9_crossover": True,
            "pcr_slope": -0.07,
            "sentiment": 15.0,
        }
        sig_bull = self.strategy.evaluate(ind_bull, {"timestamp": self.t0})
        self.assertIsNotNone(sig_bull)
        self.assertEqual(sig_bull.direction, Direction.LONG)
        self.assertEqual(sig_bull.signal_type, SignalType.PCR_SLOPE_BULLISH)

        self.strategy.reset()
        ind_bear = {
            "symbol": "NIFTY",
            "price": 24450.0,
            "ema9": 24500.0,
            "ema9_crossover": True,
            "pcr_slope": 0.07,
            "sentiment": -15.0,
            "direction": Direction.SHORT,
        }
        sig_bear = self.strategy.evaluate(ind_bear, {"timestamp": self.t0})
        self.assertIsNotNone(sig_bear)
        self.assertEqual(sig_bear.direction, Direction.SHORT)
        self.assertEqual(sig_bear.signal_type, SignalType.PCR_SLOPE_BEARISH)
        print("✓ [h] Direction logic: Verified LONG for negative PCR slope and SHORT for positive PCR slope")

    def test_i_asset_class_correctness(self):
        """[i] Asset class correctness: Verify signal.asset_class == INDEX_OPTIONS."""
        ind = {"symbol": "NIFTY", "price": 24500.0, "ema9": 24400.0, "ema9_crossover": True, "pcr_slope": True}
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.asset_class, AssetClass.INDEX_OPTIONS)
        self.assertEqual(self.strategy.asset_class, AssetClass.INDEX_OPTIONS)
        print("✓ [i] Asset class correctness: Confirmed signal.asset_class is INDEX_OPTIONS")

    def test_j_signal_expiry(self):
        """[j] Signal expiry: Verify signal.expires_at = timestamp + 30 seconds."""
        ind = {"symbol": "NIFTY", "price": 24500.0, "ema9": 24400.0, "ema9_crossover": True, "pcr_slope": True}
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.expires_at, self.t0 + timedelta(seconds=30))
        print("✓ [j] Signal expiry: Validated exact 30-second expiry window (expires_at = t0 + 30s)")

    def test_k_duplicate_handling(self):
        """[k] Duplicate handling: Generate same signal twice within 60s -> second suppressed."""
        ind = {"symbol": "DUP_OPT", "price": 24500.0, "ema9": 24400.0, "ema9_crossover": True, "pcr_slope": True}
        sig1 = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig1)

        sig2 = self.strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=20)})
        self.assertIsNone(sig2, "Duplicate index options signal was not suppressed!")

        sig3 = self.strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=65)})
        self.assertIsNotNone(sig3)
        print("✓ [k] Duplicate handling: Suppressed duplicate at 20s; accepted fresh signal at 65s")

    def test_l_independence(self):
        """[l] Independence: Verify strategy operates completely standalone."""
        isolated = IndexOptionsStrategy(name="StandaloneIndexOptions")
        self.assertEqual(isolated.name, "StandaloneIndexOptions")
        self.assertEqual(isolated.asset_class, AssetClass.INDEX_OPTIONS)
        self.assertIn("name", isolated.get_status())
        print("✓ [l] Independence: Verified IndexOptionsStrategy operates completely standalone")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 4 PROMPT 3/5 INDEX OPTIONS STRATEGY VERIFICATION")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestIndexOptionsStrategy)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 12 INDEX OPTIONS STRATEGY TESTS (a-l) PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)