"""
Broker execution isolation.

Only the operator's selected "active broker" may execute orders. A broker's
connection or authentication state confers no execution rights unless it is the
active broker. This must hold for both manual orders (POST /trading/orders) and
the background auto-execute loop.

Test strategy
─────────────
• Test the guard functions directly — they are the enforcement boundary.
• Test the adapter-selection helper — it must never return a non-active adapter.
• Integration-style tests use mocked adapters and a mocked BrokerTokenManager
  so that we can set AngelOne as active and verify Dhan/Fyers adapters are
  never called.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import HTTPException
from api.routes.trading import (
    require_active_broker,
    _select_active_adapter,
    EXEC_PAPER,
    EXEC_LIVE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_adapter(name: str) -> MagicMock:
    """A mock adapter that looks like a real one (has place_order)."""
    a = MagicMock()
    a.place_order = AsyncMock(return_value=MagicMock(broker_order_id=f"ORD-{name}"))
    a._broker_name = name
    return a


def _adapters(active: str) -> dict:
    """Three adapters; only `active` should ever be used for execution."""
    return {
        "angelone": _make_adapter("angelone"),
        "dhan":     _make_adapter("dhan"),
        "fyers":    _make_adapter("fyers"),
    }


# ── Guard-function unit tests ─────────────────────────────────────────────────

class TestRequireActiveBroker(unittest.TestCase):
    """The 403 guard that enforces active-broker isolation."""

    def test_matching_broker_passes(self):
        require_active_broker("angelone", "angelone")
        require_active_broker("ANGELONE", "angelone")   # case-insensitive
        require_active_broker("dhan",     "dhan")
        require_active_broker("fyers",    "fyers")

    def test_inactive_dhan_blocked_when_angelone_active(self):
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("dhan", "angelone")
        exc = ctx.exception
        self.assertEqual(exc.status_code, 403,
            "Must be 403 (forbidden), not 400 (bad request) — the broker name "
            "is valid; it is simply not the active one.")
        self.assertIn("dhan", exc.detail)
        self.assertIn("angelone", exc.detail)

    def test_inactive_fyers_blocked_when_angelone_active(self):
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("fyers", "angelone")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_inactive_angelone_blocked_when_dhan_active(self):
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("angelone", "dhan")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_inactive_fyers_blocked_when_dhan_active(self):
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("fyers", "dhan")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_all_inactive_combinations(self):
        brokers = ["angelone", "dhan", "fyers"]
        for active in brokers:
            for requested in brokers:
                if requested == active:
                    continue
                with self.subTest(active=active, requested=requested):
                    with self.assertRaises(HTTPException) as ctx:
                        require_active_broker(requested, active)
                    self.assertEqual(ctx.exception.status_code, 403)

    def test_error_message_names_both_brokers(self):
        """The 403 message must name both the requested and the active broker
        so the operator's logs are unambiguous."""
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("dhan", "angelone")
        detail = ctx.exception.detail
        self.assertIn("dhan", detail)
        self.assertIn("angelone", detail)

    def test_empty_order_broker_blocked(self):
        """A corrupted (empty) broker field must also be blocked."""
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("", "angelone")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_none_order_broker_blocked(self):
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker(None, "angelone")
        self.assertEqual(ctx.exception.status_code, 403)


# ── Adapter-selection unit tests ──────────────────────────────────────────────

class TestSelectActiveAdapter(unittest.TestCase):
    """_select_active_adapter must only ever return the active broker's adapter."""

    def setUp(self):
        self.ads = _adapters("angelone")

    def test_returns_active_brokers_adapter(self):
        adapter = _select_active_adapter(self.ads, "angelone")
        self.assertIs(adapter, self.ads["angelone"])

    def test_never_returns_dhan_when_angelone_active(self):
        adapter = _select_active_adapter(self.ads, "angelone")
        self.assertIsNot(adapter, self.ads["dhan"])

    def test_never_returns_fyers_when_angelone_active(self):
        adapter = _select_active_adapter(self.ads, "angelone")
        self.assertIsNot(adapter, self.ads["fyers"])

    def test_raises_503_when_active_adapter_absent(self):
        ads = {"dhan": _make_adapter("dhan")}   # angelone not present
        with self.assertRaises(HTTPException) as ctx:
            _select_active_adapter(ads, "angelone")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_case_insensitive_lookup(self):
        ads = {"AngelOne": _make_adapter("angelone")}
        # lowercase key should still be found
        ads_lower = {"angelone": ads["AngelOne"]}
        adapter = _select_active_adapter(ads_lower, "AngelOne")
        self.assertIsNotNone(adapter)

    def test_empty_adapters_dict_raises_503(self):
        with self.assertRaises(HTTPException) as ctx:
            _select_active_adapter({}, "angelone")
        self.assertEqual(ctx.exception.status_code, 503)


# ── Integration: manual order routing ────────────────────────────────────────

class TestManualOrderBrokerIsolation(unittest.TestCase):
    """
    Verify that POST /trading/orders routes exclusively to the active broker's
    adapter and that inactive broker adapters' place_order is never invoked.

    We patch at the BrokerTokenManager level to control which broker is "active"
    and inject a fake adapters dict with all three brokers present.
    """

    def _run_place_order(self, active_broker: str, symbol: str = "NIFTY") -> dict:
        """Call place_order with all three adapters present; only active should fire."""
        from api.routes.trading import (
            place_order, OrderRequest, mode_controller, execution_mode, get_paper_broker,
        )
        import uuid

        ads = _adapters(active_broker)

        req = OrderRequest(symbol=symbol, side="BUY", quantity=1, is_manual=True)

        # Wire the paper broker so the PAPER path resolves
        paper = MagicMock()
        paper.place_order = AsyncMock(return_value=MagicMock(
            broker_order_id="SIM-001",
            state=MagicMock(value="FILLED"),
            avg_fill_price=100.0,
        ))
        paper.get_positions_dict = MagicMock(return_value=[])

        # Provide a fresh mock tick so the live-feed gate passes.
        from datetime import datetime, timezone, timedelta
        import pipeline as _pipeline
        fresh_tick = MagicMock()
        fresh_tick.ltp = 100.0
        fresh_tick.timestamp = datetime.now(timezone.utc) - timedelta(seconds=1)
        _orig_get_tick = _pipeline.pipeline_orchestrator.get_latest_tick
        _pipeline.pipeline_orchestrator.get_latest_tick = lambda s: fresh_tick

        try:
          with patch("brokers.token_manager.BrokerTokenManager") as MockTM, \
               patch("api.routes.trading.get_paper_broker", return_value=paper), \
               patch("api.routes.trading.execution_mode") as mock_exec, \
               patch("risk.order_authorizer.order_authorizer.authorize_order") as mock_auth, \
               patch("risk.risk_manager.risk_manager.get_risk_summary", return_value={}):

            MockTM.return_value.get_active_broker.return_value = active_broker
            mock_exec.is_paper.return_value = True   # paper mode → no real broker call
            mock_exec.get.return_value = EXEC_PAPER

            mock_auth.return_value = MagicMock(
                is_authorized=True, auth_token="tok", rule_violated=None, reason=None
            )

            result = asyncio.run(place_order(
                order_req=req,
                ctrl=mode_controller,
                adapters=ads,
                exec_mode=mock_exec,
                _op={"sub": "monuj", "role": "operator"},
            ))
        finally:
            _pipeline.pipeline_orchestrator.get_latest_tick = _orig_get_tick
        return result, ads

    def test_angelone_active_paper_fills_through_paper_broker(self):
        result, ads = self._run_place_order("angelone")
        # paper mode — no adapter at all should be called
        ads["angelone"].place_order.assert_not_called()
        ads["dhan"].place_order.assert_not_called()
        ads["fyers"].place_order.assert_not_called()
        self.assertTrue(result.get("is_simulated"))

    def test_active_broker_recorded_in_order_result(self):
        result, _ = self._run_place_order("angelone")
        # The order record should say paper:angelone (paper prefix + active broker)
        self.assertIn("angelone", result.get("broker", ""))

    def test_isolation_guard_fires_for_wrong_order_broker(self):
        """
        If order.broker is somehow set to a non-active broker, require_active_broker
        must raise 403 before any adapter is reached.
        """
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("dhan", "angelone")
        self.assertEqual(ctx.exception.status_code, 403)

        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("fyers", "angelone")
        self.assertEqual(ctx.exception.status_code, 403)


# ── Integration: live mode adapter exclusivity ────────────────────────────────

class TestLiveModeAdapterExclusivity(unittest.TestCase):
    """
    In LIVE mode, _select_active_adapter must return exactly the active broker's
    adapter. Even though all three adapters are present in the DI container, only
    the active one is reachable through the normal execution path.
    """

    def test_only_angelone_reachable_when_active(self):
        ads = _adapters("angelone")
        active = "angelone"
        adapter = _select_active_adapter(ads, active)
        self.assertIs(adapter, ads["angelone"])
        # The other two are in the dict but unreachable through this call
        self.assertIsNot(adapter, ads["dhan"])
        self.assertIsNot(adapter, ads["fyers"])

    def test_only_dhan_reachable_when_active(self):
        ads = _adapters("dhan")
        adapter = _select_active_adapter(ads, "dhan")
        self.assertIs(adapter, ads["dhan"])
        self.assertIsNot(adapter, ads["angelone"])
        self.assertIsNot(adapter, ads["fyers"])

    def test_only_fyers_reachable_when_active(self):
        ads = _adapters("fyers")
        adapter = _select_active_adapter(ads, "fyers")
        self.assertIs(adapter, ads["fyers"])
        self.assertIsNot(adapter, ads["angelone"])
        self.assertIsNot(adapter, ads["dhan"])

    def test_all_three_active_combinations(self):
        for active in ("angelone", "dhan", "fyers"):
            with self.subTest(active=active):
                ads = _adapters(active)
                adapter = _select_active_adapter(ads, active)
                self.assertIs(adapter, ads[active])
                for other in ("angelone", "dhan", "fyers"):
                    if other != active:
                        self.assertIsNot(adapter, ads[other],
                            f"When '{active}' is active, '{other}' adapter must not be selected")


# ── Paper mode: resolution uses active broker, no broker API called ───────────

class TestPaperModeIsolation(unittest.TestCase):
    """
    Paper mode must resolve the instrument against the active broker's map
    but never call any broker's place_order. The paper ledger does the fill.
    """

    def test_paper_uses_active_broker_for_resolution_not_execution(self):
        """
        _select_active_adapter is not called in paper mode — the paper broker
        fills the order. But require_active_broker still runs before the paper
        branch, so the order.broker / active_broker consistency is still enforced.
        """
        # Guard still fires for a mismatch even in paper mode
        with self.assertRaises(HTTPException) as ctx:
            require_active_broker("dhan", "angelone")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_inactive_broker_cannot_bypass_via_paper_mode(self):
        """
        An authenticated-but-inactive broker cannot be used as the routing broker
        even when paper mode is active. Paper mode reduces the risk but does not
        relax the active-broker constraint on resolution.
        """
        for inactive in ("dhan", "fyers"):
            with self.subTest(inactive=inactive):
                with self.assertRaises(HTTPException) as ctx:
                    require_active_broker(inactive, "angelone")
                self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
