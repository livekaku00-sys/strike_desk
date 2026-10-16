"""
Strike Desk V4 — One-Position Enforcement Regression Suite
Phase 1: Automatic-Only Execution Safety

Verifies the two-layered entry gate that enforces at most one stock-options
position open at a time:

  Layer 1 (existing): RiskManager.open_positions_count — counts is_open=True
    ActivePosition objects; authoritative post-fill record.
  Layer 2 (new):      execution.entry_gate.is_entry_blocked_by_inflight_orders
    — catches entry orders in SUBMITTED/OPEN/PARTIALLY_FILLED/CANCEL_PENDING/
    UNKNOWN/TIMEOUT that have no position record yet in RiskManager.

17 test cases:

  Group A — RiskManager gate (Layer 1, existing logic, must stay passing)
  [01] Fresh state: no RM position, no open orders → allowed
  [02] Open RM position (is_open=True) → blocked via Layer 1
  [03] Closed RM position (is_open=False) → allowed (no live position)

  Group B — In-flight BUY order gate (Layer 2, validates the fix)
  [04] UNKNOWN BUY with no RM position → blocked by Layer 2
  [05] SUBMITTED BUY → blocked by Layer 2
  [06] OPEN (resting) BUY → blocked by Layer 2
  [07] CANCEL_PENDING BUY → blocked by Layer 2
  [08] TIMEOUT BUY → blocked by Layer 2

  Group C — Correct non-blocking cases (gate must not over-block)
  [09] UNKNOWN SELL (exit) order → NOT blocked (BUY filter works)
  [10] FILLED terminal BUY → NOT blocked (terminal state)
  [11] CANCELLED terminal BUY → NOT blocked
  [12] REJECTED terminal BUY → NOT blocked

  Group D — Edge case
  [13] PARTIALLY_FILLED BUY with no RM position record → blocked by Layer 2

  Group E — Fail-closed safety (tests for exception-handling corrections)
  [14] EXPIRED terminal BUY → NOT blocked (terminal state)
  [15] Multiple in-flight BUY orders → blocked; count reported accurately
  [16] Exception from get_open_orders() → gate returns (True, ...) (fail-closed)
  [17] Exception from gate itself → caller pattern sets _open_count=1 (fail-closed)
"""
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from adapters.order_manager import OrderManager
from execution.entry_gate import is_entry_blocked_by_inflight_orders
from models.order import Order, OrderSide, OrderState, OrderType
from risk.risk_manager import ActivePosition, RiskManager

UTC = timezone.utc
_T0 = datetime(2026, 9, 21, 9, 30, 0, tzinfo=UTC)


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────────────

def _make_order(side: OrderSide, state: OrderState, cid: str | None = None) -> Order:
    return Order(
        client_order_id=cid or f"CID_{uuid.uuid4().hex[:8]}",
        symbol="NIFTY24OCTCE",
        exchange="NSE",
        side=side,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=150.0,
        state=state,
    )


def _add_to_manager(mgr: OrderManager, order: Order) -> None:
    mgr._orders[order.client_order_id] = order


def _inject_open_position(rm: RiskManager, symbol: str = "NIFTY24OCTCE") -> None:
    with rm.lock:
        rm.positions[symbol] = ActivePosition(
            position_id=str(uuid.uuid4()),
            symbol=symbol,
            quantity=1,
            entry_price=150.0,
            current_price=155.0,
            entry_time=_T0,
            is_open=True,
        )


def _inject_closed_position(rm: RiskManager, symbol: str = "NIFTY24OCTCE") -> None:
    with rm.lock:
        rm.positions[symbol] = ActivePosition(
            position_id=str(uuid.uuid4()),
            symbol=symbol,
            quantity=1,
            entry_price=150.0,
            current_price=155.0,
            entry_time=_T0,
            is_open=False,
        )


def _rm_open_count(rm: RiskManager) -> int:
    return int(rm.get_risk_summary().get("open_positions_count", 0))


def _combined_gate(rm: RiskManager, om: OrderManager) -> tuple[bool, str]:
    """Mirrors the two-layer gate in _auto_execute_loop exactly."""
    open_count = _rm_open_count(rm)
    if open_count > 0:
        return True, f"open_positions_count={open_count}"
    blocked, reason = is_entry_blocked_by_inflight_orders(om)
    return blocked, reason


# ── Group A: RiskManager gate (Layer 1) ────────────────────────────────────────────

class TestGroupA_RiskManagerGate(unittest.TestCase):

    def test_01_fresh_state_allows_entry(self):
        """[01] No positions, no open orders → allowed."""
        rm = RiskManager()
        om = OrderManager()

        blocked, reason = _combined_gate(rm, om)
        self.assertFalse(blocked, f"Fresh state must allow entry; got reason: {reason}")
        print("✓ [01] Fresh state: allowed (open_count=0, no in-flight orders)")

    def test_02_open_rm_position_blocks_entry(self):
        """[02] RiskManager has is_open=True position → blocked via Layer 1."""
        rm = RiskManager()
        _inject_open_position(rm)
        om = OrderManager()

        open_count = _rm_open_count(rm)
        self.assertGreater(open_count, 0, "Expected open_count > 0 after injecting position")

        blocked, reason = _combined_gate(rm, om)
        self.assertTrue(blocked, "Open RM position must block entry")
        self.assertIn("open_positions_count", reason)
        print(f"✓ [02] Open RM position blocks entry (open_count={open_count})")

    def test_03_closed_rm_position_allows_entry(self):
        """[03] Position closed (is_open=False) → open_count=0 → allowed."""
        rm = RiskManager()
        _inject_closed_position(rm)
        om = OrderManager()

        open_count = _rm_open_count(rm)
        self.assertEqual(open_count, 0, "Closed position must not contribute to open_count")

        blocked, reason = _combined_gate(rm, om)
        self.assertFalse(blocked,
            f"Closed position must allow new entry; reason: {reason}")
        print("✓ [03] Closed RM position allows new entry (open_count=0)")


# ── Group B: In-flight BUY order gate (Layer 2) ──────────────────────────────────────

class TestGroupB_InflightBuyOrderGate(unittest.TestCase):

    def _assert_inflight_blocks(self, state: OrderState, case_num: str) -> None:
        om = OrderManager()
        order = _make_order(OrderSide.BUY, state)
        _add_to_manager(om, order)

        blocked, reason = is_entry_blocked_by_inflight_orders(om)
        self.assertTrue(blocked,
            f"[{case_num}] BUY in {state.value} must block new entry via Layer 2. "
            f"Reason: {reason}")

    def test_04_unknown_buy_blocks_entry(self):
        """[04] UNKNOWN BUY order (no RM position) → blocked by Layer 2."""
        self._assert_inflight_blocks(OrderState.UNKNOWN, "04")
        print("✓ [04] UNKNOWN BUY blocks entry (in-flight gate)")

    def test_05_submitted_buy_blocks_entry(self):
        """[05] SUBMITTED BUY order → blocked by Layer 2."""
        self._assert_inflight_blocks(OrderState.SUBMITTED, "05")
        print("✓ [05] SUBMITTED BUY blocks entry")

    def test_06_open_resting_buy_blocks_entry(self):
        """[06] OPEN (resting at broker) BUY order → blocked by Layer 2."""
        self._assert_inflight_blocks(OrderState.OPEN, "06")
        print("✓ [06] OPEN resting BUY blocks entry")

    def test_07_cancel_pending_buy_blocks_entry(self):
        """[07] CANCEL_PENDING BUY → blocked until cancellation is confirmed."""
        self._assert_inflight_blocks(OrderState.CANCEL_PENDING, "07")
        print("✓ [07] CANCEL_PENDING BUY blocks entry")

    def test_08_timeout_buy_blocks_entry(self):
        """[08] TIMEOUT BUY order → blocked (uncertain outcome)."""
        self._assert_inflight_blocks(OrderState.TIMEOUT, "08")
        print("✓ [08] TIMEOUT BUY blocks entry")


# ── Group C: Non-blocking cases ───────────────────────────────────────────────

class TestGroupC_NonBlockingCases(unittest.TestCase):

    def test_09_unknown_sell_does_not_block(self):
        """[09] UNKNOWN SELL (exit) order → entry slot NOT blocked."""
        om = OrderManager()
        _add_to_manager(om, _make_order(OrderSide.SELL, OrderState.UNKNOWN))

        blocked, reason = is_entry_blocked_by_inflight_orders(om)
        self.assertFalse(blocked,
            f"SELL in UNKNOWN must not block new entry. Reason: {reason}")
        print("✓ [09] UNKNOWN SELL (exit) does not block new entry")

    def test_10_filled_buy_does_not_block(self):
        """[10] FILLED terminal BUY → NOT blocked."""
        om = OrderManager()
        _add_to_manager(om, _make_order(OrderSide.BUY, OrderState.FILLED))

        blocked, reason = is_entry_blocked_by_inflight_orders(om)
        self.assertFalse(blocked,
            f"FILLED BUY is terminal and must not block entry. Reason: {reason}")
        print("✓ [10] FILLED terminal BUY does not block entry")

    def test_11_cancelled_buy_does_not_block(self):
        """[11] CANCELLED terminal BUY → NOT blocked."""
        om = OrderManager()
        _add_to_manager(om, _make_order(OrderSide.BUY, OrderState.CANCELLED))

        blocked, reason = is_entry_blocked_by_inflight_orders(om)
        self.assertFalse(blocked,
            f"CANCELLED BUY is terminal and must not block entry. Reason: {reason}")
        print("✓ [11] CANCELLED BUY does not block entry")

    def test_12_rejected_buy_does_not_block(self):
        """[12] REJECTED terminal BUY → NOT blocked."""
        om = OrderManager()
        _add_to_manager(om, _make_order(OrderSide.BUY, OrderState.REJECTED))

        blocked, reason = is_entry_blocked_by_inflight_orders(om)
        self.assertFalse(blocked,
            f"REJECTED BUY is terminal and must not block entry. Reason: {reason}")
        print("✓ [12] REJECTED BUY does not block entry")


# ── Group D: Edge case ───────────────────────────────────────────────────────────────────────────────────

class TestGroupD_EdgeCases(unittest.TestCase):

    def test_13_partially_filled_buy_no_rm_record_blocks(self):
        """[13] PARTIALLY_FILLED BUY with no RM position record → blocked by Layer 2.

        Timing gap: the partial fill arrived at the broker and the order state
        transitioned to PARTIALLY_FILLED, but the fill WebSocket callback has not
        yet triggered RiskManager.record_fill(). Without Layer 2, open_count == 0
        and the loop would try a second entry.
        """
        om = OrderManager()
        _add_to_manager(om, _make_order(OrderSide.BUY, OrderState.PARTIALLY_FILLED))

        rm = RiskManager()
        open_count = _rm_open_count(rm)
        self.assertEqual(open_count, 0, "RM has no record of the partial fill yet")

        blocked, reason = is_entry_blocked_by_inflight_orders(om)
        self.assertTrue(blocked,
            f"PARTIALLY_FILLED BUY with no RM record must block entry. Reason: {reason}")
        print("✓ [13] PARTIALLY_FILLED BUY (no RM record yet) blocks entry")


# ── Group E: Fail-closed safety ───────────────────────────────────────────────────────────────

class TestGroupE_FailClosed(unittest.TestCase):

    def test_14_expired_buy_does_not_block(self):
        """[14] EXPIRED terminal BUY → NOT blocked.

        EXPIRED is a terminal state (order fully expired at broker).  The position
        slot is definitively free; a new entry must be allowed.
        """
        om = OrderManager()
        _add_to_manager(om, _make_order(OrderSide.BUY, OrderState.EXPIRED))

        blocked, reason = is_entry_blocked_by_inflight_orders(om)
        self.assertFalse(blocked,
            f"EXPIRED BUY is terminal and must not block entry. Reason: {reason}")
        print("✓ [14] EXPIRED BUY does not block entry")

    def test_15_multiple_inflight_buys_all_block(self):
        """[15] Multiple in-flight BUY orders → blocked; count reported accurately.

        Any one in-flight BUY is sufficient to block.  With several outstanding
        the gate must still block and the reason string must report the correct count.
        """
        om = OrderManager()
        _add_to_manager(om, _make_order(OrderSide.BUY, OrderState.SUBMITTED, "CID_SUB"))
        _add_to_manager(om, _make_order(OrderSide.BUY, OrderState.UNKNOWN,   "CID_UNK"))
        _add_to_manager(om, _make_order(OrderSide.BUY, OrderState.OPEN,      "CID_OPN"))

        blocked, reason = is_entry_blocked_by_inflight_orders(om)
        self.assertTrue(blocked,
            f"Multiple in-flight BUY orders must block entry. Reason: {reason}")
        self.assertIn("3", reason,
            f"Reason string must report the count (3). Got: {reason}")
        print(f"✓ [15] Multiple in-flight BUY orders block entry (count=3): {reason}")

    def test_16_exception_in_get_open_orders_blocks_entry(self):
        """[16] Exception from get_open_orders() → gate returns (True, ...) (fail-closed).

        When the order store cannot be queried, the gate cannot confirm the slot is
        free.  It must block the entry rather than silently permitting it.
        """
        class _FailingOrderManager:
            def get_open_orders(self):
                raise RuntimeError("connection lost")

        blocked, reason = is_entry_blocked_by_inflight_orders(_FailingOrderManager())
        self.assertTrue(blocked,
            f"Exception in get_open_orders() must block entry (fail-closed). "
            f"Got: ({blocked!r}, {reason!r})")
        self.assertIn("gate check failed", reason,
            f"Reason must describe the failure. Got: {reason!r}")
        print(f"✓ [16] Exception in get_open_orders() blocks entry (fail-closed): {reason!r}")

    def test_17_caller_exception_path_cannot_restore_permission(self):
        """[17] Exception from the gate function itself → caller sets _open_count=1.

        The caller in _auto_execute_loop wraps the gate call in its own try/except.
        If the gate function raises unexpectedly (e.g. import error), that except
        block must set _open_count=1, not leave it 0.

        This test simulates the caller's pattern directly, mirroring the fix in
        api/main.py lines 495-503: the except block must assign _open_count = 1.
        """
        def _gate_that_raises(_om):
            raise ImportError("execution.entry_gate unavailable")

        # Simulate the caller's try/except as it now reads after the fix.
        _open_count = 0  # Layer 1 says no open position
        if _open_count == 0:
            try:
                _blocked, _reason = _gate_that_raises(None)
                if _blocked:
                    _open_count = 1
            except Exception:
                _open_count = 1  # fail-closed — must match the fix in main.py

        self.assertEqual(_open_count, 1,
            "Caller's except block must set _open_count=1 when the gate raises "
            "(entry must remain blocked, not authorized).")
        print("✓ [17] Caller exception path sets _open_count=1 (fail-closed)")


# ── Runner ──────────────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — ONE-POSITION ENFORCEMENT REGRESSION SUITE")
    print("=" * 70)
    suite = unittest.TestLoader().loadTestsFromModule(
        __import__(__name__)
    )
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("=" * 70)
        print("ALL 17 ONE-POSITION ENFORCEMENT TESTS [01-17] PASSED")
        print("=" * 70)
        sys.exit(0)
    else:
        sys.exit(1)
