"""
Strike Desk V4 — Phase 1 Regression Suite: Signal Class Lock & Cooldown Enforcement

Tests:
  [a] Class mismatch: evaluate_all(locked=STOCK_FUTURES) drops INDEX_OPTIONS signal
  [b] Class match:    evaluate_all(locked=INDEX_OPTIONS) passes INDEX_OPTIONS signal
  [c] Full isolation: only the locked class has signals in the store
  [d] Cooldown:       second identical signal within window is suppressed with state=DROPPED_DUPLICATE
  [e] Cooldown log:   suppressed signal logs at INFO with "COOLDOWN" keyword
  [f] Active state:   first stored signal has state=ACTIVE
  [g] Mismatch log:   "Dropped: Class mismatch" appears in log
  [h] SignalState enum: all expected values exist
"""
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from execution.audit_logger import AuditLogger
from models.instrument import AssetClass, Exchange
from models.signal import (
    ConfidenceTier,
    Direction,
    Signal,
    SignalState,
    SignalType,
)
from pipeline.indicators.pcr_staleness_monitor import pcr_staleness_monitor
from strategies.base_strategy import BaseStrategy
from strategies.index_options_strategy import IndexOptionsStrategy
from strategies.signal_store import SignalStore
from strategies.strategy_orchestrator import StrategyOrchestrator
from tests.option_chain_fixture import seed_many

UTC = timezone.utc

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_full_indicators() -> dict:
    """Favorable indicator payload for all 5 asset classes (mirrors orchestrator tests)."""
    return {
        AssetClass.STOCK_CASH: {
            "symbol": "TCS",
            "price": 3520.0, "prev_price": 3500.0,
            "ema9": 3510.0, "prev_ema9": 3510.0,
            "vwap": 3490.0, "volume": 25000.0, "avg_volume": 10000.0,
            "pcr_slope": -0.05, "sentiment": 25.0,
        },
        AssetClass.STOCK_FUTURES: {
            "symbol": "TCS_FUT",
            "price": 3530.0, "prev_price": 3510.0,
            "ema9": 3515.0, "prev_ema9": 3515.0,
            "vwap": 3500.0, "spot_price": 3520.0,
            "oi_change_pct": 4.0, "sentiment": 20.0,
        },
        AssetClass.INDEX_FUTURES: {
            "symbol": "NIFTY_FUT",
            "price": 24520.0, "prev_price": 24490.0,
            "ema9": 24500.0, "prev_ema9": 24500.0,
            "vwap": 24480.0, "pcr_slope": -0.05, "sentiment": 20.0,
        },
        AssetClass.INDEX_OPTIONS: {
            "symbol": "NIFTY",
            "underlying": "NIFTY",
            "price": 24520.0, "prev_price": 24490.0,
            "ema9": 24500.0, "prev_ema9": 24480.0,
            "vwap": 24480.0, "pcr_slope": -0.12,
            "oi_change": 5.0, "sentiment": 30.0,
        },
        AssetClass.STOCK_OPTIONS: {
            "symbol": "RELIANCE",
            "underlying": "RELIANCE",
            "price": 2520.0, "prev_price": 2500.0,
            "ema9": 2510.0, "prev_ema9": 2510.0,
            "vwap": 2500.0, "pcr_slope": -0.10,
            "oi_change": 3.0, "sentiment": 25.0,
        },
    }


def _make_orchestrator() -> StrategyOrchestrator:
    """Fresh StrategyOrchestrator with isolated store and logger."""
    pcr_staleness_monitor.clear()
    seed_many({"NIFTY": 24520.0, "RELIANCE": 2520.0})
    store = SignalStore()
    al = AuditLogger()
    orch = StrategyOrchestrator(signal_store_instance=store, audit_logger_instance=al)
    orch.reset()
    return orch


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestSignalClassLock(unittest.TestCase):

    def setUp(self):
        self.orch = _make_orchestrator()
        self.indicators = _make_full_indicators()
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

    # [a] Non-locked class signals must not reach the signal store
    def test_a_class_mismatch_drops_index_options(self):
        """evaluate_all locked to STOCK_FUTURES must produce no INDEX_OPTIONS signals."""
        self.orch.evaluate_all(
            self.indicators,
            market_state={"timestamp": self.t0},
            locked_class=AssetClass.STOCK_FUTURES,
        )
        idx_opt_signals = self.orch.signal_store.get_top_signals(AssetClass.INDEX_OPTIONS)
        self.assertEqual(
            len(idx_opt_signals), 0,
            f"Expected 0 INDEX_OPTIONS signals when locked to STOCK_FUTURES, "
            f"got {len(idx_opt_signals)}: {[s.symbol for s in idx_opt_signals]}"
        )

    # [b] Locked class signals MUST reach the signal store
    def test_b_matching_class_passes(self):
        """evaluate_all locked to INDEX_OPTIONS must allow INDEX_OPTIONS signals."""
        self.orch.evaluate_all(
            self.indicators,
            market_state={"timestamp": self.t0},
            locked_class=AssetClass.INDEX_OPTIONS,
        )
        idx_opt_signals = self.orch.signal_store.get_top_signals(AssetClass.INDEX_OPTIONS)
        self.assertGreater(
            len(idx_opt_signals), 0,
            "Expected ≥1 INDEX_OPTIONS signal when locked to INDEX_OPTIONS with "
            "fully favorable indicators — got none. Check strategy thresholds."
        )

    # [c] Full isolation: only locked class has signals
    def test_c_only_locked_class_has_signals(self):
        """All non-locked asset classes must have zero signals in the store."""
        locked = AssetClass.INDEX_OPTIONS
        self.orch.evaluate_all(
            self.indicators,
            market_state={"timestamp": self.t0},
            locked_class=locked,
        )
        for ac in AssetClass:
            signals = self.orch.signal_store.get_top_signals(ac)
            if ac == locked:
                continue  # allowed to have signals
            self.assertEqual(
                len(signals), 0,
                f"Asset class {ac.value} must have 0 signals when locked to "
                f"{locked.value}, but got {len(signals)}"
            )

    # [d] Cooldown — evaluate() returns None for duplicate signals within window
    def test_d_cooldown_suppresses_duplicate(self):
        """Second identical signal within the duplicate window returns None from evaluate()."""
        strategy = IndexOptionsStrategy()
        ind = self.indicators[AssetClass.INDEX_OPTIONS]

        # First signal — must be stored (ACTIVE)
        sig1 = strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig1, "First evaluation should produce a signal")
        self.assertEqual(sig1.state, SignalState.ACTIVE,
                         f"First signal should be ACTIVE, got {sig1.state}")

        # Second signal within 60s window — duplicate, evaluate() returns None
        sig2 = strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=10)})
        self.assertIsNone(sig2,
            "evaluate() must return None for a duplicate signal within the cooldown window")

    # [e] Cooldown must log at INFO with "COOLDOWN" keyword
    def test_e_cooldown_logs_at_info(self):
        """Suppressed duplicate signal must emit an INFO log containing 'COOLDOWN'."""
        strategy = IndexOptionsStrategy()
        ind = self.indicators[AssetClass.INDEX_OPTIONS]

        with self.assertLogs("strategies.base_strategy", level="INFO") as cm:
            strategy.evaluate(ind, {"timestamp": self.t0})
            strategy.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=5)})

        cooldown_logs = [line for line in cm.output if "COOLDOWN" in line]
        self.assertGreater(
            len(cooldown_logs), 0,
            f"Expected at least one INFO log containing 'COOLDOWN'. Got:\n" +
            "\n".join(cm.output)
        )

    # [f] First stored signal must be ACTIVE
    def test_f_first_stored_signal_is_active(self):
        """First signal from a strategy (not in cooldown) must have state=ACTIVE."""
        strategy = IndexOptionsStrategy()
        ind = self.indicators[AssetClass.INDEX_OPTIONS]
        sig = strategy.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.state, SignalState.ACTIVE,
                         f"Expected ACTIVE, got {sig.state}")

    # [g] Class mismatch must emit "[Signal] Dropped: Class mismatch" in log
    def test_g_mismatch_drops_with_log(self):
        """evaluate_all with locked_class mismatch must log 'Dropped: Class mismatch'."""
        with self.assertLogs("strategies.strategy_orchestrator", level="INFO") as cm:
            self.orch.evaluate_all(
                self.indicators,
                market_state={"timestamp": self.t0},
                locked_class=AssetClass.STOCK_FUTURES,
            )
        mismatch_logs = [line for line in cm.output if "Class mismatch" in line]
        self.assertGreater(
            len(mismatch_logs), 0,
            "Expected at least one log line containing 'Class mismatch'. "
            "Got:\n" + "\n".join(cm.output)
        )
        # Should have 4 mismatch entries (one per non-locked class)
        self.assertEqual(
            len(mismatch_logs), 4,
            f"Expected 4 class-mismatch drops (4 non-locked classes), got {len(mismatch_logs)}"
        )

    # [h] SignalState enum completeness
    def test_h_signal_state_enum_values(self):
        """SignalState must expose all documented states."""
        expected = {
            "GENERATED", "ACTIVE", "COOLDOWN", "EXPIRED",
            "DROPPED_CLASS_MISMATCH", "DROPPED_DUPLICATE",
            "DROPPED_RISK_REJECTION",
        }
        actual = {s.value for s in SignalState}
        self.assertEqual(actual, expected,
                         f"SignalState values mismatch.\nExpected: {expected}\nActual: {actual}")


class TestSignalStateField(unittest.TestCase):
    """Signal.state is persisted on the object and emitted by to_dict()."""

    def _make_signal(self, state=None) -> Signal:
        sig = Signal(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            asset_class=AssetClass.INDEX_OPTIONS,
            signal_type=SignalType.PCR_SLOPE_BULLISH,
            direction=Direction.LONG,
            entry_price=24500.0,
            confidence_score=80.0,
            confidence_tier=ConfidenceTier.A,
        )
        if state is not None:
            sig.state = state
        return sig

    def test_default_state_is_generated(self):
        sig = self._make_signal()
        self.assertEqual(sig.state, SignalState.GENERATED)

    def test_state_in_to_dict(self):
        sig = self._make_signal(SignalState.ACTIVE)
        d = sig.to_dict()
        self.assertIn("state", d)
        self.assertEqual(d["state"], "ACTIVE")

    def test_state_assignment(self):
        sig = self._make_signal()
        sig.state = SignalState.DROPPED_CLASS_MISMATCH
        self.assertEqual(sig.state, SignalState.DROPPED_CLASS_MISMATCH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
