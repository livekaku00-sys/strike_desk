"""
P0-4 — Order-State Machine Reliability

Coverage matrix (13 tests across 5 categories):
  01  Terminal-state immutability via transition_to — FILLED blocks transition
  02  Terminal-state immutability via transition_to — CANCELLED blocks transition
  03  Terminal-state immutability via transition_to — REJECTED blocks transition
  04  Terminal-state immutability via transition_to — EXPIRED blocks transition
  05  Non-terminal invalid transition still logs-and-proceeds (existing behaviour preserved)
  06  apply_fill on FILLED order returns snapshot, order unchanged
  07  apply_fill on CANCELLED order returns snapshot, order unchanged
  08  handle_order_update drops last_fill_qty path for FILLED order
  09  handle_order_update drops filled_qty-delta path for FILLED order
  10  handle_order_update drops fill for CANCELLED order
  11  Overfill clamped to remaining qty; avg_price and state correct
  12  Zero-remaining overfill returns snapshot; order unchanged
  13  TERMINAL_STATES constant exported from models.order
"""

import unittest

from models.order import (
    Order,
    OrderSide,
    OrderState,
    OrderType,
    TERMINAL_STATES,
)
from adapters.order_manager import OrderManager
from models.order import OrderUpdate


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_filled_order(cid: str = "CID_FILLED") -> Order:
    o = Order(
        client_order_id=cid,
        symbol="NIFTY",
        exchange="NSE",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100.0,
        price=500.0,
    )
    o.apply_fill(fill_qty=100.0, fill_price=500.0)
    assert o.state == OrderState.FILLED
    return o


def _make_cancelled_order(cid: str = "CID_CANCELLED") -> Order:
    o = Order(
        client_order_id=cid,
        symbol="BANKNIFTY",
        exchange="NSE",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50.0,
        price=45000.0,
    )
    o.transition_to(OrderState.OPEN)
    o.transition_to(OrderState.CANCEL_PENDING)
    o.transition_to(OrderState.CANCELLED)
    assert o.state == OrderState.CANCELLED
    return o


# ── 01–04: Terminal-state immutability via transition_to ──────────────────────

class TestTerminalStateImmutabilityTransition(unittest.TestCase):

    def test_01_filled_blocks_transition_to_open(self):
        """FILLED → OPEN must be silently blocked: state unchanged, no exception."""
        order = _make_filled_order("CID_T01")
        prev_filled = order.filled_qty
        result = order.transition_to(OrderState.OPEN)
        self.assertEqual(order.state, OrderState.FILLED,
                         "FILLED order must not transition to OPEN")
        self.assertEqual(order.filled_qty, prev_filled,
                         "filled_qty must be unchanged")
        # The returned snapshot carries the current (FILLED) state
        self.assertEqual(result.state, OrderState.FILLED)

    def test_02_cancelled_blocks_transition_to_partially_filled(self):
        """CANCELLED → PARTIALLY_FILLED must be silently blocked."""
        order = _make_cancelled_order("CID_T02")
        prev_qty = order.filled_qty
        result = order.transition_to(OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.state, OrderState.CANCELLED)
        self.assertEqual(order.filled_qty, prev_qty)
        self.assertEqual(result.state, OrderState.CANCELLED)

    def test_03_rejected_blocks_transition_to_submitted(self):
        """REJECTED → SUBMITTED must be silently blocked."""
        order = Order(
            client_order_id="CID_T03",
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=10.0,
        )
        order.transition_to(OrderState.REJECTED)
        result = order.transition_to(OrderState.SUBMITTED)
        self.assertEqual(order.state, OrderState.REJECTED)
        self.assertEqual(result.state, OrderState.REJECTED)

    def test_04_expired_blocks_transition_to_open(self):
        """EXPIRED → OPEN must be silently blocked."""
        order = Order(
            client_order_id="CID_T04",
            symbol="TCS",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=20.0,
            price=3900.0,
        )
        order.transition_to(OrderState.OPEN)
        order.transition_to(OrderState.EXPIRED)
        result = order.transition_to(OrderState.OPEN)
        self.assertEqual(order.state, OrderState.EXPIRED)
        self.assertEqual(result.state, OrderState.EXPIRED)


# ── 05: Non-terminal invalid transition still logs-and-proceeds ───────────────

class TestNonTerminalInvalidTransitionPreserved(unittest.TestCase):

    def test_05_non_terminal_invalid_still_applies(self):
        """
        SUBMITTED → CANCEL_PENDING is not in VALID_TRANSITIONS[SUBMITTED].
        The pre-S05 behaviour (log-and-proceed) must be preserved for non-terminal
        states so out-of-order broker updates do not desync the book.
        """
        order = Order(
            client_order_id="CID_T05",
            symbol="INFY",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=30.0,
            price=1500.0,
        )
        order.transition_to(OrderState.SUBMITTED)
        # SUBMITTED → CANCEL_PENDING is not a permitted transition
        from models.order import VALID_TRANSITIONS
        self.assertNotIn(
            OrderState.CANCEL_PENDING,
            VALID_TRANSITIONS[OrderState.SUBMITTED],
        )
        # Must proceed (existing desync-prevention behaviour)
        order.transition_to(OrderState.CANCEL_PENDING)
        self.assertEqual(order.state, OrderState.CANCEL_PENDING,
                         "Non-terminal invalid transitions must still be applied")


# ── 06–07: apply_fill on terminal-state orders ───────────────────────────────

class TestApplyFillOnTerminalOrder(unittest.TestCase):

    def test_06_apply_fill_on_filled_order_is_ignored(self):
        """
        A fill arriving on an already-FILLED order must not change filled_qty,
        avg_fill_price, remaining_qty, or state.
        """
        order = _make_filled_order("CID_T06")
        before_filled_qty = order.filled_qty
        before_avg = order.avg_fill_price
        before_remaining = order.remaining_qty
        history_len = len(order.state_history)

        result = order.apply_fill(fill_qty=50.0, fill_price=510.0)

        self.assertEqual(order.state, OrderState.FILLED,
                         "State must remain FILLED")
        self.assertEqual(order.filled_qty, before_filled_qty,
                         "filled_qty must be unchanged")
        self.assertEqual(order.avg_fill_price, before_avg,
                         "avg_fill_price must be unchanged")
        self.assertEqual(order.remaining_qty, before_remaining)
        self.assertEqual(len(order.state_history), history_len,
                         "No new history entry must be appended for a no-op fill")
        self.assertEqual(result.last_fill_qty, 0.0,
                         "Returned snapshot must carry last_fill_qty=0")

    def test_07_apply_fill_on_cancelled_order_is_ignored(self):
        """A fill on a CANCELLED order must leave the order unchanged."""
        order = _make_cancelled_order("CID_T07")
        before_filled_qty = order.filled_qty
        history_len = len(order.state_history)

        result = order.apply_fill(fill_qty=20.0, fill_price=45100.0)

        self.assertEqual(order.state, OrderState.CANCELLED)
        self.assertEqual(order.filled_qty, before_filled_qty)
        self.assertEqual(len(order.state_history), history_len)
        self.assertEqual(result.last_fill_qty, 0.0)


# ── 08–10: handle_order_update drops fills for terminal-state orders ──────────

class TestHandleOrderUpdateTerminalGuard(unittest.TestCase):

    def setUp(self):
        self.mgr = OrderManager()

    def _register(self, order: Order) -> None:
        self.mgr._orders[order.client_order_id] = order

    def test_08_last_fill_qty_path_dropped_for_filled_order(self):
        """
        handle_order_update with last_fill_qty > 0 must not modify a FILLED order.
        """
        order = _make_filled_order("CID_T08")
        self._register(order)
        before_filled_qty = order.filled_qty

        self.mgr.handle_order_update(OrderUpdate(
            client_order_id="CID_T08",
            broker_order_id="BROKER_X",
            state=OrderState.FILLED,
            last_fill_qty=50.0,
            last_fill_price=502.0,
        ))

        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_qty, before_filled_qty,
                         "filled_qty must be unchanged after duplicate fill update")

    def test_09_filled_qty_delta_path_dropped_for_filled_order(self):
        """
        handle_order_update with filled_qty > order.filled_qty (delta path) must
        not modify a FILLED order.
        """
        order = _make_filled_order("CID_T09")
        self._register(order)
        before_filled_qty = order.filled_qty

        self.mgr.handle_order_update(OrderUpdate(
            client_order_id="CID_T09",
            broker_order_id="BROKER_Y",
            state=OrderState.FILLED,
            filled_qty=order.filled_qty + 30.0,  # delta > 0
            avg_fill_price=500.0,
        ))

        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_qty, before_filled_qty)

    def test_10_fill_dropped_for_cancelled_order(self):
        """A fill update on a CANCELLED order must leave it unchanged."""
        order = _make_cancelled_order("CID_T10")
        self._register(order)

        self.mgr.handle_order_update(OrderUpdate(
            client_order_id="CID_T10",
            broker_order_id="BROKER_Z",
            state=OrderState.PARTIALLY_FILLED,
            last_fill_qty=10.0,
            last_fill_price=45050.0,
        ))

        self.assertEqual(order.state, OrderState.CANCELLED,
                         "CANCELLED order must remain CANCELLED after delayed fill")
        self.assertEqual(order.filled_qty, 0.0)


# ── 11–12: Overfill protection in apply_fill ─────────────────────────────────

class TestApplyFillOverfillProtection(unittest.TestCase):

    def test_11_overfill_clamped_to_remaining_qty(self):
        """
        A fill_qty that exceeds remaining_qty is clamped: the order reaches
        FILLED with filled_qty == quantity, and avg_fill_price is correct.
        """
        order = Order(
            client_order_id="CID_T11",
            symbol="WIPRO",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100.0,
            price=400.0,
        )
        # Partial fill: 60 @ 400
        order.apply_fill(fill_qty=60.0, fill_price=400.0)
        self.assertEqual(order.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_qty, 60.0)

        # Overfill attempt: remaining is 40, but we send 80
        result = order.apply_fill(fill_qty=80.0, fill_price=402.0)

        self.assertEqual(order.state, OrderState.FILLED,
                         "Order must be FILLED after clamped overfill")
        self.assertEqual(order.filled_qty, 100.0,
                         "filled_qty must equal order.quantity exactly")
        self.assertEqual(order.remaining_qty, 0.0)
        # avg_fill_price: (60*400 + 40*402) / 100 = (24000 + 16080) / 100 = 400.80
        self.assertAlmostEqual(order.avg_fill_price, 400.80, places=4)
        # Snapshot shows the actual applied fill_qty (clamped to 40)
        self.assertAlmostEqual(result.last_fill_qty, 40.0, places=4)

    def test_12_zero_remaining_overfill_returns_snapshot_unchanged(self):
        """
        If the order is fully filled and remaining_qty == 0, an overfill attempt
        returns a snapshot with no state change (equivalent to test_06 but via
        the overfill path rather than the terminal state path — still no-op).
        """
        order = _make_filled_order("CID_T12")
        # The terminal-state guard fires before the overfill guard for FILLED.
        before_filled_qty = order.filled_qty

        result = order.apply_fill(fill_qty=1.0, fill_price=501.0)

        self.assertEqual(order.state, OrderState.FILLED)
        self.assertEqual(order.filled_qty, before_filled_qty)
        self.assertEqual(result.last_fill_qty, 0.0)


# ── 13: TERMINAL_STATES exported correctly ───────────────────────────────────

class TestTerminalStatesConstant(unittest.TestCase):

    def test_13_terminal_states_constant_exported(self):
        """TERMINAL_STATES must be importable from models.order and contain the four terminal states."""
        from models.order import TERMINAL_STATES
        for state in (
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        ):
            self.assertIn(state, TERMINAL_STATES,
                          f"{state.value} must be in TERMINAL_STATES")
        # Non-terminal states must not be in TERMINAL_STATES
        for state in (
            OrderState.PENDING,
            OrderState.SUBMITTED,
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.UNKNOWN,
        ):
            self.assertNotIn(state, TERMINAL_STATES,
                             f"{state.value} must NOT be in TERMINAL_STATES")


if __name__ == "__main__":
    unittest.main(verbosity=2)
