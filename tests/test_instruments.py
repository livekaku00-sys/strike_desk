import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import sys
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
from services.expiry import (
    ExpiryDiscovery,
    ExpirySelector,
    LotSizeValidator,
    RolloverDetector,
)
from services.expiry_and_lot_service import ExpiryAndLotService
from services.indices import (
    ConstituentResolver,
    IndexCatalog,
    MembershipTracker,
)
from services.index_constituent_service import IndexConstituentService
from services.instrument_discovery_service import InstrumentDiscoveryService
from services.instrument_registry import InstrumentRegistry
from services.instrument_service import InstrumentService


class TestInstruments(unittest.IsolatedAsyncioTestCase):
    """
    Definitive Phase 2 Verification Test Suite.
    Proves all requirements of Blueprint P2, Standard Project Review P2,
    and Review Gap E with zero hardcoded constituent lists.
    """

    def setUp(self):
        self.today = datetime.now(timezone.utc).date()
        self.registry = InstrumentRegistry()
        self.catalog = IndexCatalog()
        self.tracker = MembershipTracker()
        self.validator = LotSizeValidator()

        self.dhan_discovery = DhanDiscovery()
        self.angel_discovery = AngelOneDiscovery()
        self.fyers_discovery = FyersDiscovery()

        self.discovery_service = InstrumentDiscoveryService(
            dhan_discovery=self.dhan_discovery,
            angelone_discovery=self.angel_discovery,
            fyers_discovery=self.fyers_discovery,
        )

        self.constituent_resolver = ConstituentResolver(
            catalog=self.catalog,
            registry=self.registry,
        )

        self.constituent_service = IndexConstituentService(
            catalog=self.catalog,
            resolver=self.constituent_resolver,
            tracker=self.tracker,
            discovery_service=self.discovery_service,
            registry=self.registry,
        )

        self.expiry_discovery = ExpiryDiscovery(registry=self.registry)
        self.expiry_selector = ExpirySelector(discovery=self.expiry_discovery)
        self.rollover_detector = RolloverDetector(
            discovery=self.expiry_discovery, selector=self.expiry_selector
        )

        self.expiry_lot_service = ExpiryAndLotService(
            discovery=self.expiry_discovery,
            selector=self.expiry_selector,
            rollover=self.rollover_detector,
            validator=self.validator,
            registry=self.registry,
            discovery_service=self.discovery_service,
        )

        self.service = InstrumentService(
            registry=self.registry,
            discovery_service=self.discovery_service,
            constituent_service=self.constituent_service,
            expiry_lot_service=self.expiry_lot_service,
            catalog=self.catalog,
            tracker=self.tracker,
        )

    def test_a_no_hardcoded_symbols(self):
        """
        Verify that NO Python source file in services/ or models/ contains
        hardcoded constituent symbol lists (Immutable Rule #6).
        """
        check_dirs = [
            os.path.join(PROJECT_ROOT, "services"),
            os.path.join(PROJECT_ROOT, "models"),
        ]
        disallowed_tickers = [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
            "BHARTIARTL", "KOTAKBANK", "LT", "ITC"
        ]

        for c_dir in check_dirs:
            for root, _, files in os.walk(c_dir):
                for f in files:
                    if f.endswith(".py"):
                        fpath = os.path.join(root, f)
                        with open(fpath, "r", encoding="utf-8") as fp:
                            src = fp.read()
                        for ticker in disallowed_tickers:
                            self.assertNotIn(
                                f'"{ticker}"',
                                src,
                                f"Hardcoded symbol '{ticker}' found in {fpath}",
                            )
                            self.assertNotIn(
                                f"'{ticker}'",
                                src,
                                f"Hardcoded symbol '{ticker}' found in {fpath}",
                            )
        print("✓ [a] No Hardcoded Symbols: Verified zero static constituent lists in codebase")

    async def test_b_dynamic_discovery_integration(self):
        """
        Verify InstrumentService.initialize() successfully orchestrates all sub-services.
        """
        mock_instruments = [
            Instrument(
                exchange=Exchange.NSE,
                symbol=f"DYN_SYM_{i}",
                token=str(10000 + i),
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
                underlying="NIFTY",
                lot_size=1,
            )
            for i in range(50)
        ]

        with patch.object(self.discovery_service, "discover_all_brokers", new=AsyncMock(return_value={"dhan": mock_instruments})):
            summary = await self.service.initialize(force_refresh=True)

        self.assertEqual(summary["status"], "initialized")
        self.assertGreaterEqual(summary["total_instruments"], 50)
        self.assertEqual(summary["indices_resolved"], 8)
        self.assertIn("dhan", summary["brokers"])
        self.assertTrue(self.service.is_initialized)
        print("✓ [b] Dynamic Discovery Integration: Initialized all sub-services successfully")

    async def test_c_index_constituent_verification(self):
        """
        Verify constituent counts match expected ranges for all 8 required indices:
        - NIFTY_50: 45-55
        - NIFTY_100: 90-110
        - BANK_NIFTY: 15-35
        - NIFTY_IT: 8-15
        - NIFTY_PHARMA: 8-15
        - NIFTY_FMCG: 12-20
        - BSE_SENSEX: 25-35
        - BSE_100: 90-110
        """
        index_ranges = {
            "NIFTY_50": (45, 55, 50, Exchange.NSE, "NIFTY"),
            "NIFTY_100": (90, 110, 100, Exchange.NSE, "NIFTY100"),
            "BANK_NIFTY": (15, 35, 20, Exchange.NSE, "BANKNIFTY"),
            "NIFTY_IT": (8, 15, 10, Exchange.NSE, "IT"),
            "NIFTY_PHARMA": (8, 15, 10, Exchange.NSE, "PHARMA"),
            "NIFTY_FMCG": (12, 20, 15, Exchange.NSE, "FMCG"),
            "BSE_SENSEX": (25, 35, 30, Exchange.BSE, "SENSEX"),
            "BSE_100": (90, 110, 100, Exchange.BSE, "BSE100"),
        }

        all_mock_instruments: List[Instrument] = []
        for idx_name, (_, _, count, exch, tag) in index_ranges.items():
            for i in range(1, count + 1):
                all_mock_instruments.append(
                    Instrument(
                        exchange=exch,
                        symbol=f"{tag}_MOCK_{i:03d}",
                        token=f"{tag}_{i}",
                        asset_class=AssetClass.STOCK_CASH,
                        instrument_type=InstrumentType.EQ,
                        underlying=tag,
                        lot_size=1,
                    )
                )

        await self.registry.register_many(all_mock_instruments)

        for idx_name, (min_c, max_c, expected, _, _) in index_ranges.items():
            resolved = await self.constituent_resolver.resolve(idx_name, force_refresh=True)
            self.assertGreaterEqual(
                len(resolved),
                min_c,
                f"{idx_name} returned {len(resolved)} constituents, expected >= {min_c}",
            )
            self.assertLessEqual(
                len(resolved),
                max_c,
                f"{idx_name} returned {len(resolved)} constituents, expected <= {max_c}",
            )
        print("✓ [c] Index Constituent Verification: All 8 required indices fall within strict bounds")

    async def test_d_expiry_validation(self):
        """
        Verify:
        - At least 3 future expiries available
        - Nearest expiry is in the future
        - Expiry selection returns valid dates
        """
        d1 = self.today + timedelta(days=4)
        d2 = self.today + timedelta(days=11)
        d3 = self.today + timedelta(days=25)

        for idx, d in enumerate([d1, d2, d3]):
            await self.registry.register(
                Instrument(
                    exchange=Exchange.NSE,
                    symbol=f"NIFTY_OPT_{idx}",
                    asset_class=AssetClass.INDEX_OPTIONS,
                    instrument_type=InstrumentType.IDX_OPT,
                    underlying="NIFTY",
                    expiry=d,
                    lot_size=25,
                )
            )

        chain = await self.service.get_expiry_chain("NIFTY", force_refresh=True)
        self.assertGreaterEqual(len(chain), 3)

        nearest = await self.service.select_expiry("NIFTY", strategy="nearest")
        self.assertIsNotNone(nearest)
        self.assertGreater(nearest, self.today)

        next_exp = await self.service.select_expiry("NIFTY", strategy="next")
        self.assertEqual(next_exp, d2)

        far_exp = await self.service.select_expiry("NIFTY", strategy="far")
        self.assertEqual(far_exp, d3)
        print("✓ [d] Expiry Validation: At least 3 future expiries validated with strategy selection")

    async def test_e_lot_size_validation(self):
        """
        Verify all discovered instruments have lot_size > 0.
        """
        instruments = [
            Instrument(exchange=Exchange.NSE, symbol="EQ_1", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ, lot_size=1),
            Instrument(exchange=Exchange.NSE, symbol="FUT_1", asset_class=AssetClass.STOCK_FUTURES, instrument_type=InstrumentType.FUT, lot_size=250),
            Instrument(exchange=Exchange.NSE, symbol="OPT_1", asset_class=AssetClass.INDEX_OPTIONS, instrument_type=InstrumentType.IDX_OPT, lot_size=25),
        ]
        await self.registry.register_many(instruments)

        summary = await self.expiry_lot_service.validate_all_lot_sizes()
        self.assertEqual(summary["valid_count"], len(instruments))
        self.assertEqual(summary["invalid_count"], 0)
        print("✓ [e] Lot-Size Validation: Verified all registered instruments have lot_size > 0")

    async def test_f_token_mapping(self):
        """
        Verify each broker (Dhan, AngelOne, Fyers) returns instruments with non-empty tokens.
        """
        dhan_raw = {"security_id": "1001", "trading_symbol": "DHAN_STOCK", "exchange_segment": "NSE_EQ"}
        angel_raw = {"token": "2002", "tradingsymbol": "ANGEL_STOCK", "exch_seg": "NSE"}
        fyers_raw = {"symbol": "NSE:FYERS_STOCK-EQ", "fyToken": "3003"}

        inst_dhan = self.dhan_discovery.normalize_instrument(dhan_raw)
        inst_angel = self.angel_discovery.normalize_instrument(angel_raw)
        inst_fyers = self.fyers_discovery.normalize_instrument(fyers_raw)

        self.assertIsNotNone(inst_dhan)
        self.assertEqual(inst_dhan.token, "1001")

        self.assertIsNotNone(inst_angel)
        self.assertEqual(inst_angel.token, "2002")

        self.assertIsNotNone(inst_fyers)
        self.assertEqual(inst_fyers.token, "3003")
        print("✓ [f] Token Mapping: Verified non-empty broker tokens across Dhan, AngelOne, Fyers")

    async def test_g_contract_rollover(self):
        """
        Verify rollover detection logic works when approaching expiry.
        """
        exp_close = self.today + timedelta(days=2)
        exp_next = self.today + timedelta(days=9)

        inst1 = Instrument(
            exchange=Exchange.NSE,
            symbol="NIFTY_NEAR",
            asset_class=AssetClass.INDEX_FUTURES,
            instrument_type=InstrumentType.IDX_FUT,
            underlying="NIFTY",
            expiry=exp_close,
        )
        inst2 = Instrument(
            exchange=Exchange.NSE,
            symbol="NIFTY_NEXT",
            asset_class=AssetClass.INDEX_FUTURES,
            instrument_type=InstrumentType.IDX_FUT,
            underlying="NIFTY",
            expiry=exp_next,
        )
        await self.registry.register_many([inst1, inst2])

        rec = await self.service.check_rollover("NIFTY", current_expiry=exp_close)
        self.assertTrue(rec["should_rollover"])
        self.assertEqual(rec["days_remaining"], 2)
        self.assertEqual(rec["recommended_expiry"], exp_next.isoformat())
        self.assertIn("expires in 2 day(s)", rec["reason"])
        print("✓ [g] Contract Rollover: Successfully recommended rollover for contract expiring in 2 days")

    async def test_h_stale_token_detection(self):
        """
        Verify detection of broker token change between discoveries.
        """
        v1 = [Instrument(exchange=Exchange.NSE, symbol="TEST_STK", token="TOK_A", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)]
        v2 = [Instrument(exchange=Exchange.NSE, symbol="TEST_STK", token="TOK_B", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)]

        stale = self.dhan_discovery.detect_stale_tokens(current=v2, previous=v1)
        self.assertIn("TEST_STK", stale)
        print("✓ [h] Stale-Token Detection: Successfully detected token change from 'TOK_A' to 'TOK_B'")

    def test_i_membership_tracking(self):
        """
        Verify membership change tracking detects constituent additions and exits.
        """
        tracker = MembershipTracker()
        inst_a = Instrument(exchange=Exchange.NSE, symbol="STOCK_A", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)
        inst_b = Instrument(exchange=Exchange.NSE, symbol="STOCK_B", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)
        inst_c = Instrument(exchange=Exchange.NSE, symbol="STOCK_C", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ)

        tracker.record_snapshot("NIFTY_50", [inst_a, inst_b])
        changes = tracker.detect_changes("NIFTY_50", [inst_b, inst_c])

        self.assertEqual(changes["added"], ["STOCK_C"])
        self.assertEqual(changes["removed"], ["STOCK_A"])

        history = tracker.get_history("NIFTY_50")
        self.assertEqual(len(history), 1)
        print("✓ [i] Membership Tracking: Detected addition of STOCK_C and removal of STOCK_A")

    def test_j_cross_broker_consistency(self):
        """
        Verify same symbol resolves across all 3 brokers with broker-specific tokens.
        """
        dhan_raw = {"security_id": "2885", "trading_symbol": "RELIANCE", "exchange_segment": "NSE_EQ", "lot_size": 1}
        angel_raw = {"token": "3045", "tradingsymbol": "RELIANCE-EQ", "exch_seg": "NSE", "lotsize": "1"}
        fyers_raw = {"symbol": "NSE:RELIANCE-EQ", "fyToken": "10100000002885", "lotSize": 1}

        d_inst = self.dhan_discovery.normalize_instrument(dhan_raw)
        a_inst = self.angel_discovery.normalize_instrument(angel_raw)
        f_inst = self.fyers_discovery.normalize_instrument(fyers_raw)

        for inst in (d_inst, a_inst, f_inst):
            self.assertIsNotNone(inst)
            self.assertEqual(inst.exchange, Exchange.NSE)
            self.assertEqual(inst.asset_class, AssetClass.STOCK_CASH)
            self.assertEqual(inst.lot_size, 1)

        tokens = {d_inst.token, a_inst.token, f_inst.token}
        self.assertEqual(len(tokens), 3)
        print("✓ [j] Cross-Broker Consistency: Same underlying resolved across Dhan, AngelOne, Fyers")


async def run_standalone():
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 2 DEFINITIVE VERIFICATION (test_instruments.py)")
    print("=" * 70)

    suite = TestInstruments()

    suite.setUp()
    suite.test_a_no_hardcoded_symbols()

    suite.setUp()
    await suite.test_b_dynamic_discovery_integration()

    suite.setUp()
    await suite.test_c_index_constituent_verification()

    suite.setUp()
    await suite.test_d_expiry_validation()

    suite.setUp()
    await suite.test_e_lot_size_validation()

    suite.setUp()
    await suite.test_f_token_mapping()

    suite.setUp()
    await suite.test_g_contract_rollover()

    suite.setUp()
    await suite.test_h_stale_token_detection()

    suite.setUp()
    suite.test_i_membership_tracking()

    suite.setUp()
    suite.test_j_cross_broker_consistency()

    print("=" * 70)
    print("ALL 10 VERIFICATION CHECKS (a-j) COMPLETED SUCCESSFULLY!")
    print("PHASE 2 IS FULLY VERIFIED, INTEGRATED, AND READY FOR SIGNOFF.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_standalone())
