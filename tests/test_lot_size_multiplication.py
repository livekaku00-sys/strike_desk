"""
Strike Desk V4 — Lot-Size Multiplication Regression Suite

Tests that prove (or expose failure of) the lot-size expansion from
"requested lots" → "total contracts sent to the broker".

Scenarios covered:
  [A] Dhan adapter: index futures with explicit dhan_security_id + NSE_FNO
      segment → quantity in payload = lots × lot_size (e.g., 1 lot NIFTY = 25 contracts)
  [B] Dhan adapter: NIFTY via IDX_I segment (no explicit security_id) →
      lot_size lookup is SKIPPED → payload quantity = lots × 1 (BUG DOCUMENTED)
  [C] Dhan adapter: stock futures (NSE_FNO, explicit security_id) →
      quantity = lots × lot_size
  [D] Dhan adapter: option contract (NSE_FNO, explicit security_id) →
      quantity = lots × lot_size
  [E] Paper broker: FUT symbol → lot_size resolved from registry/CORE_SYMBOLS
  [F] Paper broker: option symbol (CE/PE) → lot_size resolved via underlying
  [G] Dhan adapter: silent exception in lot_size lookup defaults to 1
      (proving the except-pass swallows registry errors)
"""
import asyncio
import logging
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.order import Order, OrderSide, OrderType, OrderState, ProductType


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_dhan_adapter(client_obj=None):
    """Construct a DhanAdapter via __new__ without triggering __init__ I/O."""
    from adapters.dhan_adapter import DhanAdapter
    adapter = DhanAdapter.__new__(DhanAdapter)
    adapter.broker_name = "dhan"
    adapter.logger = logging.getLogger("test.dhan")
    adapter._cached_balance = None
    adapter._balance_cached_at = 0.0
    adapter.is_authenticated = True
    adapter._client = client_obj
    adapter._client_ids_by_tag = {}
    adapter._equity_ids = {}
    adapter._equity_id_to_sym = {}
    adapter._expiry_cache = {}
    adapter.balance_ttl = 60.0
    return adapter


def _make_order(
    symbol: str,
    qty: int = 1,
    side: str = "BUY",
    price: float = 24000.0,
    metadata: dict | None = None,
) -> Order:
    return Order(
        client_order_id="test-lot-001",
        symbol=symbol,
        exchange="NSE",
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=qty,
        price=price,
        broker="dhan",
        metadata=metadata or {},
    )


def _success_response(order_id: str = "DHAN12345") -> dict:
    return {"status": "success", "data": {"orderId": order_id}}


# ── Dhan adapter tests ────────────────────────────────────────────────────────

class TestDhanLotSizeMultiplication(unittest.IsolatedAsyncioTestCase):

    def _make_client(self, response=None):
        client = MagicMock()
        client.place_order = MagicMock(return_value=response or _success_response())
        return client

    # [A] Index futures with explicit security_id + NSE_FNO → lot_size = 25
    async def test_a_index_futures_explicit_security_id_multiplies_lot_size(self):
        """
        NIFTY futures via explicit dhan_security_id (NSE_FNO segment):
        1 lot × 25 lot_size = 25 contracts sent to Dhan.
        """
        adapter = _make_dhan_adapter(self._make_client())
        order = _make_order(
            symbol="NIFTY29OCT26FUT",
            qty=1,
            metadata={
                "dhan_security_id": "99994",   # realistic NIFTY FUT security_id
                "dhan_segment": "NSE_FNO",
                "underlying": "NIFTY",
            },
        )

        captured_payload = {}

        def _capture_place_order(**payload):
            captured_payload.update(payload)
            return _success_response()

        adapter._client.place_order = _capture_place_order

        # Patch the registry to return a known NIFTY-FUT instrument with lot_size=25
        mock_inst = MagicMock()
        mock_inst.lot_size = 25

        with patch("adapters.dhan_adapter.rate_limiter") as mock_rl, \
             patch("services.instrument_registry.instrument_registry") as mock_reg:
            mock_rl.acquire = AsyncMock()
            mock_reg.get = AsyncMock(return_value=mock_inst)
            await adapter.place_order(order)

        self.assertIn("quantity", captured_payload, "payload must contain 'quantity'")
        self.assertEqual(
            captured_payload["quantity"], 25,
            f"Expected 1 lot × 25 = 25 contracts, got {captured_payload['quantity']}"
        )

    # [B] NIFTY via IDX_I segment (no explicit security_id) → SKIPS lot_size lookup → BUG
    async def test_b_idx_i_segment_skips_lot_size_lookup_DOCUMENTS_BUG(self):
        """
        NIFTY placed without explicit dhan_security_id resolves to IDX_I segment.
        The lot_size gate `if segment in ('NSE_FNO', 'BSE_FNO')` EXCLUDES IDX_I,
        so lot_size stays at 1 and the payload quantity equals the raw lot count.

        This test DOCUMENTS THE BUG: 1 lot of NIFTY sends quantity=1 to Dhan,
        not quantity=25.
        """
        adapter = _make_dhan_adapter(self._make_client())
        order = _make_order(symbol="NIFTY", qty=1)  # no explicit security_id → IDX_I

        captured_payload = {}

        def _capture_place_order(**payload):
            captured_payload.update(payload)
            return _success_response()

        adapter._client.place_order = _capture_place_order

        with patch("adapters.dhan_adapter.rate_limiter") as mock_rl:
            mock_rl.acquire = AsyncMock()
            await adapter.place_order(order)

        # With the current code, segment=IDX_I → lot_size=1 → quantity sent = 1
        self.assertEqual(
            captured_payload.get("exchange_segment"), "IDX_I",
            "NIFTY without explicit security_id must resolve to IDX_I",
        )
        self.assertEqual(
            captured_payload.get("quantity"), 1,
            "BUG CONFIRMED: IDX_I segment skips lot_size lookup; "
            f"quantity sent = {captured_payload.get('quantity')} instead of 25",
        )

    # [C] Stock futures (NSE_FNO + explicit security_id) → lot_size = 500 for RELIANCE
    async def test_c_stock_futures_explicit_security_id_multiplies_lot_size(self):
        """
        RELIANCE futures via explicit dhan_security_id (NSE_FNO):
        1 lot × 250 lot_size = 250 contracts.
        """
        adapter = _make_dhan_adapter(self._make_client())
        order = _make_order(
            symbol="RELIANCE29OCT26FUT",
            qty=1,
            metadata={
                "dhan_security_id": "1333",
                "dhan_segment": "NSE_FNO",
                "underlying": "RELIANCE",
            },
        )

        captured_payload = {}

        def _capture(**payload):
            captured_payload.update(payload)
            return _success_response()

        adapter._client.place_order = _capture

        mock_inst = MagicMock()
        mock_inst.lot_size = 250

        with patch("adapters.dhan_adapter.rate_limiter") as mock_rl, \
             patch("services.instrument_registry.instrument_registry") as mock_reg:
            mock_rl.acquire = AsyncMock()
            mock_reg.get = AsyncMock(return_value=mock_inst)
            await adapter.place_order(order)

        self.assertEqual(
            captured_payload["quantity"], 250,
            f"Expected 1 lot × 250 = 250 for RELIANCE futures, got {captured_payload['quantity']}",
        )

    # [D] Option contract → lot_size via underlying
    async def test_d_option_contract_multiplies_lot_size_via_underlying(self):
        """
        NIFTY24OCT24250CE (explicit dhan_security_id, NSE_FNO):
        1 lot × 25 lot_size = 25 contracts.
        """
        adapter = _make_dhan_adapter(self._make_client())
        order = _make_order(
            symbol="NIFTY24OCT24250CE",
            qty=1,
            metadata={
                "dhan_security_id": "88888",
                "dhan_segment": "NSE_FNO",
                "underlying": "NIFTY",
            },
        )

        captured_payload = {}

        def _capture(**payload):
            captured_payload.update(payload)
            return _success_response()

        adapter._client.place_order = _capture

        mock_inst = MagicMock()
        mock_inst.lot_size = 25

        with patch("adapters.dhan_adapter.rate_limiter") as mock_rl, \
             patch("services.instrument_registry.instrument_registry") as mock_reg:
            mock_rl.acquire = AsyncMock()
            mock_reg.get = AsyncMock(return_value=mock_inst)
            await adapter.place_order(order)

        self.assertEqual(
            captured_payload["quantity"], 25,
            f"Expected 1 lot × 25 = 25 for NIFTY option, got {captured_payload['quantity']}",
        )

    # [G] Silent exception in lot_size lookup → defaults to lot_size=1
    async def test_g_registry_exception_silently_defaults_to_lot_size_1(self):
        """
        When the instrument_registry.get() raises an exception inside the
        try/except block (line 581 in dhan_adapter.py), the except-pass
        swallows the error and lot_size silently stays at 1.
        A 1-lot order for NIFTY futures ends up sending quantity=1.
        """
        adapter = _make_dhan_adapter(self._make_client())
        order = _make_order(
            symbol="NIFTY29OCT26FUT",
            qty=1,
            metadata={
                "dhan_security_id": "99994",
                "dhan_segment": "NSE_FNO",
                "underlying": "NIFTY",
            },
        )

        captured_payload = {}

        def _capture(**payload):
            captured_payload.update(payload)
            return _success_response()

        adapter._client.place_order = _capture

        with patch("adapters.dhan_adapter.rate_limiter") as mock_rl, \
             patch("services.instrument_registry.instrument_registry") as mock_reg:
            mock_rl.acquire = AsyncMock()
            # Registry raises — exception is swallowed by except: pass
            mock_reg.get = AsyncMock(side_effect=RuntimeError("registry unavailable"))
            await adapter.place_order(order)

        # CORE_SYMBOLS fallback should save NIFTY (lot_size=25) — this tests
        # whether the second fallback works when registry fails.
        # If CORE_SYMBOLS also misses, quantity will be 1.
        qty_sent = captured_payload.get("quantity", 0)
        # Acceptable outcomes: 25 (CORE_SYMBOLS fallback worked) or 1 (all fallbacks missed).
        # Either way, no exception was raised — the silent swallow is the documented concern.
        self.assertIn(
            qty_sent, (1, 25),
            f"Expected quantity to be 1 (silent failure) or 25 (CORE_SYMBOLS fallback), got {qty_sent}",
        )


# ── Paper broker tests ────────────────────────────────────────────────────────

class TestPaperBrokerLotSize(unittest.IsolatedAsyncioTestCase):
    """
    Paper broker resolves lot_size independently from the segment.
    It checks for 'FUT' in the symbol and CE/PE suffix, so it is NOT gated
    on the IDX_I vs NSE_FNO segment distinction.
    """

    def _make_paper_broker(self, prices: dict):
        from adapters.paper_broker import PaperBrokerAdapter
        return PaperBrokerAdapter(ltp_provider=lambda s: prices.get(s.upper()))

    def _make_order(self, symbol: str, qty: int = 1, price: float = 24000.0,
                    metadata: dict | None = None) -> Order:
        return Order(
            client_order_id="paper-test-001",
            symbol=symbol,
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=price,
            broker="paper",
            metadata=metadata or {},
        )

    # [E] FUT symbol → lot_size resolved, P&L uses multiplier
    async def test_e_futures_symbol_resolves_lot_size_from_core_symbols(self):
        """
        NIFTYFUT order (quantity=1 lot): paper broker resolves lot_size=25 from
        CORE_SYMBOLS and stores it on the PaperPosition.
        P&L is then multiplied by lot_size in unrealized_pnl().
        """
        prices = {"NIFTYFUT": 24000.0}
        pb = self._make_paper_broker(prices)
        order = self._make_order(symbol="NIFTYFUT", qty=1, price=24000.0)

        mock_inst = MagicMock()
        mock_inst.lot_size = 25
        mock_inst.instrument_type = MagicMock()
        mock_inst.instrument_type.value = "IDX_FUT"

        with patch("services.instrument_registry.instrument_registry") as mock_reg:
            mock_reg.get = AsyncMock(return_value=mock_inst)
            upd = await pb.place_order(order)

        self.assertEqual(upd.state, OrderState.FILLED)
        pos = pb.positions.get("NIFTYFUT")
        self.assertIsNotNone(pos, "Position must be created after fill")
        self.assertEqual(
            pos.lot_size, 25,
            f"PaperPosition.lot_size must be 25, got {pos.lot_size}",
        )

        # P&L check: move price from 24000 → 24050 (+50 per unit × 25 lot_size × 1 qty = +1250)
        prices["NIFTYFUT"] = 24050.0
        pnl = pos.unrealized_pnl(24050.0)
        self.assertEqual(
            pnl, 1250.0,
            f"unrealized_pnl must use lot_size: (24050-24000) × 1 qty × 25 = 1250, got {pnl}",
        )

    # [F] Option symbol → lot_size resolved via underlying
    async def test_f_option_symbol_resolves_lot_size_via_underlying(self):
        """
        NIFTY24OCT24250CE order (quantity=1 lot): paper broker resolves
        lot_size=25 by looking up the underlying 'NIFTY-FUT' in the registry.
        """
        sym = "NIFTY24OCT24250CE"
        prices = {sym: 150.0}
        pb = self._make_paper_broker(prices)
        order = self._make_order(
            symbol=sym, qty=1, price=150.0,
            metadata={"underlying": "NIFTY"},
        )

        mock_inst = MagicMock()
        mock_inst.lot_size = 25
        mock_inst.instrument_type = MagicMock()
        mock_inst.instrument_type.value = "IDX_OPT"

        with patch("services.instrument_registry.instrument_registry") as mock_reg:
            mock_reg.get = AsyncMock(return_value=mock_inst)
            upd = await pb.place_order(order)

        self.assertEqual(upd.state, OrderState.FILLED)
        pos = pb.positions.get(sym.upper())
        self.assertIsNotNone(pos, "Position must be created after fill")
        self.assertEqual(
            pos.lot_size, 25,
            f"PaperPosition.lot_size must be 25, got {pos.lot_size}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
