"""
Strike Desk V4 — Phase 4 Prompt 1/5 Verification Suite: Strategy Base Architecture & Signal Model
Verifies:
  a) Signal creation: Required fields, valid defaults, is_actionable() logic
  b) Signal expiry: Expiry time check, is_expired() after 35s
  c) Confidence scoring: Weighted sum calculation matching reference manual score
  d) Confidence tier mapping: 85 -> A+, 70 -> A, 55 -> B, 54 -> C
  e) Duplicate detection: Same symbol + direction within 60s rejected
  f) Signal store top-N: Only top 2 signals per asset class retained
  g) Signal store pruning: Pruning expired signals returns correct count
  h) Base strategy interface: Concrete subclass evaluate() returns Signal or None
  i) Criteria validation: Weights sum to 100, missing weights rejected
  j) Confidence NOT probability: Explicit documentation check
"""
from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from typing import Any, Dict, Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal, SignalType
import strategies.confidence
from strategies.base_strategy import BaseStrategy
from strategies.confidence import ConfidenceScorer, confidence_scorer
from strategies.signal_store import SignalStore, signal_store

UTC = timezone.utc


class MockCashStrategy(BaseStrategy):
    """Concrete mock strategy for testing BaseStrategy contract."""

    def __init__(self) -> None:
        weights = {
            "ema_crossover": 30.0,
            "vwap_support": 25.0,
            "volume_surge": 20.0,
            "pcr_bullish": 15.0,
            "sentiment_bullish": 10.0,
        }
        super().__init__(
            name="MockStockCashStrategy",
            asset_class=AssetClass.STOCK_CASH,
            criteria_weights=weights,
            signal_expiry_seconds=30,
            duplicate_window_seconds=60,
        )

    def get_criteria_checklist(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Dict[str, bool]:
        return {
            "ema_crossover": indicators.get("ema_cross", False),
            "vwap_support": indicators.get("price", 0.0) > indicators.get("vwap", 0.0),
            "volume_surge": indicators.get("volume", 0.0) > 1000.0,
            "pcr_bullish": indicators.get("pcr_slope", 0.0) < 0.0,
            "sentiment_bullish": indicators.get("sentiment", 0.0) > 0.0,
        }

    def evaluate(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Optional[Signal]:
        checklist = self.get_criteria_checklist(indicators, market_state)
        score = confidence_scorer.calculate_score(checklist, self.criteria_weights)
        tier = confidence_scorer.get_tier(score)

        if tier == ConfidenceTier.C:
            return None

        entry_price = float(indicators.get("price", 100.0))
        sig = Signal(
            symbol=indicators.get("symbol", "TEST"),
            exchange=Exchange.NSE,
            asset_class=self.asset_class,
            signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
            direction=Direction.LONG,
            entry_price=entry_price,
            target_price=entry_price * 1.02,
            stop_loss_price=entry_price * 0.99,
            confidence_score=score,
            confidence_tier=tier,
            checklist=[k for k, v in checklist.items() if v],
            strategy_name=self.name,
            timestamp=market_state.get("timestamp", datetime.now(UTC)),
        )

        if self._store_signal(sig):
            return sig
        return None


class TestStrategyBaseArchitecture(unittest.TestCase):
    """
    Verification suite for Strategy Base Architecture and Signal Model.
    """

    def test_a_signal_creation_and_actionable(self):
        """[a] Signal creation: Verify required fields and is_actionable() logic."""
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)
        sig_a_plus = Signal(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_CASH,
            signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
            direction=Direction.LONG,
            entry_price=2500.0,
            target_price=2550.0,
            stop_loss_price=2475.0,
            confidence_score=88.0,
            confidence_tier=ConfidenceTier.A_PLUS,
            timestamp=t0,
        )

        self.assertTrue(sig_a_plus.is_actionable(t0))
        self.assertFalse(sig_a_plus.is_expired(t0))
        self.assertEqual(sig_a_plus.confidence_tier, ConfidenceTier.A_PLUS)

        # Tier C signal is not actionable
        sig_c = Signal(
            symbol="TCS",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_CASH,
            signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
            direction=Direction.LONG,
            entry_price=3500.0,
            confidence_score=40.0,
            confidence_tier=ConfidenceTier.C,
            timestamp=t0,
        )
        self.assertFalse(sig_c.is_actionable(t0))

        # Check serialization dictionary
        d = sig_a_plus.to_dict()
        self.assertEqual(d["symbol"], "RELIANCE")
        self.assertEqual(d["confidence_tier"], "A+")
        self.assertEqual(d["direction"], "LONG")
        print("✓ [a] Signal creation: Validated signal attributes and is_actionable() for tiers A+ vs C")

    def test_b_signal_expiry(self):
        """[b] Signal expiry: Advance time by 35 seconds, verify is_expired() returns True."""
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)
        sig = Signal(
            symbol="INFY",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_CASH,
            signal_type=SignalType.VWAP_DEVIATION_LONG,
            direction=Direction.LONG,
            entry_price=1500.0,
            confidence_score=75.0,
            confidence_tier=ConfidenceTier.A,
            timestamp=t0,
        )

        # At t=25s: Not expired and actionable
        t_25 = t0 + timedelta(seconds=25)
        self.assertFalse(sig.is_expired(t_25))
        self.assertTrue(sig.is_actionable(t_25))

        # At t=35s: Expired (default expiry is 30s)
        t_35 = t0 + timedelta(seconds=35)
        self.assertTrue(sig.is_expired(t_35))
        self.assertFalse(sig.is_actionable(t_35))
        print("✓ [b] Signal expiry: Signal active at 25s, expired and non-actionable at 35s (> 30s window)")

    def test_c_confidence_scoring(self):
        """[c] Confidence scoring: 5 criteria with known weights, verify score matches manual calculation."""
        weights = {
            "ema9_crossover": 30.0,
            "price_above_vwap": 25.0,
            "volume_surge": 20.0,
            "pcr_slope_negative": 15.0,
            "sentiment_positive": 10.0,
        }
        criteria_results = {
            "ema9_crossover": True,      # +30
            "price_above_vwap": True,    # +25
            "volume_surge": False,       # 0
            "pcr_slope_negative": True,  # +15
            "sentiment_positive": True,  # +10
        }

        # Expected: 30 + 25 + 0 + 15 + 10 = 80.0
        score = confidence_scorer.calculate_score(criteria_results, weights)
        self.assertAlmostEqual(score, 80.0)
        self.assertEqual(confidence_scorer.get_tier(score), ConfidenceTier.A)
        print(f"✓ [c] Confidence scoring: 4 of 5 criteria met -> calculated score = {score:.1f} (expected: 80.0)")

    def test_d_confidence_tier_mapping(self):
        """[d] Confidence tier mapping: Verify score 85 -> A+, 70 -> A, 55 -> B, 54 -> C."""
        self.assertEqual(confidence_scorer.get_tier(85.0), ConfidenceTier.A_PLUS)
        self.assertEqual(confidence_scorer.get_tier(95.0), ConfidenceTier.A_PLUS)
        self.assertEqual(confidence_scorer.get_tier(100.0), ConfidenceTier.A_PLUS)

        self.assertEqual(confidence_scorer.get_tier(70.0), ConfidenceTier.A)
        self.assertEqual(confidence_scorer.get_tier(84.9), ConfidenceTier.A)

        self.assertEqual(confidence_scorer.get_tier(55.0), ConfidenceTier.B)
        self.assertEqual(confidence_scorer.get_tier(69.9), ConfidenceTier.B)

        self.assertEqual(confidence_scorer.get_tier(54.9), ConfidenceTier.C)
        self.assertEqual(confidence_scorer.get_tier(30.0), ConfidenceTier.C)
        self.assertEqual(confidence_scorer.get_tier(0.0), ConfidenceTier.C)
        print("✓ [d] Confidence tier mapping: Verified boundary scores 85->A+, 70->A, 55->B, 54->C")

    def test_e_duplicate_detection(self):
        """[e] Duplicate detection: Same symbol + direction within 60s rejected; different direction accepted."""
        strategy = MockCashStrategy()
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

        sig1 = Signal(
            symbol="HDFCBANK",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_CASH,
            signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
            direction=Direction.LONG,
            entry_price=1600.0,
            confidence_score=75.0,
            confidence_tier=ConfidenceTier.A,
            timestamp=t0,
        )
        stored1 = strategy._store_signal(sig1)
        self.assertTrue(stored1)

        # Same symbol + direction at t+30s (< 60s duplicate window) -> REJECTED
        sig2 = Signal(
            symbol="HDFCBANK",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_CASH,
            signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
            direction=Direction.LONG,
            entry_price=1605.0,
            confidence_score=80.0,
            confidence_tier=ConfidenceTier.A,
            timestamp=t0 + timedelta(seconds=30),
        )
        stored2 = strategy._store_signal(sig2)
        self.assertFalse(stored2, "Duplicate signal was not suppressed!")

        # Opposite direction (SHORT) at t+30s -> ACCEPTED
        sig3 = Signal(
            symbol="HDFCBANK",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_CASH,
            signal_type=SignalType.EMA9_CROSSOVER_BEARISH,
            direction=Direction.SHORT,
            entry_price=1595.0,
            confidence_score=75.0,
            confidence_tier=ConfidenceTier.A,
            timestamp=t0 + timedelta(seconds=30),
        )
        stored3 = strategy._store_signal(sig3)
        self.assertTrue(stored3)

        # Same symbol + direction at t+65s (> 60s duplicate window) -> ACCEPTED
        sig4 = Signal(
            symbol="HDFCBANK",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_CASH,
            signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
            direction=Direction.LONG,
            entry_price=1610.0,
            confidence_score=85.0,
            confidence_tier=ConfidenceTier.A_PLUS,
            timestamp=t0 + timedelta(seconds=65),
        )
        stored4 = strategy._store_signal(sig4)
        self.assertTrue(stored4)
        print("✓ [e] Duplicate detection: Suppressed duplicate at 30s; accepted opposite direction and tick at 65s")

    def test_f_signal_store_top_n(self):
        """[f] Signal store top-N: Add 5 signals for same asset class, verify only top 2 retained."""
        store = SignalStore(max_per_asset_class=2)
        store.clear()
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

        scores = [60.0, 75.0, 80.0, 70.0, 90.0]
        symbols = ["SYM_1", "SYM_2", "SYM_3", "SYM_4", "SYM_5"]

        for sym, sc in zip(symbols, scores):
            sig = Signal(
                symbol=sym,
                exchange=Exchange.NSE,
                asset_class=AssetClass.STOCK_CASH,
                signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
                direction=Direction.LONG,
                entry_price=100.0,
                confidence_score=sc,
                confidence_tier=confidence_scorer.get_tier(sc),
                timestamp=t0,
            )
            store.add_signal(sig)

        top_signals = store.get_top_signals(AssetClass.STOCK_CASH, n=2)
        self.assertEqual(len(top_signals), 2)
        self.assertEqual(top_signals[0].confidence_score, 90.0)
        self.assertEqual(top_signals[0].symbol, "SYM_5")
        self.assertEqual(top_signals[1].confidence_score, 80.0)
        self.assertEqual(top_signals[1].symbol, "SYM_3")
        print("✓ [f] Signal store top-N: Retained top 2 recommendations (scores: 90.0, 80.0) out of 5 added")

    def test_g_signal_store_pruning(self):
        """[g] Signal store pruning: Add expired signals, call prune_expired(), verify count matches."""
        store = SignalStore(max_per_asset_class=5)
        store.clear()
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

        # 2 signals expiring at t0 + 30s
        sig1 = Signal(
            symbol="EXP_1",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_OPTIONS,
            signal_type=SignalType.PCR_SLOPE_BULLISH,
            direction=Direction.LONG,
            entry_price=150.0,
            confidence_score=75.0,
            timestamp=t0,
            expires_at=t0 + timedelta(seconds=30),
        )
        sig2 = Signal(
            symbol="EXP_2",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_OPTIONS,
            signal_type=SignalType.PCR_SLOPE_BULLISH,
            direction=Direction.LONG,
            entry_price=200.0,
            confidence_score=70.0,
            timestamp=t0,
            expires_at=t0 + timedelta(seconds=30),
        )
        # 1 signal expiring at t0 + 60s
        sig3 = Signal(
            symbol="ACTIVE_1",
            exchange=Exchange.NSE,
            asset_class=AssetClass.STOCK_OPTIONS,
            signal_type=SignalType.PCR_SLOPE_BULLISH,
            direction=Direction.LONG,
            entry_price=250.0,
            confidence_score=85.0,
            timestamp=t0,
            expires_at=t0 + timedelta(seconds=60),
        )

        store.add_signal(sig1)
        store.add_signal(sig2)
        store.add_signal(sig3)

        # Prune at t0 + 35s -> 2 signals expired
        pruned_count = store.prune_expired(current_time=t0 + timedelta(seconds=35))
        self.assertEqual(pruned_count, 2)

        retained = store.get_top_signals(AssetClass.STOCK_OPTIONS)
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].symbol, "ACTIVE_1")
        print("✓ [g] Signal store pruning: Pruned 2 expired signals at t=35s, retained active signal (expires at t=60s)")

    def test_h_base_strategy_interface(self):
        """[h] Base strategy interface: Concrete subclass evaluate() returns Signal or None."""
        strategy = MockCashStrategy()
        t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

        # Case 1: High conviction setup (4 criteria met: score=80, Tier A) -> Returns Signal
        indicators_bullish = {
            "symbol": "SBIN",
            "price": 820.0,
            "vwap": 815.0,        # True (+25)
            "ema_cross": True,     # True (+30)
            "volume": 2000.0,      # True (+20)
            "pcr_slope": 0.05,     # False (0)
            "sentiment": 15.0,     # True (+10)
        }
        sig = strategy.evaluate(indicators_bullish, {"timestamp": t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.symbol, "SBIN")
        self.assertEqual(sig.confidence_tier, ConfidenceTier.A_PLUS)
        self.assertEqual(sig.confidence_score, 85.0)

        # Case 2: Poor conviction setup (only 1 criterion met: score=20, Tier C) -> Returns None
        indicators_weak = {
            "symbol": "SBIN",
            "price": 810.0,
            "vwap": 815.0,        # False
            "ema_cross": False,    # False
            "volume": 2000.0,      # True (+20)
            "pcr_slope": 0.05,     # False
            "sentiment": -10.0,    # False
        }
        sig_weak = strategy.evaluate(indicators_weak, {"timestamp": t0 + timedelta(seconds=70)})
        self.assertIsNone(sig_weak)
        print("✓ [h] Base strategy interface: evaluate() produced Signal for Tier A setup and None for Tier C")

    def test_i_criteria_validation(self):
        """[i] Criteria validation: Verify weights must sum to 100, all criteria must have weights."""
        valid_weights = {"c1": 50.0, "c2": 30.0, "c3": 20.0}
        valid_criteria = {"c1": True, "c2": False, "c3": True}
        self.assertTrue(confidence_scorer.validate_criteria(valid_criteria, valid_weights))

        # Weight sum != 100
        invalid_weights = {"c1": 50.0, "c2": 30.0}
        self.assertFalse(confidence_scorer.validate_criteria(valid_criteria, invalid_weights))
        with self.assertRaises(ValueError):
            confidence_scorer.validate_criteria(valid_criteria, invalid_weights, raise_exception=True)

        # Missing weight for criterion
        missing_weight_dict = {"c1": 50.0, "c2": 50.0}
        criteria_with_c3 = {"c1": True, "c2": True, "c3": False}
        self.assertFalse(confidence_scorer.validate_criteria(criteria_with_c3, missing_weight_dict))
        with self.assertRaises(ValueError):
            confidence_scorer.validate_criteria(criteria_with_c3, missing_weight_dict, raise_exception=True)

        # BaseStrategy initialization validation
        class DummyInvalidStrategy(BaseStrategy):
            def evaluate(self, indicators, market_state): return None
            def get_criteria_checklist(self, indicators, market_state): return {}

        with self.assertRaises(ValueError):
            DummyInvalidStrategy(
                name="InvalidWeightStrategy",
                asset_class=AssetClass.STOCK_CASH,
                criteria_weights={"a": 40.0, "b": 40.0},
            )
        print("✓ [i] Criteria validation: Verified requirement that weights must sum to 100.0 and cover all criteria")

    def test_j_confidence_not_probability_documentation(self):
        """[j] Confidence NOT probability: Verify docstring explicitly states 'NOT a probability of profit'."""
        required_phrase = "NOT a probability of profit"

        mod_doc = strategies.confidence.__doc__
        class_doc = ConfidenceScorer.__doc__
        method_doc = ConfidenceScorer.calculate_score.__doc__

        self.assertIn(required_phrase, mod_doc, "Module docstring must contain disclaimer")
        self.assertIn(required_phrase, class_doc, "ConfidenceScorer class docstring must contain disclaimer")
        self.assertIn(required_phrase, method_doc, "calculate_score docstring must contain disclaimer")
        print("✓ [j] Confidence NOT probability: Confirmed mandatory disclaimer explicitly stated in documentation")


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestStrategyBaseArchitecture)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 10 STRATEGY BASE ARCHITECTURE TESTS (a-j) PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)
