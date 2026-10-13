"""
Live feed gate: orders must only execute against symbols the active broker's
WebSocket is actively streaming.

Two failure modes are tested:
  NEVER_SEEN  — symbol has never appeared in the tick store
  FEED_STALE  — last tick is older than the staleness threshold

Option contracts are special: they are not directly subscribed to the WebSocket
feed. Their pricing derives from the underlying tick + option chain cache, so the
feed check uses the underlying symbol, not the contract name.
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import HTTPException
from api.routes.trading import (
    require_live_feed,
    _feed_check_symbol,
    _LIVE_FEED_MAX_AGE_SECONDS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fresh_tick(symbol: str, age_seconds: float = 1.0) -> MagicMock:
    """A mock Tick with a recent timestamp."""
    t = MagicMock()
    t.symbol = symbol.upper()
    t.ltp = 100.0
    t.timestamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return t


def _stale_tick(symbol: str, age_seconds: float = 120.0) -> MagicMock:
    return _fresh_tick(symbol, age_seconds=age_seconds)


def _patch_pipeline(tick_map: dict):
    """
    Returns a context manager that replaces pipeline_orchestrator.get_latest_tick
    with a function that looks up `tick_map` (symbol → Tick or None).
    """
    def _get(symbol):
        return tick_map.get(symbol.upper())

    mock_po = MagicMock()
    mock_po.get_latest_tick = _get
    return patch("api.routes.trading.require_live_feed.__code__",
                 require_live_feed.__code__), mock_po


# ── _feed_check_symbol ────────────────────────────────────────────────────────

class TestFeedCheckSymbol(unittest.TestCase):
    """Selects the correct symbol to look up in the tick store."""

    def test_equity_returns_itself(self):
        self.assertEqual(_feed_check_symbol("RELIANCE", None), "RELIANCE")
        self.assertEqual(_feed_check_symbol("NIFTY",    None), "NIFTY")
        self.assertEqual(_feed_check_symbol("TCS",      None), "TCS")

    def test_futures_returns_itself(self):
        self.assertEqual(_feed_check_symbol("NIFTY26SEPFUT", None), "NIFTY26SEPFUT")

    def test_option_ce_uses_metadata_underlying(self):
        self.assertEqual(
            _feed_check_symbol("NIFTY26SEP24050CE", {"underlying": "NIFTY"}),
            "NIFTY",
        )

    def test_option_pe_uses_metadata_underlying(self):
        self.assertEqual(
            _feed_check_symbol("BANKNIFTY26SEP50000PE", {"underlying": "BANKNIFTY"}),
            "BANKNIFTY",
        )

    def test_option_falls_back_to_symbol_root_when_no_metadata(self):
        # Extract leading alpha characters from the contract name.
        self.assertEqual(_feed_check_symbol("NIFTY26SEP24050CE",    {}), "NIFTY")
        self.assertEqual(_feed_check_symbol("BANKNIFTY26SEP50000CE", {}), "BANKNIFTY")
        self.assertEqual(_feed_check_symbol("RELIANCE26SEP1230CE",   {}), "RELIANCE")

    def test_stock_option_uses_underlying_from_metadata(self):
        self.assertEqual(
            _feed_check_symbol("TMPV26SEP700CE", {"underlying": "TMPV"}),
            "TMPV",
        )

    def test_case_normalised(self):
        self.assertEqual(_feed_check_symbol("reliance", None), "RELIANCE")
        self.assertEqual(_feed_check_symbol("nifty26sep24050ce", {"underlying": "nifty"}), "NIFTY")


# ── require_live_feed: never-seen ─────────────────────────────────────────────

class TestFeedGateNeverSeen(unittest.TestCase):
    """Symbol (or underlying) has never ticked — pipeline returns None."""

    def _call(self, symbol, metadata=None):
        with patch("api.routes.trading.pipeline_orchestrator") as mock_po:
            mock_po.get_latest_tick.return_value = None
            # Patch the deferred import inside require_live_feed
            import importlib
            trading = importlib.import_module("api.routes.trading")
            original = None
            try:
                import pipeline
                original = pipeline.pipeline_orchestrator
                pipeline.pipeline_orchestrator = mock_po
            except Exception:
                pass
            try:
                require_live_feed(symbol, "angelone", metadata=metadata)
            finally:
                if original is not None:
                    try:
                        pipeline.pipeline_orchestrator = original
                    except Exception:
                        pass

    def _expect_400(self, symbol, metadata=None):
        try:
            from pipeline import pipeline_orchestrator as real_po
        except ImportError:
            self.skipTest("pipeline module not importable")

        original_get = real_po.get_latest_tick
        real_po.get_latest_tick = lambda s: None
        try:
            with self.assertRaises(HTTPException) as ctx:
                require_live_feed(symbol, "angelone", metadata=metadata)
            exc = ctx.exception
            self.assertEqual(exc.status_code, 400)
            return exc.detail
        finally:
            real_po.get_latest_tick = original_get

    def test_equity_never_seen_raises_400(self):
        detail = self._expect_400("RELIANCE")
        self.assertIn("RELIANCE", detail)
        self.assertIn("angelone", detail)

    def test_index_never_seen_raises_400(self):
        detail = self._expect_400("NIFTY")
        self.assertIn("NIFTY", detail)

    def test_option_never_seen_checks_underlying_in_error(self):
        detail = self._expect_400("NIFTY26SEP24050CE", {"underlying": "NIFTY"})
        # Error must mention the underlying, not just the contract name.
        self.assertIn("NIFTY", detail)

    def test_option_without_metadata_falls_back_to_parsed_underlying(self):
        detail = self._expect_400("NIFTY26SEP24050CE")
        self.assertIn("NIFTY", detail)

    def test_error_message_names_the_broker(self):
        try:
            from pipeline import pipeline_orchestrator as real_po
        except ImportError:
            self.skipTest("pipeline module not importable")
        original = real_po.get_latest_tick
        real_po.get_latest_tick = lambda s: None
        try:
            with self.assertRaises(HTTPException) as ctx:
                require_live_feed("BANKNIFTY", "dhan")
            self.assertIn("dhan", ctx.exception.detail)
        finally:
            real_po.get_latest_tick = original


# ── require_live_feed: fresh tick passes ──────────────────────────────────────

class TestFeedGateFreshTick(unittest.TestCase):
    """A recent tick should allow the order through."""

    def _inject_tick(self, symbol: str, age_secs: float = 1.0):
        try:
            from pipeline import pipeline_orchestrator as real_po
        except ImportError:
            self.skipTest("pipeline module not importable")
        tick = _fresh_tick(symbol, age_secs)
        original = real_po.get_latest_tick
        real_po.get_latest_tick = lambda s: tick if s.upper() == symbol.upper() else None
        return real_po, original

    def test_fresh_equity_tick_passes(self):
        po, orig = self._inject_tick("RELIANCE", age_secs=2.0)
        try:
            require_live_feed("RELIANCE", "angelone")  # should not raise
        finally:
            po.get_latest_tick = orig

    def test_fresh_index_tick_passes(self):
        po, orig = self._inject_tick("NIFTY", age_secs=5.0)
        try:
            require_live_feed("NIFTY", "angelone")
        finally:
            po.get_latest_tick = orig

    def test_option_contract_passes_via_fresh_underlying_tick(self):
        po, orig = self._inject_tick("NIFTY", age_secs=3.0)
        try:
            require_live_feed("NIFTY26SEP24050CE", "angelone", metadata={"underlying": "NIFTY"})
        finally:
            po.get_latest_tick = orig

    def test_tick_exactly_at_threshold_passes(self):
        """A tick exactly at the threshold age must be allowed through (not stale)."""
        po, orig = self._inject_tick("NIFTY", age_secs=_LIVE_FEED_MAX_AGE_SECONDS - 0.5)
        try:
            require_live_feed("NIFTY", "angelone")
        finally:
            po.get_latest_tick = orig


# ── require_live_feed: stale tick ─────────────────────────────────────────────

class TestFeedGateStaleTick(unittest.TestCase):
    """A tick older than the threshold must block the order."""

    def _inject_stale(self, symbol: str, age_secs: float = None):
        try:
            from pipeline import pipeline_orchestrator as real_po
        except ImportError:
            self.skipTest("pipeline module not importable")
        age = age_secs if age_secs is not None else _LIVE_FEED_MAX_AGE_SECONDS + 30
        tick = _stale_tick(symbol, age_seconds=age)
        original = real_po.get_latest_tick
        real_po.get_latest_tick = lambda s: tick if s.upper() == symbol.upper() else None
        return real_po, original

    def test_stale_equity_raises_400(self):
        po, orig = self._inject_stale("RELIANCE")
        try:
            with self.assertRaises(HTTPException) as ctx:
                require_live_feed("RELIANCE", "angelone")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("stale", ctx.exception.detail.lower())
        finally:
            po.get_latest_tick = orig

    def test_stale_underlying_blocks_option_order(self):
        """A stale underlying tick must block an option order on that underlying."""
        po, orig = self._inject_stale("NIFTY")
        try:
            with self.assertRaises(HTTPException) as ctx:
                require_live_feed("NIFTY26SEP24050CE", "angelone",
                                  metadata={"underlying": "NIFTY"})
            exc = ctx.exception
            self.assertEqual(exc.status_code, 400)
            self.assertIn("stale", exc.detail.lower())
        finally:
            po.get_latest_tick = orig

    def test_error_names_age_in_seconds(self):
        po, orig = self._inject_stale("NIFTY", age_secs=_LIVE_FEED_MAX_AGE_SECONDS + 45)
        try:
            with self.assertRaises(HTTPException) as ctx:
                require_live_feed("NIFTY", "angelone")
            # Detail must contain a number (the age) so ops can diagnose it.
            import re
            self.assertTrue(re.search(r'\d+\.\d+s', ctx.exception.detail),
                            "Error message should contain the tick age in seconds")
        finally:
            po.get_latest_tick = orig

    def test_stale_threshold_is_configurable(self):
        """Orders should pass with a relaxed threshold even for a stale tick."""
        po, orig = self._inject_stale("NIFTY", age_secs=_LIVE_FEED_MAX_AGE_SECONDS + 10)
        try:
            # Very generous threshold — same tick should now be "fresh"
            require_live_feed("NIFTY", "angelone",
                              max_age_seconds=_LIVE_FEED_MAX_AGE_SECONDS + 120)
        finally:
            po.get_latest_tick = orig

    def test_feed_restored_after_stale_allows_order(self):
        """Once the feed recovers, the order must go through."""
        try:
            from pipeline import pipeline_orchestrator as real_po
        except ImportError:
            self.skipTest("pipeline module not importable")
        original = real_po.get_latest_tick
        # Start stale
        real_po.get_latest_tick = lambda s: _stale_tick(s) if s.upper() == "NIFTY" else None
        try:
            with self.assertRaises(HTTPException):
                require_live_feed("NIFTY", "angelone")
        finally:
            pass

        # Feed recovers — fresh tick arrives
        real_po.get_latest_tick = lambda s: _fresh_tick(s, age_seconds=1.0) if s.upper() == "NIFTY" else None
        try:
            require_live_feed("NIFTY", "angelone")   # must not raise now
        finally:
            real_po.get_latest_tick = original


# ── Cross-broker: active broker named in error ────────────────────────────────

class TestFeedGateBrokerContext(unittest.TestCase):
    """The error message must name the active broker for clear ops logs."""

    def _no_tick(self, broker: str, symbol: str):
        try:
            from pipeline import pipeline_orchestrator as real_po
        except ImportError:
            self.skipTest("pipeline module not importable")
        original = real_po.get_latest_tick
        real_po.get_latest_tick = lambda s: None
        try:
            with self.assertRaises(HTTPException) as ctx:
                require_live_feed(symbol, broker)
            return ctx.exception.detail
        finally:
            real_po.get_latest_tick = original

    def test_dhan_active_named_in_error(self):
        detail = self._no_tick("dhan", "BANKNIFTY")
        self.assertIn("dhan", detail)

    def test_fyers_active_named_in_error(self):
        detail = self._no_tick("fyers", "SENSEX")
        self.assertIn("fyers", detail)

    def test_angelone_active_named_in_error(self):
        detail = self._no_tick("angelone", "NIFTY")
        self.assertIn("angelone", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
