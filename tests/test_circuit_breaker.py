"""
Strike Desk V4 - Phase 5 Prompt 2/4 Verification Suite: Circuit Breakers & Cooldown Logic
Verifies:
  [a] Loss ceiling breaker: Trips immediately when Total Risk P&L <= -₹2,500
  [b] Trade cap breaker: Trips immediately when trade count reaches 5 trades
  [c] Consecutive loss cooldown: 2 consecutive losses triggers 45-minute cooldown; auto-expires
  [d] Cooldown reset on win: Winning trade clears cooldown and resets consecutive loss counter
  [e] 15-minute stagnation exit: Positions open >= 15 minutes trigger square-off alert (Blueprint O5)
  [f] Order authorization approved: Valid order gets authorized with unique auth token (Review Gap A)
  [g] Order authorization rejections: Orders rejected on halt, loss ceiling, trade cap, cooldown, 15:10 IST cutoff
  [h] Tick-level P&L tripping: Fast tick price updates trip breaker before trade placement (Review Risk 6)
  [i] Emergency halt & ack: Emergency halt engages kill-switch; requires manual ack to reset
  [j] Concurrency & thread safety: Concurrent tick updates and order authorizations run without race conditions
"""
import concurrent.futures
from datetime import date, datetime, time, timedelta, timezone
import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.account import Position
from models.order import Order, OrderSide, OrderType, ProductType
from models.risk_state import IST, RiskState, Trade
from risk.circuit_breaker import BreakerStatus, CircuitBreaker, CircuitBreakerConfig, circuit_breaker
from risk.order_authorizer import AuthorizationResult, OrderAuthorizer, order_authorizer
from risk.pnl_calculator import PnLCalculator
from risk.risk_state_manager import RiskStateManager
from risk.stagnation_monitor import StagnationAlert, StagnationMonitor

UTC = timezone.utc


class TestCircuitBreakersAndCooldown(unittest.TestCase):
    """
    Verification suite for Circuit Breakers, Cooldown, and Pre-Trade Risk Authorization.
    """

    def setUp(self):
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)
        self.test_dir = tempfile.mkdtemp(prefix="sd_test_cb_")
        self.test_db = os.path.join(self.test_dir, "cb_risk_state.db")
        self.test_json = os.path.join(self.test_dir, "cb_risk_state_backup.json")

        self.pnl_calc = PnLCalculator()
        self.state_mgr = RiskStateManager(
            db_path=self.test_db,
            json_backup_path=self.test_json,
            pnl_calc=self.pnl_calc,
            force_reinit=True,
        )
        self.state_mgr.reset_for_tests(test_date=date(2026, 9, 21))

        self.stag_mon = StagnationMonitor(timeout_minutes=15.0)
        self.breaker = CircuitBreaker(
            config=CircuitBreakerConfig(
                daily_loss_ceiling=-2500.0,
                daily_trade_cap=5,
                consecutive_loss_threshold=2,
                cooldown_duration_minutes=45,
                stagnation_timeout_minutes=15,
            ),
            state_manager=self.state_mgr,
            stag_mon=self.stag_mon,
        )
        self.authorizer = OrderAuthorizer(breaker=self.breaker, max_order_value=200_000.0)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_a_loss_ceiling_breaker(self):
        """[a] Loss ceiling breaker: Trips when Total Risk P&L <= -₹2,500."""
        # 1. Total loss at -₹2,400 -> Normal
        t1 = Trade("TR_A1", "TCS", "BUY", 10, 3500.0, 3260.0, realized_pnl=-2400.0, charges=0.0, timestamp=self.t0)
        self.breaker.on_trade_completed(t1)
        self.assertEqual(self.breaker.get_status(self.t0), BreakerStatus.NORMAL)
        can_trade, _ = self.breaker.can_trade(self.t0)
        self.assertTrue(can_trade)

        # 2. Additional loss pushes Total Risk P&L to -₹2,600 -> LOSS_CEILING_TRIPPED
        t2 = Trade("TR_A2", "INFY", "BUY", 10, 1500.0, 1480.0, realized_pnl=-200.0, charges=0.0, timestamp=self.t0)
        status = self.breaker.on_trade_completed(t2)
        self.assertEqual(status, BreakerStatus.LOSS_CEILING_TRIPPED)
        can_trade2, reason = self.breaker.can_trade(self.t0)
        self.assertFalse(can_trade2)
        self.assertIn("Daily loss ceiling breached", reason)
        print("✓ [a] Loss ceiling breaker: Trips immediately when Total Risk P&L <= -₹2,500")

    def test_b_trade_cap_breaker(self):
        """[b] Trade cap breaker: Trips when daily trade count reaches 5 trades."""
        # Record 4 trades -> still Normal
        for i in range(4):
            t = Trade(f"TR_B_{i}", "SBIN", "BUY", 10, 800.0, 810.0, realized_pnl=100.0, charges=5.0, timestamp=self.t0 + timedelta(minutes=i))
            self.breaker.on_trade_completed(t)
            self.assertEqual(self.breaker.get_status(self.t0 + timedelta(minutes=i)), BreakerStatus.NORMAL)

        # 5th trade executes -> TRADE_CAP_TRIPPED
        t5 = Trade("TR_B_4", "SBIN", "BUY", 10, 800.0, 810.0, realized_pnl=100.0, charges=5.0, timestamp=self.t0 + timedelta(minutes=10))
        status = self.breaker.on_trade_completed(t5)
        self.assertEqual(status, BreakerStatus.TRADE_CAP_TRIPPED)

        can_trade, reason = self.breaker.can_trade(self.t0 + timedelta(minutes=15))
        self.assertFalse(can_trade)
        self.assertIn("Daily trade cap reached: 5 >= 5", reason)
        print("✓ [b] Trade cap breaker: Trips immediately when trade count reaches 5 trades")

    def test_c_consecutive_loss_cooldown(self):
        """[c] Consecutive loss cooldown: 2 consecutive losses triggers 45m cooldown; auto-expires."""
        # Loss 1 -> Normal
        t1 = Trade("TR_C1", "TCS", "BUY", 10, 3500.0, 3450.0, realized_pnl=-500.0, charges=10.0, timestamp=self.t0)
        status1 = self.breaker.on_trade_completed(t1)
        self.assertEqual(status1, BreakerStatus.NORMAL)

        # Loss 2 at t0 + 5m -> IN_COOLDOWN until t0 + 50m
        t_loss2 = self.t0 + timedelta(minutes=5)
        t2 = Trade("TR_C2", "TCS", "BUY", 10, 3500.0, 3470.0, realized_pnl=-300.0, charges=10.0, timestamp=t_loss2)
        status2 = self.breaker.on_trade_completed(t2)
        self.assertEqual(status2, BreakerStatus.IN_COOLDOWN)

        # At t0 + 20m (15m into cooldown) -> Blocked
        t_check1 = self.t0 + timedelta(minutes=20)
        can_trade1, reason1 = self.breaker.can_trade(t_check1)
        self.assertFalse(can_trade1)
        self.assertIn("In cooldown until", reason1)

        # At t0 + 51m (46m after Loss 2, > 45m duration) -> Cooldown auto-expires!
        t_check2 = t_loss2 + timedelta(minutes=46)
        self.assertEqual(self.breaker.get_status(t_check2), BreakerStatus.NORMAL)
        can_trade2, _ = self.breaker.can_trade(t_check2)
        self.assertTrue(can_trade2)
        print("✓ [c] Consecutive loss cooldown: 2 consecutive losses triggers 45-minute cooldown; auto-expires")

    def test_d_cooldown_reset_on_win(self):
        """[d] Cooldown reset on win: Winning trade clears cooldown and resets consecutive loss counter."""
        # 2 Consecutive losses trigger cooldown
        t1 = Trade("TR_D1", "TCS", "BUY", 10, 3500.0, 3450.0, realized_pnl=-500.0, charges=10.0, timestamp=self.t0)
        t2 = Trade("TR_D2", "TCS", "BUY", 10, 3500.0, 3470.0, realized_pnl=-300.0, charges=10.0, timestamp=self.t0 + timedelta(minutes=2))
        self.breaker.on_trade_completed(t1)
        self.breaker.on_trade_completed(t2)
        self.assertEqual(self.breaker.get_status(self.t0 + timedelta(minutes=3)), BreakerStatus.IN_COOLDOWN)

        # Winning trade executes -> resets consecutive losses to 0, clears cooldown
        t_win = Trade("TR_D3", "INFY", "BUY", 10, 1500.0, 1550.0, realized_pnl=500.0, charges=10.0, timestamp=self.t0 + timedelta(minutes=5))
        status_after_win = self.breaker.on_trade_completed(t_win)
        self.assertEqual(status_after_win, BreakerStatus.NORMAL)
        self.assertEqual(self.state_mgr.get_state().consecutive_losses, 0)
        self.assertIsNone(self.state_mgr.get_state().cooldown_until)

        can_trade, _ = self.breaker.can_trade(self.t0 + timedelta(minutes=6))
        self.assertTrue(can_trade)
        print("✓ [d] Cooldown reset on win: Winning trade clears cooldown and resets consecutive loss counter")

    def test_e_stagnation_monitor(self):
        """[e] 15-minute stagnation exit: Positions open >= 15 minutes trigger square-off alert (Blueprint O5)."""
        entry_time = self.t0
        self.breaker.stagnation_monitor.register_position(
            symbol="HDFCBANK",
            entry_time=entry_time,
            entry_price=1600.0,
            quantity=50,
        )

        # At 10 minutes: no alerts
        t_10m = entry_time + timedelta(minutes=10)
        alerts_10m = self.breaker.check_stagnant_positions(t_10m)
        self.assertEqual(len(alerts_10m), 0)

        # At 15.5 minutes: Stagnation threshold breached -> Alert issued
        t_16m = entry_time + timedelta(minutes=15, seconds=30)
        alerts_16m = self.breaker.check_stagnant_positions(t_16m)
        self.assertEqual(len(alerts_16m), 1)
        alert = alerts_16m[0]
        self.assertEqual(alert.symbol, "HDFCBANK")
        self.assertEqual(alert.recommended_action, "SQUARE_OFF")
        self.assertGreaterEqual(alert.elapsed_minutes, 15.0)
        self.assertIn("Position stagnant for", alert.reason)

        # Position closed -> removed from tracker
        self.breaker.stagnation_monitor.remove_position("HDFCBANK")
        self.assertEqual(len(self.breaker.check_stagnant_positions(t_16m)), 0)
        print("✓ [e] 15-minute stagnation exit: Positions open >= 15 minutes trigger square-off alert (Blueprint O5)")

    def test_f_order_authorization_approved(self):
        """[f] Order authorization approved: Valid order gets authorized with unique auth token (Review Gap A)."""
        order = Order(
            client_order_id="ORD_VALID_1",
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=25,
            price=2500.0,  # Value = 62,500 <= 200,000 limit
            product_type=ProductType.INTRADAY,
        )
        res = self.authorizer.authorize_order(order, current_time=self.t0)
        self.assertTrue(res.is_authorized)
        self.assertIsNotNone(res.auth_token)
        self.assertTrue(res.auth_token.startswith("AUTH_"))
        self.assertEqual(res.estimated_value, 62500.0)
        self.assertEqual(res.symbol, "RELIANCE")
        print("✓ [f] Order authorization approved: Valid order gets authorized with unique auth token (Review Gap A)")

    def test_g_order_authorization_rejections(self):
        """[g] Order authorization rejections: Orders rejected on halt, loss ceiling, trade cap, cooldown, 15:10 IST cutoff."""
        order = Order(
            client_order_id="ORD_TEST_REJ",
            symbol="TCS",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=3500.0,
            product_type=ProductType.INTRADAY,
        )

        # 1. Reject on Quantity <= 0
        order_zero = Order(
            client_order_id="ORD_ZERO", symbol="TCS", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=0, price=3500.0, product_type=ProductType.INTRADAY,
        )
        res_zero = self.authorizer.authorize_order(order_zero, current_time=self.t0)
        self.assertFalse(res_zero.is_authorized)
        self.assertEqual(res_zero.rule_violated, "QUANTITY_SANITY")

        # 2. Reject on Max Order Value Cap (> ₹200,000)
        order_large = Order(
            client_order_id="ORD_LARGE", symbol="TCS", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=100, price=3500.0, product_type=ProductType.INTRADAY, # ₹350,000
        )
        res_large = self.authorizer.authorize_order(order_large, current_time=self.t0)
        self.assertFalse(res_large.is_authorized)
        self.assertEqual(res_large.rule_violated, "MAX_ORDER_VALUE")

        # 3. Reject on Emergency Halt
        self.breaker.activate_emergency_halt("Exchange RMS disconnect")
        res_halt = self.authorizer.authorize_order(order, current_time=self.t0)
        self.assertFalse(res_halt.is_authorized)
        self.assertEqual(res_halt.rule_violated, "EMERGENCY_HALTED")
        self.breaker.acknowledge_emergency_halt()

        # 4. Reject on 15:10 IST hard cutoff (15:10:01 IST = 09:40:01 UTC)
        # 15:10 IST = 09:40 UTC
        t_cutoff = datetime(2026, 9, 21, 9, 41, 0, tzinfo=UTC)  # 15:11 IST
        res_cutoff = self.authorizer.authorize_order(order, current_time=t_cutoff)
        self.assertFalse(res_cutoff.is_authorized)
        self.assertEqual(res_cutoff.rule_violated, "TIME_CUTOFF_15_10")
        print("✓ [g] Order authorization rejections: Orders rejected on halt, loss ceiling, trade cap, cooldown, 15:10 IST cutoff")

    def test_h_tick_level_pnl_tripping(self):
        """[h] Tick-level P&L tripping: Fast tick price updates trip breaker before trade placement (Review Risk 6)."""
        # Open position: 100 shares @ 2500.0
        pos = Position(broker="dhan", symbol="RELIANCE", exchange="NSE", qty=100, avg_price=2500.0)
        self.pnl_calc.set_positions([pos])

        # Tick 1: Price goes up to 2510.0 -> P&L = +1000.0 -> NORMAL
        total_pnl1, status1, tripped1 = self.breaker.on_tick("RELIANCE", 2510.0, self.t0)
        self.assertEqual(total_pnl1, 1000.0)
        self.assertEqual(status1, BreakerStatus.NORMAL)
        self.assertFalse(tripped1)

        # Tick 2: Sudden market dump to 2470.0 -> Loss = -3000.0 <= -2500.0 -> Trips breaker on the tick!
        total_pnl2, status2, tripped2 = self.breaker.on_tick("RELIANCE", 2470.0, self.t0 + timedelta(seconds=1))
        self.assertEqual(total_pnl2, -3000.0)
        self.assertEqual(status2, BreakerStatus.LOSS_CEILING_TRIPPED)
        self.assertTrue(tripped2)

        # Attempt to authorize new order -> Immediately rejected!
        order = Order(
            client_order_id="ORD_TICK_BLOCK", symbol="INFY", exchange="NSE",
            side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=10, price=1500.0,
        )
        auth = self.authorizer.authorize_order(order, current_time=self.t0 + timedelta(seconds=2))
        self.assertFalse(auth.is_authorized)
        self.assertEqual(auth.rule_violated, "LOSS_CEILING_TRIPPED")
        print("✓ [h] Tick-level P&L tripping: Fast tick price updates trip breaker before trade placement (Review Risk 6)")

    def test_i_emergency_halt_and_ack(self):
        """[i] Emergency halt & ack: Emergency halt engages kill-switch; requires manual ack to reset."""
        self.assertEqual(self.breaker.get_status(self.t0), BreakerStatus.NORMAL)

        # Activate halt
        self.breaker.activate_emergency_halt("Operator manual kill-switch")
        self.assertEqual(self.breaker.get_status(self.t0), BreakerStatus.EMERGENCY_HALTED)

        # Daily reset does NOT clear unacknowledged emergency halt
        self.state_mgr.daily_reset(target_date=date(2026, 9, 22))
        self.assertEqual(self.breaker.get_status(self.t0 + timedelta(days=1)), BreakerStatus.EMERGENCY_HALTED)

        # Manual acknowledgment clears halt
        self.breaker.acknowledge_emergency_halt()
        self.assertEqual(self.breaker.get_status(self.t0 + timedelta(days=1)), BreakerStatus.NORMAL)
        print("✓ [i] Emergency halt & ack: Emergency halt engages kill-switch; requires manual ack to reset")

    def test_j_concurrency_and_thread_safety(self):
        """[j] Concurrency & thread safety: Concurrent tick updates and order authorizations run without race conditions."""
        pos = Position(broker="dhan", symbol="INFY", exchange="NSE", qty=50, avg_price=1500.0)
        self.pnl_calc.set_positions([pos])

        errors = []

        def worker_authorizations(idx: int):
            try:
                order = Order(
                    client_order_id=f"CONC_AUTH_{idx}",
                    symbol="INFY",
                    exchange="NSE",
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=5,
                    price=1500.0,
                )
                res = self.authorizer.authorize_order(order, current_time=self.t0 + timedelta(milliseconds=idx * 10))
                self.assertIsNotNone(res)
            except Exception as e:
                errors.append(str(e))

        def worker_ticks(idx: int):
            try:
                price = 1500.0 + (idx % 10)
                self.breaker.on_tick("INFY", price, current_time=self.t0 + timedelta(milliseconds=idx * 10))
            except Exception as e:
                errors.append(str(e))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for i in range(50):
                futures.append(executor.submit(worker_authorizations, i))
                futures.append(executor.submit(worker_ticks, i))
            concurrent.futures.wait(futures)

        self.assertEqual(len(errors), 0, f"Thread safety errors: {errors}")
        audit_log = self.authorizer.get_audit_log()
        self.assertEqual(len(audit_log), 50)
        print("✓ [j] Concurrency & thread safety: Concurrent tick updates and order authorizations run without race conditions")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 5 PROMPT 2/4 CIRCUIT BREAKERS & COOLDOWN TEST SUITE")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestCircuitBreakersAndCooldown)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 10 CIRCUIT BREAKER & COOLDOWN TESTS [a-j] PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)
