import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from models.tick import Tick
from pipeline.tick_buffer import TickBuffer
from utils.tick_normalizer import TickNormalizer


class TestTickNormalization(unittest.IsolatedAsyncioTestCase):
    def test_a_normalization_from_each_broker_format(self):
        dhan_raw = {
            "type": "Quote", "security_id": "RELIANCE", "exchange_segment": "NSE_EQ",
            "LTP": 2980.50, "LTT": 1726740000, "volume": 1250000, "total_buy_quantity": 45000,
            "total_sell_quantity": 38000, "open": 2950.0, "high": 2995.0, "low": 2942.0,
            "close": 2945.0, "avg_price": 2972.3, "OI": 0,
            "depth": {"buy": [{"price": 2980.0, "quantity": 150}], "sell": [{"price": 2980.5, "quantity": 300}]},
        }
        dhan_tick = TickNormalizer.normalize_dhan(dhan_raw)
        self.assertEqual(dhan_tick.symbol, "RELIANCE")
        self.assertEqual(dhan_tick.instrument_type, "EQ")
        self.assertEqual(dhan_tick.ltp, 2980.50)

        # AngelOne SmartWebSocket v2 sends every price field in paise.
        angel_raw = {
            "token": "NIFTY26SEP24500CE", "exchange_type": "2", "last_traded_price": 14275,
            "exchange_timestamp": 1726740000000, "volume_trade_for_the_day": 850000,
            "total_buy_quantity": 120000, "total_sell_quantity": 115000, "open_price_of_the_day": 12000,
            "high_price_of_the_day": 16500, "low_price_of_the_day": 11000, "closed_price": 12500,
            "avg_price": 13850, "open_interest": 450000,
        }
        angel_tick = TickNormalizer.normalize_angelone(angel_raw)
        self.assertEqual(angel_tick.symbol, "NIFTY26SEP24500CE")
        self.assertEqual(angel_tick.instrument_type, "OPT")
        self.assertEqual(angel_tick.ltp, 142.75)

        fyers_raw = {
            "symbol": "NSE:BANKNIFTY24SEPFUT", "ltp": 51200.0, "exch_feed_time": 1726740000,
            "bid": 51195.0, "ask": 51205.0, "bqty": 15, "aqty": 30, "vol_traded_today": 320000,
            "tot_buy_gbd": 50000, "tot_sell_gbd": 48000, "open_price": 50800.0, "high_price": 51350.0,
            "low_price": 50750.0, "prev_close_price": 50900.0, "atp": 51110.0, "oi": 180000, "pdoi": 175000,
        }
        fyers_tick = TickNormalizer.normalize_fyers(fyers_raw)
        self.assertEqual(fyers_tick.symbol, "NSE:BANKNIFTY24SEPFUT")
        self.assertEqual(fyers_tick.instrument_type, "FUT")
        self.assertEqual(fyers_tick.ltp, 51200.0)

    async def test_b_duplicate_tick_detection(self):
        buffer = TickBuffer()
        now = datetime.now(timezone.utc)
        t1 = Tick(symbol="INFY", exchange="NSE", instrument_type="EQ", timestamp=now, ltp=1850.0)
        self.assertTrue(await buffer.push(t1))
        self.assertFalse(await buffer.push(t1))

        stale_time = now - timedelta(milliseconds=250)
        stale_tick = Tick(symbol="INFY", exchange="NSE", instrument_type="EQ", timestamp=stale_time, ltp=1848.0)
        self.assertFalse(await buffer.push(stale_tick))

    async def test_c_out_of_order_handling(self):
        buffer = TickBuffer(buffer_window_ms=500, clock_skew_tolerance_ms=100)
        q = buffer.subscribe("TCS")
        t0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)

        t_later = Tick(symbol="TCS", exchange="NSE", instrument_type="EQ", timestamp=t0 + timedelta(milliseconds=60), ltp=3502.0)
        t_earlier = Tick(symbol="TCS", exchange="NSE", instrument_type="EQ", timestamp=t0 + timedelta(milliseconds=10), ltp=3500.0)

        await buffer.push(t_later)
        await buffer.push(t_earlier)
        self.assertTrue(t_earlier.late)

        await buffer.flush()
        item1 = q.get_nowait()
        item2 = q.get_nowait()
        self.assertEqual(item1.timestamp, t_earlier.timestamp)
        self.assertEqual(item2.timestamp, t_later.timestamp)

    async def test_d_tick_distribution_to_multiple_consumers(self):
        buffer = TickBuffer()
        sym = "SBIN"
        q1 = buffer.subscribe(sym)
        q2 = buffer.subscribe(sym)

        tick = Tick(symbol=sym, exchange="NSE", instrument_type="EQ", timestamp=datetime.now(timezone.utc), ltp=820.0)
        await buffer.push(tick)
        await buffer.flush()

        self.assertEqual(q1.qsize(), 1)
        self.assertEqual(q2.qsize(), 1)

    async def test_e_backpressure_mechanism(self):
        buffer = TickBuffer(max_queue_size=3)
        sym = "HDFCBANK"
        q = buffer.subscribe(sym)
        t0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)

        for i in range(5):
            t = Tick(symbol=sym, exchange="NSE", instrument_type="EQ", timestamp=t0 + timedelta(seconds=i), ltp=1600.0 + i)
            await buffer.push(t)
            await buffer.flush()

        self.assertEqual(q.qsize(), 3)
        item1 = q.get_nowait()
        self.assertEqual(item1.ltp, 1602.0)


if __name__ == "__main__":
    unittest.main()
