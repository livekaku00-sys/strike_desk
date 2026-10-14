"""
S07 — Fyers index bid/ask data integration tests.

Verifies:
  - Index instruments are correctly classified (IDX_FUT) for both short names
    ("NIFTY") and Fyers fully-qualified symbols ("NSE:NIFTY50-INDEX").
  - bid_ask_valid is False for index instruments regardless of payload content.
  - bid_ask_valid is True only when both sides are finite, strictly positive, and
    the instrument has an order book.
  - Non-finite values (NaN, Inf) never produce bid_ask_valid=True.
  - The old 'or'-chain bug is fixed: an explicit bid=0.0 is recorded correctly
    and does not silently fall through to an alternate key.
  - Tick.bid_ask_valid field exists and defaults to False.
  - FyersAdapter.handle_raw_tick propagates bid_ask_valid from normalize_fyers.
"""
import math
import sys
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.tick import Tick
from utils.tick_normalizer import TickNormalizer, detect_instrument_type


# ── helpers ─────────────────────────────────────────────────────────────────

def _ts() -> int:
    return 1726740000


def _fyers_index_raw(symbol: str, ltp: float = 24500.0, bid=None, ask=None) -> dict:
    raw = {"symbol": symbol, "ltp": ltp, "exch_feed_time": _ts()}
    if bid is not None:
        raw["bid"] = bid
    if ask is not None:
        raw["ask"] = ask
    return raw


def _fyers_fut_raw(
    symbol: str = "NSE:BANKNIFTY24SEPFUT",
    ltp: float = 51200.0,
    bid: float = 51195.0,
    ask: float = 51205.0,
) -> dict:
    return {
        "symbol": symbol,
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "bqty": 15,
        "aqty": 30,
        "exch_feed_time": _ts(),
        "vol_traded_today": 320000,
        "open_price": 50800.0,
        "high_price": 51350.0,
        "low_price": 50750.0,
        "prev_close_price": 50900.0,
        "atp": 51110.0,
        "oi": 180000,
        "pdoi": 175000,
    }


# ── instrument-type classification ──────────────────────────────────────────

class TestFyersIndexInstrumentType(unittest.TestCase):
    def test_a_fyers_index_suffix_classified_idx_fut(self):
        self.assertEqual(detect_instrument_type("NSE:NIFTY50-INDEX"), "IDX_FUT")

    def test_b_banknifty_index_classified_idx_fut(self):
        self.assertEqual(detect_instrument_type("NSE:NIFTYBANK-INDEX"), "IDX_FUT")

    def test_c_finnifty_index_classified_idx_fut(self):
        self.assertEqual(detect_instrument_type("NSE:FINNIFTY-INDEX"), "IDX_FUT")

    def test_d_sensex_bse_index_classified_idx_fut(self):
        self.assertEqual(detect_instrument_type("BSE:SENSEX-INDEX"), "IDX_FUT")

    def test_e_midcpnifty_index_classified_idx_fut(self):
        self.assertEqual(detect_instrument_type("NSE:MIDCPNIFTY-INDEX"), "IDX_FUT")

    def test_f_short_name_nifty_classified_idx_fut(self):
        # Short canonical name (as produced by handle_raw_tick symbol translation)
        self.assertEqual(detect_instrument_type("NIFTY"), "IDX_FUT")

    def test_g_short_name_banknifty_classified_idx_fut(self):
        self.assertEqual(detect_instrument_type("BANKNIFTY"), "IDX_FUT")

    def test_h_futures_not_classified_as_idx(self):
        self.assertEqual(detect_instrument_type("NSE:BANKNIFTY24SEPFUT"), "FUT")

    def test_i_option_not_classified_as_idx(self):
        self.assertEqual(detect_instrument_type("NSE:NIFTY26SEP24050CE"), "OPT")

    def test_j_equity_not_classified_as_idx(self):
        self.assertEqual(detect_instrument_type("NSE:RELIANCE-EQ"), "EQ")


# ── Tick model ───────────────────────────────────────────────────────────────

class TestTickBidAskValidField(unittest.TestCase):
    def test_a_field_exists_defaults_false(self):
        t = Tick(
            symbol="X", exchange="NSE", instrument_type="EQ",
            timestamp=datetime.now(timezone.utc), ltp=100.0,
        )
        self.assertFalse(t.bid_ask_valid)

    def test_b_field_can_be_set_true(self):
        t = Tick(
            symbol="X", exchange="NSE", instrument_type="EQ",
            timestamp=datetime.now(timezone.utc), ltp=100.0,
            bid_price=99.0, ask_price=101.0, bid_ask_valid=True,
        )
        self.assertTrue(t.bid_ask_valid)

    def test_c_to_dict_includes_field(self):
        t = Tick(
            symbol="X", exchange="NSE", instrument_type="EQ",
            timestamp=datetime.now(timezone.utc), ltp=100.0,
            bid_price=99.0, ask_price=101.0, bid_ask_valid=True,
        )
        d = t.to_dict()
        self.assertIn("bid_ask_valid", d)
        self.assertTrue(d["bid_ask_valid"])


# ── normalize_fyers bid/ask validity ────────────────────────────────────────

class TestNormalizeFyersBidAskValidity(unittest.TestCase):
    def test_a_index_symbol_bid_ask_valid_false(self):
        raw = _fyers_index_raw("NSE:NIFTY50-INDEX", ltp=24500.0, bid=0.0, ask=0.0)
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)
        self.assertEqual(tick.instrument_type, "IDX_FUT")

    def test_b_index_symbol_no_bid_ask_keys_valid_false(self):
        raw = _fyers_index_raw("NSE:NIFTYBANK-INDEX", ltp=51200.0)
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)

    def test_c_index_short_name_bid_ask_valid_false(self):
        # After handle_raw_tick translation, symbol="NIFTY"
        raw = {"symbol": "NIFTY", "ltp": 24500.0, "bid": 0.0, "ask": 0.0, "exch_feed_time": _ts()}
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)
        self.assertEqual(tick.instrument_type, "IDX_FUT")

    def test_d_index_ltp_preserved_correctly(self):
        raw = _fyers_index_raw("NSE:NIFTY50-INDEX", ltp=24567.80)
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertAlmostEqual(tick.ltp, 24567.80)

    def test_e_futures_valid_bid_ask_valid_true(self):
        tick = TickNormalizer.normalize_fyers(_fyers_fut_raw())
        self.assertTrue(tick.bid_ask_valid)
        self.assertEqual(tick.instrument_type, "FUT")

    def test_f_futures_missing_bid_ask_valid_false(self):
        raw = {
            "symbol": "NSE:BANKNIFTY24SEPFUT", "ltp": 51200.0,
            "exch_feed_time": _ts(),
        }
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)
        self.assertEqual(tick.bid_price, 0.0)
        self.assertEqual(tick.ask_price, 0.0)

    def test_g_futures_zero_bid_bid_ask_valid_false(self):
        raw = _fyers_fut_raw(bid=0.0, ask=51205.0)
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)

    def test_h_futures_zero_ask_bid_ask_valid_false(self):
        raw = _fyers_fut_raw(bid=51195.0, ask=0.0)
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)

    def test_i_nan_bid_bid_ask_valid_false(self):
        raw = _fyers_fut_raw(bid=float("nan"), ask=51205.0)
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)

    def test_j_inf_bid_bid_ask_valid_false(self):
        raw = _fyers_fut_raw(bid=float("inf"), ask=51205.0)
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)

    def test_k_nan_ask_bid_ask_valid_false(self):
        raw = _fyers_fut_raw(bid=51195.0, ask=float("nan"))
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertFalse(tick.bid_ask_valid)

    def test_l_option_valid_bid_ask_valid_true(self):
        raw = {
            "symbol": "NSE:NIFTY26SEP24050CE", "ltp": 142.5,
            "bid": 141.8, "ask": 143.2, "bqty": 50, "aqty": 75,
            "exch_feed_time": _ts(),
        }
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertTrue(tick.bid_ask_valid)
        self.assertEqual(tick.instrument_type, "OPT")
        self.assertAlmostEqual(tick.bid_price, 141.8)
        self.assertAlmostEqual(tick.ask_price, 143.2)


# ── fix for the 'or'-chain zero bug ─────────────────────────────────────────

class TestNormalizeFyersOrChainFix(unittest.TestCase):
    def test_a_explicit_zero_bid_recorded_not_skipped_to_bid_price(self):
        # bid=0.0 and bid_price=99.0 — bid=0 must be recorded, not skipped
        raw = {
            "symbol": "NSE:BANKNIFTY24SEPFUT", "ltp": 51200.0,
            "bid": 0.0, "bid_price": 99.0, "ask": 51205.0,
            "exch_feed_time": _ts(),
        }
        tick = TickNormalizer.normalize_fyers(raw)
        # bid key is present (=0.0), so bid_price should NOT be used
        self.assertEqual(tick.bid_price, 0.0)
        # bid=0 means bid_ask_valid must be False (zero is not a valid price)
        self.assertFalse(tick.bid_ask_valid)

    def test_b_bid_key_absent_falls_back_to_bid_price(self):
        raw = {
            "symbol": "NSE:BANKNIFTY24SEPFUT", "ltp": 51200.0,
            "bid_price": 51195.0, "ask_price": 51205.0,
            "exch_feed_time": _ts(),
        }
        tick = TickNormalizer.normalize_fyers(raw)
        self.assertAlmostEqual(tick.bid_price, 51195.0)
        self.assertAlmostEqual(tick.ask_price, 51205.0)
        self.assertTrue(tick.bid_ask_valid)

    def test_c_bqty_zero_key_present_recorded_not_skipped(self):
        raw = {
            "symbol": "NSE:BANKNIFTY24SEPFUT", "ltp": 51200.0,
            "bid": 51195.0, "ask": 51205.0,
            "bqty": 0, "bid_qty": 15,
            "exch_feed_time": _ts(),
        }
        tick = TickNormalizer.normalize_fyers(raw)
        # "bqty" key present (=0), so bid_qty should not be used
        self.assertEqual(tick.bid_qty, 0.0)


# ── FyersAdapter.handle_raw_tick integration ─────────────────────────────────

class TestHandleRawTickBidAskValid(unittest.TestCase):
    def _make_adapter(self):
        from adapters.fyers_adapter import FyersAdapter
        return FyersAdapter()

    def test_a_index_tick_bid_ask_valid_false(self):
        adapter = self._make_adapter()
        raw = {
            "symbol": "NSE:NIFTY50-INDEX",
            "ltp": 24500.0,
            "bid": 0.0,
            "ask": 0.0,
            "exch_feed_time": _ts(),
        }
        tick = adapter.handle_raw_tick(raw)
        self.assertIsNotNone(tick)
        # handle_raw_tick translates "NSE:NIFTY50-INDEX" → "NIFTY"
        self.assertEqual(tick.symbol, "NIFTY")
        self.assertFalse(tick.bid_ask_valid)
        self.assertEqual(tick.instrument_type, "IDX_FUT")

    def test_b_futures_tick_bid_ask_valid_true(self):
        adapter = self._make_adapter()
        raw = _fyers_fut_raw()
        tick = adapter.handle_raw_tick(raw)
        self.assertIsNotNone(tick)
        self.assertTrue(tick.bid_ask_valid)

    def test_c_callback_receives_tick_with_bid_ask_valid(self):
        adapter = self._make_adapter()
        received = []
        adapter.set_tick_callback(received.append)
        adapter.handle_raw_tick(_fyers_fut_raw())
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0].bid_ask_valid)


if __name__ == "__main__":
    unittest.main()
