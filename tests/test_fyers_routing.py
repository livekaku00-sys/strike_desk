"""
Fyers instrument routing.

Fyers addresses instruments by an exchange-prefixed symbol of its own
(NSE:RELIANCE-EQ, NSE:NIFTY26SEP24050CE) — not a numeric token like Dhan or
AngelOne. Its option symbol happens to resemble the canonical contract name, which
makes it especially important to prove the value is read from Fyers' own master
rather than assembled from a string.
"""
import asyncio
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from adapters.angelone_adapter import AngelOneAdapter
from adapters.dhan_adapter import DhanAdapter
from adapters.fyers_adapter import FyersAdapter
from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from models.order import Order, OrderSide, OrderType
from utils.exceptions import DataError

# Real column layout of Fyers' NSE_FO.csv. Strike (col 15) is in rupees, expiry
# (col 8) is an epoch, and col 9 is the tradeable symbol.
def fo_row(fytoken, desc, lot, epoch, symbol, underlying, strike, opt_type):
    row = [""] * 21
    row[0], row[1], row[3], row[8] = fytoken, desc, str(lot), str(epoch)
    row[9], row[13], row[15], row[16] = symbol, underlying, str(strike), opt_type
    return row


# 1790676600 == 2026-09-29 IST
FAKE_FO_ROWS = [
    fo_row("101126092935034", "NIFTY 29 Sep 26 24050 CE", 65, 1790676600,
           "NSE:NIFTY26SEP24050CE", "NIFTY", 24050.0, "CE"),
    fo_row("101126092935035", "NIFTY 29 Sep 26 24050 PE", 65, 1790676600,
           "NSE:NIFTY26SEP24050PE", "NIFTY", 24050.0, "PE"),
    fo_row("101126092935036", "RELIANCE 29 Sep 26 1230 CE", 500, 1790676600,
           "NSE:RELIANCE26SEP1230CE", "RELIANCE", 1230.0, "CE"),
    # A futures row carries no option type and must be ignored.
    fo_row("101126092935037", "NIFTY 29 Sep 26 FUT", 65, 1790676600,
           "NSE:NIFTY26SEPFUT", "NIFTY", 0.0, ""),
]


def fake_csv():
    import csv, io
    buf = io.StringIO()
    csv.writer(buf).writerows(FAKE_FO_ROWS)
    return buf.getvalue()


def mk_order(symbol, metadata=None):
    return Order(client_order_id="t", symbol=symbol, exchange="NSE",
                 side=OrderSide.BUY, order_type=OrderType.MARKET,
                 quantity=1, price=0.0, metadata=metadata or {})


class TestFyersRouting(unittest.TestCase):
    def setUp(self):
        import services.broker_discovery.universe as uni
        self._orig = uni.download_text
        uni.download_text = lambda url, timeout=120: fake_csv()
        self.f = FyersAdapter()
        asyncio.run(self.f.load_fno_master(["NIFTY", "RELIANCE"]))
        self.f.register_equity_instruments([
            Instrument(exchange=Exchange.NSE, symbol="RELIANCE",
                       asset_class=AssetClass.STOCK_CASH,
                       instrument_type=InstrumentType.EQ, token="10100000002885",
                       broker_symbol="NSE:RELIANCE-EQ"),
        ])

    def tearDown(self):
        import services.broker_discovery.universe as uni
        uni.download_text = self._orig

    def test_a_only_option_rows_indexed(self):
        self.assertEqual(len(self.f._fno_index), 3)
        self.assertNotIn("NSE:NIFTY26SEPFUT", self.f._fno_by_symbol)

    def test_b_strike_is_rupees_not_paise(self):
        entry = self.f.resolve_fno_contract("NIFTY", "2026-09-29", 24050.0, "CE")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["strike"], 24050.0)

    def test_c_expiry_epoch_parsed_to_ist_date(self):
        self.assertIn(("NIFTY", "2026-09-29", 24050.0, "CE"), self.f._fno_index)

    def test_d_option_resolves_to_the_masters_own_symbol(self):
        got = self.f.resolve_order_instrument(mk_order("NIFTY26SEP24050CE", {
            "underlying": "NIFTY", "selected_expiry": "2026-09-29",
            "selected_strike": 24050.0, "option_type": "CE",
        }))
        self.assertEqual(got["symbol"], "NSE:NIFTY26SEP24050CE")
        # The value must come from the master, not be assembled by string work.
        self.assertIn(got["symbol"], self.f._fno_by_symbol)

    def test_e_call_and_put_differ(self):
        ce = self.f.resolve_fno_contract("NIFTY", "2026-09-29", 24050.0, "CE")
        pe = self.f.resolve_fno_contract("NIFTY", "2026-09-29", 24050.0, "PE")
        self.assertNotEqual(ce["symbol"], pe["symbol"])

    def test_f_equity_uses_the_eq_suffixed_symbol(self):
        self.assertEqual(self.f.resolve_order_instrument(mk_order("RELIANCE"))["symbol"],
                         "NSE:RELIANCE-EQ")

    def test_g_index_uses_the_index_symbol(self):
        self.assertEqual(self.f.resolve_order_instrument(mk_order("NIFTY"))["symbol"],
                         "NSE:NIFTY50-INDEX")

    def test_h_unknown_symbol_is_refused(self):
        for bad in ("NOT_A_SYMBOL", "NIFTY26SEP99999CE"):
            with self.subTest(symbol=bad):
                with self.assertRaises(DataError):
                    self.f.resolve_order_instrument(mk_order(bad))

    def test_i_foreign_broker_ids_are_ignored_not_consumed(self):
        """Another broker's id must not influence the Fyers symbol chosen."""
        sym = "NIFTY26SEP24050CE"
        meta = {"underlying": "NIFTY", "selected_expiry": "2026-09-29",
                "selected_strike": 24050.0, "option_type": "CE"}
        clean = self.f.resolve_order_instrument(mk_order(sym, meta))
        poisoned = self.f.resolve_order_instrument(mk_order(sym, {
            **meta, "dhan_security_id": "73906", "angel_symboltoken": "74060",
        }))
        self.assertEqual(clean, poisoned)
        self.assertNotIn("73906", str(poisoned))
        self.assertNotIn("74060", str(poisoned))

    def test_j_subscription_drops_unmapped_symbols(self):
        # The old ".get(sym, sym)" fallback sent canonical symbols to Fyers as if
        # they were Fyers symbols.
        out = self.f._to_fyers_symbols(["NIFTY", "RELIANCE", "NO_SUCH_SYMBOL"])
        self.assertIn("NSE:NIFTY50-INDEX", out)
        self.assertIn("NSE:RELIANCE-EQ", out)
        self.assertNotIn("NO_SUCH_SYMBOL", out)
        self.assertEqual(len(out), 2)

    def test_k_legacy_name_resolves_through_the_alias(self):
        self.f.register_equity_instruments([
            Instrument(exchange=Exchange.NSE, symbol="TMPV",
                       asset_class=AssetClass.STOCK_CASH,
                       instrument_type=InstrumentType.EQ, token="1",
                       broker_symbol="NSE:TMPV-EQ"),
        ])
        self.assertEqual(self.f.resolve_order_instrument(mk_order("TATAMOTORS"))["symbol"],
                         "NSE:TMPV-EQ")

    def test_l_three_brokers_produce_three_different_addresses(self):
        """The same instrument is addressed differently by each broker."""
        dhan, angel = DhanAdapter(), AngelOneAdapter()
        d = dhan.resolve_order_instrument(mk_order("NIFTY"))["security_id"]
        a = angel.resolve_order_instrument(mk_order("NIFTY"))["symboltoken"]
        f = self.f.resolve_order_instrument(mk_order("NIFTY"))["symbol"]
        self.assertEqual(len({d, a, f}), 3, f"addresses collided: {d} {a} {f}")
        self.assertTrue(f.startswith("NSE:"))
        self.assertTrue(d.isdigit() and a.isdigit())


if __name__ == "__main__":
    unittest.main()
