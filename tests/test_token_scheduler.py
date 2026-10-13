"""
P0-01 — Token Refresh Scheduler tests

Coverage:
- compute_next_run_delay: 06:00 IST = 00:30 UTC; correct delay before/after trigger time
- AngelOne refresh: success path updates token_manager session state
- AngelOne refresh: failure path logs error without leaking token values
- Dhan/Fyers ACTIVE: no alert, no block
- Dhan/Fyers EXPIRING: CRITICAL alert logged, LIVE mode NOT blocked
- Dhan/Fyers EXPIRED: CRITICAL alert logged, LIVE mode IS blocked
- clear_live_mode_block() unblocks after operator reauth
- get_live_mode_block_reason() returns (False, "") when all tokens active
- get_live_mode_block_reason() returns (True, reason) when any token expired
- LIVE MODE arming (set_exec_mode LIVE) returns 403 when token expired
- LIVE MODE arming returns 200 when all tokens active
- Health endpoint returns expected structure with no token values
- record_angelone_refresh() updates BrokerTokenManager session correctly
- Zero token leakage: log output must not contain mock token values
"""
import asyncio
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("AUDIT_DB_PATH", "")

from services.token_scheduler import (
    TokenScheduler,
    compute_next_run_delay,
    ALERT_THRESHOLD_SECONDS,
    DAILY_CHECK_HOUR_IST,
)
from brokers.token_manager import BrokerTokenManager, TokenStatus

IST = pytz.timezone("Asia/Kolkata")


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_scheduler(mgr=None, adapters=None) -> TokenScheduler:
    ts = TokenScheduler()
    if mgr or adapters is not None:
        ts.configure(token_manager=mgr or _fresh_mgr(), adapters=adapters or {})
    return ts


def _fresh_mgr() -> BrokerTokenManager:
    """Return a freshly reset BrokerTokenManager (avoids singleton state leakage)."""
    mgr = object.__new__(BrokerTokenManager)
    mgr._initialized = False
    # Patch env so dhan/fyers tokens exist
    with patch.dict(os.environ, {
        "DHAN_ACCESS_TOKEN": "dhan_test_token",
        "DHAN_CLIENT_ID": "dhan123",
        "FYERS_ACCESS_TOKEN": "fyers_test_token",
        "FYERS_CLIENT_ID": "fyers123",
        "ANGELONE_JWT_TOKEN": "angel_test_jwt",
        "ANGEL_CLIENT_ID": "angel123",
        "ANGELONE_REFRESH_TOKEN": "angel_refresh_tok",
        "ANGELONE_FEED_TOKEN": "angel_feed_tok",
    }):
        BrokerTokenManager.__init__(mgr)
    return mgr


# ── Scheduler timing ──────────────────────────────────────────────────────────

class TestSchedulerTiming(unittest.TestCase):

    def test_06_ist_is_00_30_utc(self):
        """06:00 IST must equal 00:30 UTC — IST = UTC+5:30."""
        utc = pytz.utc
        ist_06 = IST.localize(datetime(2026, 1, 15, 6, 0, 0))
        utc_06 = ist_06.astimezone(utc)
        self.assertEqual(utc_06.hour, 0)
        self.assertEqual(utc_06.minute, 30)

    def test_delay_before_trigger_is_positive_and_short(self):
        """If now is 05:50 IST the delay should be ~10 minutes."""
        now = IST.localize(datetime(2026, 1, 15, 5, 50, 0))
        delay = compute_next_run_delay(now)
        self.assertAlmostEqual(delay, 10 * 60, delta=5)

    def test_delay_after_trigger_is_near_24h(self):
        """If now is 07:00 IST the delay should be ~23 hours."""
        now = IST.localize(datetime(2026, 1, 15, 7, 0, 0))
        delay = compute_next_run_delay(now)
        self.assertAlmostEqual(delay, 23 * 3600, delta=60)

    def test_delay_at_exactly_trigger_time_is_24h(self):
        """Exactly at 06:00 IST, delay should be 24h (schedules next day)."""
        now = IST.localize(datetime(2026, 1, 15, 6, 0, 0))
        delay = compute_next_run_delay(now)
        self.assertAlmostEqual(delay, 24 * 3600, delta=5)

    def test_delay_never_negative(self):
        for h in range(24):
            now = IST.localize(datetime(2026, 1, 15, h, 0, 0))
            self.assertGreater(compute_next_run_delay(now), 0)


# ── AngelOne refresh ──────────────────────────────────────────────────────────

class TestAngelOneRefresh(unittest.TestCase):

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_refresh_calls_adapter_authenticate(self):
        mgr = _fresh_mgr()
        mock_adapter = AsyncMock()
        mock_adapter.authenticate = AsyncMock(return_value=True)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={"angelone": mock_adapter})

        ok, msg = self._run(ts.refresh_angelone())

        mock_adapter.authenticate.assert_called_once()
        self.assertTrue(ok)
        self.assertIn("succeeded", msg)

    def test_refresh_success_updates_token_manager(self):
        mgr = _fresh_mgr()
        mock_adapter = AsyncMock()
        mock_adapter.authenticate = AsyncMock(return_value=True)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={"angelone": mock_adapter})

        before = mgr._sessions["angelone"].get("last_refreshed_at")
        self._run(ts.refresh_angelone())

        sess = mgr._sessions["angelone"]
        self.assertTrue(sess["is_authenticated"])
        self.assertEqual(sess["status"], TokenStatus.ACTIVE.value)
        self.assertIsNone(sess["error_message"])
        # expires_at should be ~24h from now
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")
        remaining = (sess["expires_at"] - datetime.now(_IST)).total_seconds()
        self.assertGreater(remaining, 23 * 3600)

    def test_refresh_failure_returns_false(self):
        mgr = _fresh_mgr()
        mock_adapter = AsyncMock()
        mock_adapter.authenticate = AsyncMock(return_value=False)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={"angelone": mock_adapter})

        ok, msg = self._run(ts.refresh_angelone())
        self.assertFalse(ok)
        self.assertIn("failed", msg.lower())

    def test_refresh_exception_does_not_leak_token(self):
        mgr = _fresh_mgr()
        secret_value = "SUPER_SECRET_TOKEN_VALUE_DO_NOT_LOG"
        mock_adapter = AsyncMock()
        mock_adapter.authenticate = AsyncMock(
            side_effect=RuntimeError(f"auth error: {secret_value}")
        )
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={"angelone": mock_adapter})

        with self.assertLogs("services.token_scheduler", level="ERROR") as captured:
            ok, msg = self._run(ts.refresh_angelone())

        # Verify that none of the logs or the returned message contain the secret
        all_output = " ".join(captured.output) + msg
        self.assertNotIn(secret_value, all_output,
                         "Secret value leaked into log output or return message")
        self.assertFalse(ok)

    def test_refresh_no_adapter_returns_false(self):
        ts = TokenScheduler()
        ts.configure(token_manager=_fresh_mgr(), adapters={})
        ok, msg = self._run(ts.refresh_angelone())
        self.assertFalse(ok)
        self.assertIn("not available", msg)

    def test_refresh_sets_last_angelone_refresh(self):
        mgr = _fresh_mgr()
        mock_adapter = AsyncMock()
        mock_adapter.authenticate = AsyncMock(return_value=True)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={"angelone": mock_adapter})
        self._run(ts.refresh_angelone())
        self.assertIsNotNone(ts._last_angelone_refresh)


# ── Dhan / Fyers monitor ──────────────────────────────────────────────────────

class TestDhanFyersMonitor(unittest.TestCase):

    def _make_ts_with_dhan_expiry(self, remaining_seconds: float) -> TokenScheduler:
        mgr = _fresh_mgr()
        now = datetime.now(IST)
        mgr._sessions["dhan"]["is_authenticated"] = True
        mgr._sessions["dhan"]["expires_at"] = now + timedelta(seconds=remaining_seconds)
        mgr._sessions["fyers"]["is_authenticated"] = False
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        return ts

    def test_active_token_no_block(self):
        ts = self._make_ts_with_dhan_expiry(remaining_seconds=8 * 3600)
        results = ts.check_manual_brokers()
        self.assertEqual(results.get("dhan"), "ACTIVE")
        self.assertFalse(ts._live_mode_blocks)

    def test_near_expiry_triggers_critical_alert_no_block(self):
        remaining = ALERT_THRESHOLD_SECONDS - 300  # 5 minutes inside the alert window
        ts = self._make_ts_with_dhan_expiry(remaining_seconds=remaining)

        with self.assertLogs("services.token_scheduler", level="CRITICAL") as logs:
            results = ts.check_manual_brokers()

        self.assertEqual(results.get("dhan"), "EXPIRING")
        # EXPIRING does NOT block
        self.assertNotIn("dhan", ts._live_mode_blocks)
        self.assertTrue(any("CRITICAL" in l and "dhan" in l.lower() for l in logs.output),
                        "Expected CRITICAL log for expiring token")

    def test_expired_token_sets_block_and_logs_critical(self):
        ts = self._make_ts_with_dhan_expiry(remaining_seconds=-1)

        with self.assertLogs("services.token_scheduler", level="CRITICAL") as logs:
            results = ts.check_manual_brokers()

        self.assertEqual(results.get("dhan"), "EXPIRED")
        self.assertIn("dhan", ts._live_mode_blocks)
        self.assertTrue(any("CRITICAL" in l and "dhan" in l.lower() for l in logs.output))

    def test_unauthenticated_broker_not_blocked(self):
        ts = self._make_ts_with_dhan_expiry(remaining_seconds=8 * 3600)
        # fyers is set to unauthenticated in helper — should not set a block
        results = ts.check_manual_brokers()
        self.assertNotIn("fyers", ts._live_mode_blocks)

    def test_both_brokers_expired_sets_both_blocks(self):
        mgr = _fresh_mgr()
        now = datetime.now(IST)
        for broker in ("dhan", "fyers"):
            mgr._sessions[broker]["is_authenticated"] = True
            mgr._sessions[broker]["expires_at"] = now - timedelta(seconds=1)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        with self.assertLogs("services.token_scheduler", level="CRITICAL"):
            ts.check_manual_brokers()
        self.assertIn("dhan", ts._live_mode_blocks)
        self.assertIn("fyers", ts._live_mode_blocks)


# ── Live-mode block ───────────────────────────────────────────────────────────

class TestLiveModeBlock(unittest.TestCase):

    def test_no_block_when_all_active(self):
        mgr = _fresh_mgr()
        now = datetime.now(IST)
        for broker in ("dhan", "fyers"):
            mgr._sessions[broker]["is_authenticated"] = True
            mgr._sessions[broker]["expires_at"] = now + timedelta(hours=8)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        blocked, reason = ts.get_live_mode_block_reason()
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_blocked_when_dhan_expired(self):
        mgr = _fresh_mgr()
        now = datetime.now(IST)
        mgr._sessions["dhan"]["is_authenticated"] = True
        mgr._sessions["dhan"]["expires_at"] = now - timedelta(seconds=1)
        mgr._sessions["fyers"]["is_authenticated"] = False
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        with self.assertLogs("services.token_scheduler", level="CRITICAL"):
            blocked, reason = ts.get_live_mode_block_reason()
        self.assertTrue(blocked)
        self.assertIn("DHAN", reason.upper())
        self.assertIn("expired", reason.lower())

    def test_clear_live_mode_block_unblocks(self):
        mgr = _fresh_mgr()
        now = datetime.now(IST)
        mgr._sessions["dhan"]["is_authenticated"] = True
        mgr._sessions["dhan"]["expires_at"] = now - timedelta(seconds=1)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        with self.assertLogs("services.token_scheduler", level="CRITICAL"):
            ts.get_live_mode_block_reason()
        self.assertTrue(ts._live_mode_blocks)

        # Simulate operator pasting fresh token → clear block
        ts.clear_live_mode_block("dhan")
        # Also update session so the fresh check sees it as active
        mgr._sessions["dhan"]["expires_at"] = now + timedelta(hours=8)

        blocked, _ = ts.get_live_mode_block_reason()
        self.assertFalse(blocked)

    def test_block_reason_contains_reauth_instructions(self):
        mgr = _fresh_mgr()
        now = datetime.now(IST)
        mgr._sessions["dhan"]["is_authenticated"] = True
        mgr._sessions["dhan"]["expires_at"] = now - timedelta(seconds=1)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        with self.assertLogs("services.token_scheduler", level="CRITICAL"):
            blocked, reason = ts.get_live_mode_block_reason()
        self.assertIn("reauth", reason.lower())


# ── set_exec_mode LIVE arming guard ──────────────────────────────────────────

class TestExecModeGuard(unittest.TestCase):
    """Verify POST /execution-mode returns 403 when live-mode block is active."""

    def _make_client(self, ts_blocked: bool):
        from fastapi.testclient import TestClient
        from api.main import create_app
        from api.routes.auth import require_operator

        # Mock block status
        mock_ts = MagicMock()
        if ts_blocked:
            mock_ts.get_live_mode_block_reason.return_value = (
                True, "DHAN token expired — paste fresh token"
            )
        else:
            mock_ts.get_live_mode_block_reason.return_value = (False, "")

        app = create_app()
        app.dependency_overrides[require_operator] = lambda: {"sub": "operator", "role": "operator"}

        with patch("api.routes.trading.token_scheduler", mock_ts, create=True):
            client = TestClient(app, raise_server_exceptions=False)
            return client

    def test_live_mode_arming_blocked_returns_403(self):
        from fastapi.testclient import TestClient
        from api.main import create_app
        from api.routes.auth import require_operator
        from api.routes.trading import execution_mode, EXEC_PAPER

        execution_mode.mode = EXEC_PAPER

        app = create_app()
        app.dependency_overrides[require_operator] = lambda: {"sub": "op", "role": "operator"}

        mock_ts = MagicMock()
        mock_ts.get_live_mode_block_reason.return_value = (
            True, "DHAN token expired"
        )

        # Patch the singleton inside the services module — the function does a
        # local `from services.token_scheduler import token_scheduler as _ts`.
        with patch("services.token_scheduler.token_scheduler", mock_ts):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/v1/trading/execution-mode", json={"mode": "LIVE"})

        self.assertEqual(resp.status_code, 403, resp.text)
        body = resp.json()
        detail = body.get("message", body.get("detail", ""))
        self.assertIn("blocked", detail.lower())

    def test_live_mode_arming_allowed_when_tokens_active(self):
        from fastapi.testclient import TestClient
        from api.main import create_app
        from api.routes.auth import require_operator
        from api.routes.trading import execution_mode, EXEC_PAPER

        execution_mode.mode = EXEC_PAPER

        app = create_app()
        app.dependency_overrides[require_operator] = lambda: {"sub": "op", "role": "operator"}

        mock_ts = MagicMock()
        mock_ts.get_live_mode_block_reason.return_value = (False, "")

        with patch("services.token_scheduler.token_scheduler", mock_ts):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/v1/trading/execution-mode", json={"mode": "LIVE"})

        self.assertNotEqual(resp.status_code, 403, resp.text)

    def test_paper_mode_arming_never_blocked(self):
        """Switching to PAPER must never be blocked regardless of token state."""
        from fastapi.testclient import TestClient
        from api.main import create_app
        from api.routes.auth import require_operator
        from api.routes.trading import execution_mode, EXEC_LIVE

        execution_mode.mode = EXEC_LIVE

        app = create_app()
        app.dependency_overrides[require_operator] = lambda: {"sub": "op", "role": "operator"}

        mock_ts = MagicMock()
        mock_ts.get_live_mode_block_reason.return_value = (True, "expired")

        with patch("services.token_scheduler.token_scheduler", mock_ts):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/v1/trading/execution-mode", json={"mode": "PAPER"})

        self.assertNotEqual(resp.status_code, 403, resp.text)


# ── BrokerTokenManager.record_angelone_refresh ───────────────────────────────

class TestRecordAngeloneRefresh(unittest.TestCase):

    def test_success_sets_active_status(self):
        mgr = _fresh_mgr()
        mgr.record_angelone_refresh(success=True)
        sess = mgr._sessions["angelone"]
        self.assertTrue(sess["is_authenticated"])
        self.assertEqual(sess["status"], TokenStatus.ACTIVE.value)
        self.assertIsNone(sess["error_message"])

    def test_success_sets_expires_at_24h(self):
        mgr = _fresh_mgr()
        mgr.record_angelone_refresh(success=True)
        sess = mgr._sessions["angelone"]
        remaining = (sess["expires_at"] - datetime.now(IST)).total_seconds()
        self.assertGreater(remaining, 23 * 3600)

    def test_failure_sets_error_message(self):
        mgr = _fresh_mgr()
        mgr.record_angelone_refresh(success=False, error="auth error")
        sess = mgr._sessions["angelone"]
        self.assertEqual(sess["error_message"], "auth error")

    def test_no_token_value_in_log(self):
        mgr = _fresh_mgr()
        fake_token = "FAKE_JWT_SHOULD_NOT_APPEAR_IN_LOGS"
        mgr._tokens["angelone"]["jwt_token"] = fake_token
        with self.assertLogs("strike_desk.brokers.token_manager", level="INFO") as logs:
            mgr.record_angelone_refresh(success=True)
        all_output = " ".join(logs.output)
        self.assertNotIn(fake_token, all_output,
                         "Token value leaked into broker token_manager log output")


# ── Health status ─────────────────────────────────────────────────────────────

class TestHealthStatus(unittest.TestCase):

    def test_health_status_structure(self):
        mgr = _fresh_mgr()
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        status = ts.get_health_status()
        self.assertIn("brokers", status)
        self.assertIn("any_live_mode_blocked", status)
        self.assertIn("checked_at", status)
        self.assertIn("next_check_in_seconds", status)

    def test_health_status_per_broker(self):
        mgr = _fresh_mgr()
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        status = ts.get_health_status()
        broker_names = {b["broker"] for b in status["brokers"]}
        self.assertIn("angelone", broker_names)
        self.assertIn("dhan", broker_names)
        self.assertIn("fyers", broker_names)

    def test_health_no_token_values(self):
        """Health response must not contain any raw token strings."""
        mgr = _fresh_mgr()
        fake_jwt = "FAKE_JWT_TOKEN_VALUE_XYZ_123"
        fake_dhan = "FAKE_DHAN_ACCESS_TOKEN_ABC"
        mgr._tokens["angelone"]["jwt_token"] = fake_jwt
        mgr._tokens["dhan"]["access_token"] = fake_dhan
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})

        import json
        health_json = json.dumps(ts.get_health_status())
        self.assertNotIn(fake_jwt, health_json,
                         "AngelOne JWT leaked into health status")
        self.assertNotIn(fake_dhan, health_json,
                         "Dhan access token leaked into health status")

    def test_health_shows_live_mode_blocked(self):
        mgr = _fresh_mgr()
        now = datetime.now(IST)
        mgr._sessions["dhan"]["is_authenticated"] = True
        mgr._sessions["dhan"]["expires_at"] = now - timedelta(seconds=1)
        ts = TokenScheduler()
        ts.configure(token_manager=mgr, adapters={})
        with self.assertLogs("services.token_scheduler", level="CRITICAL"):
            ts.check_manual_brokers()
        health = ts.get_health_status()
        self.assertTrue(health["any_live_mode_blocked"])
        dhan_entry = next(b for b in health["brokers"] if b["broker"] == "dhan")
        self.assertTrue(dhan_entry["live_mode_blocked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
