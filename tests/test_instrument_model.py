import asyncio
from datetime import date, datetime, timedelta, timezone
import os
import sys
import unittest

from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from services.instrument_registry import InstrumentRegistry, load_symbol_map


class TestInstrumentModel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = InstrumentRegistry(ttl_seconds=14400)

    def test_a_instrument_creation_all_5_asset_classes(self):
        # 1. STOCK_CASH
        eq = Instrument(
            exchange=Exchange.NSE,
            symbol="RELIANCE",
            broker_symbol="NSE:RELIANCE-EQ",
            token="2885",
            asset_class=AssetClass.STOCK_CASH,
            instrument_type=InstrumentType.EQ,
            lot_size=1,
            isin="INE002A01018",
            name="Reliance Industries Limited",
        )
        self.assertEqual(eq.asset_class, AssetClass.STOCK_CASH)
        self.assertEqual(eq.lot_size, 1)

        # 2. INDEX_OPTIONS
        idx_opt = Instrument(
            exchange=Exchange.NSE,
            symbol="NIFTY26SEP24500CE",
            broker_symbol="NSE:NIFTY26SEP24500CE",
            token="35012",
            asset_class=AssetClass.INDEX_OPTIONS,
            instrument_type=InstrumentType.IDX_OPT,
            underlying="NIFTY",
            expiry=date(2026, 9, 26),
            strike_price=24500.0,
            option_type="CE",
            lot_size=25,
        )
        self.assertEqual(idx_opt.asset_class, AssetClass.INDEX_OPTIONS)
        self.assertEqual(idx_opt.strike_price, 24500.0)
        self.assertEqual(idx_opt.option_type, "CE")

        # 3. STOCK_OPTIONS
        stk_opt = Instrument(
            exchange=Exchange.NSE,
            symbol="RELIANCE26SEP3000PE",
            broker_symbol="NSE:RELIANCE26SEP3000PE",
            token="42100",
            asset_class=AssetClass.STOCK_OPTIONS,
            instrument_type=InstrumentType.OPT,
            underlying="RELIANCE",
            expiry=date(2026, 9, 26),
            strike_price=3000.0,
            option_type="PE",
            lot_size=250,
        )
        self.assertEqual(stk_opt.asset_class, AssetClass.STOCK_OPTIONS)
        self.assertEqual(stk_opt.lot_size, 250)

        # 4. STOCK_FUTURES
        stk_fut = Instrument(
            exchange=Exchange.NSE,
            symbol="RELIANCE26SEPFUT",
            broker_symbol="NSE:RELIANCE26SEPFUT",
            token="51200",
            asset_class=AssetClass.STOCK_FUTURES,
            instrument_type=InstrumentType.FUT,
            underlying="RELIANCE",
            expiry=date(2026, 9, 26),
            lot_size=250,
        )
        self.assertEqual(stk_fut.asset_class, AssetClass.STOCK_FUTURES)

        # 5. INDEX_FUTURES
        idx_fut = Instrument(
            exchange=Exchange.NSE,
            symbol="NIFTY26SEPFUT",
            broker_symbol="NSE:NIFTY26SEPFUT",
            token="51300",
            asset_class=AssetClass.INDEX_FUTURES,
            instrument_type=InstrumentType.IDX_FUT,
            underlying="NIFTY",
            expiry=date(2026, 9, 26),
            lot_size=25,
        )
        self.assertEqual(idx_fut.asset_class, AssetClass.INDEX_FUTURES)
        self.assertEqual(idx_fut.lot_size, 25)

        for inst in (eq, idx_opt, stk_opt, stk_fut, idx_fut):
            d = inst.to_dict()
            self.assertIn("symbol", d)
            self.assertIn("asset_class", d)
            self.assertTrue(inst.is_active)
        print("✓ [a] Instrument creation verified for all 5 asset classes")

    def test_b_validation_rules_rejection(self):
        # 1. lot_size <= 0
        with self.assertRaises(ValueError):
            Instrument(
                exchange=Exchange.NSE,
                symbol="TCS",
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
                lot_size=0,
            )

        with self.assertRaises(ValueError):
            Instrument(
                exchange=Exchange.NSE,
                symbol="TCS",
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
                lot_size=-10,
            )

        # 2. Empty symbol
        with self.assertRaises(ValueError):
            Instrument(
                exchange=Exchange.NSE,
                symbol="",
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
            )

        with self.assertRaises(ValueError):
            Instrument(
                exchange=Exchange.NSE,
                symbol="   ",
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
            )

        # 3. Invalid exchange
        with self.assertRaises(ValueError):
            Instrument(
                exchange="NASDAQ",  # type: ignore
                symbol="AAPL",
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
            )

        # 4. Invalid tick_size <= 0
        with self.assertRaises(ValueError):
            Instrument(
                exchange=Exchange.NSE,
                symbol="INFY",
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
                tick_size=0.0,
            )

        # 5. Invalid option_type
        with self.assertRaises(ValueError):
            Instrument(
                exchange=Exchange.NSE,
                symbol="NIFTY24500CALL",
                asset_class=AssetClass.INDEX_OPTIONS,
                instrument_type=InstrumentType.IDX_OPT,
                option_type="CALL",  # Must be 'CE' or 'PE'
            )
        print("✓ [b] Validation rules rejection verified")

    async def test_c_registry_registration_and_retrieval(self):
        await self.registry.clear()
        inst1 = Instrument(
            exchange=Exchange.NSE,
            symbol="RELIANCE",
            token="2885",
            asset_class=AssetClass.STOCK_CASH,
            instrument_type=InstrumentType.EQ,
            isin="INE002A01018",
        )
        inst2 = Instrument(
            exchange=Exchange.BSE,
            symbol="500325",
            token="500325",
            asset_class=AssetClass.STOCK_CASH,
            instrument_type=InstrumentType.EQ,
            isin="INE002A01018",
        )

        await self.registry.register(inst1)
        await self.registry.register(inst2)

        # Lookup by exchange & symbol
        res1 = await self.registry.get(Exchange.NSE, "RELIANCE")
        self.assertEqual(res1, inst1)
        res_bse = await self.registry.get(Exchange.BSE, "500325")
        self.assertEqual(res_bse, inst2)

        # Lookup by token
        res_tok = await self.registry.get_by_token(Exchange.NSE, "2885")
        self.assertEqual(res_tok, inst1)

        # Lookup by ISIN
        res_isin = await self.registry.get_by_isin("INE002A01018")
        self.assertIsNotNone(res_isin)
        self.assertEqual(res_isin.isin, "INE002A01018")

        # Missing lookup
        self.assertIsNone(await self.registry.get(Exchange.NSE, "NONEXISTENT"))
        self.assertIsNone(await self.registry.get_by_token(Exchange.NSE, "999999"))
        self.assertIsNone(await self.registry.get_by_isin("INE000000000"))
        print("✓ [c] Registry registration and retrieval (symbol/token/ISIN) verified")

    async def test_d_registry_filtering(self):
        await self.registry.clear()
        items = [
            Instrument(exchange=Exchange.NSE, symbol="SBIN", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ),
            Instrument(exchange=Exchange.NSE, symbol="SBIN26SEPFUT", asset_class=AssetClass.STOCK_FUTURES, instrument_type=InstrumentType.FUT, underlying="SBIN"),
            Instrument(exchange=Exchange.NSE, symbol="SBIN26SEP800CE", asset_class=AssetClass.STOCK_OPTIONS, instrument_type=InstrumentType.OPT, underlying="SBIN", strike_price=800.0, option_type="CE"),
            Instrument(exchange=Exchange.NSE, symbol="NIFTY26SEP24000CE", asset_class=AssetClass.INDEX_OPTIONS, instrument_type=InstrumentType.IDX_OPT, underlying="NIFTY", strike_price=24000.0, option_type="CE"),
            Instrument(exchange=Exchange.BSE, symbol="SBIN_BSE", asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ, is_active=False),
        ]
        await self.registry.register_many(items)

        # Filter by asset class
        cash_items = await self.registry.list_by_asset_class(AssetClass.STOCK_CASH)
        self.assertEqual(len(cash_items), 2)

        # Filter by underlying
        sbin_derivatives = await self.registry.list_by_underlying("SBIN")
        self.assertEqual(len(sbin_derivatives), 2)

        # Multi-attribute filter with active_only
        active_nse_cash = await self.registry.filter(exchange=Exchange.NSE, asset_class=AssetClass.STOCK_CASH, active_only=True)
        self.assertEqual(len(active_nse_cash), 1)
        self.assertEqual(active_nse_cash[0].symbol, "SBIN")

        # Multi-attribute filter including inactive
        all_bse = await self.registry.filter(exchange=Exchange.BSE, active_only=False)
        self.assertEqual(len(all_bse), 1)
        print("✓ [d] Registry filtering by asset class, underlying, and exchange verified")

    async def test_e_ttl_freshness_detection(self):
        await self.registry.clear()
        self.assertFalse(self.registry.is_fresh())

        inst = Instrument(
            exchange=Exchange.NSE,
            symbol="INFY",
            asset_class=AssetClass.STOCK_CASH,
            instrument_type=InstrumentType.EQ,
        )
        await self.registry.register(inst)
        self.assertTrue(self.registry.is_fresh(max_age_seconds=14400))

        # Stale check: simulated past update
        old_time = datetime.now(timezone.utc) - timedelta(seconds=14405)
        self.registry._last_updated = old_time
        self.assertFalse(self.registry.is_fresh(max_age_seconds=14400))
        print("✓ [e] TTL freshness detection (fresh within 4h, stale after 4h) verified")

    async def test_f_symbol_map_fallback_loader(self):
        await self.registry.clear()
        initial_count = self.registry.count()
        data = load_symbol_map()
        self.assertIsInstance(data, dict)
        self.assertIn("RELIANCE", data)
        self.assertIn("TCS", data)

        # Verify fallback loader does NOT auto-register into primary registry
        self.assertEqual(self.registry.count(), initial_count)
        print("✓ [f] Symbol map fallback loader verified (loads JSON, does NOT auto-register)")

    async def test_g_thread_safe_concurrent_registration(self):
        await self.registry.clear()

        async def register_worker(i: int):
            inst = Instrument(
                exchange=Exchange.NSE,
                symbol=f"CONCUR_{i}",
                token=f"T_{i}",
                asset_class=AssetClass.STOCK_CASH,
                instrument_type=InstrumentType.EQ,
                isin=f"INE{str(i).zfill(9)}",
            )
            await self.registry.register(inst)

        tasks = [register_worker(i) for i in range(100)]
        await asyncio.gather(*tasks)

        self.assertEqual(self.registry.count(), 100)
        check_inst = await self.registry.get(Exchange.NSE, "CONCUR_42")
        self.assertIsNotNone(check_inst)
        self.assertEqual(check_inst.token, "T_42")
        print("✓ [g] Thread-safe concurrent registration verified (100 async tasks)")


async def main():
    print("=" * 60)
    print("STRIKE DESK V4 — PHASE 2 PROMPT 1/5 INSTRUMENT MODEL & REGISTRY")
    print("=" * 60)

    suite = TestInstrumentModel()
    suite.setUp()
    suite.test_a_instrument_creation_all_5_asset_classes()
    suite.test_b_validation_rules_rejection()
    await suite.test_c_registry_registration_and_retrieval()
    await suite.test_d_registry_filtering()
    await suite.test_e_ttl_freshness_detection()
    await suite.test_f_symbol_map_fallback_loader()
    await suite.test_g_thread_safe_concurrent_registration()

    print("=" * 60)
    print("ALL PHASE 2 PROMPT 1/5 VERIFICATIONS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())