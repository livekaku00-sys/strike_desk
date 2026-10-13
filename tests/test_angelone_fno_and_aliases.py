"""
AngelOne F&O contract resolution, and the TATAMOTORS -> TMPV rename.

Option contracts cannot be matched by name across brokers: AngelOne spells one
NIFTY29SEP2623100CE (day + two-digit year) while the canonical form is
NIFTY26SEP23100CE. Matching is therefore structural — underlying, expiry, strike,
type — which is what the signal already carries.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from adapters.angelone_adapter import AngelOneAdapter, _parse_angel_expiry
from adapters.dhan_adapter import DhanAdapter
from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from models.order import Order, OrderSide, OrderType, ProductType
from utils.exceptions import DataError
from utils.symbol_aliases import canonical_symbol, canonical_symbols, historical_names

# One OPTIDX and one OPTSTK row in AngelOne's master format. Strike is in paise.
FAKE_MASTER = [
    {"token": "74060", "symbol": "NIFTY29SEP2624050CE", "name": "NIFTY",
     "expiry": "29SEP2026", "strike": "2405000.000000", "lotsize": "65",
     "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
    {"token": "74061", "symbol": "NIFTY29SEP2624050PE", "name": "NIFTY",
     "expiry": "29SEP2026", "strike": "2405000.000000", "lotsize": "65",
     "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
    {"token": "106362", "symbol": "RELIANCE29SEP261230CE", "name": "RELIANCE",
     "expiry": "29SEP2026", "strike": "123000.000000", "lotsize": "500",
     "instrumenttype": "OPTSTK", "exch_seg": "NFO"},
    {"token": "999999", "symbol": "TMPV29SEP26700CE", "name": "TMPV",
     "expiry": "29SEP2026", "strike": "70000.000000", "lotsize": "1500",
     "instrumenttype": "OPTSTK", "exch_seg": "NFO"},
    # Futures rows — must now land in _futures_index, NOT in _fno_index.
    {"token": "1", "symbol": "NIFTY29SEP26FUT", "name": "NIFTY",
     "expiry": "29SEP2026", "strike": "0", "lotsize": "65",
     "instrumenttype": "FUTIDX", "exch_seg": "NFO"},
    {"token": "555", "symbol": "RELIANCE29SEP26FUT", "name": "RELIANCE",
     "expiry": "29SEP2026", "strike": "0", "lotsize": "500",
     "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
]


def mk_order(symbol, metadata=None):
    return Order(client_order_id="t", symbol=symbol, exchange="NSE",
                 side=OrderSide.BUY, order_type=OrderType.MARKET,
                 quantity=1, price=0.0, metadata=metadata or {})


class TestAngelOneFnO(unittest.TestCase):
    def setUp(self):
        self.a = AngelOneAdapter()
        # Load from the fixture rather than the network.
        import adapters.angelone_adapter as mod
        self._orig = mod.download_json if hasattr(mod, "download_json") else None
        import services.broker_discovery.universe as uni
        self._orig_dl = uni.download_json
        uni.download_json = lambda url, timeout=180: FAKE_MASTER
        asyncio.run(self.a.load_fno_master(["NIFTY", "RELIANCE", "TMPV"]))

    def tearDown(self):
        import services.broker_discovery.universe as uni
        uni.download_json = self._orig_dl

    def test_a_expiry_parser(self):
        self.assertEqual(_parse_angel_expiry("29SEP2026").isoformat(), "2026-09-29")
        self.assertIsNone(_parse_angel_expiry("garbage"))
        self.assertIsNone(_parse_angel_expiry(None))

    def test_b_only_option_rows_are_indexed(self):
        self.assertEqual(len(self.a._fno_index), 4)           # options index unchanged
        self.assertNotIn("NIFTY29SEP26FUT", self.a._fno_by_symbol)  # options by-sym unchanged
        # Futures go into the separate futures index.
        self.assertEqual(len(self.a._futures_index), 2)        # NIFTY + RELIANCE
        self.assertIn("NIFTY", self.a._futures_index)
        self.assertIn("RELIANCE", self.a._futures_index)

    def test_c_strike_is_converted_from_paise(self):
        entry = self.a.resolve_fno_contract("NIFTY", "2026-09-29", 24050.0, "CE")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["strike"], 24050.0)
        self.assertEqual(entry["symboltoken"], "74060")

    def test_d_call_and_put_resolve_to_different_tokens(self):
        ce = self.a.resolve_fno_contract("NIFTY", "2026-09-29", 24050.0, "CE")
        pe = self.a.resolve_fno_contract("NIFTY", "2026-09-29", 24050.0, "PE")
        self.assertNotEqual(ce["symboltoken"], pe["symboltoken"])

    def test_e_order_resolves_structurally_not_by_name(self):
        # Canonical name differs from AngelOne's spelling; resolution must still work.
        order = mk_order("NIFTY26SEP24050CE", {
            "underlying": "NIFTY", "selected_expiry": "2026-09-29",
            "selected_strike": 24050.0, "option_type": "CE",
        })
        got = self.a.resolve_order_instrument(order)
        self.assertEqual(got["symboltoken"], "74060")
        self.assertEqual(got["tradingsymbol"], "NIFTY29SEP2624050CE")
        self.assertEqual(got["exchange"], "NFO")
        self.assertNotEqual(got["tradingsymbol"], order.symbol)

    def test_f_stock_option_resolves(self):
        got = self.a.resolve_order_instrument(mk_order("RELIANCE26SEP1230CE", {
            "underlying": "RELIANCE", "selected_expiry": "2026-09-29",
            "selected_strike": 1230.0, "option_type": "CE",
        }))
        self.assertEqual(got["symboltoken"], "106362")

    def test_g_nearby_expiry_is_tolerated(self):
        # Sources can disagree by a day or two on the expiry date.
        got = self.a.resolve_fno_contract("NIFTY", "2026-09-28", 24050.0, "CE")
        self.assertIsNotNone(got)
        self.assertEqual(got["symboltoken"], "74060")

    def test_h_far_expiry_is_not_silently_substituted(self):
        self.assertIsNone(self.a.resolve_fno_contract("NIFTY", "2026-12-29", 24050.0, "CE"))

    def test_i_unknown_strike_still_refuses(self):
        with self.assertRaises(DataError):
            self.a.resolve_order_instrument(mk_order("NIFTY26SEP99999CE", {
                "underlying": "NIFTY", "selected_expiry": "2026-09-29",
                "selected_strike": 99999.0, "option_type": "CE",
            }))

    def test_j_angelone_still_ignores_dhans_id(self):
        with self.assertRaises(DataError):
            self.a.resolve_order_instrument(
                mk_order("NIFTY26SEP24050CE", {"dhan_security_id": "73906"})
            )


class TestAngelOneFuturesPayload(unittest.IsolatedAsyncioTestCase):
    """
    Tests for AngelOne F&O order payload construction:
    - Futures resolve via _futures_index to a valid numeric symboltoken + NFO exchange
    - producttype = CARRYFORWARD for NFO orders (never DELIVERY)
    - quantity is lot-size-multiplied (lots → shares)
    """

    def setUp(self):
        self.a = AngelOneAdapter()
        import services.broker_discovery.universe as uni
        self._orig_dl = uni.download_json
        uni.download_json = lambda url, timeout=180: FAKE_MASTER
        asyncio.run(self.a.load_fno_master(["NIFTY", "RELIANCE", "TMPV"]))

    def tearDown(self):
        import services.broker_discovery.universe as uni
        uni.download_json = self._orig_dl

    # ── Futures resolution ────────────────────────────────────────────────

    def test_k_nifty_fut_resolves_to_front_month_contract(self):
        """NIFTY-FUT → front-month futures symboltoken and NFO exchange."""
        order = mk_order("NIFTY-FUT")
        got = self.a.resolve_order_instrument(order)
        self.assertEqual(got["symboltoken"], "1")
        self.assertEqual(got["tradingsymbol"], "NIFTY29SEP26FUT")
        self.assertEqual(got["exchange"], "NFO")

    def test_l_reliance_fut_resolves_to_stock_futures(self):
        """RELIANCE-FUT → RELIANCE29SEP26FUT with lot_size 500."""
        order = mk_order("RELIANCE-FUT")
        got = self.a.resolve_order_instrument(order)
        self.assertEqual(got["symboltoken"], "555")
        self.assertEqual(got["tradingsymbol"], "RELIANCE29SEP26FUT")
        self.assertEqual(got["exchange"], "NFO")

    # ── producttype ───────────────────────────────────────────────────────

    async def test_m_futures_order_payload_uses_carryforward_not_delivery(self):
        """NFO order must use producttype=CARRYFORWARD, never DELIVERY."""
        captured = {}

        def fake_place_order(params):
            captured.update(params)
            return {"status": True, "data": {"orderid": "AO123"}}

        mock_api = MagicMock()
        mock_api.placeOrder = fake_place_order
        self.a._smart_api = mock_api

        order = mk_order("NIFTY-FUT")  # default product_type = INTRADAY but exchange is NFO
        # Even DELIVERY order_type must yield CARRYFORWARD on NFO.
        order.product_type = ProductType.DELIVERY
        await self.a.place_order(order)

        self.assertEqual(captured.get("exchange"), "NFO")
        self.assertEqual(captured.get("producttype"), "CARRYFORWARD")
        self.assertEqual(captured.get("symboltoken"), "1")

    async def test_n_intraday_fno_order_uses_intraday_producttype(self):
        """MIS F&O order explicitly requesting INTRADAY → INTRADAY, not CARRYFORWARD."""
        captured = {}

        def fake_place_order(params):
            captured.update(params)
            return {"status": True, "data": {"orderid": "AO124"}}

        mock_api = MagicMock()
        mock_api.placeOrder = fake_place_order
        self.a._smart_api = mock_api

        order = mk_order("NIFTY-FUT")
        order.product_type = ProductType.INTRADAY
        await self.a.place_order(order)

        self.assertEqual(captured.get("producttype"), "INTRADAY")

    # ── lot_size quantity multiplication ─────────────────────────────────

    async def test_o_stock_future_quantity_is_lot_size_multiplied(self):
        """1-lot RELIANCE-FUT order → quantity=500 in the API payload (1 * 500)."""
        captured = {}

        def fake_place_order(params):
            captured.update(params)
            return {"status": True, "data": {"orderid": "AO125"}}

        mock_api = MagicMock()
        mock_api.placeOrder = fake_place_order
        self.a._smart_api = mock_api

        order = mk_order("RELIANCE-FUT")  # quantity=1 lot in mk_order
        await self.a.place_order(order)

        self.assertEqual(captured.get("quantity"), "500")

    async def test_p_index_future_quantity_is_lot_size_multiplied(self):
        """2-lot NIFTY-FUT order → quantity=130 in the API payload (2 * 65)."""
        captured = {}

        def fake_place_order(params):
            captured.update(params)
            return {"status": True, "data": {"orderid": "AO126"}}

        mock_api = MagicMock()
        mock_api.placeOrder = fake_place_order
        self.a._smart_api = mock_api

        order = mk_order("NIFTY-FUT")
        order.quantity = 2
        await self.a.place_order(order)

        self.assertEqual(captured.get("quantity"), "130")  # 2 * 65


class TestSymbolAliases(unittest.TestCase):
    def test_a_legacy_name_maps_forward(self):
        self.assertEqual(canonical_symbol("TATAMOTORS"), "TMPV")
        self.assertEqual(canonical_symbol("tatamotors"), "TMPV")

    def test_b_current_name_is_unchanged(self):
        self.assertEqual(canonical_symbol("TMPV"), "TMPV")
        self.assertEqual(canonical_symbol("RELIANCE"), "RELIANCE")

    def test_c_list_canonicalisation_dedupes(self):
        out = canonical_symbols(["TATAMOTORS", "TMPV", "RELIANCE"])
        self.assertEqual(out, ["TMPV", "RELIANCE"])

    def test_d_historical_names_are_recoverable(self):
        names = historical_names("TMPV")
        self.assertEqual(names[0], "TMPV")
        self.assertIn("TATAMOTORS", names)

    def test_e_adapters_resolve_the_legacy_name(self):
        dhan = DhanAdapter()
        angel = AngelOneAdapter()
        inst = Instrument(exchange=Exchange.NSE, symbol="TMPV",
                          asset_class=AssetClass.STOCK_CASH,
                          instrument_type=InstrumentType.EQ, token="3456")
        dhan.register_equity_instruments([inst])
        angel.register_equity_instruments([inst])
        # An order still carrying the pre-rename name must reach the same instrument.
        self.assertEqual(dhan.resolve_order_instrument(mk_order("TATAMOTORS"))["security_id"], "3456")
        self.assertEqual(angel.resolve_order_instrument(mk_order("TATAMOTORS"))["symboltoken"], "3456")

    def test_f_watchlist_config_canonicalises(self):
        import os as _os
        from config.settings import Settings
        prev = _os.environ.get("FNO_OPTION_WATCHLIST")
        _os.environ["FNO_OPTION_WATCHLIST"] = "TATAMOTORS,RELIANCE"
        try:
            self.assertEqual(Settings().fno_option_watchlist, ["TMPV", "RELIANCE"])
        finally:
            if prev is None:
                _os.environ.pop("FNO_OPTION_WATCHLIST", None)
            else:
                _os.environ["FNO_OPTION_WATCHLIST"] = prev


if __name__ == "__main__":
    unittest.main()
