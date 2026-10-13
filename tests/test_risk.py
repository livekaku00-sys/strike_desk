"""
======================================================================
STRIKE DESK V4 — PHASE 5 FINAL VERIFICATION TEST SUITE (test_risk.py)
Blueprint P5: Enforces hard limits, cooldowns, and EOD square-off
Review P5: Limit boundaries, partial fills, concurrent trades,
           cooldown persistence, square-off failures, emergency halt
======================================================================
"""

import unittest
from datetime import datetime, timedelta
import threading
import pytz
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from risk.risk_manager import (
    RiskManager,
    RiskLimitsConfig,
    OrderAuthResult,
)
from risk.eod_square_off import (
    SquareOffStatus,
    SquareOffReason,
)
from risk.emergency_halt import (
    HaltLevel,
)

IST = pytz.timezone("Asia/Kolkata")


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


class TestPhase5RiskManager(unittest.TestCase):

    def setUp(self):
        self.config = RiskLimitsConfig(
            daily_loss_ceiling=-2500.0,
            max_daily_trades=5,
            consecutive_loss_threshold=2,
            cooldown_duration_minutes=45,
            stagnation_ceiling_minutes=15.0,
            eod_cutoff_ist="15:10:00",
            max_square_off_retries=3,
        )
        self.rm = RiskManager(config=self.config)

    def test_01_limit_boundaries_loss_ceiling(self):
        """Review P5: Exact boundary verification for daily loss ceiling (₹-2,500)"""
        # Baseline: P&L 0 -> Allowed
        res = self.rm.authorize_order("BANKNIFTY", 25, "BUY")
        self.assertTrue(res.authorized)

        # Realized P&L at -₹2,499.00 (just above ceiling) -> Allowed
        self.rm.realized_pnl = -2499.0
        res = self.rm.authorize_order("BANKNIFTY", 25, "BUY")
        self.assertTrue(res.authorized)

        # Realized P&L at -₹2,500.00 (exact ceiling boundary) -> REJECTED
        self.rm.realized_pnl = -2500.0
        res = self.rm.authorize_order("BANKNIFTY", 25, "BUY")
        self.assertFalse(res.authorized)
        self.assertIn("Daily loss ceiling breached", res.reason)

        # Realized P&L at -₹2,550.00 -> REJECTED
        self.rm.realized_pnl = -2550.0
        res = self.rm.authorize_order("BANKNIFTY", 25, "BUY")
        self.assertFalse(res.authorized)
        print("✓ [01] Limit boundaries: Daily loss ceiling hard boundary (-₹2,500) strictly enforced")

    def test_02_limit_boundaries_trade_cap(self):
        """Review P5: Exact boundary verification for daily trade cap (5 trades)"""
        for i in range(4):
            self.rm.trade_count = i
            res = self.rm.authorize_order("NIFTY", 50, "BUY")
            self.assertTrue(res.authorized)

        # 5th trade allowed
        self.rm.trade_count = 4
        res = self.rm.authorize_order("NIFTY", 50, "BUY")
        self.assertTrue(res.authorized)

        # 6th trade REJECTED
        self.rm.trade_count = 5
        res = self.rm.authorize_order("NIFTY", 50, "BUY")
        self.assertFalse(res.authorized)
        self.assertIn("Daily trade cap reached: 5/5 trades", res.reason)
        print("✓ [02] Limit boundaries: Daily trade cap boundary (5 trades) strictly enforced")

    def test_03_partial_fills_and_position_aggregation(self):
        """Review P5: Multi-tranche partial fills and weighted average price calculation"""
        # Tranche 1: 40 shares @ 2500
        self.rm.record_fill("ORD_1", "RELIANCE", filled_qty=40, fill_price=2500.0, is_complete=False)
        pos = self.rm.positions["RELIANCE"]
        self.assertEqual(pos.quantity, 40)
        self.assertEqual(pos.entry_price, 2500.0)
        self.assertEqual(self.rm.trade_count, 0)

        # Tranche 2: 60 shares @ 2550 (Weighted Avg Price: (40*2500 + 60*2550)/100 = 2530.0)
        self.rm.record_fill("ORD_1", "RELIANCE", filled_qty=60, fill_price=2550.0, is_complete=True)
        self.assertEqual(pos.quantity, 100)
        self.assertEqual(pos.entry_price, 2530.0)
        self.assertEqual(self.rm.trade_count, 1)

        # Tick price update to 2560 -> Unrealized P&L = 100 * (2560 - 2530) = +₹3,000.00
        self.rm.update_tick("RELIANCE", 2560.0)
        self.assertEqual(self.rm.get_total_risk_pnl(), 3000.0)
        print("✓ [03] Partial fills: Multi-fill aggregation and weighted entry pricing validated")

    def test_04_cooldown_persistence_and_reset(self):
        """Review P5: Consecutive loss cooldown persistence (45m) and win reset"""
        now = IST.localize(datetime(2026, 9, 21, 10, 0, 0))
        self.rm.current_ist_date = now.date()

        # Trade 1: Loss -₹500 -> 1 loss, no cooldown
        self.rm.record_fill("O1", "TCS", 10, 3500.0, True, current_time=now)
        self.rm.close_trade("TCS", 3450.0, current_time=now)
        self.assertEqual(self.rm.consecutive_losses, 1)
        self.assertFalse(self.rm.is_cooldown_active(now))

        # Trade 2: Loss -₹500 -> 2 consecutive losses -> 45 min cooldown triggered
        self.rm.record_fill("O2", "INFY", 10, 1500.0, True, current_time=now + timedelta(minutes=5))
        self.rm.close_trade("INFY", 1450.0, current_time=now + timedelta(minutes=5))
        self.assertEqual(self.rm.consecutive_losses, 2)
        self.assertTrue(self.rm.is_cooldown_active(now + timedelta(minutes=10)))

        # Orders during cooldown rejected
        res = self.rm.authorize_order("WIPRO", 10, "BUY", current_time=now + timedelta(minutes=20))
        self.assertFalse(res.authorized)
        self.assertIn("Consecutive loss cooldown active", res.reason)

        # After 46 minutes: Cooldown expires
        self.assertFalse(self.rm.is_cooldown_active(now + timedelta(minutes=55)))
        res = self.rm.authorize_order("WIPRO", 10, "BUY", current_time=now + timedelta(minutes=55))
        self.assertTrue(res.authorized)

        # Winning trade resets consecutive losses and clears cooldown immediately
        self.rm.consecutive_losses = 2
        self.rm.cooldown_until = now + timedelta(minutes=100)
        self.rm.record_fill("O3", "WIPRO", 10, 500.0, True, current_time=now)
        self.rm.close_trade("WIPRO", 520.0, current_time=now)
        self.assertEqual(self.rm.consecutive_losses, 0)
        self.assertFalse(self.rm.is_cooldown_active(now))
        print("✓ [04] Cooldown persistence: 45m timer persists across state queries and resets on win")

    def test_05_stagnation_monitoring_15min(self):
        """Blueprint O5: 15-minute stagnation ceiling exit identification"""
        t_entry = IST.localize(datetime(2026, 9, 21, 10, 0, 0))
        self.rm.record_fill("O1", "BANKNIFTY", 25, 450.0, True, current_time=t_entry)

        # 14 minutes -> Not stagnant
        stagnant = self.rm.check_stagnant_positions(current_time=t_entry + timedelta(minutes=14))
        self.assertEqual(len(stagnant), 0)

        # 15.5 minutes -> Alert triggered
        stagnant = self.rm.check_stagnant_positions(current_time=t_entry + timedelta(minutes=15, seconds=30))
        self.assertIn("BANKNIFTY", stagnant)
        print("✓ [05] Stagnation monitoring: 15-minute position holding ceiling verified (Blueprint O5)")

    def test_06_eod_1510_cutoff_and_automated_square_off(self):
        """Blueprint O5: 15:10 IST hard square-off liquidation and order cutoff"""
        t_pre = IST.localize(datetime(2026, 9, 21, 15, 9, 0))
        t_post = IST.localize(datetime(2026, 9, 21, 15, 10, 1))
        self.rm.current_ist_date = t_pre.date()

        self.rm.record_fill("O1", "NIFTY_CE", 50, 120.0, True, current_time=t_pre)
        self.rm.record_fill("O2", "NIFTY_PE", -50, 80.0, True, current_time=t_pre)

        # Pre-cutoff allowed
        res = self.rm.authorize_order("HDFC", 10, "BUY", current_time=t_pre)
        self.assertTrue(res.authorized)

        # Post-cutoff rejected
        res = self.rm.authorize_order("HDFC", 10, "BUY", current_time=t_post)
        self.assertFalse(res.authorized)
        self.assertIn("Past 15:10 IST cutoff", res.reason)

        # Automated square-off execution
        router = MockBrokerRouter()
        sq_result = self.rm.run_eod_square_off(broker_router=router, current_time=t_post)
        self.assertIsNotNone(sq_result)
        self.assertEqual(sq_result.status, SquareOffStatus.COMPLETED)
        self.assertEqual(sq_result.closed_positions, 2)
        self.assertEqual(len(router.placed_orders), 2)
        print("✓ [06] EOD hard cutoff: 15:10 IST order gating and automated square-off confirmed")

    def test_07_square_off_failures_and_emergency_escalation(self):
        """Review P5: Unrecoverable square-off failure retries and escalates to Emergency Halt"""
        now = IST.localize(datetime(2026, 9, 21, 15, 10, 5))
        self.rm.record_fill("O1", "ERROR_SYM", 100, 50.0, True, current_time=now)
        
        router = MockBrokerRouter(reject_permanently=True)
        sq_result = self.rm.run_eod_square_off(broker_router=router, current_time=now)

        self.assertEqual(sq_result.status, SquareOffStatus.FAILED)
        self.assertTrue(self.rm.halt_manager.is_halted)
        self.assertIn("Failed EOD square-off", self.rm.halt_manager.current_reason)
        
        # New orders blocked by emergency halt
        res = self.rm.authorize_order("NEW_SYM", 10, "BUY", current_time=now)
        self.assertFalse(res.authorized)
        self.assertIn("Emergency halt active", res.reason)
        print("✓ [07] Square-off failures: Retry exhaustion escalates to Emergency Halt kill-switch")

    def test_08_emergency_halt_and_acknowledgment_workflow(self):
        """Review P5: Two-step cryptographic manual acknowledgment workflow"""
        self.rm.halt_manager.trigger_halt("Operator manual intervention", level=HaltLevel.HARD_HALT)
        self.assertTrue(self.rm.halt_manager.is_halted)

        # Invalid token fails
        with self.assertRaises(PermissionError):
            self.rm.halt_manager.acknowledge_and_reset("OPERATOR_1", "INVALID_TOKEN")

        # Request and submit valid token
        token = self.rm.halt_manager.request_acknowledgment_token("MONUJ_ADMIN")
        self.assertTrue(token.startswith("ACK-MONUJ_ADMIN-"))

        success = self.rm.halt_manager.acknowledge_and_reset(
            operator_id="MONUJ_ADMIN",
            token=token,
            resolution_notes="Risk limits inspected, all broker sockets reconnected.",
        )
        self.assertTrue(success)
        self.assertFalse(self.rm.halt_manager.is_halted)
        print("✓ [08] Emergency halt: Manual acknowledgment token validation and reset verified")

    def test_09_concurrency_and_thread_safety(self):
        """Review P5: Multi-threaded tick updates and concurrent order authorizations"""
        errors = []

        def worker(idx: int):
            try:
                for j in range(20):
                    self.rm.authorize_order(f"SYM_{idx}", 10, "BUY")
                    self.rm.update_tick(f"SYM_{idx}", 100.0 + j)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        summary = self.rm.get_risk_summary()
        self.assertIsInstance(summary, dict)
        print("✓ [09] Concurrency safety: Multi-threaded tick streams and order authorizations stable")

    def test_10_full_simulated_trade_sequence(self):
        """Blueprint P5: Complete intraday trade sequence with limits, cooldowns, and EOD exit"""
        base_time = IST.localize(datetime(2026, 9, 21, 9, 30, 0))

        # Trade 1: 09:30 IST - Win +₹1,200 (Trade count: 1)
        res = self.rm.authorize_order("BANKNIFTY", 25, "BUY", current_time=base_time)
        self.assertTrue(res.authorized)
        self.rm.record_fill("T1", "BANKNIFTY", 25, 400.0, True, current_time=base_time)
        self.rm.close_trade("BANKNIFTY", 448.0, current_time=base_time + timedelta(minutes=5))
        self.assertEqual(self.rm.realized_pnl, 1200.0)

        # Trade 2: 10:00 IST - Loss -₹800 (Trade count: 2, 1st loss)
        t2_time = base_time + timedelta(minutes=30)
        res = self.rm.authorize_order("NIFTY", 50, "BUY", current_time=t2_time)
        self.assertTrue(res.authorized)
        self.rm.record_fill("T2", "NIFTY", 50, 200.0, True, current_time=t2_time)
        self.rm.close_trade("NIFTY", 184.0, current_time=t2_time + timedelta(minutes=5))
        self.assertEqual(self.rm.realized_pnl, 400.0)
        self.assertEqual(self.rm.consecutive_losses, 1)

        # Trade 3: 10:45 IST - Loss -₹1,800 (Trade count: 3, 2nd consecutive loss)
        t3_time = base_time + timedelta(minutes=75)
        res = self.rm.authorize_order("NIFTY", 50, "BUY", current_time=t3_time)
        self.assertTrue(res.authorized)
        self.rm.record_fill("T3", "NIFTY", 50, 200.0, True, current_time=t3_time)
        self.rm.close_trade("NIFTY", 164.0, current_time=t3_time + timedelta(minutes=5))
        self.assertEqual(self.rm.realized_pnl, -1400.0)
        self.assertEqual(self.rm.consecutive_losses, 2)
        self.assertTrue(self.rm.is_cooldown_active(t3_time + timedelta(minutes=10)))

        # Blocked during cooldown (11:15 IST)
        t_blocked = t3_time + timedelta(minutes=30)
        res = self.rm.authorize_order("RELIANCE", 10, "BUY", current_time=t_blocked)
        self.assertFalse(res.authorized)
        self.assertIn("Consecutive loss cooldown active", res.reason)

        # Trade 4: 11:40 IST (After cooldown) - Win +₹600 (Trade count: 4, streak reset)
        t4_time = t3_time + timedelta(minutes=55)
        res = self.rm.authorize_order("RELIANCE", 10, "BUY", current_time=t4_time)
        self.assertTrue(res.authorized)
        self.rm.record_fill("T4", "RELIANCE", 10, 2500.0, True, current_time=t4_time)
        self.rm.close_trade("RELIANCE", 2560.0, current_time=t4_time + timedelta(minutes=10))
        self.assertEqual(self.rm.realized_pnl, -800.0)
        self.assertEqual(self.rm.consecutive_losses, 0)
        self.assertFalse(self.rm.is_cooldown_active(t4_time + timedelta(minutes=15)))

        # Trade 5: 13:00 IST - Trade count reaches 5
        t5_time = base_time + timedelta(hours=3, minutes=30)
        res = self.rm.authorize_order("INFY", 20, "BUY", current_time=t5_time)
        self.assertTrue(res.authorized)
        self.rm.record_fill("T5", "INFY", 20, 1500.0, True, current_time=t5_time)
        self.assertEqual(self.rm.trade_count, 5)

        # Trade 6 attempted (14:00 IST) -> Rejected by Daily Trade Cap!
        t6_time = base_time + timedelta(hours=4, minutes=30)
        res = self.rm.authorize_order("TCS", 10, "BUY", current_time=t6_time)
        self.assertFalse(res.authorized)
        self.assertIn("Daily trade cap reached: 5/5 trades", res.reason)

        # 15:10 IST EOD Hard Square-Off
        t_eod = IST.localize(datetime(2026, 9, 21, 15, 10, 0))
        router = MockBrokerRouter()
        sq_res = self.rm.run_eod_square_off(broker_router=router, current_time=t_eod)
        self.assertIsNotNone(sq_res)
        self.assertEqual(sq_res.status, SquareOffStatus.COMPLETED)
        self.assertEqual(sq_res.closed_positions, 1)

        summary = self.rm.get_risk_summary()
        self.assertEqual(summary["trade_count"], 5)
        self.assertEqual(summary["open_positions_count"], 0)
        self.assertFalse(summary["loss_ceiling_breached"])
        print("✓ [10] End-to-end simulated trade sequence: Hard limits, cooldowns & EOD square-off passed")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 5 FINAL VERIFICATION TEST SUITE (test_risk.py)")
    print("=" * 70)
    unittest.main(verbosity=0)
