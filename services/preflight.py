"""
Pre-Flight Check Engine (P0-02)

10 system-health checks run before market open (scheduled 08:45 IST) and on demand.
Checks 1-5, 7, 9 are CRITICAL — a FAIL blocks LIVE MODE arming.
Checks 6, 8, 10 are ADVISORY — WARN status never blocks LIVE MODE.
A PENDING result (PCR before 09:15 IST) does NOT block LIVE MODE.
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger("strike_desk.services.preflight")

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_PENDING = "PENDING"
STATUS_WARN = "WARN"

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_ADVISORY = "ADVISORY"

_MARKET_OPEN_HOUR = 9
_MARKET_OPEN_MINUTE = 15
_CORE_PCR_INDICES = ["NIFTY", "BANKNIFTY"]
_TOKEN_WARN_THRESHOLD_SECONDS = 2 * 3600
_MIN_INSTRUMENTS = 1


@dataclass
class CheckResult:
    name: str
    severity: str
    status: str
    message: str
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity,
            "status": self.status,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class PreflightReport:
    checks: List[CheckResult]
    ran_at: datetime
    live_mode_blocked: bool
    overall: str  # "GREEN", "AMBER", "RED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ran_at": self.ran_at.isoformat(),
            "overall": self.overall,
            "live_mode_blocked": self.live_mode_blocked,
            "checks": [c.to_dict() for c in self.checks],
        }


def _overall_from_results(results: List[CheckResult]) -> str:
    for r in results:
        if r.severity == SEVERITY_CRITICAL and r.status == STATUS_FAIL:
            return "RED"
    for r in results:
        if r.status in (STATUS_WARN, STATUS_FAIL):
            return "AMBER"
    return "GREEN"


def _live_mode_blocked_from_results(results: List[CheckResult]) -> bool:
    return any(
        r.severity == SEVERITY_CRITICAL and r.status == STATUS_FAIL
        for r in results
    )


class BaseCheck(ABC):
    NAME = ""
    SEVERITY = SEVERITY_CRITICAL

    @abstractmethod
    def run(self, now: datetime) -> CheckResult:
        ...

    def _result(self, status: str, message: str, detail: Optional[str] = None) -> CheckResult:
        return CheckResult(
            name=self.NAME,
            severity=self.SEVERITY,
            status=status,
            message=message,
            detail=detail,
        )


# ── Check 1: Broker Auth (CRITICAL) ──────────────────────────────────────────

class BrokerAuthCheck(BaseCheck):
    NAME = "broker_auth"
    SEVERITY = SEVERITY_CRITICAL

    def __init__(self, token_manager=None) -> None:
        self._token_manager = token_manager

    def _get_mgr(self):
        if self._token_manager is not None:
            return self._token_manager
        from brokers.token_manager import token_manager
        return token_manager

    def run(self, now: datetime) -> CheckResult:
        try:
            mgr = self._get_mgr()
            active = mgr.get_active_broker()
            info = mgr.get_broker_token_status(active)
            if info is None:
                return self._result(STATUS_FAIL, f"Broker '{active}' not found in token manager.")
            is_authed = info.get("is_authenticated", False)
            tok_status = info.get("status", "UNKNOWN")
            if not is_authed or tok_status in ("EXPIRED", "UNAUTHENTICATED"):
                return self._result(
                    STATUS_FAIL,
                    f"Active broker '{active}' is not authenticated (status={tok_status}).",
                    detail=info.get("error_message"),
                )
            return self._result(
                STATUS_PASS,
                f"Active broker '{active}' is authenticated (status={tok_status}).",
            )
        except Exception as exc:
            return self._result(STATUS_FAIL, f"BrokerAuthCheck error: {type(exc).__name__}.")


# ── Check 2: Feed Health (CRITICAL) ──────────────────────────────────────────

class FeedHealthCheck(BaseCheck):
    NAME = "feed_health"
    SEVERITY = SEVERITY_CRITICAL

    def __init__(self, pipeline_status_fn: Optional[Callable[[], Dict]] = None) -> None:
        self._pipeline_status_fn = pipeline_status_fn

    def _get_status(self) -> Dict:
        if self._pipeline_status_fn is not None:
            return self._pipeline_status_fn()
        from pipeline import pipeline_orchestrator
        return pipeline_orchestrator.get_pipeline_status()

    def run(self, now: datetime) -> CheckResult:
        try:
            ps = self._get_status()
            is_running = ps.get("is_running", False)
            stale = int(ps.get("stale_count", 0))
            if not is_running:
                return self._result(STATUS_FAIL, "Live tick pipeline is not running.")
            if stale > 0:
                return self._result(
                    STATUS_FAIL,
                    f"Live feed has {stale} stale symbol(s).",
                    detail=f"stale_count={stale}",
                )
            return self._result(STATUS_PASS, "Live tick pipeline running with no stale symbols.")
        except Exception as exc:
            return self._result(STATUS_FAIL, f"FeedHealthCheck error: {type(exc).__name__}.")


# ── Check 3: Instrument Registry (CRITICAL) ───────────────────────────────────

class InstrumentRegistryCheck(BaseCheck):
    NAME = "instrument_registry"
    SEVERITY = SEVERITY_CRITICAL

    def __init__(self, registry_count_fn: Optional[Callable[[], int]] = None) -> None:
        self._registry_count_fn = registry_count_fn

    def _get_count(self) -> int:
        if self._registry_count_fn is not None:
            return self._registry_count_fn()
        from services.instrument_registry import instrument_registry
        return instrument_registry.count()

    def run(self, now: datetime) -> CheckResult:
        try:
            count = self._get_count()
            if count < _MIN_INSTRUMENTS:
                return self._result(
                    STATUS_FAIL,
                    f"Instrument registry is empty (count={count}).",
                )
            return self._result(STATUS_PASS, f"Instrument registry has {count} instrument(s).")
        except Exception as exc:
            return self._result(STATUS_FAIL, f"InstrumentRegistryCheck error: {type(exc).__name__}.")


# ── Check 4: Risk Config (CRITICAL) ───────────────────────────────────────────

class RiskConfigCheck(BaseCheck):
    NAME = "risk_config"
    SEVERITY = SEVERITY_CRITICAL

    def __init__(self, risk_summary_fn: Optional[Callable[[], Dict]] = None) -> None:
        self._risk_summary_fn = risk_summary_fn

    def _get_summary(self) -> Dict:
        if self._risk_summary_fn is not None:
            return self._risk_summary_fn()
        from risk.risk_manager import risk_manager
        return risk_manager.get_risk_summary()

    def run(self, now: datetime) -> CheckResult:
        try:
            summary = self._get_summary()
            ceiling = summary.get("daily_loss_ceiling")
            max_trades = summary.get("max_daily_trades")
            issues = []
            if ceiling is None or float(ceiling) >= 0:
                issues.append(f"daily_loss_ceiling={ceiling} (must be < 0)")
            if max_trades is None or int(max_trades) <= 0:
                issues.append(f"max_daily_trades={max_trades} (must be > 0)")
            if issues:
                return self._result(
                    STATUS_FAIL,
                    "Risk config validation failed: " + "; ".join(issues) + ".",
                )
            return self._result(
                STATUS_PASS,
                f"Risk config valid (ceiling={ceiling}, max_trades={max_trades}).",
            )
        except Exception as exc:
            return self._result(STATUS_FAIL, f"RiskConfigCheck error: {type(exc).__name__}.")


# ── Check 5: Reconciliation (CRITICAL) ────────────────────────────────────────

class ReconciliationCheck(BaseCheck):
    NAME = "reconciliation"
    SEVERITY = SEVERITY_CRITICAL

    def __init__(self, reconciler=None) -> None:
        self._reconciler = reconciler

    def _get_reconciler(self):
        if self._reconciler is not None:
            return self._reconciler
        from services.position_reconciler import position_reconciler
        return position_reconciler

    def run(self, now: datetime) -> CheckResult:
        try:
            rec = self._get_reconciler()
            if rec.is_mismatch_active():
                last = rec.get_last_result()
                detail = None
                if last and last.diffs:
                    detail = "; ".join(
                        f"{d.symbol}: internal={d.internal_qty} broker={d.broker_qty}"
                        for d in last.diffs
                    )
                return self._result(
                    STATUS_FAIL,
                    "Position reconciliation mismatch is active. "
                    "Clear via POST /api/risk/reconcile/clear.",
                    detail=detail,
                )
            return self._result(STATUS_PASS, "No active position reconciliation mismatch.")
        except Exception as exc:
            return self._result(STATUS_FAIL, f"ReconciliationCheck error: {type(exc).__name__}.")


# ── Check 6: Token Freshness (ADVISORY) ───────────────────────────────────────

class TokenFreshnessCheck(BaseCheck):
    NAME = "token_freshness"
    SEVERITY = SEVERITY_ADVISORY

    def __init__(self, token_manager=None) -> None:
        self._token_manager = token_manager

    def _get_mgr(self):
        if self._token_manager is not None:
            return self._token_manager
        from brokers.token_manager import token_manager
        return token_manager

    def run(self, now: datetime) -> CheckResult:
        try:
            mgr = self._get_mgr()
            active = mgr.get_active_broker()
            info = mgr.get_broker_token_status(active)
            if info is None:
                return self._result(STATUS_WARN, f"No token info for active broker '{active}'.")
            expires_at = info.get("expires_at")
            if expires_at is None:
                return self._result(STATUS_WARN, f"No expiry time recorded for '{active}'.")
            if isinstance(expires_at, str):
                try:
                    expires_at = datetime.fromisoformat(expires_at)
                except Exception:
                    return self._result(STATUS_WARN, "Could not parse token expiry time.")
            if expires_at.tzinfo is None:
                expires_at = IST.localize(expires_at)
            now_tz = now if now.tzinfo is not None else IST.localize(now)
            remaining = (expires_at - now_tz).total_seconds()
            if remaining <= 0:
                return self._result(STATUS_WARN, f"Active broker '{active}' token has expired.")
            if remaining < _TOKEN_WARN_THRESHOLD_SECONDS:
                return self._result(
                    STATUS_WARN,
                    f"Active broker '{active}' token expires in {remaining / 3600:.1f}h (< 2h).",
                )
            return self._result(
                STATUS_PASS,
                f"Active broker '{active}' token valid for {remaining / 3600:.1f}h.",
            )
        except Exception as exc:
            return self._result(STATUS_WARN, f"TokenFreshnessCheck error: {type(exc).__name__}.")


# ── Check 7: Emergency Halt (CRITICAL) ────────────────────────────────────────

class EmergencyHaltCheck(BaseCheck):
    NAME = "emergency_halt"
    SEVERITY = SEVERITY_CRITICAL

    def __init__(self, risk_summary_fn: Optional[Callable[[], Dict]] = None) -> None:
        self._risk_summary_fn = risk_summary_fn

    def _get_summary(self) -> Dict:
        if self._risk_summary_fn is not None:
            return self._risk_summary_fn()
        from risk.risk_manager import risk_manager
        return risk_manager.get_risk_summary()

    def run(self, now: datetime) -> CheckResult:
        try:
            if self._get_summary().get("emergency_halt_active", False):
                return self._result(
                    STATUS_FAIL,
                    "Emergency halt is ACTIVE — all order placement frozen by kill-switch. "
                    "Disengage via POST /api/risk/halt before arming LIVE MODE.",
                )
            return self._result(STATUS_PASS, "Emergency halt is OFF.")
        except Exception as exc:
            return self._result(STATUS_FAIL, f"EmergencyHaltCheck error: {type(exc).__name__}.")


# ── Check 8: Trading Calendar (CRITICAL) ──────────────────────────────────────

class TradingCalendarCheck(BaseCheck):
    NAME = "trading_calendar"
    SEVERITY = SEVERITY_CRITICAL

    def run(self, now: datetime) -> CheckResult:
        try:
            if now.weekday() >= 5:
                return self._result(
                    STATUS_FAIL,
                    f"Today is {now.strftime('%A')} — NSE markets are closed on weekends.",
                )
            h, m = now.hour, now.minute
            in_hours = (h > 9 or (h == 9 and m >= 15)) and (h < 15 or (h == 15 and m <= 30))
            if not in_hours:
                return self._result(
                    STATUS_FAIL,
                    f"Current time {now.strftime('%H:%M')} IST is outside market hours (09:15–15:30).",
                )
            return self._result(
                STATUS_PASS,
                f"Trading day confirmed ({now.strftime('%A %H:%M')} IST).",
            )
        except Exception as exc:
            return self._result(STATUS_FAIL, f"TradingCalendarCheck error: {type(exc).__name__}.")


# ── Check 9: PCR Poller (CRITICAL, PENDING before 09:15) ─────────────────────

class PCRPollerCheck(BaseCheck):
    NAME = "pcr_poller"
    SEVERITY = SEVERITY_CRITICAL

    def __init__(self, last_updates_fn: Optional[Callable[[], Dict[str, datetime]]] = None) -> None:
        self._last_updates_fn = last_updates_fn

    def _get_last_updates(self) -> Dict[str, datetime]:
        if self._last_updates_fn is not None:
            return self._last_updates_fn()
        from pipeline.indicators.pcr_staleness_monitor import pcr_staleness_monitor
        return dict(pcr_staleness_monitor._last_updates)

    def run(self, now: datetime) -> CheckResult:
        h, m = now.hour, now.minute
        before_market = h < _MARKET_OPEN_HOUR or (h == _MARKET_OPEN_HOUR and m < _MARKET_OPEN_MINUTE)
        if before_market:
            return self._result(
                STATUS_PENDING,
                f"PCR check deferred — market opens at 09:15 IST "
                f"(now {now.strftime('%H:%M')} IST).",
            )
        try:
            updates = self._get_last_updates()
            if not updates:
                return self._result(
                    STATUS_FAIL,
                    "PCR poller has no data for any symbol. "
                    "The option-chain fetch loop may not have started.",
                )
            core_with_data = [s for s in _CORE_PCR_INDICES if s in updates]
            if not core_with_data:
                all_syms = ", ".join(sorted(updates.keys())[:5]) or "none"
                return self._result(
                    STATUS_FAIL,
                    f"PCR poller has no data for core indices ({', '.join(_CORE_PCR_INDICES)}). "
                    f"Symbols with data: {all_syms}.",
                )
            return self._result(
                STATUS_PASS,
                f"PCR data present for {len(core_with_data)}/{len(_CORE_PCR_INDICES)} "
                f"core indices ({', '.join(core_with_data)}).",
            )
        except Exception as exc:
            return self._result(STATUS_FAIL, f"PCRPollerCheck error: {type(exc).__name__}.")


# ── Check 10: Risk State (ADVISORY) ───────────────────────────────────────────

class RiskStateCheck(BaseCheck):
    NAME = "risk_state"
    SEVERITY = SEVERITY_ADVISORY

    def __init__(self, risk_summary_fn: Optional[Callable[[], Dict]] = None) -> None:
        self._risk_summary_fn = risk_summary_fn

    def _get_summary(self) -> Dict:
        if self._risk_summary_fn is not None:
            return self._risk_summary_fn()
        from risk.risk_manager import risk_manager
        return risk_manager.get_risk_summary()

    def run(self, now: datetime) -> CheckResult:
        try:
            summary = self._get_summary()
            issues = []
            if summary.get("loss_ceiling_breached", False):
                issues.append("daily loss ceiling breached")
            if summary.get("cooldown_active", False):
                issues.append("risk cooldown active")
            trade_count = int(summary.get("trade_count", 0))
            max_trades = int(summary.get("max_daily_trades", 5))
            if trade_count >= max_trades:
                issues.append(f"daily trade cap reached ({trade_count}/{max_trades})")
            if issues:
                return self._result(
                    STATUS_WARN,
                    "Risk state warnings: " + "; ".join(issues) + ".",
                    detail=f"trade_count={trade_count}, max_daily_trades={max_trades}",
                )
            return self._result(
                STATUS_PASS,
                f"Risk state clean (trades={trade_count}/{max_trades}, ceiling intact).",
            )
        except Exception as exc:
            return self._result(STATUS_WARN, f"RiskStateCheck error: {type(exc).__name__}.")


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

def _default_checks() -> List[BaseCheck]:
    return [
        BrokerAuthCheck(),          # 1  CRITICAL
        FeedHealthCheck(),          # 2  CRITICAL
        InstrumentRegistryCheck(),  # 3  CRITICAL
        RiskConfigCheck(),          # 4  CRITICAL
        ReconciliationCheck(),      # 5  CRITICAL
        TokenFreshnessCheck(),      # 6  ADVISORY
        EmergencyHaltCheck(),       # 7  CRITICAL
        TradingCalendarCheck(),     # 8  ADVISORY
        PCRPollerCheck(),           # 9  CRITICAL (PENDING before 09:15 IST)
        RiskStateCheck(),           # 10 ADVISORY
    ]


class PreflightEngine:
    def __init__(self, checks: Optional[List[BaseCheck]] = None) -> None:
        self._checks: List[BaseCheck] = checks if checks is not None else _default_checks()
        self._last_report: Optional[PreflightReport] = None
        self._lock = threading.Lock()

    def run_all(self, now: Optional[datetime] = None) -> PreflightReport:
        if now is None:
            now = datetime.now(IST)
        elif now.tzinfo is None:
            now = IST.localize(now)

        results = [check.run(now) for check in self._checks]
        blocked = _live_mode_blocked_from_results(results)
        overall = _overall_from_results(results)

        report = PreflightReport(
            checks=results,
            ran_at=now,
            live_mode_blocked=blocked,
            overall=overall,
        )
        with self._lock:
            self._last_report = report

        logger.info(
            "[Preflight] %s | %d checks | LIVE=%s | %d FAIL/WARN",
            overall,
            len(results),
            "BLOCKED" if blocked else "OK",
            sum(1 for r in results if r.status in (STATUS_FAIL, STATUS_WARN)),
        )
        return report

    def get_last_report(self) -> Optional[PreflightReport]:
        with self._lock:
            return self._last_report

    def is_live_mode_blocked(self) -> bool:
        with self._lock:
            if self._last_report is None:
                return False
            return self._last_report.live_mode_blocked

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            last = self._last_report
        if last is None:
            return {
                "overall": "UNKNOWN",
                "live_mode_blocked": False,
                "last_ran_at": None,
                "checks": [],
            }
        return last.to_dict()


preflight_engine = PreflightEngine()
