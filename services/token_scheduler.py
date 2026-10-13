"""
P0-01 — Token Refresh Scheduler

Two paths, one scheduler:

AngelOne  (AUTO_REFRESH): calls adapter.authenticate() which executes TOTP-based
          renewal internally. The adapter holds the TOTP secret — this module never
          touches it, satisfying C4.

Dhan/Fyers (MONITOR & BLOCK): no browser automation, no portal scraping. At 06:00 IST
          daily (and on demand) we read the session expiry stored in BrokerTokenManager.
          • EXPIRING (< ALERT_THRESHOLD):  log CRITICAL, no blocking.
          • EXPIRED:                        log CRITICAL + set live-mode block flag.
          Operator pastes a fresh token via POST /api/v1/brokers/{dhan|fyers}/reauth,
          which clears the block.

Security constraints (zero secret leakage):
  - This module never logs or returns a token value, JWT, TOTP code or secret.
  - The AngelOne adapter refresh is fire-and-forget via adapter.authenticate(); secrets
    stay inside the adapter's settings object.
  - Log lines contain only broker name, success/failure, and timestamps.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Alert when remaining token lifetime drops below this threshold.
ALERT_THRESHOLD_SECONDS = 2 * 3600      # 2 hours
# Daily scheduler fires at this IST hour.
DAILY_CHECK_HOUR_IST = 6               # 06:00 IST = 00:30 UTC

MANUAL_REAUTH_BROKERS = ("dhan", "fyers")


def _mask(value: str) -> str:
    """Return a masked representation of a token/secret for safe logging."""
    if not value:
        return "[empty]"
    return f"{value[:4]}…[REDACTED]" if len(value) > 4 else "[REDACTED]"


def compute_next_run_delay(now: datetime) -> float:
    """
    Seconds until the next 06:00 IST occurrence after `now`.
    Exported as a free function so tests can call it without an instance.
    """
    if now.tzinfo is None:
        now = IST.localize(now)
    now_ist = now.astimezone(IST)
    today_06 = now_ist.replace(hour=DAILY_CHECK_HOUR_IST, minute=0, second=0, microsecond=0)
    if now_ist >= today_06:
        today_06 += timedelta(days=1)
    return (today_06 - now_ist).total_seconds()


class TokenScheduler:
    """
    Process-wide token refresh / monitor service.

    Configured at startup via configure(); runs via run_loop() as an asyncio task.
    All check/refresh logic is also callable synchronously for tests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token_manager: Any = None          # BrokerTokenManager singleton
        self._adapters: Dict[str, Any] = {}      # name → live adapter
        self._last_angelone_refresh: Optional[datetime] = None
        self._last_check_time: Optional[datetime] = None
        # broker → reason string; populated when a manual-auth token expires
        self._live_mode_blocks: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Wiring (called at startup)
    # ------------------------------------------------------------------

    def configure(self, token_manager: Any, adapters: Dict[str, Any]) -> None:
        """Inject live dependencies.  Safe to call after construction."""
        with self._lock:
            self._token_manager = token_manager
            self._adapters = dict(adapters)
        logger.info(
            "[TokenScheduler] Configured with %d adapters: %s",
            len(adapters), ", ".join(sorted(adapters)),
        )

    # ------------------------------------------------------------------
    # AngelOne auto-refresh
    # ------------------------------------------------------------------

    async def refresh_angelone(self) -> Tuple[bool, str]:
        """
        Trigger AngelOne TOTP re-authentication via the live adapter.
        Returns (success, message).  Never logs token values.
        """
        with self._lock:
            adapter = self._adapters.get("angelone")
            mgr = self._token_manager

        if adapter is None:
            msg = "AngelOne adapter not available — skip refresh"
            logger.warning("[AngelOne] %s", msg)
            return False, msg

        try:
            logger.info("[AngelOne] Starting scheduled TOTP refresh")
            success = await adapter.authenticate()
            if success:
                now = datetime.now(IST)
                logger.info("[AngelOne] Scheduled refresh succeeded")
                if mgr is not None:
                    mgr.record_angelone_refresh(success=True)
                with self._lock:
                    self._last_angelone_refresh = now
                return True, "AngelOne refresh succeeded"
            else:
                msg = "adapter.authenticate() returned False"
                logger.error("[AngelOne] Scheduled refresh FAILED: %s", msg)
                if mgr is not None:
                    mgr.record_angelone_refresh(success=False, error=msg)
                return False, f"AngelOne refresh failed: {msg}"
        except Exception as exc:
            # Sanitise: only log the exception type, never any string that might
            # contain a token returned in an error response body.
            msg = f"{type(exc).__name__}: refresh exception"
            logger.error("[AngelOne] Scheduled refresh FAILED: %s", msg)
            if mgr is not None:
                mgr.record_angelone_refresh(success=False, error=msg)
            return False, f"AngelOne refresh failed: {msg}"

    # ------------------------------------------------------------------
    # Dhan / Fyers monitor
    # ------------------------------------------------------------------

    def check_manual_brokers(self) -> Dict[str, str]:
        """
        Check Dhan and Fyers token status.  Returns a dict mapping broker name
        to status string ("ACTIVE", "EXPIRING", "EXPIRED", "UNAUTHENTICATED").
        Side-effects: logs CRITICAL alerts and sets/clears _live_mode_blocks.
        """
        with self._lock:
            mgr = self._token_manager

        if mgr is None:
            return {}

        now = datetime.now(IST)
        results: Dict[str, str] = {}

        for broker in MANUAL_REAUTH_BROKERS:
            sess = mgr._sessions.get(broker, {})
            if not sess.get("is_authenticated"):
                results[broker] = "UNAUTHENTICATED"
                continue

            expires_at = sess.get("expires_at")
            if expires_at is None:
                results[broker] = "UNAUTHENTICATED"
                continue

            if expires_at.tzinfo is None:
                expires_at = IST.localize(expires_at)

            remaining = (expires_at - now).total_seconds()

            if remaining <= 0:
                results[broker] = "EXPIRED"
                reason = (
                    f"{broker.upper()} access token has EXPIRED. "
                    f"Paste a fresh token via POST /api/v1/brokers/{broker}/reauth "
                    f"to re-enable LIVE trading."
                )
                logger.critical(
                    "[TokenScheduler] CRITICAL — %s token EXPIRED. "
                    "LIVE MODE is blocked until the operator pastes a new token.",
                    broker.upper(),
                )
                with self._lock:
                    self._live_mode_blocks[broker] = reason
            elif remaining < ALERT_THRESHOLD_SECONDS:
                results[broker] = "EXPIRING"
                hrs = int(remaining // 3600)
                mins = int((remaining % 3600) // 60)
                logger.critical(
                    "[TokenScheduler] CRITICAL — %s token expires in %dh %dm. "
                    "Paste a fresh token via POST /api/v1/brokers/%s/reauth before it expires.",
                    broker.upper(), hrs, mins, broker,
                )
                # EXPIRING does not block — only EXPIRED does.
                with self._lock:
                    self._live_mode_blocks.pop(broker, None)
            else:
                results[broker] = "ACTIVE"
                with self._lock:
                    self._live_mode_blocks.pop(broker, None)

        return results

    # ------------------------------------------------------------------
    # Gate query (called by set_exec_mode)
    # ------------------------------------------------------------------

    def get_live_mode_block_reason(self) -> Tuple[bool, str]:
        """
        Returns (True, reason) if LIVE MODE should be blocked because a
        manual-auth broker's token has expired.  (False, "") otherwise.
        """
        # Run a fresh check so the flag reflects the current session state.
        self.check_manual_brokers()
        with self._lock:
            if not self._live_mode_blocks:
                return False, ""
            reasons = "; ".join(self._live_mode_blocks.values())
            return True, reasons

    def clear_live_mode_block(self, broker: str) -> None:
        """Called by reauth endpoints after a successful token paste."""
        with self._lock:
            self._live_mode_blocks.pop(broker.lower(), None)
        logger.info("[TokenScheduler] Live-mode block cleared for %s", broker)

    # ------------------------------------------------------------------
    # Full daily check (public for tests + the loop)
    # ------------------------------------------------------------------

    async def run_daily_check(self) -> Dict[str, Any]:
        """
        Execute the full 06:00 IST daily check:
          1. Refresh AngelOne via TOTP.
          2. Check Dhan/Fyers expiry and alert/block if needed.
        Returns a summary dict.
        """
        with self._lock:
            self._last_check_time = datetime.now(IST)

        angelone_ok, angelone_msg = await self.refresh_angelone()
        manual_statuses = self.check_manual_brokers()

        result = {
            "checked_at": datetime.now(IST).isoformat(),
            "angelone": {"refreshed": angelone_ok, "message": angelone_msg},
            "manual_brokers": manual_statuses,
            "live_mode_blocked": bool(self._live_mode_blocks),
            "block_reasons": list(self._live_mode_blocks.values()),
        }
        logger.info("[TokenScheduler] Daily check complete: %s", result)
        return result

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def run_loop(self) -> None:
        """
        Background asyncio task: sleep until 06:00 IST then run_daily_check().
        Loops indefinitely.
        """
        logger.info(
            "[TokenScheduler] Background loop started — daily check fires at %02d:00 IST",
            DAILY_CHECK_HOUR_IST,
        )
        while True:
            delay = compute_next_run_delay(datetime.now(IST))
            logger.info(
                "[TokenScheduler] Next daily check in %.1fh (at %02d:00 IST)",
                delay / 3600,
                DAILY_CHECK_HOUR_IST,
            )
            await asyncio.sleep(delay)
            try:
                await self.run_daily_check()
            except Exception as exc:
                logger.error("[TokenScheduler] Daily check raised: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    # Health / status read
    # ------------------------------------------------------------------

    def get_health_status(self) -> Dict[str, Any]:
        """
        Per-broker health snapshot.  Safe for API response — no token values included.
        """
        with self._lock:
            mgr = self._token_manager
            last_angelone = self._last_angelone_refresh
            last_check = self._last_check_time
            blocks = dict(self._live_mode_blocks)

        now = datetime.now(IST)
        brokers: List[Dict[str, Any]] = []

        if mgr is not None:
            for sess in mgr.get_all_token_statuses():
                name = sess.get("broker_name", "")
                expires_at_raw = sess.get("expires_at")
                age_secs: Optional[float] = None
                remaining_secs: Optional[float] = None
                try:
                    auth_at_raw = sess.get("authenticated_at")
                    if isinstance(auth_at_raw, str):
                        auth_at = datetime.fromisoformat(auth_at_raw).replace(tzinfo=IST) \
                            if auth_at_raw.endswith("+00:00") is False \
                            else datetime.fromisoformat(auth_at_raw)
                    else:
                        auth_at = auth_at_raw
                    if auth_at is not None:
                        if auth_at.tzinfo is None:
                            auth_at = IST.localize(auth_at)
                        age_secs = (now - auth_at.astimezone(IST)).total_seconds()
                except Exception:
                    pass
                try:
                    if isinstance(expires_at_raw, str):
                        exp = datetime.fromisoformat(expires_at_raw)
                    else:
                        exp = expires_at_raw
                    if exp is not None:
                        if exp.tzinfo is None:
                            exp = IST.localize(exp)
                        remaining_secs = (exp.astimezone(IST) - now).total_seconds()
                except Exception:
                    pass

                entry: Dict[str, Any] = {
                    "broker": name,
                    "token_status": sess.get("status", "UNKNOWN"),
                    "auth_type": sess.get("auth_type"),
                    "auto_refresh_supported": sess.get("auto_refresh_supported", False),
                    "token_age_seconds": round(age_secs, 0) if age_secs is not None else None,
                    "token_remaining_seconds": round(remaining_secs, 0) if remaining_secs is not None else None,
                    "live_mode_blocked": name in blocks,
                    "block_reason": blocks.get(name),
                    "expires_in": sess.get("expires_in"),
                }
                if name == "angelone":
                    entry["last_refresh"] = last_angelone.isoformat() if last_angelone else None
                brokers.append(entry)

        return {
            "checked_at": now.isoformat(),
            "last_daily_check": last_check.isoformat() if last_check else None,
            "next_check_in_seconds": round(compute_next_run_delay(now), 0),
            "any_live_mode_blocked": bool(blocks),
            "brokers": brokers,
        }


# Process-wide singleton.
token_scheduler = TokenScheduler()
