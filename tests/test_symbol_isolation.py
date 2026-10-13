"""
Broker-specific instrument routing.

The same canonical symbol means a different instrument at each broker — NIFTY is
security_id 13 at Dhan and symboltoken 26000 at AngelOne. Equity ids happen to
coincide (RELIANCE is 2885 at both), which is precisely why a bug here is easy to
miss: it works by accident for cash and silently addresses the wrong instrument
for indices and derivatives.

Each adapter must resolve only from its own map, and refuse rather than guess.
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from adapters.angelone_adapter import AngelOneAdapter
from adapters.dhan_adapter import DhanAdapter
from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from models.order import Order, OrderSide, OrderType
from utils.exceptions import DataError


def mk_order(symbol, metadata=None):
    return Order(
        client_order_id="t1", symbol=symbol, exchange="NSE",
        side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=1, price=0.0, metadata=metadata or {},
    )


def mk_instrument(symbol, token, exchange=Exchange.NSE, itype=InstrumentType.EQ):
    return Instrument(
        exchange=exchange, symbol=symbol, asset_class=AssetClass.STOCK_CASH,
        instrument_type=itype, token=token,
    )


class TestBrokerSymbolIsolation(unittest.TestCase):
    def setUp(self):
        self.dhan = DhanAdapter()
        self.angel = AngelOneAdapter()
        # Deliberately different tokens for the same symbol, as the real masters are.
        self.dhan.register_equity_instruments([
            mk_instrument("RELIANCE", "2885"),
            mk_instrument("SOMEBSE", "500325", exchange=Exchange.BSE),
        ])
        self.angel.register_equity_instruments([
            mk_instrument("RELIANCE", "2885"),
            mk_instrument("SOMEBSE", "500325", exchange=Exchange.BSE),
        ])

    def test_a_index_resolves_to_different_ids_per_broker(self):
        d = self.dhan.resolve_order_instrument(mk_order("NIFTY"))
        a = self.angel.resolve_order_instrument(mk_order("NIFTY"))
        self.assertEqual(d["security_id"], "13")
        self.assertEqual(a["symboltoken"], "26000")
        self.assertNotEqual(d["security_id"], a["symboltoken"])

    def test_b_index_uses_the_index_segment_not_equity(self):
        self.assertEqual(self.dhan.resolve_order_instrument(mk_order("NIFTY"))["exchange_segment"], "IDX_I")

    def test_c_equity_uses_the_equity_segment(self):
        # Regression: instrument_type is a (str, Enum) whose str() is
        # "InstrumentType.EQ", so a naive compare filed every equity under F&O.
        d = self.dhan.resolve_order_instrument(mk_order("RELIANCE"))
        self.assertEqual(d["exchange_segment"], "NSE_EQ")

    def test_d_angelone_equity_carries_the_eq_suffix(self):
        a = self.angel.resolve_order_instrument(mk_order("RELIANCE"))
        self.assertEqual(a["tradingsymbol"], "RELIANCE-EQ")
        self.assertEqual(a["exchange"], "NSE")

    def test_e_bse_instrument_is_not_flattened_to_nse(self):
        # Regression: exchange is also a (str, Enum); reading it raw sent BSE to NSE.
        a = self.angel.resolve_order_instrument(mk_order("SOMEBSE"))
        self.assertEqual(a["exchange"], "BSE")

    def test_f_unknown_symbol_is_refused_by_both(self):
        for adapter in (self.dhan, self.angel):
            with self.subTest(adapter=adapter.__class__.__name__):
                with self.assertRaises(DataError):
                    adapter.resolve_order_instrument(mk_order("NOT_A_REAL_SYMBOL"))

    def test_g_option_contract_without_a_mapping_is_refused(self):
        # Contract names cannot be converted to a broker id by string manipulation.
        for adapter in (self.dhan, self.angel):
            with self.subTest(adapter=adapter.__class__.__name__):
                with self.assertRaises(DataError):
                    adapter.resolve_order_instrument(mk_order("NIFTY26SEP23100CE"))

    def test_h_explicit_dhan_id_is_used_only_by_dhan(self):
        meta = {"dhan_security_id": "73906", "dhan_segment": "NSE_FNO"}
        d = self.dhan.resolve_order_instrument(mk_order("NIFTY26SEP23100CE", meta))
        self.assertEqual(d["security_id"], "73906")
        self.assertEqual(d["exchange_segment"], "NSE_FNO")
        # AngelOne must not consume another broker's id.
        with self.assertRaises(DataError):
            self.angel.resolve_order_instrument(mk_order("NIFTY26SEP23100CE", meta))

    def test_i_explicit_angel_token_is_used_only_by_angelone(self):
        meta = {"angel_symboltoken": "54321", "angel_tradingsymbol": "NIFTY26SEP23100CE", "angel_exchange": "NFO"}
        a = self.angel.resolve_order_instrument(mk_order("NIFTY26SEP23100CE", meta))
        self.assertEqual(a["symboltoken"], "54321")
        self.assertEqual(a["exchange"], "NFO")
        with self.assertRaises(DataError):
            self.dhan.resolve_order_instrument(mk_order("NIFTY26SEP23100CE", meta))

    def test_j_one_adapters_registrations_never_reach_the_other(self):
        solo = DhanAdapter()
        solo.register_equity_instruments([mk_instrument("ONLYDHAN", "999")])
        self.assertEqual(solo.resolve_order_instrument(mk_order("ONLYDHAN"))["security_id"], "999")
        # A different adapter instance, and a different broker, know nothing of it.
        with self.assertRaises(DataError):
            self.angel.resolve_order_instrument(mk_order("ONLYDHAN"))
        with self.assertRaises(DataError):
            self.dhan.resolve_order_instrument(mk_order("ONLYDHAN"))

    def test_k_order_payload_never_contains_the_raw_symbol_as_an_id(self):
        # The original defect: the canonical symbol was passed where a numeric
        # broker id was required, so every live order was malformed.
        d = self.dhan.resolve_order_instrument(mk_order("RELIANCE"))
        a = self.angel.resolve_order_instrument(mk_order("RELIANCE"))
        self.assertNotEqual(d["security_id"], "RELIANCE")
        self.assertNotEqual(a["symboltoken"], "RELIANCE")
        self.assertTrue(d["security_id"].isdigit())
        self.assertTrue(a["symboltoken"].isdigit())


if __name__ == "__main__":
    unittest.main()
