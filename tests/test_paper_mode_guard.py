"""
P0-09 — Live-Data-Only Paper Mode Guard tests

Coverage:
- Tick.source defaults to "live"
- Tick.source can be set explicitly (historical / replay)
- PaperBrokerAdapter rejects fills when tick.source != "live"
- PaperBrokerAdapter idles (returns None) when live feed has no tick
- A limit order is also rejected if the live source guard fails
- Backward compat: no tick_provider → ltp_provider path unchanged
- Constraints C1 and C3 enforced at the adapter boundary
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from adapters.paper_broker import PaperBrokerAdapter
from models.order import Order, OrderSide, OrderType
from models.tick import Tick


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_tick(symbol="NIFTY", ltp=100.0, source="live") -> Tick:
    return Tick(
        symbol=symbol,
        exchange="NSE",
        instrument_type="INDEX",
        timestamp=datetime.now(timezone.utc),
        ltp=ltp,
        source=source,
    )


def _mk_order(symbol="NIFTY", side="BUY", qty=10, price=None) -> Order:
    return Order(
        client_order_id="test-c1",
        symbol=symbol,
        exchange="NSE",
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        order_type=OrderType.LIMIT if price else OrderType.MARKET,
        quantity=float(qty),
        price=float(price or 0.0),
        broker="paper",
    )


def _make_adapter_with_tick(tick):
    """Return a PaperBrokerAdapter whose tick_provider returns the given tick."""
    tick_store = {"NIFTY": tick} if tick is not None else {}
    return PaperBrokerAdapter(
        ltp_provider=lambda s: tick_store.get(s.upper(), {ltp.ltp if tick else None}
                                              if tick else None),
        tick_provider=lambda s: tick_store.get(s.upper()),
    )


# ── Tick model tests ──────────────────────────────────────────────────────────

class TestTickSourceField(unittest.TestCase):

    def test_default_source_is_live(self):
        t = _mk_tick()
        self.assertEqual(t.source, "live")

    def test_source_can_be_set_to_historical(self):
        t = _mk_tick(source="historical")
        self.assertEqual(t.source, "historical")

    def test_source_can_be_set_to_replay(self):
        t = _mk_tick(source="replay")
        self.assertEqual(t.source, "replay")

    def test_tick_to_dict_includes_source(self):
        t = _mk_tick(source="live")
        d = t.to_dict()
        self.assertEqual(d["source"], "live")

    def test_tick_construction_without_source_still_works(self):
        # All callers that don't pass source= must keep working (default="live")
        t = Tick(
            symbol="BANKNIFTY",
            exchange="NSE",
            instrument_type="INDEX",
            timestamp=datetime.now(timezone.utc),
            ltp=42000.0,
        )
        self.assertEqual(t.source, "live")


# ── Paper broker guard tests ──────────────────────────────────────────────────

class TestPaperBrokerLiveSourceGuard(unittest.TestCase):
    """tick_provider path: source validation is enforced."""

    def setUp(self):
        self.live_tick = _mk_tick(ltp=100.0, source="live")
        self.hist_tick = _mk_tick(ltp=100.0, source="historical")
        self.replay_tick = _mk_tick(ltp=100.0, source="replay")

    def _adapter(self, tick):
        """PaperBrokerAdapter with tick_provider returning the given tick."""
        store = {}
        if tick is not None:
            store["NIFTY"] = tick
        return PaperBrokerAdapter(
            ltp_provider=lambda s: (store.get(s.upper()) or MagicMock(ltp=None)).ltp,
            tick_provider=lambda s: store.get(s.upper()),
        )

    def test_live_tick_market_order_fills(self):
        pb = self._adapter(self.live_tick)
        upd = asyncio.run(pb.place_order(_mk_order()))
        self.assertEqual(upd.state.value, "FILLED")
        self.assertEqual(upd.avg_fill_price, 100.0)

    def test_historical_tick_raises_value_error(self):
        pb = self._adapter(self.hist_tick)
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(pb.place_order(_mk_order()))
        self.assertIn("historical", str(ctx.exception))
        self.assertIn("C1", str(ctx.exception))

    def test_replay_tick_raises_value_error(self):
        pb = self._adapter(self.replay_tick)
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(pb.place_order(_mk_order()))
        self.assertIn("replay", str(ctx.exception))

    def test_no_tick_returns_none_from_get_fill_price(self):
        pb = self._adapter(None)  # live feed has no tick
        price = pb.get_fill_price(_mk_order())
        self.assertIsNone(price)

    def test_no_tick_raises_on_place_order(self):
        """When get_fill_price returns None, place_order raises (Paper Mode idle)."""
        pb = self._adapter(None)
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(pb.place_order(_mk_order()))
        self.assertIn("live price", str(ctx.exception).lower())

    def test_limit_order_with_non_live_tick_rejected(self):
        """Source check runs before using order.price — limit orders are not exempt."""
        pb = self._adapter(self.hist_tick)
        with self.assertRaises(ValueError):
            asyncio.run(pb.place_order(_mk_order(price=99.0)))

    def test_limit_order_with_live_tick_uses_limit_price(self):
        """Limit orders fill at order.price when the source guard passes."""
        pb = self._adapter(self.live_tick)
        upd = asyncio.run(pb.place_order(_mk_order(price=98.5)))
        self.assertEqual(upd.avg_fill_price, 98.5)

    def test_source_check_error_message_names_symbol(self):
        pb = self._adapter(self.hist_tick)
        try:
            asyncio.run(pb.place_order(_mk_order(symbol="NIFTY")))
            self.fail("Expected ValueError")
        except ValueError as exc:
            self.assertIn("NIFTY", str(exc))


class TestPaperBrokerBackwardCompat(unittest.TestCase):
    """Without tick_provider, ltp_provider path is unchanged (existing tests still pass)."""

    def setUp(self):
        self.prices = {"NIFTY": 200.0}
        self.pb = PaperBrokerAdapter(
            ltp_provider=lambda s: self.prices.get(s.upper())
        )

    def test_market_order_fills_via_ltp_provider(self):
        upd = asyncio.run(self.pb.place_order(_mk_order()))
        self.assertEqual(upd.avg_fill_price, 200.0)

    def test_limit_order_fills_at_limit(self):
        upd = asyncio.run(self.pb.place_order(_mk_order(price=195.0)))
        self.assertEqual(upd.avg_fill_price, 195.0)

    def test_no_price_no_ltp_raises(self):
        pb = PaperBrokerAdapter(ltp_provider=lambda s: None)
        with self.assertRaises(ValueError):
            asyncio.run(pb.place_order(_mk_order()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
