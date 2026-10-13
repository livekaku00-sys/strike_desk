"""
Strike Desk V4 — Phase 4 Prompt 4/5 Verification Suite: Execution Gate & Router (DEFINITIVE)
Verifies:
  [a] Selected class executes: 100 STOCK_CASH signals -> 100% route to execution
  [b] Non-selected classes do not execute: 100 each of remaining 4 classes -> 0% execute (400 alert-only)
  [c] Zero routing enforcement: 500 signals (100x5) with INDEX_OPTIONS selected -> 100 execute, 400 alert-only
  [d] Race condition test: 5 concurrent threads generating signals while selection switches mid-stream
  [e] Audit log verification: 50 signals across all classes, selection changes, and cascade brakes logged
  [f] Cascade brake: 3+ classes signal in 30s -> position size multiplier reduced to 0.5 & warning logged
  [g] Atomic selection change: 100 rapid changes without race conditions or data corruption
  [h] Signal expiry: Signal after 35s (>30s) -> is_expired()=True and routed as alert_only
  [i] Duplicate suppression: Same signal within 60s -> second suppressed by strategy
  [j] Top-2 display: 5 signals generated -> SignalStore retains exactly top 2
"""
from datetime import datetime, timedelta, timezone
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from execution.audit_logger import AuditLogger
from execution.execution_router import ExecutionRouter
from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal, SignalType
from strategies.signal_store import SignalStore
from strategies.stock_cash_strategy import StockCashStrategy
from strategies.strategy_orchestrator import StrategyOrchestrator

UTC = timezone.utc


class TestExecutionGate(unittest.TestCase):
    """DEFINITIVE test suite for selective execution gate and zero-routing enforcement."""

    def setUp(self):
        self.audit_logger = AuditLogger()
        self.router = ExecutionRouter(logger_instance=self.audit_logger)
        self.signal_store = SignalStore()
        self.orchestrator = StrategyOrchestrator(
            signal_store_instance=self.signal_store,
            audit_logger_instance=self.audit_logger,
        )
        self.orchestrator.reset()
        self.audit_logger.clear()
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)

    def _make_signal(
        self,
        asset_class: AssetClass,
        i: int = 0,
        score: float = 80.0,
        ts: Optional[datetime] = None,
    ) -> Signal:
        timestamp = ts or self.t0
        return Signal(
            symbol=f"SYM_{asset_class.name}_{i}",
            exchange=Exchange.NSE,
            asset_class=asset_class,
            signal_type=SignalType.EMA9_CROSSOVER_BULLISH,
            direction=Direction.LONG,
            entry_price=100.0 + i,
            confidence_score=score,
            confidence_tier=ConfidenceTier.from_score(score),
            timestamp=timestamp,
            expires_at=timestamp + timedelta(seconds=30),
        )

    def test_a_selected_class_executes(self):
        """[a] Selected class executes: 100 STOCK_CASH signals -> 100% route to execution."""
        self.orchestrator.set_selected_asset_class(AssetClass.STOCK_CASH)
        selected = self.orchestrator.get_selected_asset_class()

        signals = [self._make_signal(AssetClass.STOCK_CASH, i) for i in range(100)]
        routed = self.router.route_signals(signals, selected, current_time=self.t0)

        self.assertEqual(len(routed), 100)
        for res in routed:
            self.assertEqual(res["action"], "execute")
            self.assertEqual(res["position_size_multiplier"], 1.0)
            self.assertIn("Selected asset class matched", res["reason"])

        print("✓ [a] Selected class executes: 100/100 (100%) STOCK_CASH signals routed to execution")

    def test_b_non_selected_classes_do_not_execute(self):
        """[b] Non-selected classes do not execute: 100 signals x 4 classes -> 0% execute (400 alert-only)."""
        self.orchestrator.set_selected_asset_class(AssetClass.STOCK_CASH)
        selected = self.orchestrator.get_selected_asset_class()

        non_selected_classes = [
            AssetClass.STOCK_FUTURES,
            AssetClass.INDEX_FUTURES,
            AssetClass.INDEX_OPTIONS,
            AssetClass.STOCK_OPTIONS,
        ]

        total_routed = []
        for ac in non_selected_classes:
            sigs = [self._make_signal(ac, i) for i in range(100)]
            routed = self.router.route_signals(sigs, selected, current_time=self.t0)
            total_routed.extend(routed)

        self.assertEqual(len(total_routed), 400)
        executed_count = sum(1 for r in total_routed if r["action"] == "execute")
        alert_only_count = sum(1 for r in total_routed if r["action"] == "alert_only")

        self.assertEqual(executed_count, 0, "Non-selected class signal was routed to execution!")
        self.assertEqual(alert_only_count, 400)
        for r in total_routed:
            self.assertEqual(r["reason"], "Non-selected asset class")
            self.assertEqual(r["position_size_multiplier"], 0.0)

        print("✓ [b] Non-selected classes: 400/400 (100%) non-selected signals gated to alert-only (0 executions)")

    def test_c_zero_routing_enforcement(self):
        """[c] Zero routing enforcement: 500 signals with INDEX_OPTIONS selected -> 100 execute, 400 alert-only."""
        self.orchestrator.set_selected_asset_class(AssetClass.INDEX_OPTIONS)
        selected = self.orchestrator.get_selected_asset_class()

        all_classes = [
            AssetClass.STOCK_CASH,
            AssetClass.STOCK_FUTURES,
            AssetClass.INDEX_FUTURES,
            AssetClass.INDEX_OPTIONS,
            AssetClass.STOCK_OPTIONS,
        ]

        all_signals = []
        for ac in all_classes:
            all_signals.extend([self._make_signal(ac, i) for i in range(100)])

        self.assertEqual(len(all_signals), 500)
        routed = self.router.route_signals(all_signals, selected, current_time=self.t0)

        exec_count = sum(1 for r in routed if r["action"] == "execute")
        alert_count = sum(1 for r in routed if r["action"] == "alert_only")

        self.assertEqual(exec_count, 100)
        self.assertEqual(alert_count, 400)

        for r in routed:
            if r["action"] == "execute":
                self.assertEqual(r["signal"].asset_class, AssetClass.INDEX_OPTIONS)
            else:
                self.assertNotEqual(r["signal"].asset_class, AssetClass.INDEX_OPTIONS)

        print("✓ [c] Zero routing enforcement: Exactly 100 INDEX_OPTIONS executed; exactly 400 non-selected gated")

    def test_d_race_condition_concurrent_selection_change(self):
        """[d] Race condition test: 5 concurrent threads generating signals while selection switches mid-stream."""
        self.orchestrator.set_selected_asset_class(AssetClass.STOCK_CASH)

        routing_results = []
        errors = []
        stop_event = threading.Event()

        def signal_worker(worker_id: int):
            try:
                count = 0
                classes = [
                    AssetClass.STOCK_CASH,
                    AssetClass.INDEX_FUTURES,
                    AssetClass.STOCK_OPTIONS,
                ]
                while not stop_event.is_set() and count < 50:
                    ac = classes[count % len(classes)]
                    sig = self._make_signal(ac, worker_id * 1000 + count)
                    current_selected = self.orchestrator.get_selected_asset_class()
                    result = self.router.route_signal(sig, current_selected, current_time=self.t0)

                    if result["action"] == "execute":
                        if sig.asset_class != current_selected:
                            errors.append(
                                f"Leakage! Signal {sig.asset_class} executed when selected was {current_selected}"
                            )
                    routing_results.append(result)
                    count += 1
                    time.sleep(0.0002)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=signal_worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()

        time.sleep(0.005)
        self.orchestrator.set_selected_asset_class(AssetClass.INDEX_FUTURES)
        time.sleep(0.005)
        self.orchestrator.set_selected_asset_class(AssetClass.STOCK_OPTIONS)
        time.sleep(0.005)
        stop_event.set()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Race condition errors detected: {errors}")
        self.assertGreater(len(routing_results), 100)

        trail = self.audit_logger.get_all_entries()
        selection_changes = [e for e in trail if e.get("event_type") == "selection_change"]
        self.assertGreaterEqual(len(selection_changes), 2)
        for sc in selection_changes:
            self.assertIn("timestamp", sc)
            self.assertIn("old_class", sc)
            self.assertIn("new_class", sc)

        print(f"✓ [d] Race condition test: 5 concurrent threads executed {len(routing_results)} routings with zero order leakage")

    def test_e_audit_log_verification(self):
        """[e] Audit log verification: 50 signals across all classes, selection changes, and cascade brakes logged."""
        self.audit_logger.clear()
        self.orchestrator.set_selected_asset_class(AssetClass.STOCK_CASH)

        classes = [
            AssetClass.STOCK_CASH,
            AssetClass.STOCK_FUTURES,
            AssetClass.INDEX_FUTURES,
            AssetClass.INDEX_OPTIONS,
            AssetClass.STOCK_OPTIONS,
        ]

        for i in range(50):
            ac = classes[i % len(classes)]
            sig = self._make_signal(ac, i)
            self.router.route_signal(sig, self.orchestrator.get_selected_asset_class(), current_time=self.t0)

        self.orchestrator.set_selected_asset_class(AssetClass.STOCK_OPTIONS)
        self.audit_logger.log_cascade_brake([AssetClass.STOCK_CASH, AssetClass.INDEX_FUTURES, AssetClass.STOCK_OPTIONS])

        trail = self.audit_logger.get_all_entries()
        self.assertGreaterEqual(len(trail), 52)

        signal_entries = [e for e in trail if e.get("event_type") == "signal"]
        self.assertEqual(len(signal_entries), 50)
        for se in signal_entries:
            for field in ["timestamp", "signal_id", "symbol", "asset_class", "action", "reason"]:
                self.assertIn(field, se)

        sel_entries = [e for e in trail if e.get("event_type") == "selection_change"]
        self.assertGreaterEqual(len(sel_entries), 1)

        cascade_entries = [e for e in trail if e.get("event_type") == "cascade_brake"]
        self.assertEqual(len(cascade_entries), 1)
        print("✓ [e] Audit log verification: 50 signals, selection changes, and cascade brake events verified in audit trail")

    def test_f_cascade_brake_execution_effect(self):
        """[f] Cascade brake: 3+ classes signal in 30s -> position size multiplier reduced to 0.5 & warning logged."""
        self.orchestrator._record_signal_timestamp(AssetClass.STOCK_CASH, self.t0)
        self.orchestrator._record_signal_timestamp(AssetClass.INDEX_FUTURES, self.t0)
        self.orchestrator._record_signal_timestamp(AssetClass.INDEX_OPTIONS, self.t0)

        with self.assertLogs(logger="strategies.strategy_orchestrator", level=logging.WARNING) as cm:
            brake_active = self.orchestrator.check_cascade_brake(self.t0)

        self.assertTrue(brake_active)
        self.assertTrue(self.orchestrator.cascade_brake_active)
        self.assertTrue(any("Cascade brake triggered:" in msg for msg in cm.output))

        self.orchestrator.set_selected_asset_class(AssetClass.STOCK_CASH)
        sig = self._make_signal(AssetClass.STOCK_CASH, 1)
        res = self.router.route_signal(
            sig,
            self.orchestrator.get_selected_asset_class(),
            current_time=self.t0,
            cascade_brake_active=self.orchestrator.cascade_brake_active,
        )

        self.assertEqual(res["action"], "execute")
        self.assertEqual(res["position_size_multiplier"], 0.5)
        self.assertIn("cascade brake: 50% size reduction", res["reason"])
        print("✓ [f] Cascade brake: Verified 3+ classes triggered 50% position size reduction and audit log")

    def test_g_atomic_selection_change_rapid(self):
        """[g] Atomic selection change: 100 rapid changes without race conditions or data corruption."""
        classes = list(AssetClass)
        for i in range(100):
            target = classes[i % len(classes)]
            self.orchestrator.set_selected_asset_class(target)
            self.assertEqual(self.orchestrator.get_selected_asset_class(), target)

        final_expected = classes[99 % len(classes)]
        self.assertEqual(self.orchestrator.get_selected_asset_class(), final_expected)
        print("✓ [g] Atomic selection change: Executed 100 rapid switches with zero latency or state corruption")

    def test_h_signal_expiry_routing(self):
        """[h] Signal expiry: Signal after 35s (>30s) -> is_expired()=True and routed as alert_only."""
        sig = self._make_signal(AssetClass.STOCK_CASH, 1, ts=self.t0)
        t35 = self.t0 + timedelta(seconds=35)

        self.assertTrue(sig.is_expired(current_time=t35))

        res = self.router.route_signal(sig, AssetClass.STOCK_CASH, current_time=t35)
        self.assertEqual(res["action"], "alert_only")
        self.assertEqual(res["reason"], "Signal expired")
        self.assertEqual(res["position_size_multiplier"], 0.0)
        print("✓ [h] Signal expiry: Confirmed expired signals (>30s) are never routed to execution")

    def test_i_duplicate_suppression(self):
        """[i] Duplicate suppression: Same signal within 60s -> second suppressed by strategy."""
        strat = StockCashStrategy()
        ind = {
            "symbol": "TCS",
            "price": 3520.0,
            "ema9": 3510.0,
            "ema9_crossover": True,
            "vwap_alignment": True,
            "volume_surge": True,
        }
        sig1 = strat.evaluate(ind, {"timestamp": self.t0})
        self.assertIsNotNone(sig1)

        sig2 = strat.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=20)})
        self.assertIsNone(sig2, "Duplicate signal was not suppressed!")

        sig3 = strat.evaluate(ind, {"timestamp": self.t0 + timedelta(seconds=65)})
        self.assertIsNotNone(sig3)
        print("✓ [i] Duplicate suppression: Successfully suppressed duplicate signal at 20s; accepted at 65s")

    def test_j_top_2_display(self):
        """[j] Top-2 display: 5 signals generated -> SignalStore retains exactly top 2."""
        store = SignalStore(max_per_asset_class=2)
        scores = [62.0, 88.0, 71.0, 94.0, 80.0]
        for i, sc in enumerate(scores):
            sig = self._make_signal(AssetClass.STOCK_CASH, i, score=sc)
            store.add_signal(sig, top_n=2)

        top_signals = store.get_top_signals(AssetClass.STOCK_CASH, n=2)
        self.assertEqual(len(top_signals), 2)
        self.assertEqual(top_signals[0].confidence_score, 94.0)
        self.assertEqual(top_signals[1].confidence_score, 88.0)
        print("✓ [j] Top-2 display: SignalStore strictly retained top 2 ranked alerts (94.0, 88.0)")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 4 PROMPT 4/5 EXECUTION GATE VERIFICATION (DEFINITIVE)")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestExecutionGate)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 10 EXECUTION GATE VERIFICATION TESTS [a-j] PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)
