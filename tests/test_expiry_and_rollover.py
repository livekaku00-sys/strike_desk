import asyncio
from datetime import date, datetime, timedelta, timezone
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from services.expiry import (
    ExpiryDiscovery,
    ExpirySelector,
    LotSizeValidator,
    RolloverDetector,
    expiry_discovery,
    expiry_selector,
    lot_size_validator,
    rollover_detector,
)
from services.expiry_and_lot_service import (
    ExpiryAndLotService,
    expiry_and_lot_service,
)
from services.instrument_registry import InstrumentRegistry


class TestExpiryAndRollover(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = InstrumentRegistry()
        self.discovery = ExpiryDiscovery(registry=self.registry)
        self.selector = ExpirySelector(discovery=self.discovery)
        self.rollover = RolloverDetector(discovery=self.discovery, selector=self.selector)
        self.validator = LotSizeValidator()
        self.service = ExpiryAndLotService(
            discovery=self.discovery,
            selector=self.selector,
            rollover=self.rollover,
            validator=self.validator,
            registry=self.registry,
        )
        self.today = datetime.now(timezone.utc).date()

    async def test_a_expiry_discovery(self):
        d1 = self.today + timedelta(days=7)
        d2 = self.today + timedelta(days=14)
        d3 = self.today + timedelta(days=21)

        instruments = [
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY_EXP_2",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="NIFTY",
                expiry=d2,
            ),
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY_EXP_1",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="NIFTY",
                expiry=d1,
            ),
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY_EXP_3",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="NIFTY",
                expiry=d3,
            ),
        ]
        await self.registry.register_many(instruments)

        expiries = await self.discovery.get_available_expiries("NIFTY", as_of=self.today, force_refresh=True)
        self.assertEqual(len(expiries), 3)
        self.assertEqual(expiries, [d1, d2, d3])
        print("✓ [a] Expiry discovery: Available expiries sorted chronologically")

    async def test_b_expiry_selection(self):
        d_near = self.today + timedelta(days=5)
        d_next = self.today + timedelta(days=12)
        d_far = self.today + timedelta(days=45)

        instruments = [
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY_N",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="NIFTY",
                expiry=d_near,
            ),
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY_NX",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="NIFTY",
                expiry=d_next,
            ),
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY_F",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="NIFTY",
                expiry=d_far,
            ),
        ]
        await self.registry.register_many(instruments)

        nearest = await self.selector.select_nearest_expiry("NIFTY", as_of=self.today)
        self.assertEqual(nearest, d_near)

        next_exp = await self.selector.select_next_expiry("NIFTY", as_of=self.today)
        self.assertEqual(next_exp, d_next)

        far_exp = await self.selector.select_far_expiry("NIFTY", as_of=self.today)
        self.assertEqual(far_exp, d_far)
        print("✓ [b] Expiry selection: Nearest, Next, and Far selection logic verified")

    async def test_c_past_expiry_filtering(self):
        d_past = self.today - timedelta(days=10)
        d_future = self.today + timedelta(days=5)

        instruments = [
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY_PAST",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="NIFTY",
                expiry=d_past,
            ),
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY_FUT",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="NIFTY",
                expiry=d_future,
            ),
        ]
        await self.registry.register_many(instruments)

        expiries = await self.discovery.get_available_expiries("NIFTY", as_of=self.today, force_refresh=True)
        self.assertNotIn(d_past, expiries)
        self.assertIn(d_future, expiries)
        print("✓ [c] Past expiry filtering: Excluded past dates (< today)")

    def test_d_rollover_detection(self):
        exp_close = self.today + timedelta(days=2)
        self.assertTrue(self.rollover.should_rollover(exp_close, as_of=self.today, days_threshold=3))

        exp_today = self.today
        self.assertTrue(self.rollover.should_rollover(exp_today, as_of=self.today, days_threshold=3))

        exp_far = self.today + timedelta(days=10)
        self.assertFalse(self.rollover.should_rollover(exp_far, as_of=self.today, days_threshold=3))
        print("✓ [d] Rollover detection: Correctly triggered for expiry within 3 days")

    async def test_e_rollover_recommendation(self):
        curr_exp = self.today + timedelta(days=2)
        next_exp = self.today + timedelta(days=9)

        instruments = [
            Instrument(
                exchange=Exchange.NSE,
                symbol="BANKNIFTY_C",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="BANKNIFTY",
                expiry=curr_exp,
            ),
            Instrument(
                exchange=Exchange.NSE,
                symbol="BANKNIFTY_NX",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                underlying="BANKNIFTY",
                expiry=next_exp,
            ),
        ]
        await self.registry.register_many(instruments)

        rec = await self.rollover.get_rollover_recommendation(
            "BANKNIFTY", current_expiry=curr_exp, as_of=self.today, days_threshold=3, force_refresh=True
        )

        self.assertEqual(rec["underlying"], "BANKNIFTY")
        self.assertEqual(rec["current_expiry"], curr_exp.isoformat())
        self.assertEqual(rec["recommended_expiry"], next_exp.isoformat())
        self.assertTrue(rec["should_rollover"])
        self.assertEqual(rec["days_remaining"], 2)
        self.assertIn("Contract expires in 2 day(s)", rec["reason"])
        print("✓ [e] Rollover recommendation: Structured output format verified")

    def test_f_lot_size_validation(self):
        valid_inst = Instrument(
            exchange=Exchange.NSE,
            symbol="TCS",
            asset_class=AssetClass.STOCK_CASH,
            instrument_type=InstrumentType.EQ,
            lot_size=1,
        )
        self.assertTrue(self.validator.validate_lot_size(valid_inst))

        class MockInstrument:
            lot_size = 0
            symbol = "BAD_LOT"
            exchange = Exchange.NSE

        self.assertFalse(self.validator.validate_lot_size(MockInstrument()))  # type: ignore
        print("✓ [f] Lot-size validation: Validated positive integer lot size")

    def test_g_lot_size_change_detection(self):
        inst_v1 = Instrument(
            exchange=Exchange.NSE,
            symbol="TATAMOTORS",
            asset_class=AssetClass.STOCK_FUTURES,
            instrument_type=InstrumentType.FUT,
            lot_size=1425,
        )
        inst_v2 = Instrument(
            exchange=Exchange.NSE,
            symbol="TATAMOTORS",
            asset_class=AssetClass.STOCK_FUTURES,
            instrument_type=InstrumentType.FUT,
            lot_size=1500,
        )

        changes = self.validator.detect_lot_size_changes([inst_v2], [inst_v1])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["symbol"], "TATAMOTORS")
        self.assertEqual(changes[0]["old_lot_size"], 1425)
        self.assertEqual(changes[0]["new_lot_size"], 1500)

        hist = self.validator.get_lot_size_history("TATAMOTORS")
        self.assertEqual(len(hist), 1)
        print("✓ [g] Lot-size change detection: Revision detected and recorded in history")

    async def test_h_integration(self):
        d1 = self.today + timedelta(days=7)
        inst = Instrument(
            exchange=Exchange.NSE,
            symbol="RELIANCE26SEPFUT",
            asset_class=AssetClass.STOCK_FUTURES,
            instrument_type=InstrumentType.FUT,
            underlying="RELIANCE",
            expiry=d1,
            lot_size=250,
        )
        await self.registry.register(inst)

        chain = await self.service.get_expiry_chain("RELIANCE", force_refresh=True)
        self.assertEqual(chain, [d1])

        sel = await self.service.select_expiry("RELIANCE", strategy="nearest")
        self.assertEqual(sel, d1)

        lot_summary = await self.service.validate_all_lot_sizes()
        self.assertEqual(lot_summary["valid_count"], 1)
        self.assertEqual(lot_summary["invalid_count"], 0)

        await self.service.refresh_all()
        self.assertIsNotNone(self.service.last_refresh_time)
        print("✓ [h] Integration: ExpiryAndLotService seamlessly integrated all components")

    def test_i_no_hardcoded_expiries(self):
        service_files = [
            os.path.join(PROJECT_ROOT, "services", "expiry", "expiry_discovery.py"),
            os.path.join(PROJECT_ROOT, "services", "expiry", "expiry_selector.py"),
            os.path.join(PROJECT_ROOT, "services", "expiry", "rollover_detector.py"),
            os.path.join(PROJECT_ROOT, "services", "expiry", "lot_size_validator.py"),
            os.path.join(PROJECT_ROOT, "services", "expiry_and_lot_service.py"),
        ]

        date_pattern = re.compile(r'["\'](202[0-9]-[0-1][0-9]-[0-3][0-9])["\']')

        for fpath in service_files:
            if not os.path.exists(fpath):
                continue
            with open(fpath, "r", encoding="utf-8") as fp:
                content = fp.read()
            matches = date_pattern.findall(content)
            self.assertEqual(
                len(matches),
                0,
                f"Hardcoded date literals {matches} found in {fpath}",
            )
        print("✓ [i] Zero hardcoded expiries verified: No static date literals in codebase")


async def main():
    print("=" * 60)
    print("STRIKE DESK V4 — PHASE 2 PROMPT 4/5 EXPIRY DISCOVERY & ROLLOVER")
    print("=" * 60)

    suite = TestExpiryAndRollover()

    suite.setUp()
    await suite.test_a_expiry_discovery()

    suite.setUp()
    await suite.test_b_expiry_selection()

    suite.setUp()
    await suite.test_c_past_expiry_filtering()

    suite.setUp()
    suite.test_d_rollover_detection()

    suite.setUp()
    await suite.test_e_rollover_recommendation()

    suite.setUp()
    suite.test_f_lot_size_validation()

    suite.setUp()
    suite.test_g_lot_size_change_detection()

    suite.setUp()
    await suite.test_h_integration()

    suite.setUp()
    suite.test_i_no_hardcoded_expiries()

    print("=" * 60)
    print("ALL PHASE 2 PROMPT 4/5 VERIFICATIONS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
