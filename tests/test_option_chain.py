import asyncio
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import MagicMock, patch

from adapters.dhan_adapter import DhanAdapter
from adapters.angelone_adapter import AngelOneAdapter
from adapters.fyers_adapter import FyersAdapter
from adapters.option_chain_fetcher import option_chain_fetcher
from models.option_chain import OptionChain, OptionContract, StrikeData
from utils.cache_manager import option_chain_cache, DataFreshness
from utils.pcr_calculator import pcr_calculator


class TestOptionChain(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        option_chain_cache.clear()

    def test_b_pcr_calculation_with_known_data(self):
        chain = OptionChain(
            underlying="NIFTY",
            expiry_date="26-SEP-2024",
            strikes=[
                StrikeData(strike_price=24000.0, call=OptionContract(ltp=250.0, oi=1000.0), put=OptionContract(ltp=45.0, oi=1500.0)),
                StrikeData(strike_price=24100.0, call=OptionContract(ltp=175.0, oi=2000.0), put=OptionContract(ltp=70.0, oi=2000.0)),
                StrikeData(strike_price=24200.0, call=OptionContract(ltp=110.0, oi=0.0), put=OptionContract(ltp=105.0, oi=500.0)),
            ],
        )
        overall_pcr, strike_pcrs = pcr_calculator.calculate(chain)
        self.assertEqual(chain.total_call_oi, 3000.0)
        self.assertEqual(chain.total_put_oi, 4000.0)
        self.assertAlmostEqual(overall_pcr, 1.3333, places=4)
        self.assertAlmostEqual(strike_pcrs[24000.0], 1.5, places=4)
        self.assertEqual(strike_pcrs[24200.0], 0.0)

    def test_c_cache_invalidation(self):
        underlying = "BANKNIFTY"
        exp1 = "25-SEP-2024"
        exp2 = "03-OCT-2024"

        dummy_chain1 = OptionChain(underlying=underlying, expiry_date=exp1)
        dummy_chain2 = OptionChain(underlying=underlying, expiry_date=exp2)

        option_chain_cache.put(underlying, exp1, dummy_chain1)
        self.assertIsNotNone(option_chain_cache.get(underlying, exp1))
        self.assertIsNone(option_chain_cache.get(underlying, exp1, force_refresh=True))

        option_chain_cache.put(underlying, exp2, dummy_chain2)
        self.assertIsNone(option_chain_cache.get(underlying, exp1))
        self.assertIsNotNone(option_chain_cache.get(underlying, exp2))

    def test_d_freshness_detection(self):
        underlying = "NIFTY"
        expiry = "26-SEP-2024"
        dummy = OptionChain(underlying=underlying, expiry_date=expiry)
        option_chain_cache.put(underlying, expiry, dummy)

        self.assertEqual(option_chain_cache.get_freshness(underlying, expiry), DataFreshness.FRESH.value)

        with patch("time.monotonic", return_value=time.monotonic() + 40):
            self.assertEqual(option_chain_cache.get_freshness(underlying, expiry), DataFreshness.STALE.value)

        with patch("time.monotonic", return_value=time.monotonic() + 75):
            self.assertEqual(option_chain_cache.get_freshness(underlying, expiry), DataFreshness.EXPIRED.value)
            self.assertIsNone(option_chain_cache.get(underlying, expiry))

    def test_e_data_normalization(self):
        dhan_raw = {
            "data": {
                "oc": {
                    "24500.00": {
                        "ce": {"last_price": 140.0, "oi": 5000, "volume": 20000, "top_bid_price": 139.5, "top_ask_price": 140.0, "implied_volatility": 14.2},
                        "pe": {"last_price": 85.0, "oi": 7500, "volume": 18000, "top_bid_price": 84.8, "top_ask_price": 85.0, "implied_volatility": 13.8},
                    }
                }
            }
        }
        dhan_chain = option_chain_fetcher.normalize_dhan_chain(dhan_raw, "NIFTY", "26-SEP-2024")
        self.assertEqual(dhan_chain.underlying, "NIFTY")
        self.assertAlmostEqual(dhan_chain.pcr, 1.5, places=4)

        angel_raw = {
            "data": [
                {
                    "strike_price": 24500.0,
                    "ce": {"ltp": 140.0, "oi": 5000, "volume": 20000, "bid": 139.5, "ask": 140.0, "iv": 14.2},
                    "pe": {"ltp": 85.0, "oi": 7500, "volume": 18000, "bid": 84.8, "ask": 85.0, "iv": 13.8},
                }
            ]
        }
        angel_chain = option_chain_fetcher.normalize_angelone_chain(angel_raw, "NIFTY", "26-SEP-2024")
        self.assertAlmostEqual(angel_chain.pcr, 1.5, places=4)

        fyers_raw = {
            "data": {
                "optionsChain": [
                    {
                        "strike_price": 24500.0,
                        "call_ltp": 140.0, "call_oi": 5000, "call_volume": 20000, "call_bid": 139.5, "call_ask": 140.0, "call_iv": 14.2,
                        "put_ltp": 85.0, "put_oi": 7500, "put_volume": 18000, "put_bid": 84.8, "put_ask": 85.0, "put_iv": 13.8,
                    }
                ]
            }
        }
        fyers_chain = option_chain_fetcher.normalize_fyers_chain(fyers_raw, "NIFTY", "26-SEP-2024")
        self.assertAlmostEqual(fyers_chain.pcr, 1.5, places=4)

    async def test_a_option_chain_fetching_from_each_broker(self):
        dhan = DhanAdapter()
        angel = AngelOneAdapter()
        fyers = FyersAdapter()

        # Dhan v2 nests the chain one level deeper and exposes it as client.option_chain().
        dhan_payload = {"data": {"data": {"last_price": 24010.0, "oc": {
            "24000.0": {"ce": {"last_price": 100, "oi": 1000}, "pe": {"last_price": 50, "oi": 1200}}
        }}}}
        angel_payload = {"data": [{"strike_price": 24000.0, "ce": {"ltp": 100, "oi": 1000}, "pe": {"ltp": 50, "oi": 1200}}]}
        fyers_payload = {"data": {"optionsChain": [{"strike_price": 24000.0, "call_ltp": 100, "call_oi": 1000, "put_ltp": 50, "put_oi": 1200}]}}

        with patch.object(dhan, "_client", MagicMock(option_chain=MagicMock(return_value=dhan_payload))):
            dhan_res = await dhan.get_option_chain("NIFTY", "26-SEP-2024")
            self.assertEqual(dhan_res["underlying"], "NIFTY")
            self.assertEqual(dhan_res["pcr"], 1.2)

        with patch.object(angel, "_smart_api", MagicMock(getOptionChain=MagicMock(return_value=angel_payload))):
            angel_res = await angel.get_option_chain("NIFTY", "26-SEP-2024")
            self.assertEqual(angel_res["underlying"], "NIFTY")
            self.assertEqual(angel_res["pcr"], 1.2)

        with patch.object(fyers, "_fyers_model", MagicMock(optionchain=MagicMock(return_value=fyers_payload))):
            fyers_res = await fyers.get_option_chain("NIFTY", "26-SEP-2024")
            self.assertEqual(fyers_res["underlying"], "NIFTY")
            self.assertEqual(fyers_res["pcr"], 1.2)


if __name__ == "__main__":
    unittest.main()
