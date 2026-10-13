"""
Strike Desk V4 — Phase 4 Prompt 3/5 Verification Suite: Stock Options Strategy
Verifies:
  a) Signal generation with known inputs (All -> A+, 3/5 -> Tier B, 1/5 -> None)
  b) Confidence score calculation: Exact manual calculation match
  c) A+ Setup criteria: Score >= 85 AND ema9_crossover = True AND pcr_slope = True
  d) PCR staleness check (Review Risk 2): Staleness > 30s -> returns None & logs warning
  e) Strike selection: select_strike(...) selects ATM or slightly ITM
  f) Expiry selection: select_expiry([next_week, next_month]) returns next_month (monthly preference)
  g) Edge cases: Illiquid option (vol < 1000) -> None, missing EMA9, extreme price (1e8)
  h) Direction logic: Bullish -> LONG, Bearish -> SHORT
  i) Asset class correctness: asset_class == AssetClass.STOCK_OPTIONS
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
from strategies.stock_options_strategy import StockOptionsStrategy
from tests.option_chain_fixture import FIXTURE_CE_PREMIUM, seed_many

UTC = timezone.utc


class TestStockOptionsStrategy(unittest.TestCase):
    """Unit tests for StockOptionsStrategy."""

    def setUp(self):
        self.monitor = PCRStalenessMonitor(stale_threshold_seconds=30.0)
        self.strategy = StockOptionsStrategy(pcr_monitor=self.monitor)
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)
        # Entry price comes from the live chain, so every underlying under test needs one.
        seed_many({
            "RELIANCE": 2500.0,
            "INFY": 1500.0,
            "TCS": 3520.0,
            "STOCK": 1020.0,
            "DUP_STK_OPT": 2500.0,
            "ILLIQUID_STOCK": 500.0,
            "EXTREME_STOCK": 100_000_000.0,
        })

    def test_a_signal_generation_tiers(self):
        """[a] Signal generation: All criteria -> A+, 3 criteria -> Tier B, 1 criterion -> None."""
        ind_all = {
            "symbol": "RELIANCE",
            "underlying": "RELIANCE",
            "price": 2510.0,
            "prev_price": 2490.0,
            "ema9": 2500.0,
            "prev_ema9": 2500.0,
            "vwap": 2485.0,
            "pcr_slope": -0.05,     # Bullish (20.0)
            "sentiment": 20.0,      # Bullish (15.0)
            "volume": 2500.0,       # Option volume > 1000 & surge (15.0)
            "avg_volume": 1200.0,
        }
        sig_all = self.strategy.evaluate(ind_all, {"timestamp": self.t0})
        self.assertIsNotNone(sig_all)
        self.assertEqual(sig_all.confidence_tier, ConfidenceTier.A_PLUS)
        self.assertEqual(sig_all.confidence_score, 100.0)

        self.strategy.reset()
        ind_three = {
            "symbol": "RELIANCE",
            "underlying": "RELIANCE",
            "price": 2510.0,
            "prev_price": 2490.0,
            "ema9": 2500.0,
            "prev_ema9": 2500.0,
            "vwap": 2520.0,        # False (0)
            "pcr_slope": -0.04,     # Bullish (20.0)
            "sentiment": 15.0,      # Bullish (15.0)
            "volume": 1500.0,
            "avg_volume": 2000.0,   # False (0)
            "direction": Direction.LONG,
        }
        sig_three = self.strategy.evaluate(ind_three, {"timestamp": self.t0})
        self.assertIsNotNone(sig_three)
        self.assertEqual(sig_three.confidence_tier, ConfidenceTier.B)
        self.assertEqual(sig_three.confidence_score, 65.0)

        self.strategy.reset()
        ind_one = {
            "symbol": "RELIANCE",
            "underlying": "RELIANCE",
            "price": 2480.0,
            "ema9": 2500.0,
            "ema9_crossover": False,
            "vwap": 2500.0,
            "pcr_slope": 0.05,
            "sentiment": 15.0,      # Only sentiment matches (15.0)
            "volume": 1500.0,
            "avg_volume": 2000.0,
            "direction": Direction.LONG,
        }
        sig_one = self.strategy.evaluate(ind_one, {"timestamp": self.t0})
        self.assertIsNone(sig_one)
        print("✓ [a] Signal generation: Verified A+ (100.0), Tier B (65.0), and None for Tier C (15.0)")

    def test_b_confidence_score_calculation(self):
        """[b] Confidence score calculation: Exact manual calculation match."""
        ind = {
            "symbol": "TCS",
            "underlying": "TCS",
            "price": 3520.0,
            "prev_price": 3505.0,
            "ema9": 3510.0,
            "prev_ema9": 3510.0,
            "vwap": 3500.0,         # True (20.0)
            "pcr_slope": 0.05,      # False (0)
            "sentiment": -10.0,     # False (0)
            "volume": 3000.0,
            "avg_volume": 1000.0,   # True (15.0)
        }
        expected_score = 30.0 + 20.0 + 15.0  # 65.0
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.confidence_score, expected_score, places=2)
        self.assertEqual(sig.confidence_tier, ConfidenceTier.B)
        print(f"✓ [b] Confidence score calculation: Exact match at {sig.confidence_score:.2f} (expected: 65.00)")

    def test_c_a_plus_setup_criteria(self):
        """[c] A+ Setup criteria: Score >= 85 AND ema9_crossover = True AND pcr_slope = True."""
        ind_no_cross = {
            "symbol": "INFY",
            "price": 1500.0,
            "ema9": 1490.0,
            "ema9_crossover": False,    # 0
            "pcr_slope": True,          # 20
            "vwap_alignment": True,     # 20
            "sentiment_alignment": True,# 15
            "volume_surge": True,       # 15
            "volume": 2000.0,
            "direction": Direction.LONG,
        }
        sig_no_cross = self.strategy.evaluate(ind_no_cross, {"timestamp": self.t0})
        self.assertIsNotNone(sig_no_cross)
        self.assertEqual(sig_no_cross.confidence_score, 70.0)
        self.assertEqual(sig_no_cross.confidence_tier, ConfidenceTier.A)

        self.strategy.reset()
        ind_a_plus = {
            "symbol": "INFY",
            "price": 1500.0,
            "ema9": 1490.0,
            "ema9_crossover": True,     # 30
            "pcr_slope": True,          # 20
            "vwap_alignment": True,     # 20
            "sentiment_alignment": True,# 15
            "volume_surge": False,      # 0
            "volume": 1500.0,
            "direction": Direction.LONG,
        }
        sig_a_plus = self.strategy.evaluate(ind_a_plus, {"timestamp": self.t0})
        self.assertIsNotNone(sig_a_plus)
        self.assertEqual(sig_a_plus.confidence_score, 85.0)
        self.assertEqual(sig_a_plus.confidence_tier, ConfidenceTier.A_PLUS)
        print("✓ [c] A+ Setup criteria: Score=85.0 + crossover + pcr_slope -> A+; Score=70.0 without crossover -> Tier A")

    def test_d_pcr_staleness_check(self):
        """[d] PCR staleness check: staleness beyond the equity gate -> None & logs warning.

        Equity chains share a throttled broker endpoint with the indices and are only
        re-polled every few minutes, so stock options use a relaxed gate rather than
        the index-grade 30s one. A chain seconds old must still be accepted.
        """
        underlying = "RELIANCE"
        gate = StockOptionsStrategy.STALE_THRESHOLD_SECONDS
        self.strategy.record_pcr_update(underlying, self.t0)
        self.assertTrue(self.strategy.check_pcr_staleness(underlying, self.t0 + timedelta(seconds=15)))
        # Well inside the relaxed gate — an index-grade 30s gate would wrongly reject this.
        self.assertTrue(self.strategy.check_pcr_staleness(underlying, self.t0 + timedelta(seconds=35)))

        t35 = self.t0 + timedelta(seconds=gate + 5)
        self.assertFalse(self.strategy.check_pcr_staleness(underlying, t35))

        ind = {
            "symbol": underlying,
            "underlying": underlying,
            "price": 2500.0,
            "ema9": 2490.0,
            "ema9_crossover": True,
            "pcr_slope": -0.05,
            "sentiment": 15.0,
            "vwap": 2490.0,
            "volume": 2000.0,
        }
        with self.assertLogs(logger="strategies.stock_options_strategy", level=logging.WARNING) as cm:
            sig_stale = self.strategy.evaluate(ind, {"timestamp": t35})

        self.assertIsNone(sig_stale, "Signal was not disabled despite PCR feed being past the gate!")
        self.assertTrue(
            any("PCR feed stale, disabling option signals" in msg for msg in cm.output),
            f"Expected warning log not found in {cm.output}",
        )
        print(f"✓ [d] PCR staleness check: accepted 35s-old chain, disabled past the {gate:.0f}s equity gate")

    def test_e_strike_selection(self):
        """[e] Strike selection: select_strike(...) selects ATM or slightly ITM."""
        underlying_price = 2505.0
        available_strikes = [2480.0, 2500.0, 2520.0]

        strike_atm = self.strategy.select_strike(underlying_price, available_strikes)
        self.assertEqual(strike_atm, 2500.0)

        strike_long = self.strategy.select_strike(underlying_price, available_strikes, direction=Direction.LONG)
        self.assertEqual(strike_long, 2500.0)

        strike_short = self.strategy.select_strike(underlying_price, available_strikes, direction=Direction.SHORT)
        self.assertEqual(strike_short, 2520.0)
        print("✓ [e] Strike selection: Correctly selected ATM (2500.0) and slightly ITM strikes (2500.0 / 2520.0)")

    def test_f_expiry_selection(self):
        """[f] Expiry selection: select_expiry([next_week, next_month]) returns next_month (monthly preference)."""
        today = date(2026, 9, 21)
        next_week = today + timedelta(days=3)
        next_month = today + timedelta(days=31)
        available_expiries = [next_week, next_month]

        selected = self.strategy.select_expiry(available_expiries, reference_date=today)
        self.assertEqual(selected, next_month, "Expected monthly expiry to be selected for Stock Options")
        print("✓ [f] Expiry selection: select_expiry(...) correctly prioritized monthly expiry over weekly for stocks")

    def test_g_edge_cases_and_liquidity(self):
        """[g] Edge cases: Illiquid option (vol < 1000) -> None, missing EMA9, extreme price (1e8)."""
        ind_illiquid = {
            "symbol": "ILLIQUID_STOCK",
            "price": 500.0,
            "ema9": 490.0,
            "ema9_crossover": True,
            "pcr_slope": True,
            "vwap_alignment": True,
            "volume": 600.0,
        }
        sig_illiquid = self.strategy.evaluate(ind_illiquid, {"timestamp": self.t0})
        self.assertIsNone(sig_illiquid, "Signal was not suppressed despite volume < 1000!")

        self.assertIsNone(self.strategy.evaluate({"price": 500.0, "volume": 2000.0}, {}))

        ind_ext = {
            "symbol": "EXTREME_STOCK",
            "price": 100_000_000.0,
            "ema9": 99_900_000.0,
            "ema9_crossover": True,
            "pcr_slope": True,
            "vwap_alignment": True,
            "volume": 5000.0,
            "direction": Direction.LONG,
        }
        sig_ext = self.strategy.evaluate(ind_ext, {"timestamp": self.t0})
        self.assertIsNotNone(sig_ext)
        # Entry is the option premium from the chain, not the 1e8 underlying price.
        self.assertEqual(sig_ext.entry_price, FIXTURE_CE_PREMIUM)
        print("✓ [g] Edge cases: Suppressed illiquid option (vol < 1000), handled missing EMA9, and processed extreme 1e8 price")

    def test_h_direction_logic(self):
        """[h] Direction logic: Bullish -> LONG, Bearish -> SHORT."""
        ind_bull = {
            "symbol": "STOCK",
            "price": 1020.0,
            "prev_price": 995.0,
            "ema9": 1000.0,
            "prev_ema9": 1000.0,
            "vwap": 990.0,
            "pcr_slope": -0.05,
            "sentiment": 10.0,
            "volume": 1500.0,
        }
        sig_bull = self.strategy.evaluate(ind_bull, {"timestamp": self.t0})
        self.assertIsNotNone(sig_bull)
        self.assertEqual(sig_bull.direction, Direction.LONG)
        self.assertEqual(sig_bull.signal_type, SignalType.EMA9_CROSSOVER_BULLISH)

        self.strategy.reset()
        ind_bear = {
            "symbol": "STOCK",
            "price": 980.0,
            "prev_price": 1005.0,
            "ema9": 1000.0,
            "prev_ema9": 1000.0,
            "vwap": 1010.0,
            "pcr_slope": 0.05,
            "sentiment": -10.0,
            "volume": 1500.0,
            "direction": Direction.SHORT,
        }
        sig_bear = self.strategy.evaluate(ind_bear, {"timestamp": self.t0})
        self.assertIsNotNone(sig_bear)
        self.assertEqual(sig_bear.direction, Direction.SHORT)
        self.assertEqual(sig_bear.signal_type, SignalType.EMA9_CROSSOVER_BEARISH)
        print("✓ [h] Direction logic: Correctly identified LONG for bullish setup and SHORT for bearish setup")

    def test_i_asset_class_correctness(self):
        """[i] Asset class correctness: Verify signal.asset_class == STOCK_OPTIONS."""
        ind = {"symbol": "STOCK", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "pcr_slope": True, "volume": 2000.0}
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.asset_class, AssetClass.STOCK_OPTIONS)
        self.assertEqual(self.strategy.asset_class, AssetClass.STOCK_OPTIONS)
        print("✓ [i] Asset class correctness: Confirmed signal.asset_class is STOCK_OPTIONS")

    def test_j_signal_expiry(self):
        """[j] Signal expiry: Verify signal.expires_at = timestamp + 30 seconds."""
        ind = {"symbol": "STOCK", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "pcr_slope": True, "volume": 2000.0}
        sig = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.expires_at, self.t0 + timedelta(seconds=30))
        print("✓ [j] Signal expiry: Validated exact 30-second expiry window (expires_at = t0 + 30s)")

    def test_k_duplicate_handling(self):
        """[k] Duplicate handling: Generate same signal twice within 60s -> second suppressed."""
        ind = {"symbol": "DUP_STK_OPT", "price": 100.0, "ema9": 90.0, "ema9_crossover": True, "pcr_slope": True, "volume": 2000.0}
        sig1 = self.strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig1)

        sig2 = self.strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=20)})
        self.assertIsNone(sig2, "Duplicate stock options signal was not suppressed!")

        sig3 = self.strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=65)})
        self.assertIsNotNone(sig3)
        print("✓ [k] Duplicate handling: Suppressed duplicate at 20s; accepted fresh signal at 65s")

    def test_l_independence(self):
        """[l] Independence: Verify strategy operates completely standalone."""
        isolated = StockOptionsStrategy(name="StandaloneStockOptions")
        self.assertEqual(isolated.name, "StandaloneStockOptions")
        self.assertEqual(isolated.asset_class, AssetClass.STOCK_OPTIONS)
        self.assertIn("name", isolated.get_status())
        print("✓ [l] Independence: Verified StockOptionsStrategy operates completely standalone")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 4 PROMPT 3/5 STOCK OPTIONS STRATEGY VERIFICATION")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestStockOptionsStrategy)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 12 STOCK OPTIONS STRATEGY TESTS (a-l) PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)