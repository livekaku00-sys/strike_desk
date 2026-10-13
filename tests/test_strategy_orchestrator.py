"""
Strike Desk V4 - Unit Tests for Strategy Orchestrator & Cascade Risk Controls
Verifies:
  [a] Universal signal generation: All 5 classes produce signals concurrently
  [b] Top-2 storage per asset class: SignalStore strictly retains top 2 recommendations
  [c] Selection change: Atomic category switching without platform restart
  [d] Cascade brake trigger: 3+ classes signaling within 30s triggers cascade brake
  [e] Orchestrator telemetry and status reporting
"""
from datetime import datetime, timedelta, timezone
import logging
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from execution.audit_logger import AuditLogger
from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal, SignalType
from pipeline.indicators.pcr_staleness_monitor import pcr_staleness_monitor
from strategies.signal_store import SignalStore
from strategies.strategy_orchestrator import StrategyOrchestrator
from tests.option_chain_fixture import seed_many

UTC = timezone.utc


class TestStrategyOrchestrator(unittest.TestCase):
    """Unit tests for StrategyOrchestrator."""

    def setUp(self):
        pcr_staleness_monitor.clear()
        # Option strategies resolve entry price from the live chain.
        seed_many({"NIFTY": 24520.0, "RELIANCE": 2520.0})
        self.signal_store = SignalStore()
        self.audit_logger = AuditLogger()
        self.orchestrator = StrategyOrchestrator(
            signal_store_instance=self.signal_store,
            audit_logger_instance=self.audit_logger,
        )
        self.orchestrator.reset()
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

    def _create_universal_indicator_payload(self) -> dict:
        """Helper returning favorable indicators for all 5 asset classes."""
        return {
            AssetClass.STOCK_CASH: {
                "symbol": "TCS",
                "price": 3520.0,
                "prev_price": 3500.0,
                "ema9": 3510.0,
                "prev_ema9": 3510.0,
                "vwap": 3490.0,
                "volume": 25000.0,
                "avg_volume": 10000.0,
                "pcr_slope": -0.05,
                "sentiment": 25.0,
            },
            AssetClass.STOCK_FUTURES: {
                "symbol": "TCS_FUT",
                "price": 3530.0,
                "prev_price": 3510.0,
                "ema9": 3515.0,
                "prev_ema9": 3515.0,
                "vwap": 3500.0,
                "spot_price": 3520.0,
                "oi_change_pct": 4.0,
                "sentiment": 20.0,
            },
            AssetClass.INDEX_FUTURES: {
                "symbol": "NIFTY_FUT",
                "price": 24520.0,
                "prev_price": 24490.0,
                "ema9": 24500.0,
                "prev_ema9": 24500.0,
                "vwap": 24480.0,
                "pcr_slope": -0.05,
                "oi_change_pct": 3.0,
                "sentiment": 20.0,
            },
            AssetClass.INDEX_OPTIONS: {
                "symbol": "NIFTY",
                "underlying": "NIFTY",
                "pcr_timestamp": self.t0,
                "price": 24520.0,
                "prev_price": 24490.0,
                "ema9": 24500.0,
                "prev_ema9": 24500.0,
                "vwap": 24480.0,
                "pcr_slope": -0.06,
                "sentiment": 25.0,
                "oi_change_pct": 3.0,
            },
            AssetClass.STOCK_OPTIONS: {
                "symbol": "RELIANCE",
                "underlying": "RELIANCE",
                "pcr_timestamp": self.t0,
                "price": 2520.0,
                "prev_price": 2500.0,
                "ema9": 2505.0,
                "prev_ema9": 2505.0,
                "vwap": 2490.0,
                "pcr_slope": -0.04,
                "sentiment": 20.0,
                "volume": 2500.0,
                "avg_volume": 1200.0,
            },
        }

    def test_a_universal_signal_generation(self):
        """[a] Universal signal generation: All 5 classes produce signals when indicators are favorable."""
        indicators = self._create_universal_indicator_payload()
        signals = self.orchestrator.evaluate_all(
            indicators,
            {"timestamp": self.t0, "days_to_expiry": 10.0},
        )

        self.assertIsInstance(signals, dict)
        self.assertEqual(len(signals), 5)

        for asset_class in [
            AssetClass.STOCK_CASH,
            AssetClass.STOCK_FUTURES,
            AssetClass.INDEX_FUTURES,
            AssetClass.INDEX_OPTIONS,
            AssetClass.STOCK_OPTIONS,
        ]:
            self.assertIn(asset_class, signals)
            class_signals = signals[asset_class]
            self.assertGreaterEqual(
                len(class_signals), 1, f"No signal generated for {asset_class}"
            )
            sig = class_signals[0]
            self.assertEqual(sig.asset_class, asset_class)
            self.assertIn(sig.confidence_tier, (ConfidenceTier.A_PLUS, ConfidenceTier.A, ConfidenceTier.B))

        print("✓ [a] Universal signal generation: Verified all 5 asset classes produced signals concurrently")

    def test_b_top_2_storage(self):
        """[b] Top-2 storage: Each class retains exactly top 2 signals in the SignalStore."""
        for i, score in enumerate([60.0, 75.0, 95.0, 85.0, 70.0]):
            sig = Signal(
                symbol=f"STOCK_{i}",
                exchange=Exchange.NSE,
                asset_class=AssetClass.STOCK_CASH,
                signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
                direction=Direction.LONG,
                entry_price=100.0 + i,
                confidence_score=score,
                confidence_tier=ConfidenceTier.from_score(score),
                timestamp=self.t0,
            )
            self.signal_store.add_signal(sig, top_n=2)

        top_cash = self.signal_store.get_top_signals(AssetClass.STOCK_CASH, n=2)
        self.assertEqual(len(top_cash), 2)
        self.assertEqual(top_cash[0].confidence_score, 95.0)
        self.assertEqual(top_cash[1].confidence_score, 85.0)
        print("✓ [b] Top-2 storage: Verified SignalStore retains only top 2 ranked signals per class")

    def test_c_selection_change(self):
        """[c] Selection change: set_selected_asset_class() updates immediately."""
        self.orchestrator.set_selected_asset_class(AssetClass.INDEX_OPTIONS)
        self.assertEqual(self.orchestrator.get_selected_asset_class(), AssetClass.INDEX_OPTIONS)
        self.assertEqual(self.orchestrator.selected_asset_class, AssetClass.INDEX_OPTIONS)

        self.orchestrator.set_selected_asset_class(AssetClass.STOCK_FUTURES)
        self.assertEqual(self.orchestrator.get_selected_asset_class(), AssetClass.STOCK_FUTURES)
        print("✓ [c] Selection change: Category updated atomically and immediately without restart")

    def test_d_cascade_brake(self):
        """[d] Cascade brake: 3+ classes signal in 30s -> cascade_brake_active = True."""
        indicators = self._create_universal_indicator_payload()

        with self.assertLogs(logger="strategies.strategy_orchestrator", level=logging.WARNING) as cm:
            self.orchestrator.evaluate_all(
                indicators,
                {"timestamp": self.t0, "days_to_expiry": 10.0},
            )

        self.assertTrue(self.orchestrator.cascade_brake_active)
        self.assertTrue(self.orchestrator.check_cascade_brake(self.t0))
        self.assertTrue(
            any("Cascade brake triggered:" in msg and "signaled within 30s" in msg for msg in cm.output),
            f"Expected cascade brake log not found in {cm.output}",
        )
        print("✓ [d] Cascade brake: Verified 3+ classes in 30s activated cascade brake and logged warning")

    def test_e_status_reporting(self):
        """[e] Status reporting: get_status() returns correct state."""
        self.orchestrator.set_selected_asset_class(AssetClass.INDEX_FUTURES)
        status = self.orchestrator.get_status()

        self.assertIn("selected_asset_class", status)
        self.assertEqual(status["selected_asset_class"], "INDEX_FUTURES")
        self.assertIn("registered_strategies", status)
        self.assertEqual(len(status["registered_strategies"]), 5)
        self.assertIn("cascade_brake_active", status)
        self.assertIn("signal_store_stats", status)
        print("✓ [e] Status reporting: Verified complete runtime telemetry payload")


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestStrategyOrchestrator)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 5 STRATEGY ORCHESTRATOR TESTS (a-e) PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)
