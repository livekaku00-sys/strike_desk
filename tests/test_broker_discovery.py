import asyncio
from datetime import date, datetime, timezone
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from services.broker_discovery import (
    AngelOneDiscovery,
    DhanDiscovery,
    FyersDiscovery,
)
from services.instrument_discovery_service import (
    InstrumentDiscoveryService,
    instrument_discovery_service,
)


class TestBrokerDiscovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dhan = DhanDiscovery()
        self.angelone = AngelOneDiscovery()
        self.fyers = FyersDiscovery()
        self.service = InstrumentDiscoveryService(
            dhan_discovery=self.dhan,
            angelone_discovery=self.angelone,
            fyers_discovery=self.fyers,
        )

    async def test_a_dhan_discovery(self):
        mock_raw = [
            {
                "security_id": "2885",
                "custom_symbol": "RELIANCE",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "lot_size": 1,
                "tick_size": 0.05,
                "isin": "INE002A01018",
                "display_name": "Reliance Industries Ltd",
            },
            {
                "security_id": "35012",
                "custom_symbol": "NIFTY26SEP24500CE",
                "exchange_segment": "NSE_FNO",
                "instrument_type": "OPTIDX",
                "lot_size": 25,
                "tick_size": 0.05,
                "strike_price": 24500.0,
                "option_type": "CE",
                "expiry_date": "2026-09-26",
                "underlying_symbol": "NIFTY",
            },
        ]
        with patch.object(self.dhan, "_fetch_raw_contracts", new=AsyncMock(return_value=mock_raw)):
            instruments = await self.dhan.fetch_master_contracts()

        self.assertEqual(len(instruments), 2)
        inst1 = instruments[0]
        self.assertEqual(inst1.exchange, Exchange.NSE)
        self.assertEqual(inst1.symbol, "RELIANCE")
        self.assertEqual(inst1.token, "2885")
        self.assertEqual(inst1.asset_class, AssetClass.STOCK_CASH)
        self.assertEqual(inst1.lot_size, 1)

        inst2 = instruments[1]
        self.assertEqual(inst2.symbol, "NIFTY26SEP24500CE")
        self.assertEqual(inst2.token, "35012")
        self.assertEqual(inst2.asset_class, AssetClass.INDEX_OPTIONS)
        self.assertEqual(inst2.strike_price, 24500.0)
        self.assertEqual(inst2.option_type, "CE")
        print("✓ [a] Dhan discovery: Mock Dhan API response normalized to Instrument model")

    async def test_b_angelone_discovery(self):
        mock_raw = [
            {
                "token": "3045",
                "symbol": "SBIN-EQ",
                "name": "SBIN",
                "exch_seg": "NSE",
                "instrumenttype": "",
                "lotsize": "1",
                "tick_size": "5.0",
                "isin": "INE062A01020",
            },
            {
                # F&O entry — must be silently dropped (AngelOne is data feed only).
                "token": "42100",
                "symbol": "NIFTY26SEP24500CE",
                "name": "NIFTY",
                "exch_seg": "NFO",
                "instrumenttype": "OPTIDX",
                "lotsize": "25",
                "tick_size": "0.05",
                "strike": "24500.0",
                "expiry": "26SEP2026",
            },
        ]
        with patch.object(self.angelone, "_fetch_raw_contracts", new=AsyncMock(return_value=mock_raw)):
            instruments = await self.angelone.fetch_master_contracts()

        # Only the equity row passes through; F&O is rejected by normalize_instrument.
        self.assertEqual(len(instruments), 1)
        eq = instruments[0]
        self.assertEqual(eq.symbol, "SBIN-EQ")
        self.assertEqual(eq.token, "3045")
        self.assertEqual(eq.asset_class, AssetClass.STOCK_CASH)
        self.assertEqual(eq.tick_size, 0.05)
        print("✓ [b] AngelOne discovery: equity passes, F&O silently dropped")

    async def test_c_fyers_discovery(self):
        mock_raw = [
            {
                "symbol": "NSE:RELIANCE-EQ",
                "fyToken": "10100000002885",
                "lotSize": 1,
                "tickSize": 0.05,
                "instrument_type": "EQ",
                "isin": "INE002A01018",
                "description": "Reliance Industries Limited",
            },
            {
                # F&O entry — must be silently dropped (Fyers is data feed only).
                "symbol": "NSE:SBIN26SEPFUT",
                "fyToken": "10100000009999",
                "lotSize": 750,
                "tickSize": 0.05,
                "instrument_type": "FUTSTK",
                "underlying": "SBIN",
                "expiry": "2026-09-26",
            },
        ]
        with patch.object(self.fyers, "_fetch_raw_contracts", new=AsyncMock(return_value=mock_raw)):
            instruments = await self.fyers.fetch_master_contracts()

        # Only the equity row passes through; F&O is rejected by normalize_instrument.
        self.assertEqual(len(instruments), 1)
        eq = instruments[0]
        self.assertEqual(eq.symbol, "RELIANCE-EQ")
        self.assertEqual(eq.broker_symbol, "NSE:RELIANCE-EQ")
        self.assertEqual(eq.token, "10100000002885")
        self.assertEqual(eq.asset_class, AssetClass.STOCK_CASH)
        print("✓ [c] Fyers discovery: equity passes, F&O silently dropped")

    async def test_d_lot_size_extraction(self):
        raw_dhan_bad_lot = {
            "security_id": "1111",
            "trading_symbol": "TEST1",
            "lot_size": 0,
        }
        inst_dhan = self.dhan.normalize_instrument(raw_dhan_bad_lot)
        self.assertIsNotNone(inst_dhan)
        self.assertGreater(inst_dhan.lot_size, 0)

        raw_angel_bad_lot = {
            "token": "2222",
            "tradingsymbol": "TEST2",
            "lotsize": "-5",
        }
        inst_angel = self.angelone.normalize_instrument(raw_angel_bad_lot)
        self.assertIsNotNone(inst_angel)
        self.assertGreater(inst_angel.lot_size, 0)

        raw_fyers_bad_lot = {
            "symbol": "NSE:TEST3-EQ",
            "lotSize": None,
        }
        inst_fyers = self.fyers.normalize_instrument(raw_fyers_bad_lot)
        self.assertIsNotNone(inst_fyers)
        self.assertGreater(inst_fyers.lot_size, 0)
        print("✓ [d] Lot-size extraction: Verified lot_size > 0 for all instruments")

    async def test_e_stale_token_detection(self):
        # Round 1: Token is "100"
        mock_round_1 = [
            {"security_id": "100", "trading_symbol": "TOKEN_TEST", "exchange_segment": "NSE_EQ"}
        ]
        with patch.object(self.dhan, "_fetch_raw_contracts", new=AsyncMock(return_value=mock_round_1)):
            res1 = await self.service.discover_broker("dhan", force_refresh=True)
            self.assertEqual(res1[0].token, "100")
            self.assertEqual(len(self.service.detect_stale_tokens("dhan")), 0)

        # Round 2: Token changed to "200"
        mock_round_2 = [
            {"security_id": "200", "trading_symbol": "TOKEN_TEST", "exchange_segment": "NSE_EQ"}
        ]
        with patch.object(self.dhan, "_fetch_raw_contracts", new=AsyncMock(return_value=mock_round_2)):
            res2 = await self.service.discover_broker("dhan", force_refresh=True)
            self.assertEqual(res2[0].token, "200")
            stale = self.service.detect_stale_tokens("dhan")
            self.assertIn("TOKEN_TEST", stale)
        print("✓ [e] Stale-token detection: Detected token change from '100' to '200'")

    async def test_f_caching(self):
        call_count = 0

        async def mock_fetch():
            nonlocal call_count
            call_count += 1
            return [{"security_id": "123", "trading_symbol": "CACHE_TEST"}]

        with patch.object(self.dhan, "_fetch_raw_contracts", side_effect=mock_fetch):
            res1 = await self.service.discover_broker("dhan", force_refresh=True)
            self.assertEqual(len(res1), 1)
            self.assertEqual(call_count, 1)

            res2 = await self.service.discover_broker("dhan", force_refresh=False)
            self.assertEqual(len(res2), 1)
            self.assertEqual(call_count, 1)

            res3 = await self.service.discover_broker("dhan", force_refresh=True)
            self.assertEqual(len(res3), 1)
            self.assertEqual(call_count, 2)
        print("✓ [f] Caching: Verified 2nd call uses cache, force_refresh refreshes")

    async def test_g_error_handling(self):
        dhan_items = [{"security_id": "1", "trading_symbol": "DHAN_SYM"}]
        fyers_items = [{"symbol": "NSE:FYERS_SYM", "fyToken": "3"}]

        with patch.object(self.dhan, "_fetch_raw_contracts", new=AsyncMock(return_value=dhan_items)):
            with patch.object(self.angelone, "_fetch_raw_contracts", side_effect=RuntimeError("AngelOne connection timeout")):
                with patch.object(self.fyers, "_fetch_raw_contracts", new=AsyncMock(return_value=fyers_items)):
                    res = await self.service.discover_all_brokers(force_refresh=True)

        self.assertIn("dhan", res)
        self.assertIn("angelone", res)
        self.assertIn("fyers", res)
        self.assertEqual(len(res["dhan"]), 1)
        self.assertEqual(len(res["angelone"]), 0)
        self.assertEqual(len(res["fyers"]), 1)
        print("✓ [g] Error handling: One broker failure handled gracefully without crashing others")

    async def test_h_dynamic_cache_update(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            tmp_cache_path = tf.name

        try:
            test_instruments = [
                Instrument(
                    exchange=Exchange.NSE,
                    symbol="DYNAMIC_TEST",
                    token="9999",
                    asset_class=AssetClass.STOCK_CASH,
                    instrument_type=InstrumentType.EQ,
                    isin="INE999999999",
                    name="Dynamic Test Corp",
                )
            ]
            self.service.update_metadata_cache(test_instruments, cache_file=tmp_cache_path)

            with open(tmp_cache_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)

            self.assertIn("_warning", data)
            self.assertIn("DYNAMIC_TEST", data)
            self.assertEqual(data["DYNAMIC_TEST"]["isin"], "INE999999999")
            self.assertEqual(data["DYNAMIC_TEST"]["exchange"], "NSE")
            self.assertEqual(data["DYNAMIC_TEST"]["name"], "Dynamic Test Corp")
        finally:
            if os.path.exists(tmp_cache_path):
                os.remove(tmp_cache_path)
        print("✓ [h] Dynamic cache update: Verified update_metadata_cache writes JSON automatically")


async def main():
    print("=" * 60)
    print("STRIKE DESK V4 — PHASE 2 PROMPT 2/5 BROKER DISCOVERY & DYNAMIC CACHE")
    print("=" * 60)

    suite = TestBrokerDiscovery()
    suite.setUp()
    await suite.test_a_dhan_discovery()
    await suite.test_b_angelone_discovery()
    await suite.test_c_fyers_discovery()
    await suite.test_d_lot_size_extraction()
    await suite.test_e_stale_token_detection()
    await suite.test_f_caching()
    await suite.test_g_error_handling()
    await suite.test_h_dynamic_cache_update()

    print("=" * 60)
    print("ALL PHASE 2 PROMPT 2/5 VERIFICATIONS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


class TestIndexExchangeClassification(unittest.TestCase):
    """
    P0-06 — Verify that each of the 5 core indices is classified under
    the correct exchange. SENSEX is a BSE index; the other four are NSE.
    This guards against documentation drift re-introducing the NSE/SENSEX error.
    """

    def test_angelone_sensex_is_bse(self):
        from adapters.angelone_adapter import _ANGEL_TOKEN_MAP
        _, exchange_type = _ANGEL_TOKEN_MAP["SENSEX"]
        # AngelOne exchange type: 1 = NSE, 3 = BSE
        self.assertEqual(exchange_type, 3, "SENSEX must map to exchange type 3 (BSE) in AngelOne")

    def test_angelone_nse_indices_are_nse(self):
        from adapters.angelone_adapter import _ANGEL_TOKEN_MAP
        for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            with self.subTest(symbol=sym):
                _, exchange_type = _ANGEL_TOKEN_MAP[sym]
                self.assertEqual(exchange_type, 1, f"{sym} must map to exchange type 1 (NSE) in AngelOne")

    def test_fyers_sensex_is_bse_prefixed(self):
        from adapters.fyers_adapter import _FYERS_SYMBOL_MAP
        fyers_sym = _FYERS_SYMBOL_MAP["SENSEX"]
        self.assertTrue(fyers_sym.startswith("BSE:"),
                        f"SENSEX Fyers symbol must be BSE-prefixed, got: {fyers_sym}")

    def test_fyers_nse_indices_are_nse_prefixed(self):
        from adapters.fyers_adapter import _FYERS_SYMBOL_MAP
        for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            with self.subTest(symbol=sym):
                fyers_sym = _FYERS_SYMBOL_MAP[sym]
                self.assertTrue(fyers_sym.startswith("NSE:"),
                                f"{sym} Fyers symbol must be NSE-prefixed, got: {fyers_sym}")

    def test_all_five_core_indices_present_in_each_adapter(self):
        from adapters.angelone_adapter import _ANGEL_TOKEN_MAP
        from adapters.fyers_adapter import _FYERS_SYMBOL_MAP
        from adapters.dhan_adapter import _DHAN_SECURITY_IDS
        core = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
        self.assertTrue(core.issubset(_ANGEL_TOKEN_MAP.keys()), "AngelOne missing core index")
        self.assertTrue(core.issubset(_FYERS_SYMBOL_MAP.keys()), "Fyers missing core index")
        self.assertTrue(core.issubset(_DHAN_SECURITY_IDS.keys()), "Dhan missing core index")


class TestAngelOneJsonParsing(unittest.TestCase):
    """
    Unit tests for AngelOneDiscovery._filter_master.

    Architecture (2026-09-30): AngelOne is a data feed only.
    NFO/BFO rows are silently dropped; only NSE/BSE cash/equity rows pass through.
    """

    def _filter(self, rows, tickers=None, isins=None):
        from services.broker_discovery.angelone_discovery import AngelOneDiscovery
        return AngelOneDiscovery()._filter_master(rows, tickers or set(), isins or set())

    # ── NFO/BFO rows are silently dropped ────────────────────────────────

    def test_futidx_nfo_row_is_excluded(self):
        """FUTIDX row in NFO segment is silently dropped."""
        rows = [
            {"token": "88001", "symbol": "NIFTY29OCT25FUT", "name": "NIFTY",
             "exch_seg": "NFO", "instrumenttype": "FUTIDX", "lotsize": "25",
             "tick_size": "0.05", "expiry": "29OCT2025"},
        ]
        result = self._filter(rows)
        syms = {r.get("symbol") or r.get("tradingsymbol") for r in result}
        self.assertNotIn("NIFTY-FUT", syms)
        self.assertEqual(len(result), 0)

    def test_futstk_nfo_row_is_excluded(self):
        """FUTSTK row in NFO segment is silently dropped regardless of universe membership."""
        rows = [
            {"token": "99001", "symbol": "RELIANCE29OCT25FUT", "name": "RELIANCE",
             "exch_seg": "NFO", "instrumenttype": "FUTSTK", "lotsize": "500",
             "tick_size": "0.05", "expiry": "29OCT2025"},
        ]
        result = self._filter(rows, tickers={"RELIANCE"})
        self.assertEqual(len(result), 0)

    def test_optidx_nfo_row_is_excluded(self):
        """OPTIDX row in NFO segment is silently dropped."""
        rows = [
            {"token": "60001", "symbol": "BANKNIFTY52000CE29SEP25", "name": "BANKNIFTY",
             "exch_seg": "NFO", "instrumenttype": "OPTIDX", "lotsize": "15",
             "tick_size": "0.05", "expiry": "29SEP2025"},
        ]
        result = self._filter(rows)
        self.assertEqual(len(result), 0)

    def test_optstk_nfo_row_is_excluded(self):
        """OPTSTK row in NFO segment is silently dropped."""
        rows = [
            {"token": "45001", "symbol": "LT3200CE29SEP25", "name": "LT",
             "exch_seg": "NFO", "instrumenttype": "OPTSTK", "lotsize": "175",
             "expiry": "29SEP2025"},
        ]
        result = self._filter(rows, tickers={"LT"})
        self.assertEqual(len(result), 0)

    def test_bfo_row_is_excluded(self):
        """BFO segment rows are silently dropped."""
        rows = [
            {"token": "77001", "symbol": "SENSEX29OCT25FUT", "name": "SENSEX",
             "exch_seg": "BFO", "instrumenttype": "FUTIDX", "lotsize": "10",
             "expiry": "29OCT2025"},
        ]
        result = self._filter(rows)
        self.assertEqual(len(result), 0)

    def test_mixed_nse_and_nfo_only_nse_rows_emitted(self):
        """Mixed input: NSE equity + NFO futures → only equity passes through."""
        rows = [
            {"token": "26000", "symbol": "NIFTY 50", "name": "NIFTY",
             "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1"},
            {"token": "1330", "symbol": "HINDUNILVR-EQ", "name": "HINDUNILVR",
             "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1"},
            {"token": "99001", "symbol": "HINDUNILVR29OCT25FUT", "name": "HINDUNILVR",
             "exch_seg": "NFO", "instrumenttype": "FUTSTK", "lotsize": "500",
             "expiry": "29OCT2025"},
        ]
        result = self._filter(rows, tickers={"HINDUNILVR"})
        syms = {r.get("symbol") or r.get("tradingsymbol") for r in result}
        self.assertIn("NIFTY", syms)            # spot index passes
        self.assertIn("HINDUNILVR", syms)       # equity passes
        self.assertNotIn("HINDUNILVR-FUT", syms)  # F&O dropped

    def test_no_fno_symbols_in_output_regardless_of_input(self):
        """No -FUT, CE, PE symbols appear in _filter_master output, no matter what input contains."""
        rows = [
            {"token": "88001", "symbol": "NIFTY29OCT25FUT", "name": "NIFTY",
             "exch_seg": "NFO", "instrumenttype": "FUTIDX", "lotsize": "25", "expiry": "29OCT2025"},
            {"token": "60001", "symbol": "BANKNIFTY52000CE29SEP25", "name": "BANKNIFTY",
             "exch_seg": "NFO", "instrumenttype": "OPTIDX", "lotsize": "15", "expiry": "29SEP2025"},
            {"token": "26000", "symbol": "NIFTY 50", "name": "NIFTY",
             "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1"},
        ]
        result = self._filter(rows)
        for row in result:
            sym = str(row.get("symbol") or row.get("tradingsymbol") or "")
            self.assertFalse(sym.endswith("FUT"), f"F&O symbol leaked: {sym}")
            self.assertFalse(sym.endswith("CE"), f"Option CE leaked: {sym}")
            self.assertFalse(sym.endswith("PE"), f"Option PE leaked: {sym}")

    # ── Equity and index rows still pass through ──────────────────────────

    def test_nse_equity_row_passes(self):
        """NSE EQ equity row is still emitted unchanged."""
        rows = [
            {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN",
             "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1"},
        ]
        result = self._filter(rows, tickers={"SBIN"})
        syms = {r.get("symbol") or r.get("tradingsymbol") for r in result}
        self.assertIn("SBIN", syms)

    def test_spot_index_row_passes(self):
        """NSE spot index (short-token AMXIDX) row is still emitted."""
        rows = [
            {"token": "26000", "symbol": "NIFTY 50", "name": "NIFTY",
             "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1"},
        ]
        result = self._filter(rows)
        syms = {r.get("symbol") or r.get("tradingsymbol") for r in result}
        self.assertIn("NIFTY", syms)

    # ── normalize_instrument drops F&O types even if injected ────────────

    def test_normalize_instrument_drops_futidx(self):
        """normalize_instrument returns None for FUTIDX entries."""
        from services.broker_discovery.angelone_discovery import AngelOneDiscovery
        disc = AngelOneDiscovery()
        raw = {"token": "88001", "symbol": "NIFTY-FUT", "name": "NIFTY",
               "exch_seg": "NFO", "instrumenttype": "FUTIDX", "lotsize": "25"}
        self.assertIsNone(disc.normalize_instrument(raw))

    def test_normalize_instrument_drops_futstk(self):
        """normalize_instrument returns None for FUTSTK entries."""
        from services.broker_discovery.angelone_discovery import AngelOneDiscovery
        disc = AngelOneDiscovery()
        raw = {"token": "99001", "symbol": "RELIANCE-FUT", "name": "RELIANCE",
               "exch_seg": "NFO", "instrumenttype": "FUTSTK", "lotsize": "500"}
        self.assertIsNone(disc.normalize_instrument(raw))

    def test_normalize_instrument_drops_optidx(self):
        """normalize_instrument returns None for OPTIDX entries."""
        from services.broker_discovery.angelone_discovery import AngelOneDiscovery
        disc = AngelOneDiscovery()
        raw = {"token": "42100", "symbol": "NIFTY26SEP24500CE", "name": "NIFTY",
               "exch_seg": "NFO", "instrumenttype": "OPTIDX", "lotsize": "25",
               "strike": "24500.0", "expiry": "26SEP2026"}
        self.assertIsNone(disc.normalize_instrument(raw))

    def test_normalize_instrument_keeps_equity(self):
        """normalize_instrument still returns an Instrument for equity rows."""
        from services.broker_discovery.angelone_discovery import AngelOneDiscovery
        disc = AngelOneDiscovery()
        raw = {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN",
               "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1"}
        inst = disc.normalize_instrument(raw)
        self.assertIsNotNone(inst)
        self.assertEqual(inst.asset_class, AssetClass.STOCK_CASH)


class TestDhanCsvParsing(unittest.TestCase):
    """
    Unit tests for DhanDiscovery._parse_master_csv:
    - FUTIDX/FUTSTK → UNDERLYING-FUT lot_size entries
    - OPTIDX/OPTSTK → supplemental lot_size entries (only when not covered by futures)
    - Non-universe entries excluded
    - No individual option contracts in output
    """

    _HEADER = "EXCH_ID,INSTRUMENT,UNDERLYING_SYMBOL,SYMBOL_NAME,SECURITY_ID,SERIES,ISIN,LOT_SIZE,TICK_SIZE,DISPLAY_NAME"

    def _parse(self, rows, tickers=None, isins=None):
        from services.broker_discovery.dhan_discovery import DhanDiscovery
        text = "\n".join([self._HEADER] + rows)
        return DhanDiscovery()._parse_master_csv(text, tickers or set(), isins or set())

    # ── Futures ───────────────────────────────────────────────────────────

    def test_futidx_nifty_produces_nifty_fut_with_lot_size_25(self):
        """FUTIDX for NIFTY → NIFTY-FUT, lot_size=25."""
        rows = ["NSE,FUTIDX,NIFTY,NIFTY29OCT25FUT,88001,,,25,0.05,NIFTY OCT FUT"]
        result = self._parse(rows)
        trading_syms = {r["trading_symbol"] for r in result}
        self.assertIn("NIFTY-FUT", trading_syms)
        nifty_fut = next(r for r in result if r["trading_symbol"] == "NIFTY-FUT")
        self.assertEqual(nifty_fut["lot_size"], "25")
        self.assertEqual(nifty_fut["security_id"], "FUT-NIFTY")

    def test_futstk_reliance_produces_reliance_fut_with_lot_size_500(self):
        """FUTSTK for RELIANCE (universe member) → RELIANCE-FUT, lot_size=500."""
        rows = ["NSE,FUTSTK,RELIANCE,RELIANCE29OCT25FUT,99001,,INE002A01018,500,0.05,RELIANCE OCT FUT"]
        result = self._parse(rows, tickers={"RELIANCE"}, isins={"INE002A01018"})
        trading_syms = {r["trading_symbol"] for r in result}
        self.assertIn("RELIANCE-FUT", trading_syms)
        rel_fut = next(r for r in result if r["trading_symbol"] == "RELIANCE-FUT")
        self.assertEqual(rel_fut["lot_size"], "500")

    def test_futstk_dedup_keeps_only_first_expiry(self):
        """Two monthly FUTSTK rows for the same underlying → only one entry."""
        rows = [
            "NSE,FUTSTK,SBIN,SBIN29OCT25FUT,88100,,INE062A01020,1500,0.05,SBIN OCT FUT",
            "NSE,FUTSTK,SBIN,SBIN27NOV25FUT,88101,,INE062A01020,1500,0.05,SBIN NOV FUT",
        ]
        result = self._parse(rows, tickers={"SBIN"}, isins={"INE062A01020"})
        sbin_rows = [r for r in result if r["trading_symbol"] == "SBIN-FUT"]
        self.assertEqual(len(sbin_rows), 1)
        self.assertEqual(sbin_rows[0]["lot_size"], "1500")

    def test_futstk_non_universe_excluded(self):
        """FUTSTK for a ticker not in the NIFTY 100 universe → excluded."""
        rows = ["NSE,FUTSTK,UNKNOWN_CO,UNKNOWN29OCT25FUT,77001,,,200,0.05,UNKNOWN FUT"]
        result = self._parse(rows, tickers={"RELIANCE"})  # UNKNOWN_CO not in tickers
        trading_syms = {r["trading_symbol"] for r in result}
        self.assertNotIn("UNKNOWN_CO-FUT", trading_syms)

    # ── Options ───────────────────────────────────────────────────────────

    def test_optidx_produces_lot_size_entry_when_no_futures_row(self):
        """OPTIDX row for NIFTY — no FUTIDX row present → still emits NIFTY-FUT lot_size entry."""
        rows = [
            # Only an option row; no futures row for NIFTY
            "NSE,OPTIDX,NIFTY,NIFTY24500CE29SEP25,35001,,,25,0.05,NIFTY CE",
        ]
        result = self._parse(rows)
        trading_syms = {r["trading_symbol"] for r in result}
        self.assertIn("NIFTY-FUT", trading_syms)
        nifty = next(r for r in result if r["trading_symbol"] == "NIFTY-FUT")
        self.assertEqual(nifty["lot_size"], "25")

    def test_optidx_does_not_duplicate_when_futures_row_present(self):
        """FUTIDX row already present → OPTIDX row for the same underlying is skipped."""
        rows = [
            "NSE,FUTIDX,NIFTY,NIFTY29OCT25FUT,88001,,,25,0.05,NIFTY OCT FUT",
            "NSE,OPTIDX,NIFTY,NIFTY24500CE29SEP25,35001,,,25,0.05,NIFTY CE",
            "NSE,OPTIDX,NIFTY,NIFTY24000PE29SEP25,35002,,,25,0.05,NIFTY PE",
        ]
        result = self._parse(rows)
        nifty_rows = [r for r in result if r["trading_symbol"] == "NIFTY-FUT"]
        self.assertEqual(len(nifty_rows), 1, "Expected exactly one NIFTY-FUT entry")

    def test_optstk_universe_member_emits_lot_size_entry(self):
        """OPTSTK for a universe stock (no futures row) → emits lot_size entry."""
        rows = [
            "NSE,OPTSTK,LT,LT3200CE29SEP25,45001,,INE018A01030,175,0.05,LT CE",
        ]
        result = self._parse(rows, tickers={"LT"}, isins={"INE018A01030"})
        trading_syms = {r["trading_symbol"] for r in result}
        self.assertIn("LT-FUT", trading_syms)
        lt = next(r for r in result if r["trading_symbol"] == "LT-FUT")
        self.assertEqual(lt["lot_size"], "175")

    def test_optstk_non_universe_excluded(self):
        """OPTSTK for a ticker outside the universe → excluded."""
        rows = ["NSE,OPTSTK,NOLISTED_CO,NOLISTED1000CE29SEP25,99999,,,100,0.05,NOLISTED CE"]
        result = self._parse(rows, tickers={"LT"})
        trading_syms = {r["trading_symbol"] for r in result}
        self.assertNotIn("NOLISTED_CO-FUT", trading_syms)

    def test_options_do_not_produce_individual_contract_entries(self):
        """Parser must never emit individual option contract entries — only lot_size entries."""
        rows = [
            "NSE,OPTIDX,BANKNIFTY,BANKNIFTY52000CE29SEP25,60001,,,15,0.05,BNF CE",
            "NSE,OPTIDX,BANKNIFTY,BANKNIFTY51000PE29SEP25,60002,,,15,0.05,BNF PE",
        ]
        result = self._parse(rows)
        # No row should have a symbol that looks like an option contract
        for row in result:
            sym = row.get("trading_symbol", "")
            self.assertFalse(
                sym.endswith("CE") or sym.endswith("PE"),
                f"Individual option contract leaked into registry: {sym}",
            )

    def test_instrument_type_promoted_to_futures_type(self):
        """Entries emitted from OPTIDX/OPTSTK rows use FUTIDX/FUTSTK as instrument_type."""
        rows = [
            "NSE,OPTIDX,NIFTY,NIFTY24500CE29SEP25,35001,,,25,0.05,NIFTY CE",
            "NSE,OPTSTK,SIEMENS,SIEMENS6000CE29SEP25,75001,,INE003A01024,350,0.05,SIEMENS CE",
        ]
        result = self._parse(rows, tickers={"SIEMENS"}, isins={"INE003A01024"})
        by_sym = {r["trading_symbol"]: r for r in result}
        self.assertEqual(by_sym["NIFTY-FUT"]["instrument_type"], "FUTIDX")
        self.assertEqual(by_sym["SIEMENS-FUT"]["instrument_type"], "FUTSTK")

    # ── Mixed: futures and options together ───────────────────────────────

    def test_mixed_csv_futures_and_options_correct_counts(self):
        """Complete mixed CSV: equity + index + 2 futures + 1 options-only underlying."""
        rows = [
            "NSE,EQUITY,HINDUNILVR,HINDUNILVR,1330,EQ,INE030A01027,1,0.05,HUL",
            "NSE,INDEX,NIFTY 50,NIFTY 50,99,,,1,0.0,NIFTY 50",
            "NSE,FUTIDX,NIFTY,NIFTY29OCT25FUT,88001,,,25,0.05,NIFTY OCT FUT",
            "NSE,FUTSTK,HINDUNILVR,HINDUNILVR29OCT25FUT,99001,,INE030A01027,500,0.05,HUL FUT",
            # BANKNIFTY has only an options row — should still produce a lot_size entry
            "NSE,OPTIDX,BANKNIFTY,BANKNIFTY52000CE29SEP25,60001,,,15,0.05,BNF CE",
            # OPTIDX for NIFTY already covered — must not duplicate
            "NSE,OPTIDX,NIFTY,NIFTY24500CE29SEP25,35001,,,25,0.05,NIFTY CE",
        ]
        result = self._parse(rows, tickers={"HINDUNILVR"}, isins={"INE030A01027"})
        trading_syms = {r["trading_symbol"] for r in result}
        self.assertIn("HINDUNILVR", trading_syms)   # equity
        self.assertIn("NIFTY", trading_syms)        # index (canonical_index_name maps "NIFTY 50" → "NIFTY")
        self.assertIn("NIFTY-FUT", trading_syms)    # futures
        self.assertIn("HINDUNILVR-FUT", trading_syms)
        self.assertIn("BANKNIFTY-FUT", trading_syms)  # options-derived
        # exactly one entry per underlying
        for sym in ("NIFTY-FUT", "HINDUNILVR-FUT", "BANKNIFTY-FUT"):
            self.assertEqual(sum(1 for r in result if r["trading_symbol"] == sym), 1)


class TestFyersCsvParsing(unittest.TestCase):
    """
    Unit tests for FyersDiscovery._parse_fo_csv:
    - FUT-suffixed FyersSymbol → UNDERLYING-FUT lot_size entries (FUTIDX/FUTSTK)
    - CE/PE-suffixed → supplemental lot_size entries when underlying not yet captured
    - Non-universe stock derivatives excluded
    - No individual option contracts in output
    - seen_futures set shared across exchange files prevents cross-exchange duplicates
    """

    def _make_row(self, token, name, lot, tick, fy_sym, base_sym, isin=""):
        """Build a positionally-correct headerless F&O CSV row (14 columns)."""
        cols = [""] * 14
        cols[0] = token
        cols[1] = name
        cols[3] = lot
        cols[4] = tick
        cols[5] = isin
        cols[9] = fy_sym
        cols[13] = base_sym
        return ",".join(cols)

    def _parse_fo(self, rows, tickers=None, isins=None, exch="NSE", seen_futures=None):
        from services.broker_discovery.fyers_discovery import FyersDiscovery
        text = "\n".join(rows)
        if seen_futures is None:
            seen_futures = set()
        return FyersDiscovery()._parse_fo_csv(
            text, exch, tickers or set(), isins or set(), seen_futures
        )

    # ── Futures ───────────────────────────────────────────────────────────

    def test_futidx_nifty_produces_nifty_fut_with_lot_size_25(self):
        """FUT row for NIFTY → NIFTY-FUT, lot_size=25, instrument_type=FUTIDX."""
        rows = [self._make_row("12345", "NIFTY Futures", "25", "0.05", "NSE:NIFTY24OCTFUT", "NIFTY")]
        result = self._parse_fo(rows)
        syms = {r["symbol"] for r in result}
        self.assertIn("NIFTY-FUT", syms)
        nifty = next(r for r in result if r["symbol"] == "NIFTY-FUT")
        self.assertEqual(nifty["lot_size"], "25")
        self.assertEqual(nifty["token"], "FUT-NIFTY")
        self.assertEqual(nifty["instrument_type"], "FUTIDX")

    def test_futstk_reliance_produces_reliance_fut_with_lot_size_500(self):
        """FUT row for RELIANCE (universe member) → RELIANCE-FUT, lot_size=500."""
        rows = [self._make_row("22222", "Reliance Industries", "500", "0.05",
                               "NSE:RELIANCE24OCTFUT", "RELIANCE", "INE002A01018")]
        result = self._parse_fo(rows, tickers={"RELIANCE"}, isins={"INE002A01018"})
        syms = {r["symbol"] for r in result}
        self.assertIn("RELIANCE-FUT", syms)
        rel = next(r for r in result if r["symbol"] == "RELIANCE-FUT")
        self.assertEqual(rel["lot_size"], "500")
        self.assertEqual(rel["instrument_type"], "FUTSTK")

    def test_futstk_dedup_keeps_only_first_expiry(self):
        """Two monthly FUT rows for the same underlying → only one registry entry."""
        rows = [
            self._make_row("88100", "SBI Futures", "1500", "0.05", "NSE:SBIN29OCT25FUT", "SBIN"),
            self._make_row("88101", "SBI Futures", "1500", "0.05", "NSE:SBIN27NOV25FUT", "SBIN"),
        ]
        result = self._parse_fo(rows, tickers={"SBIN"})
        sbin_rows = [r for r in result if r["symbol"] == "SBIN-FUT"]
        self.assertEqual(len(sbin_rows), 1)
        self.assertEqual(sbin_rows[0]["lot_size"], "1500")

    def test_futstk_non_universe_excluded(self):
        """FUT row for a ticker outside the NIFTY 100 universe → excluded."""
        rows = [self._make_row("77001", "Unknown Corp", "200", "0.05",
                               "NSE:UNKNOWN24OCTFUT", "UNKNOWN_CO")]
        result = self._parse_fo(rows, tickers={"RELIANCE"})
        syms = {r["symbol"] for r in result}
        self.assertNotIn("UNKNOWN_CO-FUT", syms)

    # ── Options ───────────────────────────────────────────────────────────

    def test_optidx_produces_lot_size_entry_when_no_futures_row(self):
        """CE row for BANKNIFTY with no preceding FUT row → BANKNIFTY-FUT lot_size entry."""
        # Fyers option symbols end with CE/PE: NSE:BANKNIFTY24SEP52000CE
        rows = [self._make_row("60001", "BANKNIFTY Options", "15", "0.05",
                               "NSE:BANKNIFTY24SEP52000CE", "BANKNIFTY")]
        result = self._parse_fo(rows)
        syms = {r["symbol"] for r in result}
        self.assertIn("BANKNIFTY-FUT", syms)
        bnf = next(r for r in result if r["symbol"] == "BANKNIFTY-FUT")
        self.assertEqual(bnf["lot_size"], "15")
        self.assertEqual(bnf["instrument_type"], "FUTIDX")

    def test_optidx_skipped_when_futures_row_present(self):
        """FUT row already captured → subsequent CE/PE rows for same underlying skipped."""
        rows = [
            self._make_row("88001", "NIFTY Futures", "25", "0.05", "NSE:NIFTY24OCTFUT", "NIFTY"),
            self._make_row("35001", "NIFTY Options", "25", "0.05", "NSE:NIFTY24OCT24500CE", "NIFTY"),
            self._make_row("35002", "NIFTY Options", "25", "0.05", "NSE:NIFTY24OCT24000PE", "NIFTY"),
        ]
        result = self._parse_fo(rows)
        nifty_rows = [r for r in result if r["symbol"] == "NIFTY-FUT"]
        self.assertEqual(len(nifty_rows), 1, "Expected exactly one NIFTY-FUT entry")

    def test_optstk_universe_member_emits_lot_size_entry(self):
        """CE row for universe stock with no FUT row → emits lot_size entry."""
        rows = [self._make_row("45001", "LT Options", "175", "0.05",
                               "NSE:LT24SEP3200CE", "LT")]
        result = self._parse_fo(rows, tickers={"LT"})
        syms = {r["symbol"] for r in result}
        self.assertIn("LT-FUT", syms)
        lt = next(r for r in result if r["symbol"] == "LT-FUT")
        self.assertEqual(lt["lot_size"], "175")

    def test_optstk_non_universe_excluded(self):
        """CE row for non-universe stock → excluded."""
        rows = [self._make_row("99999", "NoListed Options", "100", "0.05",
                               "NSE:NOLISTED24OCT1000CE", "NOLISTED_CO")]
        result = self._parse_fo(rows, tickers={"LT"})
        syms = {r["symbol"] for r in result}
        self.assertNotIn("NOLISTED_CO-FUT", syms)

    def test_options_do_not_produce_individual_contracts(self):
        """No individual option contract entries (CE/PE symbols) in output."""
        rows = [
            self._make_row("60001", "BANKNIFTY Options", "15", "0.05",
                           "NSE:BANKNIFTY24SEP52000CE", "BANKNIFTY"),
            self._make_row("60002", "BANKNIFTY Options", "15", "0.05",
                           "NSE:BANKNIFTY24SEP51000PE", "BANKNIFTY"),
        ]
        result = self._parse_fo(rows)
        for row in result:
            sym = str(row.get("symbol") or "")
            self.assertFalse(
                sym.endswith("CE") or sym.endswith("PE"),
                f"Individual option contract leaked into registry: {sym}",
            )

    def test_instrument_type_correct_for_index_vs_stock(self):
        """FUTIDX emitted for index underlyings, FUTSTK for stock underlyings."""
        rows = [
            self._make_row("88001", "NIFTY Futures", "25", "0.05", "NSE:NIFTY24OCTFUT", "NIFTY"),
            self._make_row("88002", "Reliance Futures", "500", "0.05",
                           "NSE:RELIANCE24OCTFUT", "RELIANCE"),
        ]
        result = self._parse_fo(rows, tickers={"RELIANCE"})
        by_sym = {r["symbol"]: r for r in result}
        self.assertEqual(by_sym["NIFTY-FUT"]["instrument_type"], "FUTIDX")
        self.assertEqual(by_sym["RELIANCE-FUT"]["instrument_type"], "FUTSTK")

    def test_seen_futures_shared_across_exchange_files(self):
        """NSE capture of NIFTY blocks a duplicate BSE entry for the same underlying."""
        seen = set()
        nse_rows = [self._make_row("88001", "NIFTY Futures", "25", "0.05",
                                   "NSE:NIFTY24OCTFUT", "NIFTY")]
        bse_rows = [self._make_row("88002", "NIFTY Futures", "25", "0.05",
                                   "BSE:NIFTY24OCTFUT", "NIFTY")]
        from services.broker_discovery.fyers_discovery import FyersDiscovery
        disc = FyersDiscovery()
        r1 = disc._parse_fo_csv("\n".join(nse_rows), "NSE", set(), set(), seen)
        r2 = disc._parse_fo_csv("\n".join(bse_rows), "BSE", set(), set(), seen)
        self.assertEqual(len(r1), 1)
        self.assertEqual(len(r2), 0, "NSE already captured NIFTY; BSE must not duplicate it")

    # ── normalize_instrument drops F&O types ─────────────────────────────

    def test_normalize_instrument_drops_futstk(self):
        """normalize_instrument returns None for FUTSTK — Fyers is data feed only."""
        from services.broker_discovery.fyers_discovery import FyersDiscovery
        disc = FyersDiscovery()
        raw = {"symbol": "NSE:SBIN26SEPFUT", "fyToken": "10100000009999",
               "lotSize": 750, "tickSize": 0.05, "instrument_type": "FUTSTK",
               "underlying": "SBIN", "expiry": "2026-09-26"}
        self.assertIsNone(disc.normalize_instrument(raw))

    def test_normalize_instrument_drops_futidx(self):
        """normalize_instrument returns None for FUTIDX."""
        from services.broker_discovery.fyers_discovery import FyersDiscovery
        disc = FyersDiscovery()
        raw = {"symbol": "NSE:NIFTY24OCTFUT", "fyToken": "12345", "lotSize": 25,
               "tickSize": 0.05, "instrument_type": "FUTIDX"}
        self.assertIsNone(disc.normalize_instrument(raw))

    def test_normalize_instrument_keeps_equity(self):
        """normalize_instrument still returns an Instrument for EQ rows."""
        from services.broker_discovery.fyers_discovery import FyersDiscovery
        disc = FyersDiscovery()
        raw = {"symbol": "NSE:RELIANCE-EQ", "fyToken": "10100000002885",
               "lotSize": 1, "tickSize": 0.05, "instrument_type": "EQ",
               "isin": "INE002A01018", "description": "Reliance Industries Limited"}
        inst = disc.normalize_instrument(raw)
        self.assertIsNotNone(inst)
        self.assertEqual(inst.asset_class, AssetClass.STOCK_CASH)


class TestFnoDiscoveryIsolation(unittest.IsolatedAsyncioTestCase):
    """
    End-to-end proof that AngelOne and Fyers discovery parsers never populate
    F&O instruments in the registry. Dhan is the sole F&O discovery source.
    """

    async def test_angelone_fetch_master_contracts_contains_no_fno(self):
        """
        AngelOne's full fetch_master_contracts pipeline must never return
        INDEX_FUTURES, STOCK_FUTURES, INDEX_OPTIONS, or STOCK_OPTIONS instruments.
        """
        from services.broker_discovery.angelone_discovery import AngelOneDiscovery
        from models.instrument import AssetClass

        # Feed a raw payload that includes both equity and F&O rows.
        mock_raw = [
            {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN",
             "exch_seg": "NSE", "instrumenttype": "", "lotsize": "1", "tick_size": "5.0"},
            {"token": "88001", "symbol": "NIFTY29OCT25FUT", "name": "NIFTY",
             "exch_seg": "NFO", "instrumenttype": "FUTIDX", "lotsize": "25",
             "tick_size": "0.05", "expiry": "29OCT2025"},
            {"token": "42100", "symbol": "NIFTY26SEP24500CE", "name": "NIFTY",
             "exch_seg": "NFO", "instrumenttype": "OPTIDX", "lotsize": "25",
             "tick_size": "0.05", "expiry": "26SEP2026"},
        ]
        disc = AngelOneDiscovery()
        with patch.object(disc, "_fetch_raw_contracts", new=AsyncMock(return_value=mock_raw)):
            instruments = await disc.fetch_master_contracts()

        fno_classes = {
            AssetClass.INDEX_FUTURES, AssetClass.STOCK_FUTURES,
            AssetClass.INDEX_OPTIONS, AssetClass.STOCK_OPTIONS,
        }
        fno_found = [i for i in instruments if i.asset_class in fno_classes]
        self.assertEqual(fno_found, [],
                         f"AngelOne emitted F&O instruments: {[i.symbol for i in fno_found]}")
        # Equity row still present
        eq_syms = {i.symbol for i in instruments if i.asset_class == AssetClass.STOCK_CASH}
        self.assertIn("SBIN-EQ", eq_syms)

    async def test_fyers_fetch_master_contracts_contains_no_fno(self):
        """
        Fyers's full fetch_master_contracts pipeline must never return F&O instruments.
        """
        from services.broker_discovery.fyers_discovery import FyersDiscovery
        from models.instrument import AssetClass

        mock_raw = [
            {"symbol": "NSE:RELIANCE-EQ", "fyToken": "10100000002885",
             "lotSize": 1, "tickSize": 0.05, "instrument_type": "EQ",
             "isin": "INE002A01018", "description": "Reliance Industries Limited"},
            {"symbol": "NSE:SBIN26SEPFUT", "fyToken": "10100000009999",
             "lotSize": 750, "tickSize": 0.05, "instrument_type": "FUTSTK",
             "underlying": "SBIN", "expiry": "2026-09-26"},
            {"symbol": "NSE:NIFTY24OCTFUT", "fyToken": "12345",
             "lotSize": 25, "tickSize": 0.05, "instrument_type": "FUTIDX"},
        ]
        disc = FyersDiscovery()
        with patch.object(disc, "_fetch_raw_contracts", new=AsyncMock(return_value=mock_raw)):
            instruments = await disc.fetch_master_contracts()

        fno_classes = {
            AssetClass.INDEX_FUTURES, AssetClass.STOCK_FUTURES,
            AssetClass.INDEX_OPTIONS, AssetClass.STOCK_OPTIONS,
        }
        fno_found = [i for i in instruments if i.asset_class in fno_classes]
        self.assertEqual(fno_found, [],
                         f"Fyers emitted F&O instruments: {[i.symbol for i in fno_found]}")
        eq_syms = {i.symbol for i in instruments if i.asset_class == AssetClass.STOCK_CASH}
        self.assertIn("RELIANCE-EQ", eq_syms)

    async def test_fyers_fetch_raw_contracts_never_calls_parse_fo_csv(self):
        """
        _fetch_raw_contracts must not call _parse_fo_csv — the F&O CSV loop
        was removed. Patching it to raise ensures we catch any regression.
        """
        from services.broker_discovery.fyers_discovery import FyersDiscovery
        from services.broker_discovery.universe import download_text, nifty100_membership

        disc = FyersDiscovery()

        def _raise_if_called(*args, **kwargs):
            raise AssertionError("_parse_fo_csv must NOT be called — F&O download disabled")

        with patch.object(disc, "_parse_fo_csv", side_effect=_raise_if_called), \
             patch("services.broker_discovery.fyers_discovery.download_text", return_value=""), \
             patch("services.broker_discovery.fyers_discovery.nifty100_membership",
                   return_value=(set(), set())):
            # Should not raise, even though _parse_fo_csv would raise if called.
            result = await disc._fetch_raw_contracts()
        self.assertIsInstance(result, list)

    async def test_dhan_still_emits_fno(self):
        """
        Dhan discovery is the sole remaining F&O source — verify it still works.
        """
        from services.broker_discovery.dhan_discovery import DhanDiscovery
        from models.instrument import AssetClass

        mock_raw = [
            {"security_id": "35012", "custom_symbol": "NIFTY26SEP24500CE",
             "exchange_segment": "NSE_FNO", "instrument_type": "OPTIDX",
             "lot_size": 25, "tick_size": 0.05, "strike_price": 24500.0,
             "option_type": "CE", "expiry_date": "2026-09-26",
             "underlying_symbol": "NIFTY"},
        ]
        disc = DhanDiscovery()
        with patch.object(disc, "_fetch_raw_contracts", new=AsyncMock(return_value=mock_raw)):
            instruments = await disc.fetch_master_contracts()

        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0].asset_class, AssetClass.INDEX_OPTIONS)


if __name__ == "__main__":
    asyncio.run(main())
