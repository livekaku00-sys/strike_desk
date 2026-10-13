"""
STRIKE DESK V4 — PHASE 5 PROMPT 3/4
EOD SQUARE-OFF & EMERGENCY HALT TEST SUITE
"""

import unittest
from datetime import datetime, timedelta
import threading
import pytz
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from risk.eod_square_off import (
    EODSquareOffManager,
    EODSquareOffConfig,
    SquareOffStatus,
    SquareOffReason,
    SquareOffOrder,
)
from risk.emergency_halt import (
    EmergencyHaltManager,
    HaltLevel,
    HaltEvent,
)

IST = pytz.timezone("Asia/Kolkata")


class MockPosition:
    def __init__(self, position_id: str, symbol: str, quantity: int):
        self.position_id = position_id
        self.symbol = symbol
        self.quantity = quantity
        self.is_open = True


class MockBrokerRouter:
    def __init__(self, fail_count: int = 0, reject_permanently: bool = False):
        self.fail_count = fail_count
        self.attempts = 0
        self.reject_permanently = reject_permanently
        self.placed_orders = []
        self.canceled_all = False

    def place_exit_order(self, position_id: str, symbol: str, quantity: int, side: str):
        self.attempts += 1
        if self.reject_permanently:
            return {"status": "REJECTED", "reason": "Exchange RMS limit reached"}
        if self.attempts <= self.fail_count:
            raise ConnectionError(f"Broker connection timeout (attempt {self.attempts})")
        self.placed_orders.append({"position_id": position_id, "symbol": symbol, "quantity": quantity, "side": side})
        return {"status": "COMPLETE", "order_id": f"ORD_{self.attempts}"}

    def cancel_all_pending_orders(self):
        self.canceled_all = True

    def cancel_all_orders(self):
        self.canceled_all = True


class TestEODAndEmergencyHalt(unittest.TestCase):

    def setUp(self):
        self.halt_manager = EmergencyHaltManager()
        self.eod_manager = EODSquareOffManager(emergency_halt_manager=self.halt_manager)

    def test_a_eod_cutoff_detection(self):
        """[a] EOD cutoff detection: Exact 15:10 IST cutoff logic"""
        today = datetime.now(IST).date()
        # 14:30 IST -> Not past cutoff
        t_1430 = IST.localize(datetime(today.year, today.month, today.day, 14, 30, 0))
        self.assertFalse(self.eod_manager.is_past_cutoff(t_1430))
        self.assertFalse(self.eod_manager.is_warning_window(t_1430))

        # 15:06 IST -> Warning window (15:05 to 15:10)
        t_1506 = IST.localize(datetime(today.year, today.month, today.day, 15, 6, 0))
        self.assertFalse(self.eod_manager.is_past_cutoff(t_1506))
        self.assertTrue(self.eod_manager.is_warning_window(t_1506))

        # 15:10:00 IST -> Hard cutoff reached
        t_1510 = IST.localize(datetime(today.year, today.month, today.day, 15, 10, 0))
        self.assertTrue(self.eod_manager.is_past_cutoff(t_1510))
        self.assertFalse(self.eod_manager.is_warning_window(t_1510))

        # 15:15:00 IST -> Past cutoff
        t_1515 = IST.localize(datetime(today.year, today.month, today.day, 15, 15, 0))
        self.assertTrue(self.eod_manager.is_past_cutoff(t_1515))
        print("✓ [a] EOD cutoff detection: Exact 15:10 IST cutoff logic (Blueprint O5)")

    def test_b_automated_eod_square_off(self):
        """[b] Automated EOD square-off: Open positions automatically close with opposite orders"""
        positions = [
            MockPosition("POS_1", "BANKNIFTY26SEP50000CE", 50),
            MockPosition("POS_2", "NIFTY26SEP24000PE", -25),
        ]
        router = MockBrokerRouter()
        result = self.eod_manager.execute_square_off(
            positions=positions,
            reason=SquareOffReason.EOD_1510_CUTOFF,
            broker_router=router,
        )

        self.assertEqual(result.status, SquareOffStatus.COMPLETED)
        self.assertEqual(result.closed_positions, 2)
        self.assertEqual(result.failed_positions, 0)
        self.assertEqual(len(router.placed_orders), 2)
        # Long position must exit with SELL
        self.assertEqual(router.placed_orders[0]["side"], "SELL")
        self.assertEqual(router.placed_orders[0]["quantity"], 50)
        # Short position must exit with BUY
        self.assertEqual(router.placed_orders[1]["side"], "BUY")
        self.assertEqual(router.placed_orders[1]["quantity"], 25)
        # Positions must reflect 0 quantity
        self.assertEqual(positions[0].quantity, 0)
        self.assertEqual(positions[1].quantity, 0)
        print("✓ [b] Automated EOD square-off: Open positions automatically close with opposite orders")

    def test_c_failed_square_off_retry(self):
        """[c] Failed square-off retry: Transient broker failures retry up to max retries"""
        positions = [MockPosition("POS_RETRY", "RELIANCE", 100)]
        # Router fails 2 times, succeeds on 3rd attempt
        router = MockBrokerRouter(fail_count=2)
        result = self.eod_manager.execute_square_off(
            positions=positions,
            reason=SquareOffReason.EOD_1510_CUTOFF,
            broker_router=router,
        )
        self.assertEqual(result.status, SquareOffStatus.COMPLETED)
        self.assertEqual(result.closed_positions, 1)
        self.assertEqual(result.orders[0].attempts, 3)
        self.assertEqual(result.orders[0].status, "FILLED")
        print("✓ [c] Failed square-off retry: Transient broker failures retry up to max retries")

    def test_d_failed_square_off_escalation(self):
        """[d] Failed square-off escalation: Unrecoverable failure triggers emergency halt"""
        positions = [MockPosition("POS_FAIL", "INFY", 200)]
        router = MockBrokerRouter(reject_permanently=True)
        result = self.eod_manager.execute_square_off(
            positions=positions,
            reason=SquareOffReason.EOD_1510_CUTOFF,
            broker_router=router,
        )
        self.assertEqual(result.status, SquareOffStatus.FAILED)
        self.assertEqual(result.failed_positions, 1)
        # Emergency halt should be actively engaged
        self.assertTrue(self.halt_manager.is_halted)
        self.assertIn("Failed EOD square-off", self.halt_manager.current_reason)
        print("✓ [d] Failed square-off escalation: Unrecoverable failure triggers emergency halt")

    def test_e_emergency_kill_switch_trigger(self):
        """[e] Emergency kill-switch trigger: Manual trigger engages halt and records state"""
        self.assertFalse(self.halt_manager.is_halted)
        event = self.halt_manager.trigger_halt(
            reason="Operator manual kill-switch",
            level=HaltLevel.HARD_HALT,
            triggered_by="ADMIN_MONUJ",
        )
        self.assertTrue(self.halt_manager.is_halted)
        self.assertEqual(self.halt_manager.current_level, HaltLevel.HARD_HALT)
        self.assertEqual(event.triggered_by, "ADMIN_MONUJ")
        status = self.halt_manager.get_status()
        self.assertTrue(status["is_halted"])
        self.assertTrue(status["pending_acknowledgment"])
        print("✓ [e] Emergency kill-switch trigger: Manual trigger engages halt and records state")

    def test_f_full_kill_switch_square_off(self):
        """[f] Full kill-switch square-off: FULL_KILL_SWITCH cancels orders and forces liquidation"""
        positions = [MockPosition("POS_KILL", "TCS", 40)]
        router = MockBrokerRouter()
        self.halt_manager.trigger_halt(
            reason="Market flash crash detected",
            level=HaltLevel.FULL_KILL_SWITCH,
            triggered_by="RMS_RISK_AGENT",
            eod_manager=self.eod_manager,
            broker_router=router,
            positions=positions,
        )
        self.assertTrue(self.halt_manager.is_halted)
        self.assertTrue(router.canceled_all)
        self.assertEqual(positions[0].quantity, 0)
        print("✓ [f] Full kill-switch square-off: FULL_KILL_SWITCH cancels orders and forces liquidation")

    def test_g_manual_acknowledgment_token(self):
        """[g] Manual acknowledgment token: Requires valid operator token to permit reset"""
        self.halt_manager.trigger_halt("Test halt for token validation")
        
        # Reset without valid token must fail
        with self.assertRaises(PermissionError):
            self.halt_manager.acknowledge_and_reset("OPERATOR_1", "INVALID_TOKEN")

        # Requesting token without operator ID must fail
        with self.assertRaises(ValueError):
            self.halt_manager.request_acknowledgment_token("")

        # Valid token generation
        token = self.halt_manager.request_acknowledgment_token("OPERATOR_1")
        self.assertTrue(token.startswith("ACK-OPERATOR_1-"))
        print("✓ [g] Manual acknowledgment token: Requires valid operator token to permit reset")

    def test_h_acknowledgment_reset_and_resume(self):
        """[h] Acknowledgment reset & resume: Valid token and operator reset halt state"""
        self.halt_manager.trigger_halt("System maintenance halt")
        token = self.halt_manager.request_acknowledgment_token("MONUJ_CRCC")
        
        # Reset with correct credentials and notes
        success = self.halt_manager.acknowledge_and_reset(
            operator_id="MONUJ_CRCC",
            token=token,
            resolution_notes="Risk limits inspected, all balances normal.",
        )
        self.assertTrue(success)
        self.assertFalse(self.halt_manager.is_halted)
        self.assertIsNone(self.halt_manager.current_level)
        
        # Verify event updated
        last_event = self.halt_manager.get_audit_log()[-1]
        self.assertTrue(last_event.acknowledged)
        self.assertEqual(last_event.acknowledged_by, "MONUJ_CRCC")
        self.assertEqual(last_event.resolution_notes, "Risk limits inspected, all balances normal.")
        print("✓ [h] Acknowledgment reset & resume: Valid token and operator reset halt state")

    def test_i_audit_trail_logging(self):
        """[i] Audit trail logging: Retains chronological records of halts and acknowledgments"""
        self.halt_manager.trigger_halt("Incident 1: Network glitch")
        t1 = self.halt_manager.request_acknowledgment_token("OP1")
        self.halt_manager.acknowledge_and_reset("OP1", t1, "Resolved glitch")

        self.halt_manager.trigger_halt("Incident 2: Feed lag")
        t2 = self.halt_manager.request_acknowledgment_token("OP2")
        self.halt_manager.acknowledge_and_reset("OP2", t2, "Switched feed")

        log = self.halt_manager.get_audit_log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0].reason, "Incident 1: Network glitch")
        self.assertEqual(log[1].reason, "Incident 2: Feed lag")
        self.assertTrue(log[0].acknowledged)
        self.assertTrue(log[1].acknowledged)
        print("✓ [i] Audit trail logging: Retains chronological records of halts and acknowledgments")

    def test_j_concurrency_and_thread_safety(self):
        """[j] Concurrency & multi-trigger: Thread-safe handling under concurrent halt requests"""
        errors = []

        def worker(thread_id: int):
            try:
                self.halt_manager.trigger_halt(f"Concurrent halt from thread {thread_id}")
                self.eod_manager.is_past_cutoff()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertTrue(self.halt_manager.is_halted)
        print("✓ [j] Concurrency & multi-trigger: Thread-safe handling under concurrent halt requests")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 5 PROMPT 3/4 EOD SQUARE-OFF & EMERGENCY HALT TEST SUITE")
    print("=" * 70)
    unittest.main(verbosity=0)
