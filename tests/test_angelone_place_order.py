"""
Regression tests for AngelOne place_order hardening.

Proves that place_order:
1. Raises hard when placeOrder is absent (SDK method missing) instead of
   silently generating a local ANGEL_xxx UUID and returning OPEN.
2. Raises hard when the response contains no orderid — never returns OPEN
   with a locally generated broker_order_id.
3. Raises hard when AngelOne returns an error response (status=False, errorcode set).
4. Returns a real OrderUpdate with the broker's orderid on a clean success.
5. Checks both "orderid" and "uniqueorderid" response locations.
"""

import asyncio
import logging
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.order import Order, OrderSide, OrderType, OrderState, ProductType


def _make_adapter(smart_api_obj=None):
    """Build an AngelOneAdapter via __new__ with minimal state for place_order tests."""
    from adapters.angelone_adapter import AngelOneAdapter
    adapter = AngelOneAdapter.__new__(AngelOneAdapter)
    adapter.broker_name = "angelone"
    adapter.logger = logging.getLogger("test.angelone")
    adapter._cached_balance = None
    adapter._balance_cached_at = 0.0
    adapter._smart_api = smart_api_obj
    adapter._fno_by_symbol = {}
    adapter._futures_by_symbol = {}
    return adapter


def _make_order(symbol="NIFTY24OCTFUT", side="BUY", qty=1, price=24000.0):
    return Order(
        client_order_id="test-cid-001",
        symbol=symbol,
        exchange="NSE",
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=qty,
        price=price,
        broker="angelone",
        metadata={
            "angel_symboltoken": "99926000",
            "angel_tradingsymbol": "NIFTY24OCTFUT",
            "angel_exchange": "NFO",
        },
    )


class TestAngelOnePlaceOrderGuards(unittest.IsolatedAsyncioTestCase):
    """
    place_order must never silently return OPEN with a locally-generated order ID.
    Every failure path must raise so the caller can surface a proper error.
    """

    async def test_raises_when_smart_api_is_none(self):
        """No session → ConnectionError, not phantom OPEN."""
        adapter = _make_adapter(smart_api_obj=None)
        order = _make_order()
        with self.assertRaises(Exception) as ctx:
            await adapter.place_order(order)
        self.assertIn("authenticated", str(ctx.exception).lower())

    async def test_raises_when_place_order_method_missing(self):
        """SDK missing placeOrder → ConnectionError, not phantom OPEN."""
        mock_api = MagicMock(spec=[])  # no methods
        adapter = _make_adapter(smart_api_obj=mock_api)
        order = _make_order()
        with patch("adapters.angelone_adapter.rate_limiter") as mock_rl:
            mock_rl.acquire = AsyncMock()
            with self.assertRaises(Exception) as ctx:
                await adapter.place_order(order)
        self.assertIn("placeOrder", str(ctx.exception))

    async def test_raises_when_api_returns_none(self):
        """placeOrder returning None must raise, not generate phantom OPEN."""
        mock_api = MagicMock()
        mock_api.placeOrder = MagicMock(return_value=None)
        adapter = _make_adapter(smart_api_obj=mock_api)
        order = _make_order()

        def _mock_resolve(o):
            return {
                "symboltoken": "99926000",
                "tradingsymbol": "NIFTY24OCTFUT",
                "exchange": "NFO",
            }

        with patch("adapters.angelone_adapter.rate_limiter") as mock_rl, \
             patch.object(adapter, "resolve_order_instrument", side_effect=_mock_resolve):
            mock_rl.acquire = AsyncMock()
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        self.assertIn("none", str(ctx.exception).lower())

    async def test_raises_on_error_response_status_false(self):
        """AngelOne error response (status:False) must raise RuntimeError."""
        mock_api = MagicMock()
        mock_api.placeOrder = MagicMock(return_value={
            "status": False,
            "message": "Insufficient margin",
            "errorcode": "AB1004",
            "data": None,
        })
        adapter = _make_adapter(smart_api_obj=mock_api)
        order = _make_order()

        def _mock_resolve(o):
            return {"symboltoken": "99926000", "tradingsymbol": "NIFTY24OCTFUT", "exchange": "NFO"}

        with patch("adapters.angelone_adapter.rate_limiter") as mock_rl, \
             patch.object(adapter, "resolve_order_instrument", side_effect=_mock_resolve):
            mock_rl.acquire = AsyncMock()
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        err = str(ctx.exception)
        self.assertIn("insufficient margin", err.lower())
        self.assertIn("AB1004", err)

    async def test_raises_on_non_empty_errorcode_even_if_status_true(self):
        """
        Some AngelOne versions return status:True with a non-empty errorcode on
        certain rejections. We must still treat this as a failure.
        """
        mock_api = MagicMock()
        mock_api.placeOrder = MagicMock(return_value={
            "status": True,
            "message": "ORDERID NOT GENERATED",
            "errorcode": "AG8001",
            "data": "",
        })
        adapter = _make_adapter(smart_api_obj=mock_api)
        order = _make_order()

        def _mock_resolve(o):
            return {"symboltoken": "99926000", "tradingsymbol": "NIFTY24OCTFUT", "exchange": "NFO"}

        with patch("adapters.angelone_adapter.rate_limiter") as mock_rl, \
             patch.object(adapter, "resolve_order_instrument", side_effect=_mock_resolve):
            mock_rl.acquire = AsyncMock()
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        self.assertIn("AG8001", str(ctx.exception))

    async def test_raises_when_status_true_but_no_orderid(self):
        """
        API reports success but provides no orderid in either data or root.
        Must raise — never silently return OPEN with a local UUID.
        """
        mock_api = MagicMock()
        mock_api.placeOrder = MagicMock(return_value={
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": {},  # no orderid
        })
        adapter = _make_adapter(smart_api_obj=mock_api)
        order = _make_order()

        def _mock_resolve(o):
            return {"symboltoken": "99926000", "tradingsymbol": "NIFTY24OCTFUT", "exchange": "NFO"}

        with patch("adapters.angelone_adapter.rate_limiter") as mock_rl, \
             patch.object(adapter, "resolve_order_instrument", side_effect=_mock_resolve):
            mock_rl.acquire = AsyncMock()
            with self.assertRaises(RuntimeError) as ctx:
                await adapter.place_order(order)
        err = str(ctx.exception)
        self.assertIn("orderid", err.lower())
        # The local fallback UUID must NOT appear as broker_order_id
        self.assertNotIn("ANGEL_", err.replace("AngelOne", ""))

    async def test_success_with_orderid_in_data(self):
        """Happy path: orderid in data.orderid → returned as broker_order_id."""
        mock_api = MagicMock()
        mock_api.placeOrder = MagicMock(return_value={
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": {"orderid": "221021000000080"},
        })
        adapter = _make_adapter(smart_api_obj=mock_api)
        order = _make_order()

        def _mock_resolve(o):
            return {"symboltoken": "99926000", "tradingsymbol": "NIFTY24OCTFUT", "exchange": "NFO"}

        with patch("adapters.angelone_adapter.rate_limiter") as mock_rl, \
             patch.object(adapter, "resolve_order_instrument", side_effect=_mock_resolve):
            mock_rl.acquire = AsyncMock()
            result = await adapter.place_order(order)

        self.assertEqual(result.broker_order_id, "221021000000080")
        self.assertEqual(result.state, OrderState.OPEN)
        # Must NOT be the locally generated fallback
        self.assertFalse(result.broker_order_id.startswith("ANGEL_"))

    async def test_success_with_uniqueorderid_in_data(self):
        """Some AngelOne SDK versions return uniqueorderid instead of orderid."""
        mock_api = MagicMock()
        mock_api.placeOrder = MagicMock(return_value={
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": {"uniqueorderid": "UNIQUE-221021-00080"},
        })
        adapter = _make_adapter(smart_api_obj=mock_api)
        order = _make_order()

        def _mock_resolve(o):
            return {"symboltoken": "99926000", "tradingsymbol": "NIFTY24OCTFUT", "exchange": "NFO"}

        with patch("adapters.angelone_adapter.rate_limiter") as mock_rl, \
             patch.object(adapter, "resolve_order_instrument", side_effect=_mock_resolve):
            mock_rl.acquire = AsyncMock()
            result = await adapter.place_order(order)

        self.assertEqual(result.broker_order_id, "UNIQUE-221021-00080")
        self.assertEqual(result.state, OrderState.OPEN)

    async def test_success_with_orderid_at_root_level(self):
        """Some responses put orderid at the root level, not inside data."""
        mock_api = MagicMock()
        mock_api.placeOrder = MagicMock(return_value={
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "orderid": "ROOT-LEVEL-ORDER-001",
            "data": {},
        })
        adapter = _make_adapter(smart_api_obj=mock_api)
        order = _make_order()

        def _mock_resolve(o):
            return {"symboltoken": "99926000", "tradingsymbol": "NIFTY24OCTFUT", "exchange": "NFO"}

        with patch("adapters.angelone_adapter.rate_limiter") as mock_rl, \
             patch.object(adapter, "resolve_order_instrument", side_effect=_mock_resolve):
            mock_rl.acquire = AsyncMock()
            result = await adapter.place_order(order)

        self.assertEqual(result.broker_order_id, "ROOT-LEVEL-ORDER-001")


if __name__ == "__main__":
    unittest.main()
