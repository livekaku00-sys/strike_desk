"""
S10 — Broker Timestamp Normalization Tests

Covers the 16 test categories required by S10:
  1.  AngelOne valid ms epoch → correct UTC datetime
  2.  AngelOne synthetic on None timestamp
  3.  AngelOne synthetic on invalid/unparseable string
  4.  AngelOne ISO-8601 UTC string → correct UTC
  5.  AngelOne ISO-8601 IST string → converted to UTC
  6.  Dhan valid seconds epoch → correct UTC datetime
  7.  Dhan synthetic on None timestamp
  8.  Dhan ISO-8601 UTC string → correct UTC
  9.  Fyers valid seconds epoch → correct UTC datetime
 10.  Fyers synthetic on None timestamp
 11.  Fyers ISO-8601 IST string → converted to UTC
 12.  AngelOne always treats epoch as ms (not magnitude heuristic)
 13.  Dhan always treats epoch as seconds (not magnitude heuristic)
 14.  All synthetic timestamps are timezone-aware UTC
 15.  Non-synthetic ticks carry source="live"; synthetic carry source="receipt_time"
 16.  Regression: existing broker payload formats still produce correct LTP values
"""
import unittest
from datetime import datetime, timezone, timedelta

from utils.tick_normalizer import TickNormalizer, _parse_timestamp_with_unit

_IST = timezone(timedelta(hours=5, minutes=30))

# Canonical epoch used across tests: 2024-09-19 10:00:00 UTC
_EPOCH_S = 1726740000          # seconds → 2024-09-19T10:00:00Z
_EPOCH_MS = 1726740000000      # milliseconds → same instant
_EXPECTED_UTC = datetime(2024, 9, 19, 10, 0, 0, tzinfo=timezone.utc)


def _angel_raw(ts_value, ltp_paise=14275):
    return {
        "token": "NIFTY26SEP24500CE",
        "exchange_type": "2",
        "last_traded_price": ltp_paise,
        "exchange_timestamp": ts_value,
    }


def _dhan_raw(ts_value, ltp=2980.5):
    return {
        "security_id": "RELIANCE",
        "exchange_segment": "NSE_EQ",
        "LTP": ltp,
        "LTT": ts_value,
    }


def _fyers_raw(ts_value, ltp=51200.0):
    return {
        "symbol": "NSE:BANKNIFTY24SEPFUT",
        "ltp": ltp,
        "exch_feed_time": ts_value,
        "bid": 51195.0,
        "ask": 51205.0,
        "bqty": 15,
        "aqty": 30,
    }


class TestS10AngelOneTimestamp(unittest.TestCase):
    # 1. Valid ms epoch
    def test_01_angelone_valid_ms_epoch(self):
        tick = TickNormalizer.normalize_angelone(_angel_raw(_EPOCH_MS))
        self.assertEqual(tick.timestamp, _EXPECTED_UTC)
        self.assertIsNotNone(tick.timestamp.tzinfo)

    # 2. Synthetic on None
    def test_02_angelone_synthetic_on_none(self):
        raw = _angel_raw(None)
        del raw["exchange_timestamp"]
        tick = TickNormalizer.normalize_angelone(raw)
        self.assertEqual(tick.source, "receipt_time")
        self.assertIsNotNone(tick.timestamp.tzinfo)

    # 3. Synthetic on invalid string
    def test_03_angelone_synthetic_on_invalid_string(self):
        tick = TickNormalizer.normalize_angelone(_angel_raw("not-a-timestamp"))
        self.assertEqual(tick.source, "receipt_time")
        self.assertIsNotNone(tick.timestamp.tzinfo)

    # 4. ISO-8601 UTC string
    def test_04_angelone_iso8601_utc_string(self):
        tick = TickNormalizer.normalize_angelone(_angel_raw("2024-09-19T10:00:00Z"))
        self.assertEqual(tick.timestamp, _EXPECTED_UTC)
        self.assertEqual(tick.source, "live")

    # 5. ISO-8601 IST string → converted to UTC
    def test_05_angelone_iso8601_ist_string(self):
        # 10:00 UTC = 15:30 IST (UTC+5:30)
        ist_str = "2024-09-19T15:30:00+05:30"
        tick = TickNormalizer.normalize_angelone(_angel_raw(ist_str))
        self.assertEqual(tick.timestamp, _EXPECTED_UTC)
        self.assertEqual(tick.source, "live")


class TestS10DhanTimestamp(unittest.TestCase):
    # 6. Valid seconds epoch
    def test_06_dhan_valid_seconds_epoch(self):
        tick = TickNormalizer.normalize_dhan(_dhan_raw(_EPOCH_S))
        self.assertEqual(tick.timestamp, _EXPECTED_UTC)
        self.assertIsNotNone(tick.timestamp.tzinfo)

    # 7. Synthetic on None
    def test_07_dhan_synthetic_on_none(self):
        raw = _dhan_raw(None)
        del raw["LTT"]
        tick = TickNormalizer.normalize_dhan(raw)
        self.assertEqual(tick.source, "receipt_time")
        self.assertIsNotNone(tick.timestamp.tzinfo)

    # 8. ISO-8601 UTC string
    def test_08_dhan_iso8601_utc_string(self):
        tick = TickNormalizer.normalize_dhan(_dhan_raw("2024-09-19T10:00:00+00:00"))
        self.assertEqual(tick.timestamp, _EXPECTED_UTC)
        self.assertEqual(tick.source, "live")


class TestS10FyersTimestamp(unittest.TestCase):
    # 9. Valid seconds epoch
    def test_09_fyers_valid_seconds_epoch(self):
        tick = TickNormalizer.normalize_fyers(_fyers_raw(_EPOCH_S))
        self.assertEqual(tick.timestamp, _EXPECTED_UTC)
        self.assertIsNotNone(tick.timestamp.tzinfo)

    # 10. Synthetic on None
    def test_10_fyers_synthetic_on_none(self):
        raw = _fyers_raw(None)
        del raw["exch_feed_time"]
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertEqual(tick.source, "receipt_time")
        self.assertIsNotNone(tick.timestamp.tzinfo)

    # 11. ISO-8601 IST string → converted to UTC
    def test_11_fyers_iso8601_ist_string(self):
        # 10:00 UTC = 15:30 IST (UTC+5:30)
        ist_str = "2024-09-19T15:30:00+05:30"
        tick = TickNormalizer.normalize_fyers(_fyers_raw(ist_str))
        self.assertEqual(tick.timestamp, _EXPECTED_UTC)
        self.assertEqual(tick.source, "live")


class TestS10EpochUnitSemantics(unittest.TestCase):
    # 12. AngelOne always treats epoch as ms, not heuristic
    def test_12_angelone_small_value_treated_as_ms(self):
        # 86_400_000 ms = exactly 1 day after Unix epoch
        # Under old heuristic (< 1e11): would be treated as seconds = 1971
        # Under S10: AngelOne always ms → 86400 s = 1970-01-02T00:00:00Z
        tick = TickNormalizer.normalize_angelone(_angel_raw(86_400_000))
        expected = datetime(1970, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(tick.timestamp, expected)
        self.assertEqual(tick.source, "live")

    # 13. Dhan always treats epoch as seconds, not heuristic
    def test_13_dhan_large_value_treated_as_seconds(self):
        # 200_000_000_000 s is above 1e11 — old heuristic would divide by 1000
        # Under S10: Dhan is always seconds → fromtimestamp(2e11, utc)
        large_s = 200_000_000_000
        tick = TickNormalizer.normalize_dhan(_dhan_raw(large_s))
        from datetime import datetime as _dt
        expected = _dt.fromtimestamp(large_s, tz=timezone.utc)
        self.assertEqual(tick.timestamp, expected)
        self.assertEqual(tick.source, "live")


class TestS10Invariants(unittest.TestCase):
    # 14. All synthetic timestamps are timezone-aware UTC
    def test_14_all_synthetic_timestamps_are_utc_aware(self):
        raw_a = {"token": "X", "exchange_type": "2", "last_traded_price": 10000}
        raw_d = {"security_id": "X"}
        raw_f = {"symbol": "NSE:X", "ltp": 100.0}

        for raw, method in [
            (raw_a, TickNormalizer.normalize_angelone),
            (raw_d, TickNormalizer.normalize_dhan),
            (raw_f, TickNormalizer.normalize_fyers),
        ]:
            with self.subTest(broker=method.__name__):
                tick = method(raw)
                self.assertIsNotNone(tick.timestamp.tzinfo, "timestamp must be tz-aware")
                self.assertEqual(
                    tick.timestamp.utcoffset().total_seconds(), 0,
                    "timestamp must be UTC",
                )

    # 15. source field distinguishes live vs receipt_time
    def test_15_source_field_live_vs_receipt_time(self):
        # Valid epochs → source="live"
        live_angel = TickNormalizer.normalize_angelone(_angel_raw(_EPOCH_MS))
        self.assertEqual(live_angel.source, "live")

        live_dhan = TickNormalizer.normalize_dhan(_dhan_raw(_EPOCH_S))
        self.assertEqual(live_dhan.source, "live")

        live_fyers = TickNormalizer.normalize_fyers(_fyers_raw(_EPOCH_S))
        self.assertEqual(live_fyers.source, "live")

        # Missing timestamps → source="receipt_time"
        synth_angel = TickNormalizer.normalize_angelone(
            {"token": "X", "exchange_type": "2", "last_traded_price": 10000}
        )
        self.assertEqual(synth_angel.source, "receipt_time")

        synth_dhan = TickNormalizer.normalize_dhan({"security_id": "X"})
        self.assertEqual(synth_dhan.source, "receipt_time")

        synth_fyers = TickNormalizer.normalize_fyers({"symbol": "NSE:X", "ltp": 100.0})
        self.assertEqual(synth_fyers.source, "receipt_time")


class TestS10Regression(unittest.TestCase):
    # 16. Existing broker formats still produce correct LTP values
    def test_16_regression_existing_broker_formats(self):
        dhan_raw = {
            "type": "Quote", "security_id": "RELIANCE", "exchange_segment": "NSE_EQ",
            "LTP": 2980.50, "LTT": 1726740000, "volume": 1250000,
            "total_buy_quantity": 45000, "total_sell_quantity": 38000,
            "open": 2950.0, "high": 2995.0, "low": 2942.0, "close": 2945.0,
            "avg_price": 2972.3, "OI": 0,
            "depth": {"buy": [{"price": 2980.0, "quantity": 150}],
                      "sell": [{"price": 2980.5, "quantity": 300}]},
        }
        dhan_tick = TickNormalizer.normalize_dhan(dhan_raw)
        self.assertEqual(dhan_tick.ltp, 2980.50)
        self.assertEqual(dhan_tick.symbol, "RELIANCE")
        self.assertEqual(dhan_tick.timestamp, _EXPECTED_UTC)
        self.assertEqual(dhan_tick.source, "live")

        angel_raw = {
            "token": "NIFTY26SEP24500CE", "exchange_type": "2",
            "last_traded_price": 14275,
            "exchange_timestamp": 1726740000000,
            "volume_trade_for_the_day": 850000,
            "total_buy_quantity": 120000, "total_sell_quantity": 115000,
            "open_price_of_the_day": 12000, "high_price_of_the_day": 16500,
            "low_price_of_the_day": 11000, "closed_price": 12500,
            "avg_price": 13850, "open_interest": 450000,
        }
        angel_tick = TickNormalizer.normalize_angelone(angel_raw)
        self.assertAlmostEqual(angel_tick.ltp, 142.75)
        self.assertEqual(angel_tick.symbol, "NIFTY26SEP24500CE")
        self.assertEqual(angel_tick.timestamp, _EXPECTED_UTC)
        self.assertEqual(angel_tick.source, "live")

        fyers_raw = {
            "symbol": "NSE:BANKNIFTY24SEPFUT", "ltp": 51200.0,
            "exch_feed_time": 1726740000,
            "bid": 51195.0, "ask": 51205.0, "bqty": 15, "aqty": 30,
            "vol_traded_today": 320000, "tot_buy_gbd": 50000, "tot_sell_gbd": 48000,
            "open_price": 50800.0, "high_price": 51350.0, "low_price": 50750.0,
            "prev_close_price": 50900.0, "atp": 51110.0, "oi": 180000, "pdoi": 175000,
        }
        fyers_tick = TickNormalizer.normalize_fyers(fyers_raw)
        self.assertEqual(fyers_tick.ltp, 51200.0)
        self.assertEqual(fyers_tick.symbol, "NSE:BANKNIFTY24SEPFUT")
        self.assertEqual(fyers_tick.timestamp, _EXPECTED_UTC)
        self.assertEqual(fyers_tick.source, "live")


if __name__ == "__main__":
    unittest.main()
