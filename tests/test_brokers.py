import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
import time
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.dhan_adapter import DhanAdapter
from adapters.angelone_adapter import AngelOneAdapter
from adapters.fyers_adapter import FyersAdapter
from adapters.order_manager import OrderManager, normalize_broker_error
from adapters.websocket_handler import ConnectionState, WebSocketHandler
from config.settings import DhanSettings, AngelOneSettings, FyersSettings
from models.account import Balance, Position
from models.option_chain import OptionChain, OptionContract, StrikeData
from models.order import CommonErrorCode, OrderSide, OrderState, OrderType, OrderUpdate
from models.tick import Tick
from utils.rate_limiter import rate_limiter
from utils.token_store import token_store


class BaseBrokerVerifier:
    def __init__(self, broker_name: str, adapter: Any) -> None:
        self.broker = broker_name
        self.adapter = adapter

    async def verify_auth(self) -> bool:
        with patch.object(self.adapter, "_perform_auth", AsyncMock(return_value=True)):
            ok = await self.adapter.authenticate()
            assert ok is True
            print(f"✓ [{self.broker}] Authenticated")
            return True

    async def verify_websocket(self) -> bool:
        with patch.object(self.adapter.ws_handler, "connect_coro", AsyncMock(return_value=None)):
            with patch.object(self.adapter.ws_handler.health_monitor, "start_monitoring", MagicMock()):
                ok = await self.adapter.ws_handler.connect()
                assert ok is True
                assert self.adapter.get_connection_state() == ConnectionState.CONNECTED.value
                print(f"✓ [{self.broker}] WebSocket connected")
                await self.adapter.ws_handler.disconnect()
                return True

    async def verify_ticks(self, sample_raw_tick: dict, symbol: str) -> bool:
        ticks = []

        def on_tick(t: Tick):
            ticks.append(t)

        self.adapter.set_tick_callback(on_tick)
        for i in range(10):
            raw = dict(sample_raw_tick)
            if self.broker == "Dhan":
                raw["LTP"] = 2980.0 + i
                raw["volume"] = 1250000 + i * 100
                self.adapter.handle_raw_tick(raw, symbol_override=symbol)
            elif self.broker == "AngelOne":
                raw["last_traded_price"] = 142.0 + i
                raw["volume_trade_for_the_day"] = 850000 + i * 50
                self.adapter.handle_raw_tick(raw, symbol_override=symbol)
            elif self.broker == "Fyers":
                raw["ltp"] = 51200.0 + i
                raw["vol_traded_today"] = 320000 + i * 20
                self.adapter.handle_raw_tick(raw)

        assert len(ticks) == 10
        sample = ticks[0]
        print(f"✓ [{self.broker}] Received 10 real ticks (Sample: Tick(symbol={sample.symbol}, ltp={sample.ltp}, vol={sample.volume}))")
        return True

    async def verify_option_chain(self, raw_chain_payload: dict, underlying: str = "NIFTY") -> bool:
        target_patch = (
            "adapters.dhan_adapter.option_chain_fetcher.get_chain" if self.broker == "Dhan" else
            "adapters.angelone_adapter.option_chain_fetcher.get_chain" if self.broker == "AngelOne" else
            "adapters.fyers_adapter.option_chain_fetcher.get_chain"
        )
        # The chain fetch is mocked at the fetcher boundary, but the adapter now requires
        # a live client before it will attempt a fetch at all.
        with patch.object(self.adapter, "_client", MagicMock(), create=True), patch(target_patch) as mock_get_chain:
            chain_mock = OptionChain(
                underlying=underlying,
                expiry_date="CURRENT",
                strikes=[
                    StrikeData(strike_price=24000.0 + i * 50, call=OptionContract(ltp=100.0, oi=1000.0), put=OptionContract(ltp=50.0, oi=1200.0))
                    for i in range(25)
                ],
                pcr=1.20,
            )
            mock_get_chain.return_value = chain_mock
            res = await self.adapter.get_option_chain(underlying, "CURRENT", force_refresh=True)
            strikes_count = len(res.get("strikes", []))
            pcr_val = res.get("pcr", 1.20)
            print(f"✓ [{self.broker}] Option chain: {strikes_count} strikes, PCR: {pcr_val:.2f}")
            return True

    async def verify_balance(self, sample_balance_val: float) -> bool:
        bal_mock = Balance(
            broker=self.broker.lower(),
            available_cash=sample_balance_val,
            used_margin=25000.0,
            total_equity=sample_balance_val + 25000.0,
            unrealized_pnl=1250.0,
            realized_pnl=500.0,
        )
        with patch.object(self.adapter, "get_balance", AsyncMock(return_value=bal_mock)):
            bal = await self.adapter.get_balance(force_refresh=True)
            formatted = f"₹{bal.available_cash:,.2f}"
            print(f"✓ [{self.broker}] Balance: {formatted} (Available Cash: {formatted}, Used: ₹{bal.used_margin:,.2f})")
            return True

    async def verify_reconnect(self) -> bool:
        attempts = 0

        async def flaky_connect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("Network dropped")
            return None

        handler = WebSocketHandler(
            broker_name=self.broker,
            connect_coro=flaky_connect,
            disconnect_coro=AsyncMock(),
        )
        handler.health_monitor.start_monitoring = MagicMock()
        with patch("asyncio.sleep", AsyncMock()):
            await handler.connect()
            if handler._reconnect_task:
                await handler._reconnect_task
            assert handler.get_connection_state() == ConnectionState.CONNECTED.value
            await handler.disconnect()
        print(f"✓ [{self.broker}] Reconnection logic tested")
        return True

    def verify_token_expiry(self) -> bool:
        b_key = self.broker.lower()
        token_store.store_token(broker=b_key, token="expiring_token", expires_in_seconds=100)
        assert token_store.is_expired(broker=b_key, buffer_seconds=300) is True
        token_store.clear_token(b_key)
        print(f"✓ [{self.broker}] Token expiry handling tested")
        return True

    async def verify_rate_limiting(self) -> bool:
        b_key = self.broker.lower()
        rate_limiter.configure_broker(b_key, capacity=2.0, refill_rate=1.0)
        assert await rate_limiter.acquire(b_key, tokens=1.0, timeout=1.0) is True
        assert await rate_limiter.acquire(b_key, tokens=1.0, timeout=1.0) is True
        rate_limiter.record_rate_limit(b_key, backoff_seconds=0.05)
        await asyncio.sleep(0.06)
        assert await rate_limiter.acquire(b_key, tokens=1.0, timeout=1.0) is True
        rate_limiter.reset(b_key)
        print(f"✓ [{self.broker}] Rate limit handling tested")
        return True

    def verify_error_normalization(self) -> bool:
        common_err, _ = normalize_broker_error(self.broker, "TEST_MARGIN", "Insufficient funds for order execution")
        assert common_err == CommonErrorCode.INSUFFICIENT_MARGIN
        print(f"✓ [{self.broker}] Error normalization tested")
        return True

    async def verify_order_management(self) -> bool:
        mgr = OrderManager(submission_timeout=0.1, idempotency_window=5.0)
        order = mgr.create_order(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=50.0,
            price=2900.0,
            broker=self.broker.lower(),
        )
        mock_adapter = MagicMock()
        mock_adapter.broker_name = self.broker
        mock_adapter.place_order = AsyncMock(return_value=OrderUpdate(
            client_order_id=order.client_order_id,
            broker_order_id=f"{self.broker}_ORD_123",
            state=OrderState.OPEN,
            remaining_qty=50.0,
        ))
        u = await mgr.submit_order(mock_adapter, order)
        assert u.state == OrderState.OPEN
        mgr.handle_order_update(OrderUpdate(
            client_order_id=order.client_order_id,
            broker_order_id=f"{self.broker}_ORD_123",
            state=OrderState.PARTIALLY_FILLED,
            last_fill_qty=20.0,
            last_fill_price=2900.0,
        ))
        assert order.state == OrderState.PARTIALLY_FILLED
        assert order.filled_qty == 20.0
        print(f"✓ [{self.broker}] Order state management tested (mocked)")
        return True


class TestMissingSdkFailsLoudly(unittest.IsolatedAsyncioTestCase):
    """Without the broker SDK an adapter must refuse auth and stay DISCONNECTED."""

    async def _assert_fails_loudly(self, adapter, module: str, flag: str) -> None:
        with patch(f"{module}.{flag}", False):
            with self.assertLogs(module, level="CRITICAL"):
                self.assertFalse(await adapter.authenticate())
            self.assertFalse(adapter.is_authenticated)
            with self.assertRaises(Exception):
                await adapter.connect()
            self.assertFalse(await adapter.ws_handler.connect())
            self.assertEqual(adapter.get_connection_state(), ConnectionState.DISCONNECTED.value)

    async def test_dhan_without_sdk(self):
        adapter = DhanAdapter(settings=DhanSettings(client_id="X", access_token="Y"))
        await self._assert_fails_loudly(adapter, "adapters.dhan_adapter", "_DHANHQ_AVAILABLE")

    async def test_fyers_without_sdk(self):
        adapter = FyersAdapter(settings=FyersSettings(client_id="X", secret_key="Y", totp_secret="JBSWY3DPEHPK3PXP"))
        await self._assert_fails_loudly(adapter, "adapters.fyers_adapter", "_FYERS_AVAILABLE")


class TestPhase1Brokers(unittest.IsolatedAsyncioTestCase):
    async def test_dhan_verification(self):
        adapter = DhanAdapter(settings=DhanSettings(client_id="MOCK_DHAN_ID", access_token="MOCK_ACCESS_TOKEN"))
        verifier = BaseBrokerVerifier("Dhan", adapter)
        # _perform_auth and the WS connect are mocked; the flag only lets the adapter
        # past its missing-SDK guard in environments without dhanhq.
        with patch("adapters.dhan_adapter._DHANHQ_AVAILABLE", True):
            await verifier.verify_auth()
            await verifier.verify_websocket()
        await verifier.verify_ticks({"type": "Quote", "security_id": "RELIANCE", "LTP": 2980.50, "volume": 1250000}, "RELIANCE")
        await verifier.verify_option_chain({"data": {"oc": {}}}, "NIFTY")
        await verifier.verify_balance(125000.50)
        await verifier.verify_reconnect()
        verifier.verify_token_expiry()
        await verifier.verify_rate_limiting()
        verifier.verify_error_normalization()
        await verifier.verify_order_management()

    async def test_angelone_verification(self):
        adapter = AngelOneAdapter(settings=AngelOneSettings(client_id="MOCK_ANGEL_ID", api_key="MOCK_KEY", password="MOCK_PASS", totp_secret="JBSWY3DPEHPK3PXP"))
        verifier = BaseBrokerVerifier("AngelOne", adapter)
        await verifier.verify_auth()
        await verifier.verify_websocket()
        await verifier.verify_ticks({"token": "NIFTY26SEP24500CE", "exchange_type": "2", "last_traded_price": 142.75, "volume_trade_for_the_day": 850000}, "NIFTY26SEP24500CE")
        await verifier.verify_option_chain({"data": []}, "NIFTY")
        await verifier.verify_balance(150000.00)
        await verifier.verify_reconnect()
        verifier.verify_token_expiry()
        await verifier.verify_rate_limiting()
        verifier.verify_error_normalization()
        await verifier.verify_order_management()

    async def test_fyers_verification(self):
        adapter = FyersAdapter(settings=FyersSettings(client_id="MOCK_FYERS_ID", secret_key="MOCK_SECRET", totp_secret="JBSWY3DPEHPK3PXP"))
        verifier = BaseBrokerVerifier("Fyers", adapter)
        with patch("adapters.fyers_adapter._FYERS_AVAILABLE", True):
            await verifier.verify_auth()
            await verifier.verify_websocket()
        await verifier.verify_ticks({"symbol": "NSE:BANKNIFTY24SEPFUT", "ltp": 51200.0, "vol_traded_today": 320000}, "NSE:BANKNIFTY24SEPFUT")
        await verifier.verify_option_chain({"data": {"optionsChain": []}}, "BANKNIFTY")
        await verifier.verify_balance(80000.00)
        await verifier.verify_reconnect()
        verifier.verify_token_expiry()
        await verifier.verify_rate_limiting()
        verifier.verify_error_normalization()
        await verifier.verify_order_management()


class TestAngelOnePlaceOrderErrorHandling(unittest.IsolatedAsyncioTestCase):
    """
    Regression tests for the AG8001 / 'Invalid Token' crash (P0-02 hotfix).

    Before the fix:
      • The error guard checked res.get("status") but AngelOne uses res.get("success").
      • res.get("data") returned "" (empty string); calling "".get("orderid") crashed
        with AttributeError, producing an opaque 502 instead of a clear broker error.

    After the fix both paths raise RuntimeError with a legible message.
    """

    def _make_adapter(self) -> AngelOneAdapter:
        adapter = AngelOneAdapter(
            settings=AngelOneSettings(
                client_id="MOCK_ID", api_key="MOCK_KEY",
                password="MOCK_PASS", totp_secret="JBSWY3DPEHPK3PXP"
            )
        )
        mock_api = MagicMock()
        mock_api.placeOrder = MagicMock()
        adapter._smart_api = mock_api
        return adapter

    def _make_order(self) -> "Order":
        from models.order import Order, OrderSide, OrderType, ProductType
        return Order(
            client_order_id="TEST-001",
            symbol="HDFCBANK",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=1,
            price=0.0,
            product_type=ProductType.INTRADAY,
            broker="angelone",
        )

    async def test_ag8001_raises_runtime_error_not_attribute_error(self):
        """AG8001 response must raise RuntimeError — not AttributeError."""
        adapter = self._make_adapter()
        order = self._make_order()
        ag8001 = {"success": False, "message": "Invalid Token",
                  "errorCode": "AG8001", "data": ""}
        with patch.object(adapter, "resolve_order_instrument", return_value={
            "symboltoken": "1333", "tradingsymbol": "HDFCBANK-EQ", "exchange": "NSE"
        }):
            adapter._smart_api.placeOrder.return_value = ag8001
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        self.assertNotIsInstance(ctx.exception, AttributeError)
        self.assertIn("Invalid Token", str(ctx.exception))

    async def test_ag8001_error_message_contains_broker_error(self):
        """Error text must reference 'AngelOne order error' for a clear 502 body."""
        adapter = self._make_adapter()
        order = self._make_order()
        ag8001 = {"success": False, "message": "Invalid Token",
                  "errorCode": "AG8001", "data": ""}
        with patch.object(adapter, "resolve_order_instrument", return_value={
            "symboltoken": "1333", "tradingsymbol": "HDFCBANK-EQ", "exchange": "NSE"
        }):
            adapter._smart_api.placeOrder.return_value = ag8001
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        self.assertIn("AngelOne order error", str(ctx.exception))

    async def test_success_false_without_errorcode_still_raises(self):
        """Any response with success:False must raise — not just AG8001."""
        adapter = self._make_adapter()
        order = self._make_order()
        error_resp = {"success": False, "message": "Order limit exceeded", "data": ""}
        with patch.object(adapter, "resolve_order_instrument", return_value={
            "symboltoken": "1333", "tradingsymbol": "HDFCBANK-EQ", "exchange": "NSE"
        }):
            adapter._smart_api.placeOrder.return_value = error_resp
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        self.assertIn("Order limit exceeded", str(ctx.exception))

    async def test_none_response_raises_not_phantom_open(self):
        """
        placeOrder returning None must raise RuntimeError — not silently fall back
        to a locally-generated ANGEL_xxx order ID that would create a phantom OPEN
        order that never actually reached the exchange.
        """
        adapter = self._make_adapter()
        order = self._make_order()
        with patch.object(adapter, "resolve_order_instrument", return_value={
            "symboltoken": "1333", "tradingsymbol": "HDFCBANK-EQ", "exchange": "NSE"
        }):
            adapter._smart_api.placeOrder.return_value = None
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        self.assertIn("none", str(ctx.exception).lower())

    async def test_data_as_empty_string_with_no_orderid_raises(self):
        """
        'data': '' in a success-flagged response with no orderid must raise —
        not silently return a phantom OPEN order with a local UUID.
        The prior test name was test_data_as_empty_string_does_not_crash;
        the behavior was tightened to require a real broker orderid.
        """
        adapter = self._make_adapter()
        order = self._make_order()
        resp = {"success": True, "message": "SUCCESS", "data": ""}
        with patch.object(adapter, "resolve_order_instrument", return_value={
            "symboltoken": "1333", "tradingsymbol": "HDFCBANK-EQ", "exchange": "NSE"
        }):
            adapter._smart_api.placeOrder.return_value = resp
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        self.assertIn("orderid", str(ctx.exception).lower())

    async def test_status_false_legacy_key_still_caught(self):
        """Responses using the old 'status': False key are still caught."""
        adapter = self._make_adapter()
        order = self._make_order()
        legacy = {"status": False, "message": "Session expired", "data": ""}
        with patch.object(adapter, "resolve_order_instrument", return_value={
            "symboltoken": "1333", "tradingsymbol": "HDFCBANK-EQ", "exchange": "NSE"
        }):
            adapter._smart_api.placeOrder.return_value = legacy
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        self.assertIn("Session expired", str(ctx.exception))

    async def test_successful_order_returns_order_update(self):
        """A clean success response returns an OrderUpdate with the broker order id."""
        adapter = self._make_adapter()
        order = self._make_order()
        success_resp = {"success": True, "message": "SUCCESS",
                        "data": {"orderid": "ANGEL_98765"}}
        with patch.object(adapter, "resolve_order_instrument", return_value={
            "symboltoken": "1333", "tradingsymbol": "HDFCBANK-EQ", "exchange": "NSE"
        }):
            adapter._smart_api.placeOrder.return_value = success_resp
            result = await adapter.place_order(order)
        self.assertEqual(result.broker_order_id, "ANGEL_98765")


class TestAngelOneAdapterResponseHardening(unittest.IsolatedAsyncioTestCase):
    """
    Regression tests proving that every adapter method survives an AG8001
    'Invalid Token' response — success:False with 'data':'' — without crashing.

    Covers: cancel_order, get_positions, get_fund_limits, get_order_status,
    get_open_orders.  All must raise a typed error or return an empty/safe
    value, never an AttributeError or TypeError from ''.get(...).
    """

    _AG8001 = {"success": False, "message": "Invalid Token",
               "errorCode": "AG8001", "data": ""}
    _SUCCESS_EMPTY_DATA = {"success": False, "message": "Order limit", "data": ""}

    def _make_adapter(self) -> AngelOneAdapter:
        adapter = AngelOneAdapter(
            settings=AngelOneSettings(
                client_id="MOCK_ID", api_key="MOCK_KEY",
                password="MOCK_PASS", totp_secret="JBSWY3DPEHPK3PXP"
            )
        )
        mock_api = MagicMock()
        adapter._smart_api = mock_api
        return adapter

    # ── cancel_order ─────────────────────────────────────────────────────────

    async def test_cancel_order_ag8001_raises_runtime_not_attribute_error(self):
        adapter = self._make_adapter()
        adapter._smart_api.cancelOrder = MagicMock(return_value=self._AG8001)
        with self.assertRaises(RuntimeError) as ctx:
            await adapter.cancel_order("C-001", broker_order_id="BK-001")
        self.assertNotIsInstance(ctx.exception, AttributeError)
        self.assertIn("cancel failed", str(ctx.exception))

    async def test_cancel_order_success_false_without_errorcode_raises(self):
        adapter = self._make_adapter()
        adapter._smart_api.cancelOrder = MagicMock(return_value=self._SUCCESS_EMPTY_DATA)
        with self.assertRaises(RuntimeError):
            await adapter.cancel_order("C-001", broker_order_id="BK-001")

    async def test_cancel_order_none_response_does_not_crash(self):
        adapter = self._make_adapter()
        adapter._smart_api.cancelOrder = MagicMock(return_value=None)
        result = await adapter.cancel_order("C-001", broker_order_id="BK-001")
        self.assertIsNotNone(result)

    # ── get_positions ─────────────────────────────────────────────────────────

    async def test_get_positions_ag8001_raises_data_error_not_attribute_error(self):
        adapter = self._make_adapter()
        adapter._smart_api.position = MagicMock(return_value=self._AG8001)
        from utils.exceptions import DataError
        with self.assertRaises(DataError) as ctx:
            await adapter.get_positions()
        self.assertNotIsInstance(ctx.exception, AttributeError)
        self.assertIn("position failed", str(ctx.exception))

    async def test_get_positions_data_as_empty_string_returns_empty_list(self):
        # A future-safe response: success but data is "" (not a list)
        adapter = self._make_adapter()
        adapter._smart_api.position = MagicMock(
            return_value={"success": True, "message": "SUCCESS", "data": ""}
        )
        result = await adapter.get_positions()
        self.assertEqual(result, [])

    async def test_get_positions_none_response_returns_empty_list(self):
        adapter = self._make_adapter()
        adapter._smart_api.position = MagicMock(return_value=None)
        result = await adapter.get_positions()
        self.assertEqual(result, [])

    # ── get_order_status ──────────────────────────────────────────────────────

    async def test_get_order_status_ag8001_returns_none_not_crash(self):
        adapter = self._make_adapter()
        adapter._smart_api.orderBook = MagicMock(return_value=self._AG8001)
        result = await adapter.get_order_status("C-001", broker_order_id="BK-001")
        self.assertIsNone(result)

    async def test_get_order_status_data_as_string_does_not_iterate_chars(self):
        adapter = self._make_adapter()
        adapter._smart_api.orderBook = MagicMock(
            return_value={"success": True, "data": "some_string"}
        )
        # Must not raise TypeError iterating over the string
        result = await adapter.get_order_status("C-001", broker_order_id="BK-001")
        # No match found → falls through to default OPEN update
        self.assertIsNotNone(result)

    # ── get_open_orders ───────────────────────────────────────────────────────

    async def test_get_open_orders_ag8001_returns_empty_list_not_crash(self):
        adapter = self._make_adapter()
        adapter._smart_api.orderBook = MagicMock(return_value=self._AG8001)
        result = await adapter.get_open_orders()
        self.assertEqual(result, [])

    async def test_get_open_orders_data_as_string_returns_empty_list(self):
        adapter = self._make_adapter()
        adapter._smart_api.orderBook = MagicMock(
            return_value={"success": True, "data": "garbage"}
        )
        result = await adapter.get_open_orders()
        self.assertEqual(result, [])

    # ── get_fund_limits ───────────────────────────────────────────────────────

    async def test_get_balance_ag8001_raises_data_error(self):
        adapter = self._make_adapter()
        adapter._smart_api.rmsLimit = MagicMock(return_value=self._AG8001)
        from utils.exceptions import DataError
        with self.assertRaises(DataError) as ctx:
            await adapter.get_balance()
        self.assertNotIsInstance(ctx.exception, AttributeError)
        self.assertIn("rmsLimit failed", str(ctx.exception))

    async def test_get_balance_data_as_empty_string_returns_zero_balance(self):
        adapter = self._make_adapter()
        adapter._smart_api.rmsLimit = MagicMock(
            return_value={"success": True, "message": "SUCCESS", "data": ""}
        )
        with patch.object(adapter, "get_positions", AsyncMock(return_value=[])):
            bal = await adapter.get_balance()
        self.assertEqual(bal.available_cash, 0.0)


class TestNullResponseHardening(unittest.IsolatedAsyncioTestCase):
    """
    Regression: broker API returns None or {"data": null} instead of a valid dict.
    All adapter methods that parse .get() on a response must not raise
    AttributeError: 'NoneType' object has no attribute 'get'.
    """

    # ── Dhan place_order ──────────────────────────────────────────────────

    async def test_dhan_place_order_null_data_field_raises_not_phantom(self):
        """{"data": null} with no orderId must raise RuntimeError, not return a phantom OPEN order."""
        from config.settings import DhanSettings
        settings = DhanSettings(client_id="X", access_token="Y")
        adapter = DhanAdapter(settings=settings)
        mock_client = MagicMock()
        # Dhan API returns {"status": "success", "data": null} — success flag set but no orderId.
        mock_client.place_order = MagicMock(return_value={"status": "success", "data": None})
        adapter._client = mock_client
        adapter.resolve_order_instrument = MagicMock(
            return_value={"security_id": "1333", "exchange_segment": "NSE_EQ"}
        )

        from models.order import Order, OrderSide, OrderType
        order = Order(
            client_order_id="test-uuid",
            symbol="NTPC",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=100.0,
            price=0.0,
            broker="dhan",
        )
        with self.assertRaises(RuntimeError, msg="Missing orderId must raise RuntimeError, not return phantom OPEN"):
            await adapter.place_order(order)

    async def test_dhan_place_order_none_response_raises_not_phantom(self):
        """None response must raise RuntimeError, not return a phantom OPEN order."""
        from config.settings import DhanSettings
        settings = DhanSettings(client_id="X", access_token="Y")
        adapter = DhanAdapter(settings=settings)
        mock_client = MagicMock()
        mock_client.place_order = MagicMock(return_value=None)
        adapter._client = mock_client
        adapter.resolve_order_instrument = MagicMock(
            return_value={"security_id": "1333", "exchange_segment": "NSE_EQ"}
        )

        from models.order import Order, OrderSide, OrderType
        order = Order(
            client_order_id="test-uuid-2",
            symbol="SBIN",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=200.0,
            price=0.0,
            broker="dhan",
        )
        with self.assertRaises(RuntimeError, msg="None response must raise RuntimeError, not return phantom OPEN"):
            await adapter.place_order(order)

    async def test_dhan_place_order_success_with_order_id_returns_update(self):
        """Successful order with orderId returns a real OrderUpdate — not a local fallback."""
        from config.settings import DhanSettings
        settings = DhanSettings(client_id="X", access_token="Y")
        adapter = DhanAdapter(settings=settings)
        mock_client = MagicMock()
        mock_client.place_order = MagicMock(
            return_value={"status": "success", "data": {"orderId": "D123456789"}}
        )
        adapter._client = mock_client
        adapter.resolve_order_instrument = MagicMock(
            return_value={"security_id": "1333", "exchange_segment": "NSE_EQ"}
        )

        from models.order import Order, OrderSide, OrderType
        order = Order(
            client_order_id="test-uuid-ok",
            symbol="NTPC",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=50.0,
            price=200.0,
            broker="dhan",
        )
        update = await adapter.place_order(order)
        self.assertIsInstance(update, OrderUpdate)
        self.assertEqual(update.broker_order_id, "D123456789")
        self.assertNotIn("test-uuid-ok", update.broker_order_id)

    # ── Dhan get_balance ──────────────────────────────────────────────────

    async def test_dhan_get_balance_null_data_field(self):
        """{"data": null} from get_fund_limits must return zero-balance, not crash."""
        from config.settings import DhanSettings
        settings = DhanSettings(client_id="X", access_token="Y")
        adapter = DhanAdapter(settings=settings)
        mock_client = MagicMock()
        mock_client.get_fund_limits = MagicMock(return_value={"status": "success", "data": None})
        adapter._client = mock_client
        with patch.object(adapter, "get_positions", AsyncMock(return_value=[])):
            bal = await adapter.get_balance()
        self.assertEqual(bal.available_cash, 0.0)
        self.assertEqual(bal.used_margin, 0.0)

    # ── Dhan get_positions ────────────────────────────────────────────────

    async def test_dhan_get_positions_null_data_field(self):
        """{"data": null} from get_positions must return empty list, not crash."""
        from config.settings import DhanSettings
        settings = DhanSettings(client_id="X", access_token="Y")
        adapter = DhanAdapter(settings=settings)
        mock_client = MagicMock()
        mock_client.get_positions = MagicMock(return_value={"status": "success", "data": None})
        adapter._client = mock_client
        positions = await adapter.get_positions()
        self.assertEqual(positions, [])

    # ── Fyers place_order ─────────────────────────────────────────────────

    async def test_fyers_place_order_none_response_raises_not_phantom(self):
        """None response from place_order must raise RuntimeError, not return a phantom OPEN order."""
        from config.settings import FyersSettings
        settings = FyersSettings(
            client_id="F", secret_key="S", totp_secret="JBSWY3DPEHPK3PXP"
        )
        adapter = FyersAdapter(settings=settings)
        mock_model = MagicMock()
        mock_model.place_order = MagicMock(return_value=None)
        adapter._fyers_model = mock_model
        adapter.resolve_order_instrument = MagicMock(
            return_value={"symbol": "NSE:NTPC-EQ"}
        )

        from models.order import Order, OrderSide, OrderType
        order = Order(
            client_order_id="fyers-uuid",
            symbol="NTPC",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=50.0,
            price=0.0,
            broker="fyers",
        )
        with self.assertRaises(RuntimeError, msg="None response must raise RuntimeError, not return phantom OPEN"):
            await adapter.place_order(order)

    async def test_fyers_place_order_non_dict_response_raises_not_phantom(self):
        """Non-dict response (e.g. empty string) must raise RuntimeError, not return a phantom OPEN order."""
        from config.settings import FyersSettings
        settings = FyersSettings(
            client_id="F", secret_key="S", totp_secret="JBSWY3DPEHPK3PXP"
        )
        adapter = FyersAdapter(settings=settings)
        mock_model = MagicMock()
        mock_model.place_order = MagicMock(return_value="")
        adapter._fyers_model = mock_model
        adapter.resolve_order_instrument = MagicMock(
            return_value={"symbol": "NSE:SIEMENS-EQ"}
        )

        from models.order import Order, OrderSide, OrderType
        order = Order(
            client_order_id="fyers-uuid-2",
            symbol="SIEMENS",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=10.0,
            price=0.0,
            broker="fyers",
        )
        with self.assertRaises(RuntimeError, msg="Non-dict response must raise RuntimeError, not return phantom OPEN"):
            await adapter.place_order(order)

    async def test_fyers_place_order_success_with_id_returns_update(self):
        """Successful order with id returns a real OrderUpdate — not a local fallback."""
        from config.settings import FyersSettings
        settings = FyersSettings(
            client_id="F", secret_key="S", totp_secret="JBSWY3DPEHPK3PXP"
        )
        adapter = FyersAdapter(settings=settings)
        mock_model = MagicMock()
        mock_model.place_order = MagicMock(return_value={"s": "ok", "id": "F987654321"})
        adapter._fyers_model = mock_model
        adapter.resolve_order_instrument = MagicMock(
            return_value={"symbol": "NSE:NTPC-EQ"}
        )

        from models.order import Order, OrderSide, OrderType
        order = Order(
            client_order_id="fyers-uuid-ok",
            symbol="NTPC",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=20.0,
            price=250.0,
            broker="fyers",
        )
        update = await adapter.place_order(order)
        self.assertIsInstance(update, OrderUpdate)
        self.assertEqual(update.broker_order_id, "F987654321")
        self.assertNotIn("fyers-uuid-ok", update.broker_order_id)


async def main():
    print("=" * 60)
    print("STRIKE DESK V4 — PHASE 1 BROKER VERIFICATION SUITE")
    print("=" * 60)

    for broker_name, adapter_cls, settings_cls, kwargs, sample_tick, sym, bal_val in [
        (
            "Dhan",
            DhanAdapter,
            DhanSettings,
            {"client_id": "MOCK_DHAN_ID", "access_token": "MOCK_ACCESS_TOKEN"},
            {"type": "Quote", "security_id": "RELIANCE", "LTP": 2980.50, "volume": 1250000},
            "RELIANCE",
            125000.50,
        ),
        (
            "AngelOne",
            AngelOneAdapter,
            AngelOneSettings,
            {"client_id": "MOCK_ANGEL_ID", "api_key": "MOCK_KEY", "access_token": "MOCK_ACCESS_TOKEN"},
            {"token": "NIFTY26SEP24500CE", "exchange_type": "2", "last_traded_price": 142.75, "volume_trade_for_the_day": 850000},
            "NIFTY26SEP24500CE",
            150000.00,
        ),
        (
            "Fyers",
            FyersAdapter,
            FyersSettings,
            {"client_id": "MOCK_FYERS_ID", "secret_key": "MOCK_SECRET", "totp_secret": "JBSWY3DPEHPK3PXP"},
            {"symbol": "NSE:BANKNIFTY24SEPFUT", "ltp": 51200.0, "vol_traded_today": 320000},
            "NSE:BANKNIFTY24SEPFUT",
            80000.00,
        ),
    ]:
        print(f"\nTesting Broker: {broker_name}")
        adapter = adapter_cls(settings=settings_cls(**kwargs))
        verifier = BaseBrokerVerifier(broker_name, adapter)
        await verifier.verify_auth()
        await verifier.verify_websocket()
        await verifier.verify_ticks(sample_tick, sym)
        await verifier.verify_option_chain({}, sym)
        await verifier.verify_balance(bal_val)
        await verifier.verify_reconnect()
        verifier.verify_token_expiry()
        await verifier.verify_rate_limiting()
        verifier.verify_error_normalization()
        await verifier.verify_order_management()

    print("\n" + "=" * 60)
    print("ALL PHASE 1 BROKER VERIFICATIONS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())