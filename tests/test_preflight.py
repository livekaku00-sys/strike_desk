"""
Tests for services/preflight.py — Pre-Flight Check Engine (P0-02)

Coverage:
  - Each check returns PASS/FAIL/PENDING/WARN for its nominal conditions
  - CRITICAL FAIL → PreflightReport.live_mode_blocked == True
  - CRITICAL PENDING (PCR before 09:15) → live_mode_blocked == False
  - ADVISORY WARN/FAIL → live_mode_blocked == False
  - set_exec_mode returns HTTP 503 when live_mode_blocked
  - set_exec_mode returns HTTP 200 for PAPER regardless of preflight state
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytz
import pytest

from services.preflight import (
    BrokerAuthCheck,
    EmergencyHaltCheck,
    FeedHealthCheck,
    InstrumentRegistryCheck,
    PCRPollerCheck,
    PreflightEngine,
    PreflightReport,
    ReconciliationCheck,
    RiskConfigCheck,
    RiskStateCheck,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_PENDING,
    STATUS_WARN,
    SEVERITY_ADVISORY,
    SEVERITY_CRITICAL,
    TokenFreshnessCheck,
    TradingCalendarCheck,
    _default_checks,
    _live_mode_blocked_from_results,
    _overall_from_results,
    preflight_engine,
)

IST = pytz.timezone("Asia/Kolkata")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ist(hour: int, minute: int = 0, weekday_offset: int = 0) -> datetime:
    """Return a timezone-aware IST datetime for today (a weekday by default)."""
    base = datetime(2026, 9, 28, hour, minute, 0)  # Monday
    base += timedelta(days=weekday_offset)
    return IST.localize(base)


def _make_token_mgr(is_authenticated=True, status="ACTIVE", expires_at=None, active="angelone"):
    mgr = MagicMock()
    mgr.get_active_broker.return_value = active
    info: dict = {
        "is_authenticated": is_authenticated,
        "status": status,
        "error_message": None,
        "expires_at": expires_at or (datetime.now(IST) + timedelta(hours=20)).isoformat(),
    }
    mgr.get_broker_token_status.return_value = info
    return mgr


def _make_reconciler(mismatch_active=False, diffs=None):
    rec = MagicMock()
    rec.is_mismatch_active.return_value = mismatch_active
    if diffs:
        diff_objs = []
        for sym, iq, bq in diffs:
            d = MagicMock()
            d.symbol = sym; d.internal_qty = iq; d.broker_qty = bq
            diff_objs.append(d)
        result = MagicMock()
        result.diffs = diff_objs
        rec.get_last_result.return_value = result
    else:
        result = MagicMock()
        result.diffs = []
        rec.get_last_result.return_value = result
    return rec


# ── TestBrokerAuthCheck ───────────────────────────────────────────────────────

class TestBrokerAuthCheck:
    def test_pass_when_authenticated(self):
        chk = BrokerAuthCheck(token_manager=_make_token_mgr())
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_PASS
        assert r.severity == SEVERITY_CRITICAL

    def test_fail_when_not_authenticated(self):
        chk = BrokerAuthCheck(token_manager=_make_token_mgr(is_authenticated=False, status="UNAUTHENTICATED"))
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL

    def test_fail_when_expired(self):
        chk = BrokerAuthCheck(token_manager=_make_token_mgr(is_authenticated=True, status="EXPIRED"))
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL

    def test_fail_when_status_none(self):
        mgr = MagicMock()
        mgr.get_active_broker.return_value = "angelone"
        mgr.get_broker_token_status.return_value = None
        chk = BrokerAuthCheck(token_manager=mgr)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL

    def test_fail_on_exception(self):
        mgr = MagicMock()
        mgr.get_active_broker.side_effect = RuntimeError("boom")
        chk = BrokerAuthCheck(token_manager=mgr)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "RuntimeError" in r.message


# ── TestFeedHealthCheck ───────────────────────────────────────────────────────

class TestFeedHealthCheck:
    def test_pass_when_running_no_stale(self):
        chk = FeedHealthCheck(pipeline_status_fn=lambda: {"is_running": True, "stale_count": 0})
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_PASS

    def test_fail_when_not_running(self):
        chk = FeedHealthCheck(pipeline_status_fn=lambda: {"is_running": False, "stale_count": 0})
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "not running" in r.message

    def test_fail_when_stale(self):
        chk = FeedHealthCheck(pipeline_status_fn=lambda: {"is_running": True, "stale_count": 3})
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "3" in r.message

    def test_fail_on_exception(self):
        def boom():
            raise RuntimeError("pipeline down")
        chk = FeedHealthCheck(pipeline_status_fn=boom)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL


# ── TestInstrumentRegistryCheck ───────────────────────────────────────────────

class TestInstrumentRegistryCheck:
    def test_pass_when_loaded(self):
        chk = InstrumentRegistryCheck(registry_count_fn=lambda: 105)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_PASS
        assert "105" in r.message

    def test_fail_when_empty(self):
        chk = InstrumentRegistryCheck(registry_count_fn=lambda: 0)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL

    def test_fail_on_exception(self):
        def boom():
            raise OSError("file not found")
        chk = InstrumentRegistryCheck(registry_count_fn=boom)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL


# ── TestRiskConfigCheck ───────────────────────────────────────────────────────

class TestRiskConfigCheck:
    def _summary(self, ceiling=-2500.0, max_trades=5, **kw):
        return {"daily_loss_ceiling": ceiling, "max_daily_trades": max_trades, **kw}

    def test_pass_valid_config(self):
        chk = RiskConfigCheck(risk_summary_fn=lambda: self._summary())
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_PASS

    def test_fail_ceiling_zero(self):
        chk = RiskConfigCheck(risk_summary_fn=lambda: self._summary(ceiling=0))
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "daily_loss_ceiling" in r.message

    def test_fail_ceiling_positive(self):
        chk = RiskConfigCheck(risk_summary_fn=lambda: self._summary(ceiling=100))
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL

    def test_fail_max_trades_zero(self):
        chk = RiskConfigCheck(risk_summary_fn=lambda: self._summary(max_trades=0))
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "max_daily_trades" in r.message

    def test_fail_both_invalid(self):
        chk = RiskConfigCheck(risk_summary_fn=lambda: self._summary(ceiling=0, max_trades=0))
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "daily_loss_ceiling" in r.message
        assert "max_daily_trades" in r.message

    def test_fail_on_exception(self):
        def boom():
            raise KeyError("risk_manager")
        chk = RiskConfigCheck(risk_summary_fn=boom)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL


# ── TestReconciliationCheck ───────────────────────────────────────────────────

class TestReconciliationCheck:
    def test_pass_no_mismatch(self):
        chk = ReconciliationCheck(reconciler=_make_reconciler(False))
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_PASS

    def test_fail_mismatch_active(self):
        chk = ReconciliationCheck(reconciler=_make_reconciler(True, [("NIFTY24OCT23400CE", 1.0, 2.0)]))
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "mismatch" in r.message.lower()

    def test_fail_includes_diff_detail(self):
        chk = ReconciliationCheck(reconciler=_make_reconciler(True, [("BANKNIFTY", 0.0, 1.0)]))
        r = chk.run(_ist(9, 30))
        assert r.detail is not None
        assert "BANKNIFTY" in r.detail

    def test_fail_on_exception(self):
        rec = MagicMock()
        rec.is_mismatch_active.side_effect = Exception("db down")
        chk = ReconciliationCheck(reconciler=rec)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL


# ── TestTokenFreshnessCheck ───────────────────────────────────────────────────

class TestTokenFreshnessCheck:
    def test_pass_far_from_expiry(self):
        mgr = _make_token_mgr(expires_at=(datetime.now(IST) + timedelta(hours=20)).isoformat())
        chk = TokenFreshnessCheck(token_manager=mgr)
        r = chk.run(datetime.now(IST))
        assert r.status == STATUS_PASS
        assert r.severity == SEVERITY_ADVISORY

    def test_warn_near_expiry(self):
        mgr = _make_token_mgr(expires_at=(datetime.now(IST) + timedelta(hours=1)).isoformat())
        chk = TokenFreshnessCheck(token_manager=mgr)
        r = chk.run(datetime.now(IST))
        assert r.status == STATUS_WARN

    def test_warn_when_expired(self):
        mgr = _make_token_mgr(expires_at=(datetime.now(IST) - timedelta(hours=1)).isoformat())
        chk = TokenFreshnessCheck(token_manager=mgr)
        r = chk.run(datetime.now(IST))
        assert r.status == STATUS_WARN

    def test_warn_no_expiry(self):
        mgr = MagicMock()
        mgr.get_active_broker.return_value = "dhan"
        mgr.get_broker_token_status.return_value = {"expires_at": None}
        chk = TokenFreshnessCheck(token_manager=mgr)
        r = chk.run(datetime.now(IST))
        assert r.status == STATUS_WARN

    def test_warn_on_exception(self):
        mgr = MagicMock()
        mgr.get_active_broker.side_effect = RuntimeError("net fail")
        chk = TokenFreshnessCheck(token_manager=mgr)
        r = chk.run(datetime.now(IST))
        assert r.status == STATUS_WARN


# ── TestEmergencyHaltCheck ────────────────────────────────────────────────────

class TestEmergencyHaltCheck:
    def test_pass_halt_off(self):
        chk = EmergencyHaltCheck(risk_summary_fn=lambda: {"emergency_halt_active": False})
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_PASS
        assert r.severity == SEVERITY_CRITICAL

    def test_fail_halt_on(self):
        chk = EmergencyHaltCheck(risk_summary_fn=lambda: {"emergency_halt_active": True})
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "kill-switch" in r.message.lower() or "ACTIVE" in r.message

    def test_fail_on_exception(self):
        def boom():
            raise RuntimeError("risk down")
        chk = EmergencyHaltCheck(risk_summary_fn=boom)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL


# ── TestTradingCalendarCheck ──────────────────────────────────────────────────

class TestTradingCalendarCheck:
    def test_pass_market_hours(self):
        chk = TradingCalendarCheck()
        r = chk.run(_ist(10, 0))  # Monday 10:00
        assert r.status == STATUS_PASS

    def test_fail_saturday(self):
        chk = TradingCalendarCheck()
        r = chk.run(_ist(10, 0, weekday_offset=5))  # Saturday
        assert r.status == STATUS_FAIL
        assert r.severity == SEVERITY_CRITICAL

    def test_fail_sunday(self):
        chk = TradingCalendarCheck()
        r = chk.run(_ist(10, 0, weekday_offset=6))  # Sunday
        assert r.status == STATUS_FAIL

    def test_fail_before_market_open(self):
        chk = TradingCalendarCheck()
        r = chk.run(_ist(8, 0))  # 08:00 IST, weekday
        assert r.status == STATUS_FAIL

    def test_fail_after_market_close(self):
        chk = TradingCalendarCheck()
        r = chk.run(_ist(16, 0))  # 16:00 IST, weekday
        assert r.status == STATUS_FAIL

    def test_pass_at_boundary_open(self):
        chk = TradingCalendarCheck()
        r = chk.run(_ist(9, 15))  # exactly open
        assert r.status == STATUS_PASS

    def test_pass_at_boundary_close(self):
        chk = TradingCalendarCheck()
        r = chk.run(_ist(15, 30))  # exactly close
        assert r.status == STATUS_PASS


# ── TestPCRPollerCheck ────────────────────────────────────────────────────────

class TestPCRPollerCheck:
    def test_pending_before_market_open(self):
        chk = PCRPollerCheck(last_updates_fn=lambda: {})
        r = chk.run(_ist(8, 0))
        assert r.status == STATUS_PENDING
        assert r.severity == SEVERITY_CRITICAL

    def test_pending_before_0915(self):
        chk = PCRPollerCheck(last_updates_fn=lambda: {})
        r = chk.run(_ist(9, 14))
        assert r.status == STATUS_PENDING

    def test_pending_at_exactly_0900(self):
        chk = PCRPollerCheck(last_updates_fn=lambda: {})
        r = chk.run(_ist(9, 0))
        assert r.status == STATUS_PENDING

    def test_pass_after_market_open_with_core_data(self):
        updates = {"NIFTY": datetime.now(IST), "BANKNIFTY": datetime.now(IST)}
        chk = PCRPollerCheck(last_updates_fn=lambda: updates)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_PASS

    def test_fail_after_open_no_data(self):
        chk = PCRPollerCheck(last_updates_fn=lambda: {})
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL

    def test_fail_after_open_only_equity_data(self):
        updates = {"RELIANCE": datetime.now(IST), "TCS": datetime.now(IST)}
        chk = PCRPollerCheck(last_updates_fn=lambda: updates)
        r = chk.run(_ist(9, 30))
        assert r.status == STATUS_FAIL
        assert "NIFTY" in r.message or "BANKNIFTY" in r.message

    def test_pass_with_one_core_index(self):
        updates = {"NIFTY": datetime.now(IST)}
        chk = PCRPollerCheck(last_updates_fn=lambda: updates)
        r = chk.run(_ist(10, 0))
        assert r.status == STATUS_PASS

    def test_fail_on_exception_after_market(self):
        def boom():
            raise RuntimeError("monitor gone")
        chk = PCRPollerCheck(last_updates_fn=boom)
        r = chk.run(_ist(10, 0))
        assert r.status == STATUS_FAIL

    def test_exactly_at_0915_is_not_pending(self):
        updates = {"NIFTY": datetime.now(IST)}
        chk = PCRPollerCheck(last_updates_fn=lambda: updates)
        r = chk.run(_ist(9, 15))
        assert r.status == STATUS_PASS


# ── TestRiskStateCheck ────────────────────────────────────────────────────────

class TestRiskStateCheck:
    def _summary(self, **kw):
        base = {
            "loss_ceiling_breached": False,
            "cooldown_active": False,
            "trade_count": 0,
            "max_daily_trades": 5,
        }
        base.update(kw)
        return base

    def test_pass_clean_state(self):
        chk = RiskStateCheck(risk_summary_fn=lambda: self._summary())
        r = chk.run(_ist(10, 0))
        assert r.status == STATUS_PASS
        assert r.severity == SEVERITY_ADVISORY

    def test_warn_ceiling_breached(self):
        chk = RiskStateCheck(risk_summary_fn=lambda: self._summary(loss_ceiling_breached=True))
        r = chk.run(_ist(10, 0))
        assert r.status == STATUS_WARN
        assert "ceiling" in r.message.lower()

    def test_warn_cooldown_active(self):
        chk = RiskStateCheck(risk_summary_fn=lambda: self._summary(cooldown_active=True))
        r = chk.run(_ist(10, 0))
        assert r.status == STATUS_WARN
        assert "cooldown" in r.message.lower()

    def test_warn_trade_cap_reached(self):
        chk = RiskStateCheck(risk_summary_fn=lambda: self._summary(trade_count=5, max_daily_trades=5))
        r = chk.run(_ist(10, 0))
        assert r.status == STATUS_WARN
        assert "cap" in r.message.lower() or "5/5" in r.message

    def test_warn_on_exception(self):
        def boom():
            raise RuntimeError("gone")
        chk = RiskStateCheck(risk_summary_fn=boom)
        r = chk.run(_ist(10, 0))
        assert r.status == STATUS_WARN


# ── TestPreflightEngine ───────────────────────────────────────────────────────

class TestPreflightEngine:
    def _all_pass_checks(self):
        from services.preflight import BaseCheck, CheckResult, STATUS_PASS, SEVERITY_CRITICAL
        class AlwaysPass(BaseCheck):
            NAME = "dummy_pass"
            SEVERITY = SEVERITY_CRITICAL
            def run(self, now):
                return CheckResult("dummy_pass", SEVERITY_CRITICAL, STATUS_PASS, "ok")
        return [AlwaysPass() for _ in range(10)]

    def test_run_all_returns_report(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        report = engine.run_all(_ist(10, 0))
        assert isinstance(report, PreflightReport)
        assert len(report.checks) == 10

    def test_overall_green_all_pass(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        report = engine.run_all(_ist(10, 0))
        assert report.overall == "GREEN"
        assert report.live_mode_blocked is False

    def test_overall_red_on_critical_fail(self):
        from services.preflight import BaseCheck, CheckResult, STATUS_FAIL, SEVERITY_CRITICAL
        class CritFail(BaseCheck):
            NAME = "crit_fail"
            SEVERITY = SEVERITY_CRITICAL
            def run(self, now):
                return CheckResult("crit_fail", SEVERITY_CRITICAL, STATUS_FAIL, "broken")
        engine = PreflightEngine(checks=[CritFail()])
        report = engine.run_all(_ist(10, 0))
        assert report.overall == "RED"
        assert report.live_mode_blocked is True

    def test_overall_amber_advisory_warn(self):
        from services.preflight import BaseCheck, CheckResult, STATUS_WARN, SEVERITY_ADVISORY
        class AdvWarn(BaseCheck):
            NAME = "adv_warn"
            SEVERITY = SEVERITY_ADVISORY
            def run(self, now):
                return CheckResult("adv_warn", SEVERITY_ADVISORY, STATUS_WARN, "note")
        engine = PreflightEngine(checks=[AdvWarn()])
        report = engine.run_all(_ist(10, 0))
        assert report.overall == "AMBER"
        assert report.live_mode_blocked is False

    def test_pending_does_not_block(self):
        from services.preflight import BaseCheck, CheckResult, STATUS_PENDING, SEVERITY_CRITICAL
        class CritPending(BaseCheck):
            NAME = "crit_pending"
            SEVERITY = SEVERITY_CRITICAL
            def run(self, now):
                return CheckResult("crit_pending", SEVERITY_CRITICAL, STATUS_PENDING, "wait")
        engine = PreflightEngine(checks=[CritPending()])
        report = engine.run_all(_ist(8, 30))
        assert report.live_mode_blocked is False

    def test_get_last_report_none_before_run(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        assert engine.get_last_report() is None

    def test_get_last_report_after_run(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        engine.run_all(_ist(10, 0))
        assert engine.get_last_report() is not None

    def test_is_live_mode_blocked_false_before_run(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        assert engine.is_live_mode_blocked() is False

    def test_get_status_unknown_before_run(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        s = engine.get_status()
        assert s["overall"] == "UNKNOWN"
        assert s["live_mode_blocked"] is False

    def test_get_status_after_run(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        engine.run_all(_ist(10, 0))
        s = engine.get_status()
        assert s["overall"] == "GREEN"
        assert "checks" in s

    def test_to_dict_structure(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        report = engine.run_all(_ist(10, 0))
        d = report.to_dict()
        assert "ran_at" in d
        assert "overall" in d
        assert "live_mode_blocked" in d
        assert isinstance(d["checks"], list)

    def test_thread_safe_concurrent_runs(self):
        engine = PreflightEngine(checks=self._all_pass_checks())
        errors = []

        def run():
            try:
                engine.run_all(_ist(10, 0))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ── TestLiveModeGating (integration) ──────────────────────────────────────────

class TestLiveModeGating:
    """Verify set_exec_mode returns HTTP 503 on preflight critical fail, 200 otherwise.

    set_exec_mode now calls _pf.run_all() on every LIVE-arm attempt (fresh check,
    fail-closed). Tests patch run_all on the real singleton so the route handler sees
    the same object regardless of how it was imported inside the function.
    """

    def _make_app(self):
        from fastapi import FastAPI
        from api.routes.trading import router as trading_router
        from api.routes.auth import require_operator
        app = FastAPI()
        app.include_router(trading_router)
        app.dependency_overrides[require_operator] = lambda: {"sub": "op1", "role": "operator"}
        return app

    def _blocked_report(self, check_name="broker_auth"):
        from services.preflight import PreflightReport, CheckResult
        c = CheckResult(check_name, SEVERITY_CRITICAL, STATUS_FAIL, "blocked")
        return PreflightReport(
            checks=[c],
            ran_at=datetime.now(IST),
            live_mode_blocked=True,
            overall="RED",
        )

    def _pass_report(self):
        from services.preflight import PreflightReport, CheckResult
        c = CheckResult("broker_auth", SEVERITY_CRITICAL, STATUS_PASS, "ok")
        return PreflightReport(
            checks=[c],
            ran_at=datetime.now(IST),
            live_mode_blocked=False,
            overall="GREEN",
        )

    def test_live_mode_blocked_by_preflight_returns_503(self):
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch.object(real_pf, "run_all", return_value=self._blocked_report("broker_auth")), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            mock_ts.get_live_mode_block_reason.return_value = (False, "")
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503
        assert "pre-flight" in resp.text.lower()

    def test_live_mode_allowed_when_preflight_passes(self):
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch.object(real_pf, "run_all", return_value=self._pass_report()), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            mock_ts.get_live_mode_block_reason.return_value = (False, "")
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 200
        assert resp.json()["execution_mode"] == "LIVE"

    def test_paper_mode_never_blocked_by_preflight(self):
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        # run_all should NOT be called for PAPER — the whole LIVE block is skipped
        with patch.object(real_pf, "run_all", side_effect=AssertionError("run_all called for PAPER")), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            mock_ts.get_live_mode_block_reason.return_value = (False, "")
            resp = client.post("/execution-mode", json={"mode": "PAPER"})
        assert resp.status_code == 200
        assert resp.json()["is_paper"] is True

    def test_preflight_503_contains_check_name(self):
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch.object(real_pf, "run_all", return_value=self._blocked_report("feed_health")), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            mock_ts.get_live_mode_block_reason.return_value = (False, "")
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert "feed_health" in resp.text

    def test_token_block_still_fires_before_preflight(self):
        """Token scheduler block (403) takes priority over preflight gate (503)."""
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf
        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Token block raises 403 before run_all is ever reached
        with patch.object(real_pf, "run_all", side_effect=AssertionError("run_all called after token block")), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            mock_ts.get_live_mode_block_reason.return_value = (True, "Dhan token expired.")
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 403


# ── TestCalendarEnforcementAtEndpoint ─────────────────────────────────────────

class TestCalendarEnforcementAtEndpoint:
    """
    Mandatory regression tests for Section 7 security audit.

    Verify every calendar-failure scenario blocks LIVE arming through the ACTUAL
    backend enforcement path (POST /execution-mode → preflight_engine.run_all()),
    and that PAPER mode is never affected.
    """

    def _make_app(self):
        from fastapi import FastAPI
        from api.routes.trading import router as trading_router
        from api.routes.auth import require_operator
        app = FastAPI()
        app.include_router(trading_router)
        app.dependency_overrides[require_operator] = lambda: {"sub": "op1", "role": "operator"}
        return app

    def _report(self, check_name, sev, st, blocked):
        from services.preflight import PreflightReport, CheckResult
        c = CheckResult(check_name, sev, st, "test")
        return PreflightReport(
            checks=[c], ran_at=datetime.now(IST),
            live_mode_blocked=blocked,
            overall="RED" if blocked else "GREEN",
        )

    def _ts_pass(self, mock_ts):
        mock_ts.get_live_mode_block_reason.return_value = (False, "")

    # ── Calendar-specific failure scenarios ───────────────────────────────────

    def test_calendar_exception_blocks_live(self):
        """TradingCalendarCheck exception → STATUS_FAIL → blocks LIVE via endpoint."""
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        bad_now = MagicMock()
        bad_now.weekday.side_effect = OSError("clock source unavailable")
        cal = TradingCalendarCheck()
        result = cal.run(bad_now)
        assert result.status == STATUS_FAIL, "exception must produce FAIL, not WARN"

        blocked_report = self._report("trading_calendar", SEVERITY_CRITICAL, STATUS_FAIL, True)
        with patch.object(real_pf, "run_all", return_value=blocked_report), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503
        assert "trading_calendar" in resp.text

    def test_calendar_weekend_blocks_live_end_to_end(self):
        """Saturday time → real TradingCalendarCheck → real PreflightEngine → 503."""
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf, PreflightEngine

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        saturday = _ist(10, 0, weekday_offset=5)
        test_engine = PreflightEngine(checks=[TradingCalendarCheck()])

        with patch.object(real_pf, "run_all", side_effect=lambda now=None: test_engine.run_all(saturday)), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503
        assert "trading_calendar" in resp.text

    def test_calendar_before_open_blocks_live_end_to_end(self):
        """Pre-market time → real TradingCalendarCheck → STATUS_FAIL → 503."""
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf, PreflightEngine

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        before_open = _ist(8, 0)
        test_engine = PreflightEngine(checks=[TradingCalendarCheck()])

        with patch.object(real_pf, "run_all", side_effect=lambda now=None: test_engine.run_all(before_open)), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503

    def test_calendar_after_close_blocks_live_end_to_end(self):
        """Post-market time → real TradingCalendarCheck → STATUS_FAIL → 503."""
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf, PreflightEngine

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        after_close = _ist(20, 0)
        test_engine = PreflightEngine(checks=[TradingCalendarCheck()])

        with patch.object(real_pf, "run_all", side_effect=lambda now=None: test_engine.run_all(after_close)), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503

    def test_stale_pass_cache_cannot_bypass_live_gate(self):
        """
        Regression: old code read _last_report (stale cache). A cached PASS from
        08:45 IST must NOT allow LIVE arming at 20:00 IST.  With the fix, run_all()
        is called on every arm attempt, so the fresh result controls the decision.
        """
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        pass_report = self._report("trading_calendar", SEVERITY_CRITICAL, STATUS_PASS, False)
        real_pf._last_report = pass_report  # seed stale cache with PASS

        fail_report = self._report("trading_calendar", SEVERITY_CRITICAL, STATUS_FAIL, True)
        with patch.object(real_pf, "run_all", return_value=fail_report), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503  # stale PASS not honored; fresh FAIL blocks

    def test_preflight_service_exception_blocks_live_fail_closed(self):
        """If run_all() raises (service crash), LIVE arming must be blocked (fail-closed)."""
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch.object(real_pf, "run_all", side_effect=RuntimeError("pytz unavailable")), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503

    def test_live_arming_succeeds_only_when_all_checks_pass(self):
        """LIVE arm succeeds exactly when the fresh report is not blocked."""
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        market_hours = _ist(10, 30)
        all_pass_engine = PreflightEngine(checks=[TradingCalendarCheck()])

        with patch.object(real_pf, "run_all", side_effect=lambda now=None: all_pass_engine.run_all(market_hours)), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 200
        assert resp.json()["execution_mode"] == "LIVE"

    # ── PAPER must remain usable in all calendar-fail scenarios ───────────────

    def test_paper_allowed_during_calendar_fail(self):
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        # run_all must NOT be called for PAPER (the entire LIVE gate is skipped)
        with patch.object(real_pf, "run_all", side_effect=AssertionError("run_all called for PAPER")), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "PAPER"})
        assert resp.status_code == 200
        assert resp.json()["is_paper"] is True

    def test_paper_allowed_when_preflight_service_crashes(self):
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch.object(real_pf, "run_all", side_effect=RuntimeError("crash")), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "PAPER"})
        assert resp.status_code == 200

    # ── Other CRITICAL check scenarios ────────────────────────────────────────

    def test_emergency_halt_blocks_live(self):
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        blocked = self._report("emergency_halt", SEVERITY_CRITICAL, STATUS_FAIL, True)
        with patch.object(real_pf, "run_all", return_value=blocked), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503
        assert "emergency_halt" in resp.text

    def test_broker_health_failure_blocks_live(self):
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        client = TestClient(app, raise_server_exceptions=False)

        blocked = self._report("broker_auth", SEVERITY_CRITICAL, STATUS_FAIL, True)
        with patch.object(real_pf, "run_all", return_value=blocked), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            resp = client.post("/execution-mode", json={"mode": "LIVE"})
        assert resp.status_code == 503
        assert "broker_auth" in resp.text

    # ── TradingCalendarCheck unit-level exception handling ────────────────────

    def test_calendar_exception_produces_fail_not_warn(self):
        """Exception inside run() must return STATUS_FAIL (SEVERITY_CRITICAL), not WARN."""
        bad_now = MagicMock()
        bad_now.weekday.side_effect = ValueError("tz database missing")
        r = TradingCalendarCheck().run(bad_now)
        assert r.status == STATUS_FAIL
        assert r.severity == SEVERITY_CRITICAL
        assert _live_mode_blocked_from_results([r]) is True

    def test_calendar_malformed_now_produces_fail(self):
        """A None-like now that can't be interrogated → STATUS_FAIL."""
        r = TradingCalendarCheck().run(None)
        assert r.status == STATUS_FAIL
        assert _live_mode_blocked_from_results([r]) is True

    # ── Concurrent mode transition ────────────────────────────────────────────

    def test_concurrent_live_arm_both_see_fresh_run(self):
        """
        Two simultaneous LIVE arm requests must each trigger their own run_all().
        Neither should read the other's cached result.
        """
        import threading
        from fastapi.testclient import TestClient
        from services.preflight import preflight_engine as real_pf

        app = self._make_app()
        call_count = {"n": 0}
        results = []

        def run_all_side_effect(now=None):
            call_count["n"] += 1
            from services.preflight import PreflightReport, CheckResult
            c = CheckResult("trading_calendar", SEVERITY_CRITICAL, STATUS_PASS, "ok")
            return PreflightReport(checks=[c], ran_at=datetime.now(IST),
                                   live_mode_blocked=False, overall="GREEN")

        with patch.object(real_pf, "run_all", side_effect=run_all_side_effect), \
             patch("services.token_scheduler.token_scheduler") as mock_ts:
            self._ts_pass(mock_ts)
            clients = [TestClient(app, raise_server_exceptions=False) for _ in range(3)]

            def arm(c):
                r = c.post("/execution-mode", json={"mode": "LIVE"})
                results.append(r.status_code)

            threads = [threading.Thread(target=arm, args=(c,)) for c in clients]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert all(s == 200 for s in results), f"Expected all 200, got {results}"
        assert call_count["n"] == 3, f"Expected 3 run_all() calls, got {call_count['n']}"


# ── TestPCRPendingDoesNotBlockLive ────────────────────────────────────────────

class TestPCRPendingDoesNotBlockLive:
    """A PENDING PCR check (before market hours) must not block LIVE MODE."""

    def test_pending_pcr_does_not_block_live(self):
        before_open = _ist(8, 0)
        pcr_chk = PCRPollerCheck(last_updates_fn=lambda: {})
        r = pcr_chk.run(before_open)
        assert r.status == STATUS_PENDING
        assert r.severity == SEVERITY_CRITICAL
        # A report with only this check must NOT block
        blocked = _live_mode_blocked_from_results([r])
        assert blocked is False

    def test_overall_helper_green_with_pending(self):
        from services.preflight import CheckResult
        results = [
            CheckResult("pcr_poller", SEVERITY_CRITICAL, STATUS_PENDING, "not yet"),
        ]
        assert _overall_from_results(results) == "GREEN"

    def test_overall_red_when_same_check_fails_after_open(self):
        from services.preflight import CheckResult
        results = [
            CheckResult("pcr_poller", SEVERITY_CRITICAL, STATUS_FAIL, "no data"),
        ]
        assert _overall_from_results(results) == "RED"
        assert _live_mode_blocked_from_results(results) is True


# ── TestAdvisoryCheckNeverBlocks ──────────────────────────────────────────────

class TestAdvisoryCheckNeverBlocks:
    """Any combination of ADVISORY WARN/FAIL must never set live_mode_blocked."""

    def test_advisory_warn_does_not_block(self):
        from services.preflight import CheckResult
        results = [
            CheckResult("token_freshness", SEVERITY_ADVISORY, STATUS_WARN, "expiring"),
            CheckResult("risk_state", SEVERITY_ADVISORY, STATUS_WARN, "cooldown"),
        ]
        assert _live_mode_blocked_from_results(results) is False
        assert _overall_from_results(results) == "AMBER"

    def test_all_advisory_pass_gives_green(self):
        from services.preflight import CheckResult
        results = [
            CheckResult("token_freshness", SEVERITY_ADVISORY, STATUS_PASS, "ok"),
            CheckResult("risk_state", SEVERITY_ADVISORY, STATUS_PASS, "ok"),
        ]
        assert _live_mode_blocked_from_results(results) is False
        assert _overall_from_results(results) == "GREEN"

    def test_default_checks_has_ten_entries(self):
        checks = _default_checks()
        assert len(checks) == 10

    def test_advisory_checks_are_6_and_10(self):
        checks = _default_checks()
        advisory = [c for c in checks if c.SEVERITY == SEVERITY_ADVISORY]
        advisory_names = {c.NAME for c in advisory}
        assert "token_freshness" in advisory_names
        assert "risk_state" in advisory_names
        assert "trading_calendar" not in advisory_names
        assert len(advisory) == 2

    def test_trading_calendar_out_of_hours_blocks_live_mode(self):
        """Out-of-hours check must produce CRITICAL FAIL, which blocks LIVE arming."""
        chk = TradingCalendarCheck()
        r = chk.run(_ist(20, 0))  # 20:00 IST weekday — after close
        assert r.severity == SEVERITY_CRITICAL
        assert r.status == STATUS_FAIL
        assert _live_mode_blocked_from_results([r]) is True

    def test_trading_calendar_weekend_blocks_live_mode(self):
        """Weekend check must produce CRITICAL FAIL, which blocks LIVE arming."""
        chk = TradingCalendarCheck()
        r = chk.run(_ist(10, 0, weekday_offset=5))  # Saturday
        assert r.severity == SEVERITY_CRITICAL
        assert r.status == STATUS_FAIL
        assert _live_mode_blocked_from_results([r]) is True

    def test_critical_checks_are_8(self):
        checks = _default_checks()
        critical = [c for c in checks if c.SEVERITY == SEVERITY_CRITICAL]
        assert len(critical) == 8
