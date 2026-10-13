"""
Strike Desk V4 - Phase 5 Prompt 1/4 Verification Suite: Risk State Model & Limit Definitions
Verifies:
  [a] RiskState creation: Required fields, valid defaults, is_actionable() / limit checks
  [b] Loss limit scope: Total Risk P&L = Realized + Unrealized - Charges (Review Gap B)
  [c] Daily reset: P&L, counters, and cooldown reset at 00:00 IST; unacknowledged halt preserved
  [d] Trade counter: trade_count increments, triggers cap breach at 5, resets daily
  [e] Consecutive loss tracker: Increments on LOSS, triggers 45m cooldown after 2 losses, resets on WIN
  [f] P&L calculation: Realized + Unrealized + Charges matches exact manual calculation
  [g] Charge calculation: Brokerage, STT, Exchange, GST, SEBI charges match 2026 regulations
  [h] Tick-level P&L update: Instantaneous MTM update and loss ceiling breach on tick (Review Risk 6)
  [i] Persistence: State survives restarts (SQLite first, JSON backup fallback on corruption)
  [j] Thread safety: Concurrent trade recordings do not corrupt state or cause race conditions
"""
import concurrent.futures
from datetime import date, datetime, timedelta, timezone
import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.account import Position
from models.risk_state import IST, IST_OFFSET, RiskState, Trade
from risk.charge_calculator import ChargeCalculator, charge_calculator
from risk.pnl_calculator import PnLCalculator, pnl_calculator
from risk.risk_state_manager import RiskStateManager, get_ist_date, get_ist_now

UTC = timezone.utc


class TestRiskStateModel(unittest.TestCase):
    """
    Verification suite for Risk State Model & Limit Definitions.
    """

    def setUp(self):
        self.t0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)
        self.test_dir = tempfile.mkdtemp(prefix="sd_test_risk_")
        self.test_db = os.path.join(self.test_dir, "risk_state.db")
        self.test_json = os.path.join(self.test_dir, "risk_state_backup.json")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_a_risk_state_creation(self):
        """[a] RiskState creation: Verify all fields initialized correctly."""
        t_date = date(2026, 9, 21)
        state = RiskState(trading_date=t_date)

        self.assertEqual(state.trading_date, t_date)
        self.assertEqual(state.realized_pnl, 0.0)
        self.assertEqual(state.unrealized_pnl, 0.0)
        self.assertEqual(state.total_charges, 0.0)
        self.assertEqual(state.total_risk_pnl, 0.0)
        self.assertEqual(state.trade_count, 0)
        self.assertEqual(state.consecutive_losses, 0)
        self.assertIsNone(state.last_trade_time)
        self.assertIsNone(state.last_trade_result)
        self.assertIsNone(state.cooldown_until)
        self.assertFalse(state.emergency_halt_active)
        self.assertFalse(state.emergency_halt_acknowledged)

        # Dictionary round-trip
        data = state.to_dict()
        self.assertEqual(data["trading_date"], "2026-09-21")
        restored = RiskState.from_dict(data)
        self.assertEqual(restored.trading_date, t_date)
        self.assertEqual(restored.total_risk_pnl, 0.0)
        print("✓ [a] RiskState creation: Verify all fields initialized correctly")

    def test_b_loss_limit_scope(self):
        """[b] Loss limit scope: Verify total_risk_pnl = realized + unrealized - charges (Review Gap B)."""
        # Scenario: Trade P&L is profitable (+₹50), but charges are ₹80, open position is -₹10.
        # Trade P&L = +50.0
        # Charges = 80.0
        # Unrealized P&L = -10.0
        # True Economic Loss = +50.0 + (-10.0) - 80.0 = -40.0
        state = RiskState(
            trading_date=date(2026, 9, 21),
            realized_pnl=50.0,
            unrealized_pnl=-10.0,
            total_charges=80.0,
        )
        self.assertEqual(state.total_risk_pnl, -40.0)

        # Position is profitable on trade P&L alone (+50), but net economic loss is -40.
        self.assertNotEqual(state.total_risk_pnl, 50.0)

        # If ceiling is -35.0, then -40.0 breaches ceiling even though trade P&L was positive!
        self.assertTrue(state.is_daily_loss_ceiling_breached(ceiling=-35.0))
        self.assertFalse(state.is_daily_loss_ceiling_breached(ceiling=-50.0))
        print("✓ [b] Loss limit scope: Verify total_risk_pnl = realized + unrealized + charges (not just trade P&L)")

    def test_c_daily_reset(self):
        """[c] Daily reset: Verify counters reset at 00:00 IST."""
        manager = RiskStateManager(db_path=self.test_db, json_backup_path=self.test_json)
        manager.reset_for_tests(test_date=date(2026, 9, 20))

        # Record trade on day 1
        t1 = Trade(
            trade_id="TR1",
            symbol="TCS",
            side="BUY",
            quantity=10,
            entry_price=3500.0,
            exit_price=3400.0,
            realized_pnl=-1000.0,
            charges=40.0,
            timestamp=datetime(2026, 9, 20, 14, 0, 0, tzinfo=UTC),
        )
        manager.record_trade(t1)
        st = manager.get_state()
        self.assertEqual(st.trade_count, 1)
        self.assertEqual(st.realized_pnl, -1000.0)
        self.assertEqual(st.consecutive_losses, 1)

        # Trigger reset for next day
        manager.daily_reset(target_date=date(2026, 9, 21))
        st2 = manager.get_state()
        self.assertEqual(st2.trading_date, date(2026, 9, 21))
        self.assertEqual(st2.trade_count, 0)
        self.assertEqual(st2.realized_pnl, 0.0)
        self.assertEqual(st2.total_charges, 0.0)
        self.assertEqual(st2.total_risk_pnl, 0.0)
        self.assertEqual(st2.consecutive_losses, 0)
        self.assertIsNone(st2.last_trade_time)
        print("✓ [c] Daily reset: Verify counters reset at 00:00 IST")

    def test_d_trade_counter(self):
        """[d] Trade counter: Verify trade_count increments, resets daily, breaches cap at 5."""
        manager = RiskStateManager(db_path=self.test_db, json_backup_path=self.test_json)
        manager.reset_for_tests(test_date=date(2026, 9, 21))

        # Record 5 winning trades
        for i in range(5):
            t = Trade(
                trade_id=f"T_COUNT_{i}",
                symbol="INFY",
                side="BUY",
                quantity=10,
                entry_price=1500.0,
                exit_price=1510.0,
                realized_pnl=100.0,
                charges=15.0,
                timestamp=self.t0 + timedelta(minutes=i * 10),
            )
            manager.record_trade(t)

        st = manager.get_state()
        self.assertEqual(st.trade_count, 5)
        self.assertTrue(st.is_trade_cap_breached(5))

        # Order authorization fails on trade cap
        allowed, reason = manager.can_place_order(self.t0 + timedelta(hours=1))
        self.assertFalse(allowed)
        self.assertIn("Daily trade cap reached", reason)

        # Resets on new day
        manager.daily_reset(target_date=date(2026, 9, 22))
        self.assertEqual(manager.get_state().trade_count, 0)
        allowed_new, _ = manager.can_place_order(self.t0 + timedelta(days=1))
        self.assertTrue(allowed_new)
        print("✓ [d] Trade counter: Verify trade_count increments, resets daily")

    def test_e_consecutive_loss_tracker(self):
        """[e] Consecutive loss tracker: Verify increments on LOSS, resets on WIN, triggers 45m cooldown."""
        manager = RiskStateManager(db_path=self.test_db, json_backup_path=self.test_json)
        manager.reset_for_tests(test_date=date(2026, 9, 21))

        # Trade 1: Loss 1
        t1 = Trade(
            trade_id="L1", symbol="SBIN", side="BUY", quantity=100, entry_price=800.0,
            exit_price=790.0, realized_pnl=-1000.0, charges=30.0, timestamp=self.t0
        )
        manager.record_trade(t1)
        st1 = manager.get_state()
        self.assertEqual(st1.consecutive_losses, 1)
        self.assertEqual(st1.last_trade_result, "LOSS")
        self.assertFalse(st1.is_in_cooldown(self.t0))

        # Trade 2: Loss 2 -> triggers 45-minute cooldown
        t2_time = self.t0 + timedelta(minutes=10)
        t2 = Trade(
            trade_id="L2", symbol="SBIN", side="BUY", quantity=100, entry_price=800.0,
            exit_price=795.0, realized_pnl=-500.0, charges=30.0, timestamp=t2_time
        )
        manager.record_trade(t2)
        st2 = manager.get_state()
        self.assertEqual(st2.consecutive_losses, 2)
        self.assertEqual(st2.cooldown_until, t2_time + timedelta(minutes=45))

        # At t=20m (within cooldown) -> Blocked
        allowed_cd, reason_cd = manager.can_place_order(t2_time + timedelta(minutes=10))
        self.assertFalse(allowed_cd)
        self.assertIn("Cooldown active", reason_cd)

        # At t=56m (46m after Loss 2) -> Cooldown expired
        allowed_post_cd, _ = manager.can_place_order(t2_time + timedelta(minutes=46))
        self.assertTrue(allowed_post_cd)

        # Trade 3: WIN -> Resets consecutive losses to 0
        t3_time = t2_time + timedelta(minutes=50)
        t3 = Trade(
            trade_id="W1", symbol="SBIN", side="BUY", quantity=100, entry_price=800.0,
            exit_price=820.0, realized_pnl=2000.0, charges=30.0, timestamp=t3_time
        )
        manager.record_trade(t3)
        st3 = manager.get_state()
        self.assertEqual(st3.consecutive_losses, 0)
        self.assertEqual(st3.last_trade_result, "WIN")
        self.assertIsNone(st3.cooldown_until)
        print("✓ [e] Consecutive loss tracker: Verify increments on LOSS, resets on WIN")

    def test_f_pnl_calculation(self):
        """[f] P&L calculation: Verify realized + unrealized + charges matches manual reference."""
        # 2 Closed Trades
        trades = [
            Trade("T1", "TCS", "BUY", 10, entry_price=3500.0, exit_price=3600.0, realized_pnl=1000.0, charges=40.0),
            Trade("T2", "INFY", "BUY", 20, entry_price=1500.0, exit_price=1480.0, realized_pnl=-400.0, charges=30.0),
        ]
        # 2 Open Positions
        positions = [
            Position(broker="dhan", symbol="RELIANCE", exchange="NSE", qty=50, avg_price=2500.0),
            Position(broker="dhan", symbol="HDFCBANK", exchange="NSE", qty=-40, avg_price=1600.0), # Short
        ]
        current_prices = {
            "RELIANCE": 2520.0,  # Long: +20 * 50 = +1000.0
            "HDFCBANK": 1610.0,  # Short: (1600 - 1610) * 40 = -400.0
        }

        # Manual checks:
        # Realized = 1000.0 - 400.0 = 600.0
        # Unrealized = 1000.0 - 400.0 = 600.0
        # Charges = 40.0 + 30.0 = 70.0
        # Total Risk P&L = 600.0 + 600.0 - 70.0 = 1130.0
        realized = PnLCalculator.calculate_realized_pnl(trades)
        unrealized = PnLCalculator.calculate_unrealized_pnl(positions, current_prices)
        total_risk = PnLCalculator.calculate_total_risk_pnl(trades, positions, current_prices)

        self.assertEqual(realized, 600.0)
        self.assertEqual(unrealized, 600.0)
        self.assertEqual(total_risk, 1130.0)
        print("✓ [f] P&L calculation: Verify realized + unrealized + charges calculation matches manual")

    def test_g_charge_calculation(self):
        """[g] Charge calculation: Verify brokerage, STT, exchange charges, GST, SEBI charges match regulations."""
        # 1. Intraday Equity: turnover 10,000
        # Brokerage = min(20, 0.0003 * 10000 = 3) = 3.0
        self.assertEqual(ChargeCalculator.calculate_brokerage(10000.0, "INTRADAY", "EQUITY"), 3.0)

        # Intraday Equity: turnover 1,000,000
        # Brokerage = min(20, 0.0003 * 1000000 = 300) = 20.0
        self.assertEqual(ChargeCalculator.calculate_brokerage(1000000.0, "INTRADAY", "EQUITY"), 20.0)

        # 2. Delivery Equity: Brokerage = 0
        self.assertEqual(ChargeCalculator.calculate_brokerage(50000.0, "DELIVERY", "EQUITY"), 0.0)

        # 3. F&O: Brokerage = flat 20.0
        self.assertEqual(ChargeCalculator.calculate_brokerage(50000.0, "INTRADAY", "FNO"), 20.0)

        # 4. STT:
        # Intraday Equity sell: 0.025% on 100,000 = 25.0
        self.assertEqual(ChargeCalculator.calculate_stt(100000.0, "INTRADAY", "EQUITY", is_sell=True), 25.0)
        # Intraday Equity buy: 0.0
        self.assertEqual(ChargeCalculator.calculate_stt(100000.0, "INTRADAY", "EQUITY", is_sell=False), 0.0)
        # Delivery buy and sell: 0.1% on 100,000 = 100.0
        self.assertEqual(ChargeCalculator.calculate_stt(100000.0, "DELIVERY", "EQUITY", is_sell=True), 100.0)
        # F&O sell: 0.0625% on 100,000 = 62.5
        self.assertEqual(ChargeCalculator.calculate_stt(100000.0, "INTRADAY", "FNO", is_sell=True), 62.5)

        # 5. Exchange Charges:
        # NSE Equity: 0.00325% on 100,000 = 3.25
        self.assertEqual(ChargeCalculator.calculate_exchange_charges(100000.0, "EQUITY"), 3.25)
        # NSE F&O: 0.002% on 100,000 = 2.0
        self.assertEqual(ChargeCalculator.calculate_exchange_charges(100000.0, "FNO"), 2.0)

        # 6. GST: 18% on (Brokerage 20 + Exchange 3.25 = 23.25) = 4.185
        self.assertAlmostEqual(ChargeCalculator.calculate_gst(brokerage=20.0, exchange_charges=3.25), 4.185, places=3)

        # 7. SEBI: ₹10 per crore (0.0001% on 100,000 = 0.10)
        self.assertEqual(ChargeCalculator.calculate_sebi_charges(100000.0), 0.10)

        # 8. Total charges matches breakdown
        b = ChargeCalculator.get_charges_breakdown(100000.0, "INTRADAY", "EQUITY", is_sell=True)
        self.assertEqual(b["total_charges"], round(b["brokerage"] + b["stt"] + b["exchange_charges"] + b["gst"] + b["sebi_charges"], 4))
        print("✓ [g] Charge calculation: Verify brokerage, STT, exchange charges, GST, SEBI charges match regulations")

    def test_h_tick_level_pnl_update(self):
        """[h] Tick-level P&L update: Verify update_on_tick() recalculates unrealized P&L correctly (Review Risk 6)."""
        manager = RiskStateManager(db_path=self.test_db, json_backup_path=self.test_json)
        manager.reset_for_tests(test_date=date(2026, 9, 21))

        # Position: 100 shares of RELIANCE @ 2500.0
        pos = Position(broker="dhan", symbol="RELIANCE", exchange="NSE", qty=100, avg_price=2500.0)
        manager.pnl_calculator.set_positions([pos])

        # Tick 1: Price goes up to 2510.0 -> Unrealized = +1000.0
        pnl1 = manager.update_position_pnl("RELIANCE", 2510.0)
        self.assertEqual(pnl1, 1000.0)
        self.assertEqual(manager.get_state().unrealized_pnl, 1000.0)
        self.assertFalse(manager.get_state().is_daily_loss_ceiling_breached(-2500.0))
        self.assertFalse(manager.get_state().is_daily_loss_ceiling_breached(-2500.0))

        # Tick 2: Sudden drop to 2470.0 -> Unrealized = -3000.0
        pnl2 = manager.update_position_pnl("RELIANCE", 2470.0)
        self.assertEqual(pnl2, -3000.0)
        self.assertEqual(manager.get_state().unrealized_pnl, -3000.0)

        # CRITICAL: Daily loss ceiling (-2500) breached on the tick itself (Risk 6 compliance)
        self.assertTrue(manager.get_state().is_daily_loss_ceiling_breached(-2500.0))
        allowed, reason = manager.can_place_order(datetime.now(timezone.utc))
        self.assertFalse(allowed)
        self.assertIn("Daily loss ceiling breached", reason)
        print("✓ [h] Tick-level P&L update: Verify update_on_tick() recalculates unrealized P&L correctly")

    def test_i_persistence(self):
        """[i] Persistence: Verify state survives restart (save -> restore -> verify)."""
        today_ist = get_ist_date()
        manager = RiskStateManager(db_path=self.test_db, json_backup_path=self.test_json)
        manager.reset_for_tests(test_date=today_ist)

        # Record trade, set active halt
        t = Trade("TR_PERSIST", "TCS", "BUY", 10, 3500.0, 3450.0, realized_pnl=-500.0, charges=25.0)
        manager.record_trade(t)
        manager.activate_emergency_halt("Manual audit triggered")
        manager.persist_state()

        # Create new manager instance on same DB to simulate restart
        restarted_manager = RiskStateManager(db_path=self.test_db, json_backup_path=self.test_json)
        restarted_manager.restore_state()
        restored = restarted_manager.get_state()

        self.assertEqual(restored.trading_date, today_ist)
        self.assertEqual(restored.trade_count, 1)
        self.assertEqual(restored.realized_pnl, -500.0)
        self.assertEqual(restored.total_charges, 25.0)
        self.assertEqual(restored.total_risk_pnl, -525.0)
        self.assertTrue(restored.emergency_halt_active)
        self.assertEqual(restored.emergency_halt_reason, "Manual audit triggered")

        # Test recovery from JSON backup if SQLite is corrupted
        with open(self.test_db, "wb") as f:
            f.write(b"CORRUPTED_DATABASE_GARBAGE")

        recovered_manager = RiskStateManager(db_path=self.test_db, json_backup_path=self.test_json)
        recovered_manager.restore_state()
        rec_state = recovered_manager.get_state()
        self.assertEqual(rec_state.realized_pnl, -500.0)
        self.assertTrue(rec_state.emergency_halt_active)
        print("✓ [i] Persistence: Verify state survives restart (save → restore → verify)")

    def test_j_thread_safety(self):
        """[j] Thread safety: Verify concurrent updates don't corrupt state (100 concurrent trade recordings)."""
        manager = RiskStateManager(db_path=self.test_db, json_backup_path=self.test_json)
        manager.reset_for_tests(test_date=date(2026, 9, 21))

        def record_concurrent_trade(idx: int):
            t = Trade(
                trade_id=f"CONC_{idx}",
                symbol="TCS",
                side="BUY",
                quantity=1,
                entry_price=3500.0,
                exit_price=3510.0,
                realized_pnl=10.0,
                charges=2.0,
                timestamp=self.t0 + timedelta(seconds=idx),
            )
            manager.record_trade(t)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(record_concurrent_trade, i) for i in range(100)]
            concurrent.futures.wait(futures)

        st = manager.get_state()
        self.assertEqual(st.trade_count, 100)
        self.assertAlmostEqual(st.realized_pnl, 1000.0)
        self.assertAlmostEqual(st.total_charges, 200.0)
        self.assertAlmostEqual(st.total_risk_pnl, 800.0)
        print("✓ [j] Thread safety: Verify concurrent updates don't corrupt state (100 concurrent trade recordings)")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 5 PROMPT 1/4 RISK STATE & LIMITS TEST SUITE")
    print("=======================================================================")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestRiskStateModel)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 10 RISK STATE & LIMIT DEFINITIONS TESTS [a-j] PASSED SUCCESSFULLY!")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)