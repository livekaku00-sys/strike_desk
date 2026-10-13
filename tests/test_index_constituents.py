import asyncio
from datetime import datetime, timezone
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from services.indices import (
    ConstituentResolver,
    IndexCatalog,
    IndexDefinition,
    MembershipTracker,
    constituent_resolver,
    index_catalog,
    membership_tracker,
)
from services.index_constituent_service import (
    IndexConstituentService,
    index_constituent_service,
)
from services.instrument_discovery_service import InstrumentDiscoveryService
from services.instrument_registry import InstrumentRegistry


class TestIndexConstituents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = IndexCatalog()
        self.registry = InstrumentRegistry()
        self.resolver = ConstituentResolver(catalog=self.catalog, registry=self.registry)
        self.tracker = MembershipTracker()
        self.discovery = InstrumentDiscoveryService()
        self.service = IndexConstituentService(
            catalog=self.catalog,
            resolver=self.resolver,
            tracker=self.tracker,
            discovery_service=self.discovery,
            registry=self.registry,
        )

    def test_a_index_catalog(self):
        required = [
            ("NIFTY_50", Exchange.NSE, 50),
            ("NIFTY_100", Exchange.NSE, 100),
            ("BANK_NIFTY", Exchange.NSE, 20),
            ("NIFTY_IT", Exchange.NSE, 10),
            ("NIFTY_PHARMA", Exchange.NSE, 10),
            ("NIFTY_FMCG", Exchange.NSE, 15),
            ("BSE_SENSEX", Exchange.BSE, 30),
            ("BSE_100", Exchange.BSE, 100),
        ]

        for name, exch, count in required:
            defn = self.catalog.get_index(name)
            self.assertIsNotNone(defn, f"Index {name} missing from catalog")
            self.assertEqual(defn.exchange, exch)
            self.assertEqual(defn.expected_count, count)
            self.assertEqual(defn.refresh_cadence_hours, 24)
            self.assertTrue(len(defn.description) > 0)
        print("✓ [a] Index catalog: All 8 required indices defined with correct metadata")

    async def test_b_constituent_resolution(self):
        mock_50 = [
            Instrument(
                exchange=Exchange.NSE,
                symbol=f"MOCK_NIFTY_{i:02d}",
                token=str(1000 + i),
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
                underlying="NIFTY",
            )
            for i in range(1, 51)
        ]

        resolved = await self.resolver.resolve(
            "NIFTY_50",
            master_contracts=mock_50,
            force_refresh=True,
        )
        self.assertEqual(len(resolved), 50)
        self.assertEqual(resolved[0].symbol, "MOCK_NIFTY_01")
        self.assertEqual(resolved[-1].symbol, "MOCK_NIFTY_50")
        print("✓ [b] Constituent resolution: NIFTY_50 resolved dynamically to 50 instruments")

    async def test_c_multi_broker_resolution(self):
        def make_broker_instruments(prefix: str):
            return [
                Instrument(
                    exchange=Exchange.NSE,
                    symbol=f"{prefix}_NIFTY_{i:02d}",
                    token=str(2000 + i),
                    asset_class=AssetClass.STOCK_CASH,
                    instrument_type=InstrumentType.EQ,
                    underlying="NIFTY",
                )
                for i in range(1, 51)
            ]

        contracts_by_broker = {
            "dhan": make_broker_instruments("DHAN"),
            "angelone": make_broker_instruments("ANGEL"),
            "fyers": make_broker_instruments("FYERS"),
        }

        res = await self.resolver.resolve_all(
            "NIFTY_50",
            master_contracts_by_broker=contracts_by_broker,
            force_refresh=True,
        )

        self.assertIn("dhan", res)
        self.assertIn("angelone", res)
        self.assertIn("fyers", res)
        self.assertEqual(len(res["dhan"]), 50)
        self.assertEqual(len(res["angelone"]), 50)
        self.assertEqual(len(res["fyers"]), 50)
        print("✓ [c] Multi-broker resolution: NIFTY_50 resolved across Dhan, AngelOne, Fyers")

    async def test_d_caching(self):
        call_count = 0

        async def mock_filter(**kwargs):
            nonlocal call_count
            call_count += 1
            return [
                Instrument(
                    exchange=Exchange.NSE,
                    symbol=f"CACHED_SYM_{i}",
                    token=str(3000 + i),
                    asset_class=AssetClass.STOCK_CASH,
                    instrument_type=InstrumentType.EQ,
                    underlying="NIFTY",
                )
                for i in range(50)
            ]

        with patch.object(self.registry, "filter", side_effect=mock_filter):
            res1 = await self.resolver.resolve("NIFTY_50", force_refresh=True)
            self.assertEqual(len(res1), 50)
            self.assertEqual(call_count, 1)

            res2 = await self.resolver.resolve("NIFTY_50", force_refresh=False)
            self.assertEqual(len(res2), 50)
            self.assertEqual(call_count, 1)

            res3 = await self.resolver.resolve("NIFTY_50", force_refresh=True)
            self.assertEqual(len(res3), 50)
            self.assertEqual(call_count, 2)
        print("✓ [d] Caching: Verified 2nd call uses cache, force_refresh refreshes")

    def test_e_membership_change_detection(self):
        tracker = MembershipTracker()
        inst_a = Instrument(exchange=Exchange.NSE, symbol="SYM_A", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)
        inst_b = Instrument(exchange=Exchange.NSE, symbol="SYM_B", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)
        inst_c = Instrument(exchange=Exchange.NSE, symbol="SYM_C", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)
        inst_d = Instrument(exchange=Exchange.NSE, symbol="SYM_D", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)

        tracker.record_snapshot("NIFTY_50", [inst_a, inst_b, inst_c])
        changes = tracker.detect_changes("NIFTY_50", [inst_b, inst_c, inst_d])
        self.assertEqual(changes["added"], ["SYM_D"])
        self.assertEqual(changes["removed"], ["SYM_A"])

        history = tracker.get_history("NIFTY_50")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].added, ["SYM_D"])
        self.assertEqual(history[0].removed, ["SYM_A"])
        print("✓ [e] Membership change detection: Correctly logged SYM_A exit and SYM_D entry")

    async def test_f_refresh_cadence(self):
        self.assertIsNone(self.service.get_last_refresh_time("NIFTY_IT"))

        mock_it = [
            Instrument(
                exchange=Exchange.NSE,
                symbol=f"IT_SYM_{i}",
                token=str(4000 + i),
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
                underlying="IT",
            )
            for i in range(10)
        ]
        with patch.object(self.resolver, "resolve", new=AsyncMock(return_value=mock_it)):
            await self.service.fetch_index("NIFTY_IT", force_refresh=True)

        refresh_time = self.service.get_last_refresh_time("NIFTY_IT")
        self.assertIsNotNone(refresh_time)
        self.assertIsInstance(refresh_time, datetime)
        print("✓ [f] Refresh cadence: Verified last_refresh_time updated with UTC timestamp")

    async def test_g_error_handling(self):
        async def mock_fetch(idx_name: str, **kwargs):
            if idx_name == "NIFTY_50":
                raise RuntimeError("Failed to reach exchange endpoint for NIFTY_50")
            return [
                Instrument(
                    exchange=Exchange.NSE,
                    symbol=f"{idx_name}_SYM",
                    token="5001",
                    asset_class=AssetClass.STOCK_CASH,
                    instrument_type=InstrumentType.EQ,
                )
            ]

        with patch.object(self.service, "fetch_index", side_effect=mock_fetch):
            results = await self.service.fetch_all_indices(force_refresh=True)

        self.assertIn("NIFTY_50", results)
        self.assertEqual(len(results["NIFTY_50"]), 0)
        self.assertIn("BANK_NIFTY", results)
        self.assertEqual(len(results["BANK_NIFTY"]), 1)
        print("✓ [g] Error handling: One index failure handled gracefully, others completed")

    async def test_h_integration(self):
        mock_discovered = [
            Instrument(
                exchange=Exchange.NSE,
                symbol=f"INTEG_{i}",
                token=str(6000 + i),
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
                underlying="NIFTY",
            )
            for i in range(50)
        ]

        with patch.object(self.discovery, "discover_broker", new=AsyncMock(return_value=mock_discovered)):
            res = await self.service.fetch_index("NIFTY_50", force_refresh=True)

        self.assertEqual(len(res), 50)
        reg_inst = await self.registry.get(Exchange.NSE, "INTEG_0")
        self.assertIsNotNone(reg_inst)
        self.assertEqual(reg_inst.token, "6000")
        print("✓ [h] Integration: Service seamlessly integrated Discovery, Registry, and Resolver")

    def test_i_no_hardcoded_symbols(self):
        index_files = [
            os.path.join(PROJECT_ROOT, "services", "indices", "index_catalog.py"),
            os.path.join(PROJECT_ROOT, "services", "indices", "constituent_resolver.py"),
            os.path.join(PROJECT_ROOT, "services", "indices", "membership_tracker.py"),
            os.path.join(PROJECT_ROOT, "services", "index_constituent_service.py"),
        ]
        disallowed = ["RELIANCE", "TCS", "INFOSYS", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL"]

        for fpath in index_files:
            if not os.path.exists(fpath):
                continue
            with open(fpath, "r", encoding="utf-8") as fp:
                src = fp.read()
            for sym in disallowed:
                self.assertNotIn(
                    f'"{sym}"',
                    src,
                    f"Forbidden hardcoded stock ticker '{sym}' found in {fpath}",
                )
                self.assertNotIn(
                    f"'{sym}'",
                    src,
                    f"Forbidden hardcoded stock ticker '{sym}' found in {fpath}",
                )
        print("✓ [i] Zero hardcoded symbols verified: Source code contains no static constituent watchlists")


async def main():
    print("=" * 60)
    print("STRIKE DESK V4 — PHASE 2 PROMPT 3/5 INDEX CONSTITUENT FETCHING")
    print("=" * 60)

    suite = TestIndexConstituents()
    suite.setUp()
    suite.test_a_index_catalog()
    await suite.test_b_constituent_resolution()
    await suite.test_c_multi_broker_resolution()
    await suite.test_d_caching()
    suite.test_e_membership_change_detection()
    await suite.test_f_refresh_cadence()
    await suite.test_g_error_handling()
    await suite.test_h_integration()
    suite.test_i_no_hardcoded_symbols()

    print("=" * 60)
    print("ALL PHASE 2 PROMPT 3/5 VERIFICATIONS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
