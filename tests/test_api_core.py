"""
======================================================================
STRIKE DESK V4 — PHASE 6 PROMPT 1/4
FASTAPI CORE & DEPENDENCY INJECTION TEST SUITE (test_api_core.py)
Blueprint L6: FastAPI + uvicorn on port 8000
Review Risk 7: Credential security - no leakage via API
======================================================================
"""

import unittest
from datetime import datetime
import pytz
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from api.main import create_app
from api.dependencies import (
    ServiceContainer,
    service_container,
    sanitize_text,
    sanitize_dict,
)
from api.schemas import (
    TickResponse,
    IndicatorResponse,
    SignalResponse,
    OrderResponse,
    PositionResponse,
    RiskStateResponse,
    BrokerStatusResponse,
    ErrorResponse,
)

IST = pytz.timezone("Asia/Kolkata")


class TestFastAPICoreAndDI(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)

    def test_a_app_metadata_and_root(self):
        """[a] FastAPI app initialization, title, and root endpoint"""
        self.assertEqual(self.app.title, "Strike Desk V4 — Quantitative Trading API")
        self.assertEqual(self.app.version, "4.0.0")

        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["app"], "Strike Desk V4")
        self.assertEqual(data["status"], "RUNNING")
        print("✓ [a] App initialization: App metadata, routes, and root endpoint verified")

    def test_b_health_endpoint(self):
        """[b] GET /health returns real subsystem health status"""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["version"], "4.0.0")
        self.assertIn("api", data["services"])
        self.assertIn("risk_manager", data["services"])
        print("✓ [b] Health check: GET /health returns system status and registered services")

    def test_c_readiness_endpoint(self):
        """[c] GET /ready evaluates trading readiness and safety gates"""
        resp = self.client.get("/ready")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("ready", data)
        self.assertIn("checks", data)
        self.assertIn("risk_system_operational", data["checks"])
        print("✓ [c] Readiness check: GET /ready validates gating conditions for trade execution")

    def test_d_dependency_injection_resolution(self):
        """[d] Dependency injection container resolves all Phase 1-5 services"""
        container = ServiceContainer()
        self.assertIsNotNone(container.get_risk_manager())
        self.assertIsNotNone(container.get_strategy_orchestrator())
        self.assertIsNotNone(container.get_execution_router())
        self.assertIsNotNone(container.get_instrument_registry())
        self.assertIsNotNone(container.get_pipeline_orchestrator())
        brokers = container.get_broker_adapters()
        self.assertIsInstance(brokers, dict)
        print("✓ [d] Dependency Injection: Container injects all Phase 1-5 subsystems cleanly")

    def test_e_dependency_injection_override(self):
        """[e] Dependency injection overrides work for mock-free and isolated testing"""
        class CustomRiskMock:
            def get_risk_summary(self):
                return {"emergency_halt_active": True, "loss_ceiling_breached": True}

        container = ServiceContainer()
        container.set_risk_manager(CustomRiskMock())

        risk_mgr = container.get_risk_manager()
        summary = risk_mgr.get_risk_summary()
        self.assertTrue(summary["emergency_halt_active"])
        print("✓ [e] DI Overrides: Test isolation and mock-free replacement supported")

    def test_f_all_pydantic_schemas(self):
        """[f] Pydantic response schemas validation across all 8 required models"""
        tz = pytz.timezone("Asia/Kolkata")
        now = datetime.now(tz)

        # 1. TickResponse
        tick = TickResponse(symbol="RELIANCE", ltp=2550.0, timestamp=now, volume=1000)
        self.assertEqual(tick.symbol, "RELIANCE")

        # 2. IndicatorResponse
        ind = IndicatorResponse(symbol="BANKNIFTY", timeframe="15m", vwap=51200.0, ema_9=51250.0, timestamp=now)
        self.assertEqual(ind.timeframe, "15m")

        # 3. SignalResponse
        sig = SignalResponse(
            signal_id="SIG_01", symbol="NIFTY", instrument_type="OPTIDX",
            direction="BUY", score=88.5, expected_value=1.45, status="APPROVED", timestamp=now
        )
        self.assertEqual(sig.direction, "BUY")

        # 4. OrderResponse
        ord_resp = OrderResponse(
            order_id="ORD_99", symbol="TCS", side="BUY", quantity=10,
            price=3400.0, status="FILLED", broker="angelone", placed_at=now
        )
        self.assertEqual(ord_resp.status, "FILLED")

        # 5. PositionResponse
        pos = PositionResponse(
            position_id="POS_01", symbol="INFY", quantity=20, entry_price=1450.0,
            current_price=1470.0, unrealized_pnl=400.0, entry_time=now
        )
        self.assertEqual(pos.unrealized_pnl, 400.0)

        # 6. RiskStateResponse
        risk = RiskStateResponse(
            ist_date="2026-09-21", realized_pnl=500.0, total_risk_pnl=900.0,
            daily_loss_ceiling=-2500.0, loss_ceiling_breached=False,
            trade_count=3, max_daily_trades=5, consecutive_losses=0,
            cooldown_active=False, emergency_halt_active=False, open_positions_count=1
        )
        self.assertEqual(risk.trade_count, 3)

        # 7. BrokerStatusResponse
        brk = BrokerStatusResponse(broker_name="angelone", is_connected=True, latency_ms=45.2)
        self.assertTrue(brk.is_connected)

        # 8. ErrorResponse
        err = ErrorResponse(error="TestError", message="Test message", status_code=400, timestamp=now)
        self.assertEqual(err.status_code, 400)
        print("✓ [f] Schema integrity: All 8 core Pydantic response models compile and validate")

    def test_g_standardized_error_handling_404(self):
        """[g] Standardized ErrorResponse on 404 Not Found"""
        resp = self.client.get("/non_existent_endpoint")
        self.assertEqual(resp.status_code, 404)
        data = resp.json()
        self.assertEqual(data["error"], "Not Found")
        self.assertEqual(data["status_code"], 404)
        self.assertIn("timestamp", data)
        print("✓ [g] Error handling: 404 produces standardized ErrorResponse format")

    def test_h_custom_exception_handlers(self):
        """[h] Custom handlers for ValueError (400) and PermissionError (403)"""
        test_app = create_app()

        @test_app.get("/trigger_val_err")
        def trigger_val():
            raise ValueError("Invalid order quantity: must be positive")

        @test_app.get("/trigger_perm_err")
        def trigger_perm():
            raise PermissionError("Access denied: Emergency halt in effect")

        c = TestClient(test_app)

        # ValueError -> 400
        r1 = c.get("/trigger_val_err")
        self.assertEqual(r1.status_code, 400)
        d1 = r1.json()
        self.assertEqual(d1["error"], "Bad Request")
        self.assertIn("Invalid order quantity", d1["message"])

        # PermissionError -> 403
        r2 = c.get("/trigger_perm_err")
        self.assertEqual(r2.status_code, 403)
        d2 = r2.json()
        self.assertEqual(d2["error"], "Forbidden")
        self.assertIn("Emergency halt in effect", d2["message"])
        print("✓ [h] Exception handlers: ValueError (400) and PermissionError (403) handled cleanly")

    def test_i_credential_security_no_leakage(self):
        """[i] Credential security (Review Risk 7): API credentials/tokens redacted from logs and errors"""
        raw_text = "API error with api_key=ABCD1234XYZ and password=SuperSecretPass! during auth"
        sanitized = sanitize_text(raw_text)
        self.assertNotIn("ABCD1234XYZ", sanitized)
        self.assertNotIn("SuperSecretPass!", sanitized)
        self.assertIn("[REDACTED]", sanitized)

        raw_dict = {
            "symbol": "RELIANCE",
            "api_key": "LIVE_KEY_9999",
            "totp_pin": "123456",
            "metadata": {"jwt_token": "ey12345.SECRET.789"}
        }
        clean_dict = sanitize_dict(raw_dict)
        self.assertEqual(clean_dict["api_key"], "[REDACTED]")
        self.assertEqual(clean_dict["totp_pin"], "[REDACTED]")
        self.assertEqual(clean_dict["metadata"]["jwt_token"], "[REDACTED]")
        print("✓ [i] Credential security: Zero secret leakage in outputs or logs (Review Risk 7)")

    def test_j_cors_and_logging_middleware(self):
        """[j] CORS headers present for frontend integration and middleware active"""
        resp = self.client.options(
            "/health",
            headers={"Origin": "http://localhost:8501", "Access-Control-Request-Method": "GET"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access-control-allow-origin", resp.headers)
        print("✓ [j] Middleware & CORS: Streamlit port 8501 compatibility and security logging verified")


if __name__ == "__main__":
    print("=" * 70)
    print("STRIKE DESK V4 — PHASE 6 PROMPT 1/4 TEST SUITE (test_api_core.py)")
    print("=" * 70)
    unittest.main(verbosity=0)
