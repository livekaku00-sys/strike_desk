"""
Risk pipeline wiring tests.

Verifies that risk_manager.record_fill() and circuit_breaker.on_trade_completed()
are called with the correct arguments after every successful order placement.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient
from api.main import create_app
from api.routes.auth import require_operator
from api.routes.trading import execution_mode, get_paper_broker, EXEC_PAPER
from api.dependencies import get_broker_adapters
from risk.order_authorizer import AuthorizationResult
from models.order import Order


def _authorized(order, **_kwargs):
    """Stub that approves every order without touching the circuit breaker."""
    return AuthorizationResult(
        is_authorized=True,
        client_order_id=order.client_order_id,
        symbol=order.symbol,
        side=order.side.value if hasattr(order.side, "value") else str(order.side),
        quantity=float(order.quantity),
        estimated_value=float(order.price or 0) * float(order.quantity),
        auth_token="TEST_AUTH_TOKEN",
    )


class TestRiskPipelineWiring(unittest.TestCase):
    """
    After a successful fill the route must call:
      1. risk_manager.record_fill(order_id, symbol, filled_qty, fill_price, is_complete=True)
      2. circuit_breaker.on_trade_completed(Trade(...))

    Both for paper and live modes. These tests cover paper mode only (live
    would require a real broker adapter) but the code path is identical.
    """

    def setUp(self):
        self.app = create_app()
        self.app.dependency_overrides[require_operator] = lambda: {"sub": "op", "role": "operator"}
        # Empty adapter map → routing_adapter=None → instrument resolution skipped.
        # Paper mode uses get_paper_broker() directly and never touches this dict.
        self.app.dependency_overrides[get_broker_adapters] = lambda: {}
        self.client = TestClient(self.app)

        self.pb = get_paper_broker()
        self.pb.reset()
        self._saved_mode = execution_mode.get()
        self._saved_tick = self.pb._tick_provider
        execution_mode.set(EXEC_PAPER)
        self.pb._tick_provider = None
        self.pb._ltp_provider = lambda _: 22000.0

    def tearDown(self):
        self.pb._tick_provider = self._saved_tick
        execution_mode.set(self._saved_mode)
        self.pb.reset()
        self.app.dependency_overrides.clear()

    def _patch_stack(self, mock_rm, mock_cb):
        """
        Common patch context: live feed gate suppressed, kill-switch cleared,
        order authorizer bypassed so tests focus only on risk pipeline wiring.
        """
        mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
        mock_cb.can_trade.return_value = (True, None)
        return [
            patch("api.routes.trading.require_live_feed", return_value=None),
            patch("risk.order_authorizer.order_authorizer.authorize_order",
                  side_effect=_authorized),
        ]

    # ------------------------------------------------------------------
    # record_fill
    # ------------------------------------------------------------------

    def test_record_fill_called_after_successful_paper_fill(self):
        """risk_manager.record_fill gets the correct symbol, qty, price."""
        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker") as mock_cb:

            for ctx in self._patch_stack(mock_rm, mock_cb):
                ctx.start()
            try:
                r = self.client.post("/api/v1/trading/orders", json={
                    "symbol": "NIFTY",
                    "side": "BUY",
                    "quantity": 1,
                    "price": 22000.0,
                    "is_manual": True,
                })
                self.assertIn(r.status_code, (200, 201), r.text)
            finally:
                for ctx in self._patch_stack(mock_rm, mock_cb):
                    try:
                        ctx.stop()
                    except RuntimeError:
                        pass

            mock_rm.record_fill.assert_called_once()
            kwargs = mock_rm.record_fill.call_args
            all_args = {**dict(zip(
                ["order_id", "symbol", "filled_qty", "fill_price", "is_complete"],
                kwargs.args,
            )), **(kwargs.kwargs or {})}

            self.assertEqual(all_args["symbol"], "NIFTY")
            self.assertEqual(all_args["filled_qty"], 1)
            self.assertAlmostEqual(all_args["fill_price"], 22000.0)
            self.assertTrue(all_args.get("is_complete", True))

    def test_record_fill_order_id_matches_response_order_id(self):
        """The order_id passed to record_fill equals the order_id in the API response."""
        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker") as mock_cb, \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}

            r = self.client.post("/api/v1/trading/orders", json={
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 2,
                "price": 2800.0,
                "is_manual": True,
            })
            self.assertIn(r.status_code, (200, 201), r.text)
            response_oid = r.json()["order_id"]

            kwargs = mock_rm.record_fill.call_args
            all_args = {**dict(zip(
                ["order_id", "symbol", "filled_qty", "fill_price", "is_complete"],
                kwargs.args,
            )), **(kwargs.kwargs or {})}
            self.assertEqual(all_args["order_id"], response_oid)

    # ------------------------------------------------------------------
    # on_trade_completed
    # ------------------------------------------------------------------

    def test_on_trade_completed_called_after_successful_fill(self):
        """circuit_breaker.on_trade_completed is called once with a Trade object."""
        from models.risk_state import Trade

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker") as mock_cb, \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}

            r = self.client.post("/api/v1/trading/orders", json={
                "symbol": "BANKNIFTY",
                "side": "SELL",
                "quantity": 1,
                "price": 50000.0,
                "is_manual": True,
            })
            self.assertIn(r.status_code, (200, 201), r.text)

            mock_cb.on_trade_completed.assert_called_once()
            trade_arg = mock_cb.on_trade_completed.call_args.args[0]
            self.assertIsInstance(trade_arg, Trade)
            self.assertEqual(trade_arg.symbol, "BANKNIFTY")
            self.assertEqual(trade_arg.side, "SELL")
            self.assertEqual(trade_arg.quantity, 1.0)
            self.assertAlmostEqual(trade_arg.entry_price, 50000.0)

    # ------------------------------------------------------------------
    # Segment routing
    # ------------------------------------------------------------------

    def test_options_order_uses_fno_segment(self):
        """Orders in AUTO_OPTIONS category produce segment='FNO' in the Trade."""
        from models.risk_state import Trade

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker") as mock_cb, \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}

            r = self.client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY24OCT22000CE",
                "side": "BUY",
                "quantity": 1,
                "price": 150.0,
                "is_manual": True,
                "category": "AUTO_OPTIONS",
            })
            self.assertIn(r.status_code, (200, 201), r.text)

            trade_arg = mock_cb.on_trade_completed.call_args.args[0]
            self.assertEqual(trade_arg.segment, "FNO")
            self.assertEqual(trade_arg.trade_type, "FNO")

    def test_cash_order_uses_equity_segment(self):
        """Cash equity orders produce segment='EQUITY' in the Trade."""
        from models.risk_state import Trade

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker") as mock_cb, \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}

            r = self.client.post("/api/v1/trading/orders", json={
                "symbol": "HDFCBANK",
                "side": "BUY",
                "quantity": 5,
                "price": 1700.0,
                "is_manual": True,
                "category": "AUTO_CASH_INTRADAY",
            })
            self.assertIn(r.status_code, (200, 201), r.text)

            trade_arg = mock_cb.on_trade_completed.call_args.args[0]
            self.assertEqual(trade_arg.segment, "EQUITY")
            self.assertEqual(trade_arg.trade_type, "INTRADAY")

    def test_futures_order_uses_fno_segment(self):
        """AUTO_FUTURES orders produce segment='FNO' in the Trade."""
        from models.risk_state import Trade

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker") as mock_cb, \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}

            r = self.client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTYFUT",
                "side": "BUY",
                "quantity": 1,
                "price": 23000.0,
                "is_manual": True,
                "category": "AUTO_FUTURES",
            })
            self.assertIn(r.status_code, (200, 201), r.text)

            trade_arg = mock_cb.on_trade_completed.call_args.args[0]
            self.assertEqual(trade_arg.segment, "FNO")

    # ------------------------------------------------------------------
    # Fault isolation
    # ------------------------------------------------------------------

    def test_risk_pipeline_exception_does_not_block_order_response(self):
        """
        A crash inside the risk recording block must NOT prevent the order
        response from reaching the caller — the order is already placed.
        """
        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker") as mock_cb, \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
            mock_rm.record_fill.side_effect = RuntimeError("DB write failed")

            r = self.client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY",
                "side": "BUY",
                "quantity": 1,
                "price": 22000.0,
                "is_manual": True,
            })
            # Order response must still succeed despite the recording crash
            self.assertIn(r.status_code, (200, 201), r.text)
            self.assertIn("order_id", r.json())

    def test_risk_not_called_for_rejected_order(self):
        """
        If the broker rejects the order (place_order raises), risk pipeline
        must NOT be called — there is no fill to record.
        """
        async def _reject(order):
            raise RuntimeError("Broker rejected")

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker") as mock_cb, \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized), \
             patch.object(self.pb, "place_order", side_effect=_reject):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}

            r = self.client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY",
                "side": "BUY",
                "quantity": 1,
                "price": 22000.0,
                "is_manual": True,
            })
            self.assertEqual(r.status_code, 502)
            mock_rm.record_fill.assert_not_called()
            mock_cb.on_trade_completed.assert_not_called()


class TestLiveFillPositionSync(unittest.TestCase):
    """
    After a LIVE fill, the route must call adapter.get_positions() and
    populate _positions_db so the "Open Positions" panel reflects the new
    position immediately without waiting for the next scheduled poll.
    """

    def setUp(self):
        from unittest.mock import AsyncMock
        from models.account import Position
        from models.order import OrderUpdate, OrderState
        from datetime import datetime, timezone
        from api.routes.trading import _positions_db, EXEC_LIVE
        from api.dependencies import get_broker_adapters

        self.AsyncMock = AsyncMock
        self.Position = Position
        self.OrderUpdate = OrderUpdate
        self.OrderState = OrderState

        self.app = create_app()
        self.app.dependency_overrides[require_operator] = lambda: {"sub": "op", "role": "operator"}

        # Switch directly to LIVE mode, bypassing the API-level token check.
        self._saved_mode = execution_mode.mode
        execution_mode.mode = EXEC_LIVE

        _positions_db.clear()
        self._positions_db = _positions_db

    def tearDown(self):
        from api.routes.trading import EXEC_PAPER
        execution_mode.mode = self._saved_mode
        self._positions_db.clear()
        self.app.dependency_overrides.clear()

    def _make_mock_adapter(self, broker_order_id="ANGEL123456", live_positions=None):
        from unittest.mock import AsyncMock, MagicMock
        from models.order import OrderUpdate, OrderState
        from datetime import datetime, timezone

        mock = MagicMock()
        mock.place_order = AsyncMock(return_value=OrderUpdate(
            client_order_id="test-cid",
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            remaining_qty=1.0,
            filled_qty=0.0,
            avg_fill_price=0.0,
            timestamp=datetime.now(timezone.utc),
        ))
        mock.get_positions = AsyncMock(return_value=live_positions or [])
        return mock

    def _common_patches(self, mock_rm, mock_adapter):
        from api.dependencies import get_broker_adapters
        mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
        self.app.dependency_overrides[get_broker_adapters] = lambda: {"angelone": mock_adapter}
        return [
            patch("api.routes.trading.require_live_feed", return_value=None),
            patch("risk.order_authorizer.order_authorizer.authorize_order",
                  side_effect=_authorized),
        ]

    def test_live_fill_calls_get_positions(self):
        """adapter.get_positions() must be awaited exactly once after a LIVE fill."""
        mock_adapter = self._make_mock_adapter()

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker"), \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
            from api.dependencies import get_broker_adapters
            self.app.dependency_overrides[get_broker_adapters] = lambda: {"angelone": mock_adapter}
            client = TestClient(self.app)

            r = client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY",
                "side": "BUY",
                "quantity": 1,
                "price": 22000.0,
                "is_manual": True,
            })
            self.assertIn(r.status_code, (200, 201), r.text)

        mock_adapter.get_positions.assert_awaited_once()

    def test_live_fill_populates_positions_db(self):
        """_positions_db is populated from the broker's live positions after a LIVE fill."""
        from models.account import Position

        live_pos = Position(
            broker="angelone",
            symbol="NIFTY",
            exchange="NSE",
            product_type="INTRADAY",
            qty=1.0,
            avg_price=22000.0,
            current_price=22000.0,
        )
        mock_adapter = self._make_mock_adapter(live_positions=[live_pos])

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker"), \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
            from api.dependencies import get_broker_adapters
            self.app.dependency_overrides[get_broker_adapters] = lambda: {"angelone": mock_adapter}
            client = TestClient(self.app)

            r = client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY",
                "side": "BUY",
                "quantity": 1,
                "price": 22000.0,
                "is_manual": True,
            })
            self.assertIn(r.status_code, (200, 201), r.text)

        self.assertIn("NIFTY", self._positions_db)
        stored = self._positions_db["NIFTY"]
        self.assertEqual(stored["symbol"], "NIFTY")
        self.assertAlmostEqual(stored["avg_price"], 22000.0)

    def test_live_fill_closed_position_not_in_db(self):
        """Closed positions (qty=0, is_open=False) are NOT written to _positions_db."""
        from models.account import Position

        closed_pos = Position(
            broker="angelone",
            symbol="HDFCBANK",
            exchange="NSE",
            product_type="INTRADAY",
            qty=0.0,          # closed — is_open will be False
            avg_price=1700.0,
            current_price=1700.0,
        )
        mock_adapter = self._make_mock_adapter(live_positions=[closed_pos])

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker"), \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
            from api.dependencies import get_broker_adapters
            self.app.dependency_overrides[get_broker_adapters] = lambda: {"angelone": mock_adapter}
            client = TestClient(self.app)

            r = client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY",
                "side": "BUY",
                "quantity": 1,
                "price": 22000.0,
                "is_manual": True,
            })
            self.assertIn(r.status_code, (200, 201), r.text)

        self.assertNotIn("HDFCBANK", self._positions_db)

    def test_live_position_sync_failure_does_not_block_order(self):
        """A get_positions() failure must NOT prevent the order confirmation response."""
        from unittest.mock import AsyncMock

        mock_adapter = self._make_mock_adapter()
        mock_adapter.get_positions = AsyncMock(side_effect=RuntimeError("Broker API timeout"))

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker"), \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
            from api.dependencies import get_broker_adapters
            self.app.dependency_overrides[get_broker_adapters] = lambda: {"angelone": mock_adapter}
            client = TestClient(self.app)

            r = client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY",
                "side": "BUY",
                "quantity": 1,
                "price": 22000.0,
                "is_manual": True,
            })
            # Order must succeed even though position sync crashed
            self.assertIn(r.status_code, (200, 201), r.text)
            self.assertIn("order_id", r.json())


class TestFnoBrokerRouting(unittest.TestCase):
    """
    Architecture rule: LIVE mode F&O orders are always routed to Dhan.
    AngelOne and Fyers are data feeds only — they must never receive F&O
    execution requests. Paper mode stays broker-agnostic.
    """

    def setUp(self):
        from unittest.mock import AsyncMock, MagicMock
        from models.order import OrderUpdate, OrderState
        from datetime import datetime, timezone
        from api.routes.trading import _positions_db, EXEC_LIVE, EXEC_PAPER

        self.AsyncMock = AsyncMock
        self.MagicMock = MagicMock
        self.OrderUpdate = OrderUpdate
        self.OrderState = OrderState

        self.app = create_app()
        self.app.dependency_overrides[require_operator] = lambda: {"sub": "op", "role": "operator"}

        self._saved_mode = execution_mode.mode
        execution_mode.mode = EXEC_LIVE

        _positions_db.clear()
        self._positions_db = _positions_db

    def tearDown(self):
        execution_mode.mode = self._saved_mode
        self._positions_db.clear()
        self.app.dependency_overrides.clear()

    def _make_adapter(self, broker_order_id="ORD001"):
        from unittest.mock import AsyncMock, MagicMock
        from models.order import OrderUpdate, OrderState
        from datetime import datetime, timezone
        mock = MagicMock()
        mock.place_order = AsyncMock(return_value=OrderUpdate(
            client_order_id="test-cid",
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            remaining_qty=1.0,
            filled_qty=0.0,
            avg_fill_price=0.0,
            timestamp=datetime.now(timezone.utc),
        ))
        mock.get_positions = AsyncMock(return_value=[])
        return mock

    def _common_patches(self, mock_rm):
        mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
        return [
            patch("api.routes.trading.require_live_feed", return_value=None),
            patch("risk.order_authorizer.order_authorizer.authorize_order",
                  side_effect=_authorized),
            patch("brokers.token_manager.BrokerTokenManager.get_active_broker",
                  return_value="angelone"),
        ]

    def test_fno_live_order_routes_to_dhan(self):
        """
        In LIVE mode an F&O order must use the Dhan adapter even when the
        operator's active broker is 'angelone'.
        """
        mock_dhan = self._make_adapter("DHAN_FNO_001")
        mock_angel = self._make_adapter("ANGEL_SHOULD_NOT_BE_CALLED")

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker"), \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized), \
             patch("brokers.token_manager.BrokerTokenManager.get_active_broker",
                   return_value="angelone"):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
            self.app.dependency_overrides[get_broker_adapters] = lambda: {
                "dhan": mock_dhan,
                "angelone": mock_angel,
            }
            client = TestClient(self.app)

            r = client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY24OCT22000CE",
                "side": "BUY",
                "quantity": 1,
                "price": 150.0,
                "is_manual": True,
                "category": "AUTO_OPTIONS",
            })
            self.assertIn(r.status_code, (200, 201), r.text)

        mock_dhan.place_order.assert_awaited_once()
        mock_angel.place_order.assert_not_called()

    def test_fno_live_response_broker_is_dhan(self):
        """The order response's broker field must be 'dhan' for a LIVE F&O order."""
        mock_dhan = self._make_adapter("DHAN_FNO_002")

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker"), \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized), \
             patch("brokers.token_manager.BrokerTokenManager.get_active_broker",
                   return_value="angelone"):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
            self.app.dependency_overrides[get_broker_adapters] = lambda: {"dhan": mock_dhan}
            client = TestClient(self.app)

            r = client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTYFUT",
                "side": "BUY",
                "quantity": 1,
                "price": 23000.0,
                "is_manual": True,
                "category": "AUTO_FUTURES",
            })
            self.assertIn(r.status_code, (200, 201), r.text)
            self.assertEqual(r.json()["broker"], "dhan")

    def test_equity_live_order_uses_active_broker(self):
        """
        Cash/equity orders in LIVE mode must NOT be overridden — they use
        whatever broker the operator has selected.
        """
        mock_angel = self._make_adapter("ANGEL_EQUITY_001")

        with patch("risk.risk_manager.risk_manager") as mock_rm, \
             patch("risk.circuit_breaker.circuit_breaker"), \
             patch("api.routes.trading.require_live_feed", return_value=None), \
             patch("risk.order_authorizer.order_authorizer.authorize_order",
                   side_effect=_authorized), \
             patch("brokers.token_manager.BrokerTokenManager.get_active_broker",
                   return_value="angelone"):

            mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
            self.app.dependency_overrides[get_broker_adapters] = lambda: {"angelone": mock_angel}
            client = TestClient(self.app)

            r = client.post("/api/v1/trading/orders", json={
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 5,
                "price": 2800.0,
                "is_manual": True,
                "category": "AUTO_CASH_INTRADAY",
            })
            self.assertIn(r.status_code, (200, 201), r.text)

        mock_angel.place_order.assert_awaited_once()
        self.assertEqual(r.json()["broker"], "angelone")

    def test_fno_paper_order_is_broker_agnostic(self):
        """
        Paper mode F&O orders must use the paper broker regardless of the
        routing rule — no Dhan override in paper mode.
        """
        from api.routes.trading import EXEC_PAPER
        execution_mode.mode = EXEC_PAPER

        pb = get_paper_broker()
        pb.reset()
        saved_ltp = pb._ltp_provider
        pb._ltp_provider = lambda _: 22000.0

        try:
            with patch("risk.risk_manager.risk_manager") as mock_rm, \
                 patch("risk.circuit_breaker.circuit_breaker"), \
                 patch("api.routes.trading.require_live_feed", return_value=None), \
                 patch("risk.order_authorizer.order_authorizer.authorize_order",
                       side_effect=_authorized):

                mock_rm.get_risk_summary.return_value = {"emergency_halt_active": False}
                self.app.dependency_overrides[get_broker_adapters] = lambda: {}
                client = TestClient(self.app)

                r = client.post("/api/v1/trading/orders", json={
                    "symbol": "NIFTY24OCT22000CE",
                    "side": "BUY",
                    "quantity": 1,
                    "price": 22000.0,
                    "is_manual": True,
                    "category": "AUTO_OPTIONS",
                })
                self.assertIn(r.status_code, (200, 201), r.text)
                # Paper mode: broker field contains "paper:" prefix, never bare "dhan"
                self.assertTrue(
                    r.json()["broker"].startswith("paper:"),
                    f"Expected paper: prefix, got: {r.json()['broker']}",
                )
                self.assertTrue(r.json()["is_simulated"])
        finally:
            pb._ltp_provider = saved_ltp
            pb.reset()


if __name__ == "__main__":
    unittest.main()
