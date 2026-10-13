import asyncio
from datetime import datetime, timezone
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Disable global audit_logger SQLite writer during tests
os.environ.setdefault("AUDIT_DB_PATH", "")

from adapters.order_manager import (
    CommonErrorCode,
    DuplicateOrderError,
    OrderError,
    OrderManager,
    normalize_broker_error,
)
from execution.audit_logger import AuditLogger
from models.order import (
    Order,
    OrderSide,
    OrderState,
    OrderType,
    OrderUpdate,
    ProductType,
    VALID_TRANSITIONS,
)
from utils.order_id_generator import (
    compute_order_checksum,
    generate_client_order_id,
    verify_client_order_id,
)


class TestOrderLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = OrderManager(submission_timeout=0.1, idempotency_window=10.0)

    def test_a_order_state_transitions(self):
        order = self.manager.create_order(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100.0,
            price=2900.0,
            broker="dhan",
        )
        self.assertEqual(order.state, OrderState.PENDING)
        self.assertEqual(order.remaining_qty, 100.0)
        self.assertEqual(order.filled_qty, 0.0)

        # Transition 1: PENDING -> OPEN
        u1 = order.transition_to(OrderState.OPEN, broker_order_id="BROKER_123")
        self.assertEqual(order.state, OrderState.OPEN)
        self.assertEqual(order.broker_order_id, "BROKER_123")
        self.assertEqual(u1.state, OrderState.OPEN)

        # Transition 2: OPEN -> PARTIALLY_FILLED
        u2 = order.apply_fill(fill_qty=40.0, fill_price=2900.0)
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_qty, 40.0)
        self.assertEqual(order.remaining_qty, 60.0)

        # Transition 3: PARTIALLY_FILLED -> FILLED
        u3 = order.apply_fill(fill_qty=60.0, fill_price=2902.0)
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_qty, 100.0)
        self.assertEqual(order.remaining_qty, 0.0)
        self.assertEqual(len(order.state_history), 3)

        # Cancellation path on fresh order
        order2 = self.manager.create_order(
            symbol="INFY",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=50.0,
            price=1800.0,
            broker="angelone",
        )
        order2.transition_to(OrderState.OPEN, broker_order_id="ANGEL_456")
        order2.transition_to(OrderState.CANCEL_PENDING)
        self.assertEqual(order2.state, OrderState.CANCEL_PENDING)
        order2.transition_to(OrderState.CANCELLED)
        self.assertEqual(order2.state, OrderState.CANCELLED)

        # Rejection path
        order3 = self.manager.create_order(
            symbol="TCS",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=10.0,
            broker="fyers",
        )
        order3.transition_to(OrderState.REJECTED, error_code=CommonErrorCode.INSUFFICIENT_MARGIN)
        self.assertEqual(order3.state, OrderState.REJECTED)
        self.assertEqual(order3.error_code, CommonErrorCode.INSUFFICIENT_MARGIN)

    def test_b_idempotency_duplicate_prevention(self):
        cid = generate_client_order_id(symbol="TCS", side="BUY", quantity=50, price=3500.0)
        self.assertTrue(verify_client_order_id(cid, symbol="TCS", side="BUY", quantity=50, price=3500.0))
        self.assertFalse(verify_client_order_id(cid, symbol="TCS", side="SELL", quantity=50, price=3500.0))

        order1 = self.manager.create_order(
            symbol="TCS",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=50.0,
            price=3500.0,
            broker="dhan",
        )

        mock_adapter = MagicMock()
        mock_adapter.place_order = AsyncMock(return_value=OrderUpdate(
            client_order_id=order1.client_order_id,
            broker_order_id="B_1",
            state=OrderState.OPEN,
        ))

        async def run():
            await self.manager.submit_order(mock_adapter, order1)
            # Duplicate order submission with same specs within window
            order2 = self.manager.create_order(
                symbol="TCS",
                exchange="NSE",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=50.0,
                price=3500.0,
                broker="dhan",
            )
            with self.assertRaises(DuplicateOrderError):
                await self.manager.submit_order(mock_adapter, order2)

        asyncio.run(run())

    async def test_c_timeout_handling_with_status_query(self):
        mgr = OrderManager(submission_timeout=0.05)

        # Case 1: Timeout occurs, but broker status query confirms order was actually OPEN
        order1 = mgr.create_order(
            symbol="SBIN",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100.0,
            price=800.0,
            broker="dhan",
        )

        async def slow_place_order(_ord):
            await asyncio.sleep(0.2)
            return OrderUpdate(client_order_id=_ord.client_order_id, state=OrderState.OPEN)

        mock_adapter1 = MagicMock()
        mock_adapter1.broker_name = "Dhan"
        mock_adapter1.place_order = slow_place_order
        mock_adapter1.get_order_status = AsyncMock(return_value=OrderUpdate(
            client_order_id=order1.client_order_id,
            broker_order_id="DHAN_REC_999",
            state=OrderState.OPEN,
        ))

        update1 = await mgr.submit_order(mock_adapter1, order1)
        # Order should NOT be assumed failed; it recovered to OPEN
        self.assertEqual(update1.state, OrderState.OPEN)
        self.assertEqual(order1.state, OrderState.OPEN)
        self.assertEqual(order1.broker_order_id, "DHAN_REC_999")

        # Case 2: Timeout occurs and status query also returns unknown
        order2 = mgr.create_order(
            symbol="HDFCBANK",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=200.0,
            price=1650.0,
            broker="angelone",
        )

        mock_adapter2 = MagicMock()
        mock_adapter2.broker_name = "AngelOne"
        mock_adapter2.place_order = slow_place_order
        mock_adapter2.get_order_status = AsyncMock(side_effect=TimeoutError("Status check failed"))

        update2 = await mgr.submit_order(mock_adapter2, order2)
        # P0-03: timeout must now produce UNKNOWN (not TIMEOUT) — state is unconfirmed
        self.assertEqual(update2.state, OrderState.UNKNOWN)
        self.assertEqual(order2.state, OrderState.UNKNOWN)

    def test_d_partial_fill_tracking(self):
        order = Order(
            client_order_id="CID_FILL_TEST",
            symbol="NIFTY",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100.0,
            price=500.0,
        )

        # Fill 1: 30 @ 498.0
        u1 = order.apply_fill(fill_qty=30.0, fill_price=498.0)
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_qty, 30.0)
        self.assertEqual(order.remaining_qty, 70.0)
        self.assertAlmostEqual(order.avg_fill_price, 498.0, places=4)

        # Fill 2: 40 @ 501.0
        # Avg = (30 * 498 + 40 * 501) / 70 = (14940 + 20040) / 70 = 34980 / 70 = 499.7143
        u2 = order.apply_fill(fill_qty=40.0, fill_price=501.0)
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_qty, 70.0)
        self.assertEqual(order.remaining_qty, 30.0)
        self.assertAlmostEqual(order.avg_fill_price, 499.7143, places=4)

        # Fill 3: 30 @ 502.0
        # Avg = (34980 + 30 * 502) / 100 = (34980 + 15060) / 100 = 50040 / 100 = 500.4
        u3 = order.apply_fill(fill_qty=30.0, fill_price=502.0)
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_qty, 100.0)
        self.assertEqual(order.remaining_qty, 0.0)
        self.assertAlmostEqual(order.avg_fill_price, 500.4, places=4)

        # Partial cancellation
        order_cancel = Order(
            client_order_id="CID_PARTIAL_CANCEL",
            symbol="BANKNIFTY",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100.0,
            price=100.0,
        )
        order_cancel.apply_fill(fill_qty=40.0, fill_price=100.0)
        order_cancel.transition_to(OrderState.CANCEL_PENDING)
        order_cancel.transition_to(OrderState.CANCELLED)
        self.assertEqual(order_cancel.state, OrderState.CANCELLED)
        self.assertEqual(order_cancel.filled_qty, 40.0)
        self.assertEqual(order_cancel.remaining_qty, 60.0)

    async def test_e_reconciliation_logic(self):
        mgr = OrderManager()

        # Local order 1: OPEN locally, but broker says FILLED
        ord1 = mgr.create_order(
            symbol="TATAMOTORS",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=50.0,
            price=1000.0,
            broker="fyers",
            client_order_id="ORD_LOC_1",
        )
        ord1.transition_to(OrderState.OPEN, broker_order_id="FY_111")
        mgr._orders[ord1.client_order_id] = ord1
        mgr._broker_to_client_id["FY_111"] = ord1.client_order_id

        # Local order 2: OPEN locally, but broker says CANCELLED
        ord2 = mgr.create_order(
            symbol="WIPRO",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=100.0,
            price=550.0,
            broker="fyers",
            client_order_id="ORD_LOC_2",
        )
        ord2.transition_to(OrderState.OPEN, broker_order_id="FY_222")
        mgr._orders[ord2.client_order_id] = ord2
        mgr._broker_to_client_id["FY_222"] = ord2.client_order_id

        mock_adapter = MagicMock()
        mock_adapter.broker_name = "Fyers"
        mock_adapter.get_open_orders = AsyncMock(return_value=[
            OrderUpdate(
                client_order_id="ORD_LOC_1",
                broker_order_id="FY_111",
                state=OrderState.FILLED,
                filled_qty=50.0,
                remaining_qty=0.0,
                avg_fill_price=1000.0,
            ),
            OrderUpdate(
                client_order_id="",
                broker_order_id="FY_333",
                state=OrderState.OPEN,
                filled_qty=0.0,
                remaining_qty=20.0,
                metadata={"symbol": "ITC", "side": "BUY", "price": 450.0},
            ),
        ])
        mock_adapter.get_order_status = AsyncMock(return_value=OrderUpdate(
            client_order_id="ORD_LOC_2",
            broker_order_id="FY_222",
            state=OrderState.CANCELLED,
            filled_qty=0.0,
            remaining_qty=100.0,
        ))

        summary = await mgr.reconcile_orders(mock_adapter)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["missing_local"], 1)
        self.assertGreaterEqual(summary["discrepancies_resolved"], 1)

        self.assertEqual(ord1.state, OrderState.FILLED)
        self.assertEqual(ord2.state, OrderState.CANCELLED)
        self.assertIsNotNone(mgr.get_order_by_broker_id("FY_333"))

    def test_f_error_normalization(self):
        cases = [
            ("dhan", 101, "Insufficient fund balance in trading account", CommonErrorCode.INSUFFICIENT_MARGIN),
            ("angelone", "AB1004", "RMS: Margin limit exceeded", CommonErrorCode.INSUFFICIENT_MARGIN),
            ("fyers", -17, "Insufficient balance", CommonErrorCode.INSUFFICIENT_MARGIN),
            ("dhan", 202, "Order price is outside current circuit band", CommonErrorCode.INVALID_PRICE),
            ("angelone", "AB2001", "Invalid price / tick size constraint", CommonErrorCode.INVALID_PRICE),
            ("fyers", -25, "Quantity exceeds exchange freeze limit", CommonErrorCode.INVALID_QTY),
            ("dhan", 303, "Exchange is closed. Trading hours are 09:15 to 15:30", CommonErrorCode.MARKET_CLOSED),
            ("angelone", "AB1007", "Market closed for this segment", CommonErrorCode.MARKET_CLOSED),
            ("fyers", -50, "Connection reset by peer / gateway timeout", CommonErrorCode.NETWORK_ERROR),
            ("dhan", 999, "Some unrecognized custom broker internal error", CommonErrorCode.UNKNOWN_ERROR),
        ]

        for broker, code, msg, expected_err in cases:
            norm_err, original_msg = normalize_broker_error(broker, code, msg)
            self.assertEqual(norm_err, expected_err, f"Failed for {broker}: {msg}")
            self.assertEqual(original_msg, msg)


class TestP003OrderStateMachine(unittest.IsolatedAsyncioTestCase):
    """P0-03 — UNKNOWN and SUBMITTED state additions."""

    def test_g_unknown_is_valid_state(self):
        """UNKNOWN is in the enum and has valid outgoing transitions."""
        self.assertIn(OrderState.UNKNOWN, OrderState)
        # UNKNOWN must resolve to any confirmed state
        allowed = VALID_TRANSITIONS[OrderState.UNKNOWN]
        for expected in (
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        ):
            self.assertIn(expected, allowed, f"UNKNOWN must allow transition to {expected}")

    def test_g_submitted_is_valid_state(self):
        """SUBMITTED is in the enum and allows transition to UNKNOWN (timeout path)."""
        self.assertIn(OrderState.SUBMITTED, OrderState)
        allowed = VALID_TRANSITIONS[OrderState.SUBMITTED]
        self.assertIn(OrderState.UNKNOWN, allowed, "SUBMITTED must allow UNKNOWN (timeout)")
        self.assertIn(OrderState.OPEN, allowed)
        self.assertIn(OrderState.REJECTED, allowed)

    def test_g_pending_to_unknown_direct(self):
        """PENDING may transition directly to UNKNOWN (timeout before SUBMITTED recorded)."""
        order = Order(
            client_order_id="CID_P2U",
            symbol="NIFTY",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=50.0,
            price=21000.0,
        )
        self.assertTrue(order.can_transition_to(OrderState.UNKNOWN))
        order.transition_to(OrderState.UNKNOWN)
        self.assertEqual(order.state, OrderState.UNKNOWN)

    async def test_h_unknown_resolves_via_reconciliation(self):
        """An UNKNOWN order becomes OPEN when the broker reports OPEN on reconciliation."""
        mgr = OrderManager(submission_timeout=0.05)

        order = mgr.create_order(
            symbol="BANKNIFTY",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=50.0,
            price=45000.0,
            broker="dhan",
            client_order_id="CID_RECON_001",
        )
        # Simulate timeout → UNKNOWN
        order.transition_to(OrderState.UNKNOWN)
        order.broker_order_id = "DHAN_BK_001"
        mgr._orders[order.client_order_id] = order
        mgr._broker_to_client_id["DHAN_BK_001"] = order.client_order_id

        mock_adapter = MagicMock()
        mock_adapter.broker_name = "dhan"
        mock_adapter.get_open_orders = AsyncMock(return_value=[
            OrderUpdate(
                client_order_id="CID_RECON_001",
                broker_order_id="DHAN_BK_001",
                state=OrderState.OPEN,
            )
        ])
        mock_adapter.get_order_status = AsyncMock(return_value=OrderUpdate(
            client_order_id="CID_RECON_001",
            broker_order_id="DHAN_BK_001",
            state=OrderState.OPEN,
        ))

        summary = await mgr.reconcile_orders(mock_adapter)
        self.assertEqual(order.state, OrderState.OPEN,
            "UNKNOWN order must resolve to OPEN after reconciliation")
        self.assertGreaterEqual(summary["discrepancies_resolved"], 1)

    async def test_h_unknown_is_in_open_orders_and_reconcile_scope(self):
        """UNKNOWN orders appear in get_open_orders() and are included in reconcile scan."""
        mgr = OrderManager()
        order = mgr.create_order(
            symbol="NIFTY", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=25.0, price=21000.0, broker="angelone",
            client_order_id="CID_OPEN_UNKNOWN",
        )
        order.transition_to(OrderState.SUBMITTED)
        order.transition_to(OrderState.UNKNOWN)
        mgr._orders[order.client_order_id] = order

        open_orders = mgr.get_open_orders()
        cids = [o.client_order_id for o in open_orders]
        self.assertIn("CID_OPEN_UNKNOWN", cids,
            "UNKNOWN orders must appear in get_open_orders()")

    async def test_i_idempotency_blocks_resubmission_after_unknown(self):
        """
        After a timeout → UNKNOWN, submitting an identical order must raise
        DuplicateOrderError — the idempotency key is still active.
        This prevents accidental double-fill when the broker actually processed the first order.
        """
        mgr = OrderManager(submission_timeout=0.05, idempotency_window=60.0)

        order1 = mgr.create_order(
            symbol="RELIANCE", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10.0, price=2900.0, broker="dhan",
        )

        async def slow_place(_ord):
            await asyncio.sleep(0.3)
            return OrderUpdate(client_order_id=_ord.client_order_id, state=OrderState.OPEN)

        mock_adapter = MagicMock()
        mock_adapter.broker_name = "dhan"
        mock_adapter.place_order = slow_place
        mock_adapter.get_order_status = AsyncMock(side_effect=TimeoutError("status unavailable"))

        update = await mgr.submit_order(mock_adapter, order1)
        self.assertEqual(update.state, OrderState.UNKNOWN)

        # Attempt an identical order — must be rejected
        order2 = mgr.create_order(
            symbol="RELIANCE", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10.0, price=2900.0, broker="dhan",
        )
        with self.assertRaises(DuplicateOrderError,
                               msg="Duplicate submission after UNKNOWN must raise DuplicateOrderError"):
            await mgr.submit_order(mock_adapter, order2)

    async def test_j_audit_logging_on_transitions(self):
        """
        submit_order must call audit_logger for PENDING→SUBMITTED and SUBMITTED→UNKNOWN,
        and handle_order_update must log each confirmed state change.
        """
        # Use an isolated AuditLogger so we don't read global singleton state
        al = AuditLogger(db_path=None)

        mgr = OrderManager(submission_timeout=0.05)

        order = mgr.create_order(
            symbol="NIFTY", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=25.0, price=21000.0, broker="dhan",
            client_order_id="CID_AUDIT_001",
        )

        async def slow_place(_ord):
            await asyncio.sleep(0.3)
            return OrderUpdate(client_order_id=_ord.client_order_id, state=OrderState.OPEN)

        mock_adapter = MagicMock()
        mock_adapter.broker_name = "dhan"
        mock_adapter.place_order = slow_place
        mock_adapter.get_order_status = AsyncMock(side_effect=Exception("unreachable"))

        # Patch the global audit_logger used inside order_manager
        with patch("adapters.order_manager._get_audit_logger", return_value=al):
            await mgr.submit_order(mock_adapter, order)

        entries = al.get_all_entries()
        event_types = [e["event_type"] for e in entries]
        self.assertIn("order_transition", event_types,
            "submit_order must produce at least one order_transition audit entry")

        transitions = [e for e in entries if e["event_type"] == "order_transition"]
        states_logged = [(e["old_state"], e["new_state"]) for e in transitions]

        self.assertIn(
            (OrderState.PENDING.value, OrderState.SUBMITTED.value),
            states_logged,
            "PENDING→SUBMITTED must be logged",
        )
        self.assertIn(
            (OrderState.SUBMITTED.value, OrderState.UNKNOWN.value),
            states_logged,
            "SUBMITTED→UNKNOWN must be logged on timeout",
        )

    async def test_j_audit_logging_on_fill(self):
        """handle_order_update must log fill transitions to the audit trail."""
        al = AuditLogger(db_path=None)
        mgr = OrderManager()

        order = Order(
            client_order_id="CID_FILL_AUDIT",
            symbol="BANKNIFTY",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=50.0,
            price=45000.0,
            broker="angelone",
            state=OrderState.OPEN,
        )
        mgr._orders[order.client_order_id] = order

        with patch("adapters.order_manager._get_audit_logger", return_value=al):
            mgr.handle_order_update(OrderUpdate(
                client_order_id="CID_FILL_AUDIT",
                broker_order_id="AO_9999",
                state=OrderState.PARTIALLY_FILLED,
                last_fill_qty=25.0,
                last_fill_price=45100.0,
            ))

        entries = [e for e in al.get_all_entries() if e["event_type"] == "order_transition"]
        self.assertTrue(len(entries) >= 1, "Fill must produce an audit entry")
        fill_entry = entries[0]
        self.assertAlmostEqual(fill_entry.get("fill_qty", 0), 25.0, places=1)
        self.assertAlmostEqual(fill_entry.get("fill_price", 0), 45100.0, places=1)

    async def test_k_submitted_transition_on_submit_order(self):
        """submit_order must transition the order through SUBMITTED before place_order is awaited."""
        mgr = OrderManager(submission_timeout=5.0)
        observed_states = []

        async def capture_state(ord_):
            observed_states.append(ord_.state)
            return OrderUpdate(
                client_order_id=ord_.client_order_id,
                broker_order_id="BK_CAP_001",
                state=OrderState.OPEN,
            )

        mock_adapter = MagicMock()
        mock_adapter.place_order = capture_state

        order = mgr.create_order(
            symbol="SBIN", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=100.0, broker="fyers",
        )

        with patch("adapters.order_manager._get_audit_logger", return_value=AuditLogger(db_path=None)):
            result = await mgr.submit_order(mock_adapter, order)

        self.assertIn(OrderState.SUBMITTED, observed_states,
            "Order must be in SUBMITTED state when place_order is called")
        self.assertEqual(result.state, OrderState.OPEN)
        self.assertEqual(order.state, OrderState.OPEN)


if __name__ == "__main__":
    unittest.main()