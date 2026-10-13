"""
======================================================================
STRIKE DESK V4 — PHASE 6 PROMPT 3/4
TRADING & RISK REST API TEST SUITE (test_trading_risk_api.py)
Blueprint O8: Selective Auto-Execution across 5 asset classes
Blueprint O5: Institutional Risk, Halts & Acknowledgment Workflow
Review Risk 4: Execution Gate Logic & Order Leakage Prevention
Review Risk 7: Credential security — zero secret leakage in payloads
======================================================================
"""

import unittest

from api.routes.auth import require_operator
from datetime import datetime
import pytz
import sys
import os
import json
import asyncio
from urllib.parse import urlencode

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from fastapi.testclient import TestClient as StarletteTestClient
except (ImportError, RuntimeError):
    StarletteTestClient = None


class SimpleASGIClient:
    def __init__(self, app):
        self.app = app

    def _request(self, method: str, path: str, headers: dict = None, body: dict = None, params: dict = None):
        headers_list = [(b"content-type", b"application/json")]
        if headers:
            for k, v in headers.items():
                headers_list.append((k.lower().encode(), v.encode()))
        q_bytes = urlencode(params or {}).encode()
        body_bytes = json.dumps(body).encode() if body else b""

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": q_bytes,
            "headers": headers_list,
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
        resp_body = []
        sc = [200]

        async def receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                sc[0] = msg["status"]
            elif msg["type"] == "http.response.body":
                resp_body.append(msg.get("body", b""))

        asyncio.run(self.app(scope, receive, send))

        class Response:
            def __init__(self, status_code, content):
                self.status_code = status_code
                self.content = content

            def json(self):
                return json.loads(self.content.decode())

        return Response(sc[0], b"".join(resp_body))

    def get(self, path: str, params: dict = None):
        return self._request("GET", path, params=params)

    def post(self, path: str, json: dict = None, json_data: dict = None):
        payload = json if json is not None else json_data
        return self._request("POST", path, body=payload)


def get_test_client(app):
    if StarletteTestClient is not None:
        try:
            return StarletteTestClient(app)
        except Exception:
            pass
    return SimpleASGIClient(app)


from api.main import create_app
from api.dependencies import (
    get_risk_manager,
    get_execution_router,
    get_strategy_orchestrator,
)
from api.routes.trading import get_mode_controller, ModeController

IST = pytz.timezone("Asia/Kolkata")


class MockHaltManager:
    def __init__(self):
        self._is_halted = False
        self._current_reason = ""
        self._current_level = "HARD_HALT"
        self._token = None
        self._token_op = None
        self._audit = []

    @property
    def is_halted(self):
        return self._is_halted

    @property
    def current_reason(self):
        return self._current_reason

    def trigger_halt(self, reason: str, level: str = "HARD_HALT", triggered_by: str = "OPERATOR"):
        self._is_halted = True
        self._current_reason = reason
        self._current_level = level
        evt = {
            "event_id": f"HALT_{len(self._audit)+1:03d}",
            "is_halted": True,
            "level": level,
            "reason": reason,
            "triggered_by": triggered_by,
            "timestamp": datetime.now(IST).isoformat(),
            "acknowledged": False,
        }
        self._audit.append(evt)
        return evt

    def request_acknowledgment_token(self, operator_id: str):
        if not self._is_halted:
            raise ValueError("System is not halted.")
        self._token = f"ACK-{operator_id.upper()}-998877"
        self._token_op = operator_id
        return self._token

    def acknowledge_and_reset(self, operator_id: str, token: str, resolution_notes: str = ""):
        if not self._is_halted:
            return True
        if token != self._token or operator_id != self._token_op:
            raise PermissionError("Invalid acknowledgment token or operator mismatch.")
        self._is_halted = False
        self._current_reason = ""
        if self._audit:
            self._audit[-1]["acknowledged"] = True
            self._audit[-1]["acknowledged_by"] = operator_id
            self._audit[-1]["resolution_notes"] = resolution_notes
        return True

    def get_audit_log(self):
        return self._audit


class MockRiskManager:
    def __init__(self):
        self.halt_manager = MockHaltManager()
        self.realized_pnl = 500.0
        self.total_risk_pnl = 500.0
        self.daily_loss_ceiling = -2500.0
        self.trade_count = 1
        self.max_daily_trades = 5
        self.consecutive_losses = 0
        self.cooldown_active = False
        self.positions = {}
        self.orders = {}

    def get_risk_summary(self):
        return {
            "ist_date": str(datetime.now(IST).date()),
            "realized_pnl": self.realized_pnl,
            "total_risk_pnl": self.total_risk_pnl,
            "daily_loss_ceiling": self.daily_loss_ceiling,
            "loss_ceiling_breached": self.total_risk_pnl <= self.daily_loss_ceiling,
            "trade_count": self.trade_count,
            "max_daily_trades": self.max_daily_trades,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_active": self.cooldown_active,
            "cooldown_until": None,
            "emergency_halt_active": self.halt_manager.is_halted,
            "emergency_halt_reason": self.halt_manager.current_reason,
            "open_positions_count": sum(1 for p in self.positions.values() if p.get("is_open", True)),
        }

    def authorize_order(self, symbol: str, quantity: int, side: str):
        if self.halt_manager.is_halted:
            return False, f"Emergency halt active: {self.halt_manager.current_reason}"
        if self.total_risk_pnl <= self.daily_loss_ceiling:
            return False, "Daily loss ceiling breached"
        if self.trade_count >= self.max_daily_trades:
            return False, "Daily trade cap reached"
        if self.cooldown_active:
            return False, "Consecutive loss cooldown active"
        return True, "AUTH_TOKEN_OK"

    def record_fill(self, order_id: str, symbol: str, filled_qty: int, fill_price: float, is_complete: bool = True):
        self.positions[symbol] = {
            "position_id": f"POS_{symbol}",
            "symbol": symbol,
            "quantity": filled_qty,
            "entry_price": fill_price,
            "current_price": fill_price,
            "unrealized_pnl": 0.0,
            "is_open": True,
            "entry_time": datetime.now(IST),
        }


class MockExecutionRouter:
    def __init__(self):
        self.orders = {}

    def place_order(self, symbol: str, side: str, quantity: int, price: float, order_type: str, broker: str):
        oid = f"ORD_{len(self.orders)+1:04d}"
        ord_data = {
            "order_id": oid,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price or 100.0,
            "status": "FILLED",
            "broker": broker or "angelone",
            "placed_at": datetime.now(IST),
            "filled_at": datetime.now(IST),
        }
        self.orders[oid] = ord_data
        return ord_data


class MockStrategyOrchestrator:
    def get_all_signals(self):
        now = datetime.now(IST)
        return [
            {
                "signal_id": "SIG_OPT_01",
                "symbol": "BANKNIFTY26SEP51200CE",
                "instrument_type": "AUTO_OPTIONS",
                "direction": "BUY",
                "score": 92.0,
                "expected_value": 1.65,
                "timestamp": now,
            },
            {
                "signal_id": "SIG_FUT_01",
                "symbol": "NIFTY26SEPFUT",
                "instrument_type": "AUTO_FUTURES",
                "direction": "BUY",
                "score": 89.0,
                "expected_value": 1.40,
                "timestamp": now,
            },
            {
                "signal_id": "SIG_CASH_01",
                "symbol": "RELIANCE",
                "instrument_type": "AUTO_CASH_INTRADAY",
                "direction": "BUY",
                "score": 84.0,
                "expected_value": 1.20,
                "timestamp": now,
            },
            {
                "signal_id": "SIG_SWING_01",
                "symbol": "TCS",
                "instrument_type": "AUTO_CASH_SWING",
                "direction": "BUY",
                "score": 86.0,
                "expected_value": 1.30,
                "timestamp": now,
            },
            {
                "signal_id": "SIG_DELIV_01",
                "symbol": "INFY",
                "instrument_type": "AUTO_CASH_DELIVERY",
                "direction": "BUY",
                "score": 90.0,
                "expected_value": 1.50,
                "timestamp": now,
            },
        ]


class TestTradingAndRiskAPI(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.mock_risk = MockRiskManager()
        self.mock_router = MockExecutionRouter()
        self.mock_strat = MockStrategyOrchestrator()
        self.ctrl = ModeController()

        self.app.dependency_overrides[get_risk_manager] = lambda: self.mock_risk
        self.app.dependency_overrides[get_execution_router] = lambda: self.mock_router
        self.app.dependency_overrides[get_strategy_orchestrator] = lambda: self.mock_strat
        self.app.dependency_overrides[get_mode_controller] = lambda: self.ctrl
        # Auth is exercised separately in tests/test_rbac.py; these suites test the
        # endpoints' own behaviour, so they run as an authenticated operator.
        self.app.dependency_overrides[require_operator] = lambda: {
            "sub": "monuj", "role": "operator", "name": "Monuj Phukon"
        }

        self.client = get_test_client(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def test_01_mode_controller_get_and_set(self):
        """[01] Blueprint O8: Mode Controller query and single-category lock update"""
        r1 = self.client.get("/api/v1/trading/mode")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["active_mode"], "AUTO_OPTIONS")

        r2 = self.client.post("/api/v1/trading/mode", json={"mode": "AUTO_FUTURES"})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["active_mode"], "AUTO_FUTURES")

        # Reset back to AUTO_OPTIONS
        self.client.post("/api/v1/trading/mode", json={"mode": "AUTO_OPTIONS"})
        print("✓ [01] Mode Controller: Mode query and single-category lock transitions verified")

    def test_02_signals_five_classes_selective_auto(self):
        """[02] Blueprint O8: All 5 classes signal; only selected class executes; universal alerts"""
        resp = self.client.get("/api/v1/trading/signals")
        self.assertEqual(resp.status_code, 200)
        signals = resp.json()
        self.assertEqual(len(signals), 10)

        opt_sig = next(s for s in signals if s["instrument_type"] == "AUTO_OPTIONS")
        self.assertEqual(opt_sig["status"], "APPROVED_FOR_EXECUTION")

        other_sigs = [s for s in signals if s["instrument_type"] != "AUTO_OPTIONS"]
        self.assertTrue(all(s["status"] == "ALERT_ONLY" for s in other_sigs))
        print("✓ [02] Selective auto-execution: 5 classes signal, only active class executes, alerts universal")

    def test_03_order_execution_gate_category_mismatch(self):
        """[03] Review Risk 4: Execution gate rejects AUTO order on category mismatch"""
        order_payload = {
            "symbol": "NIFTY26SEPFUT",
            "side": "BUY",
            "quantity": 50,
            "instrument_type": "AUTO_FUTURES",
            "is_manual": False,
        }
        resp = self.client.post("/api/v1/trading/orders", json=order_payload)
        self.assertIn(resp.status_code, [200, 400, 403])
        err_msg = resp.json().get("message") or resp.json().get("detail", "")
        self.assertIn("Auto-execution is restricted", err_msg)
        print("✓ [03] Execution gate lock: AUTO category mismatch strictly rejected (Review Risk 4)")

    def test_04_order_placement_authorized(self):
        """[04] Authorized order placement generates fills and position updates"""
        order_payload = {
            "symbol": "BANKNIFTY26SEP51200CE",
            "side": "BUY",
            "quantity": 25,
            "price": 240.0,
            "instrument_type": "AUTO_OPTIONS",
            "broker": "angelone",
        }
        resp = self.client.post("/api/v1/trading/orders", json=order_payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["symbol"], "BANKNIFTY26SEP51200CE")
        self.assertEqual(data["status"], "FILLED")
        print("✓ [04] Order execution: Valid category authorized by RiskManager and filled")

    def test_05_orders_list_and_detail(self):
        """[05] Querying order book and single order lookup"""
        self.mock_router.place_order("TCS", "BUY", 10, 3400.0, "MARKET", "angelone")

        r_list = self.client.get("/api/v1/trading/orders")
        self.assertEqual(r_list.status_code, 200)
        orders = r_list.json()
        self.assertTrue(len(orders) >= 1)

        oid = orders[0]["order_id"]
        r_detail = self.client.get(f"/api/v1/trading/orders/{oid}")
        self.assertEqual(r_detail.status_code, 200)
        self.assertEqual(r_detail.json()["order_id"], oid)
        print("✓ [05] Order book telemetry: Orders list and detail lookup verified")

    def test_06_positions_and_square_off(self):
        """[06] Query active open positions and trigger manual square-off"""
        self.mock_risk.record_fill("O_POS", "INFY", 20, 1450.0, True)

        r_pos = self.client.get("/api/v1/trading/positions")
        self.assertEqual(r_pos.status_code, 200)
        positions = r_pos.json()
        self.assertTrue(len(positions) >= 1)

        sym = positions[0]["symbol"]
        r_sq = self.client.post(f"/api/v1/trading/positions/{sym}/square-off")
        self.assertEqual(r_sq.status_code, 200)
        self.assertEqual(r_sq.json()["status"], "SUCCESS")
        print("✓ [06] Position lifecycle: Position tracking and manual square-off confirmed")

    def test_07_risk_state_endpoint(self):
        """[07] Blueprint O5: Real-time institutional risk state metrics"""
        resp = self.client.get("/api/v1/risk/state")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["daily_loss_ceiling"], -2500.0)
        self.assertEqual(data["max_daily_trades"], 5)
        self.assertFalse(data["emergency_halt_active"])
        print("✓ [07] Risk State: Institutional limits, P&L, and breakers telemetry verified")

    def test_08_emergency_halt_kill_switch_trigger(self):
        """[08] Blueprint O5: Engage emergency halt and verify order gating"""
        r_halt = self.client.post(
            "/api/v1/risk/halt",
            json={"reason": "Exchange RMS timeout", "level": "HARD_HALT", "operator_id": "MONUJ"},
        )
        self.assertEqual(r_halt.status_code, 200)
        self.assertTrue(r_halt.json()["is_halted"])

        order_payload = {
            "symbol": "BANKNIFTY26SEP51200CE",
            "side": "BUY",
            "quantity": 25,
            "instrument_type": "AUTO_OPTIONS",
        }
        r_order = self.client.post("/api/v1/trading/orders", json=order_payload)
        self.assertEqual(r_order.status_code, 400)
        err_msg = r_order.json().get("message") or r_order.json().get("detail", "")
        self.assertIn("Emergency halt active", err_msg)
        print("✓ [08] Emergency Halt: Kill-switch blocks orders and enforces freeze (Blueprint O5)")

    def test_09_manual_acknowledgment_workflow(self):
        """[09] Cryptographic token authorization and manual kill-switch reset"""
        self.mock_risk.halt_manager.trigger_halt("Test incident", "HARD_HALT", "MONUJ")

        r_tok = self.client.post("/api/v1/risk/acknowledge/request-token", json={"operator_id": "MONUJ"})
        self.assertEqual(r_tok.status_code, 200)
        token = r_tok.json()["token"]
        self.assertTrue(token.startswith("ACK-MONUJ-"))

        r_bad = self.client.post(
            "/api/v1/risk/acknowledge/reset",
            json={"operator_id": "MONUJ", "token": "BAD_TOKEN"},
        )
        self.assertEqual(r_bad.status_code, 403)

        r_ok = self.client.post(
            "/api/v1/risk/acknowledge/reset",
            json={"operator_id": "MONUJ", "token": token, "resolution_notes": "Exchange line restored"},
        )
        self.assertEqual(r_ok.status_code, 200)
        self.assertTrue(r_ok.json()["success"])

        r_state = self.client.get("/api/v1/risk/state")
        self.assertFalse(r_state.json()["emergency_halt_active"])

        r_audit = self.client.get("/api/v1/risk/audit-log")
        self.assertEqual(r_audit.status_code, 200)
        self.assertTrue(len(r_audit.json()["events"]) >= 1)
        print("✓ [09] Manual Acknowledgment: Two-step token verification and audit log verified")

    def test_10_credential_security_trading_and_risk(self):
        """[10] Review Risk 7: Trading and Risk endpoints strictly free of secrets"""
        endpoints = [
            "/api/v1/trading/mode",
            "/api/v1/trading/signals",
            "/api/v1/trading/orders",
            "/api/v1/trading/positions",
            "/api/v1/risk/state",
            "/api/v1/risk/audit-log",
        ]
        sensitive_keywords = ["api_key", "secret", "password", "totp", "jwt", "pin", "auth_token"]
        for ep in endpoints:
            resp = self.client.get(ep)
            raw_text = resp.content.decode().lower()
            for kw in sensitive_keywords:
                self.assertNotIn(f'"{kw}"', raw_text)
        print("✓ [10] Credential security: Trading & Risk responses completely free of secrets (Review Risk 7)")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 6 PROMPT 3/4 TEST SUITE (test_trading_risk_api.py)")
    print("=" * 70)
    unittest.main(verbosity=0)
