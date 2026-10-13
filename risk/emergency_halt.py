"""
Strike Desk V4 - Emergency Halt Subsystem
Blueprint O5: Emergency halt kill-switch
Review Risk Areas: Manual acknowledgment workflow, multi-level kill-switch, audit logging

P0-05 additions (non-breaking):
- Atomic disk persistence via tmp-then-rename (data/halt_state.json by default)
- request_flatten_confirmation_token() with 60-second expiry for FLATTEN ALL
- stop_entry() / flatten_all() — operator-facing methods mapping to SOFT_HALT / FULL_KILL_SWITCH
- get_operator_status() — UI-facing view; HARD_HALT is internal and never exposed there
- threading.RLock for all state mutations
"""

import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import pytz

logger = logging.getLogger("EmergencyHalt")
IST = pytz.timezone("Asia/Kolkata")

_DEFAULT_STATE_PATH = "data/halt_state.json"


class HaltLevel(str, Enum):
    SOFT_HALT = "SOFT_HALT"                # Blocks new order entry; existing positions held
    HARD_HALT = "HARD_HALT"                # Blocks new orders + cancels all pending orders (internal)
    FULL_KILL_SWITCH = "FULL_KILL_SWITCH"  # Cancels pending orders + hard squares off all open positions


@dataclass
class HaltEvent:
    event_id: str
    timestamp: datetime
    reason: str
    level: HaltLevel
    triggered_by: str
    acknowledged: bool = False
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None


class EmergencyHaltManager:
    """
    Central emergency halt orchestrator enforcing kill-switch protocols
    and requiring strict manual acknowledgment before system resumption.

    Operator-facing surface:
        stop_entry()   → SOFT_HALT    (block new orders, hold positions)
        flatten_all()  → FULL_KILL_SWITCH (cancel + close all; requires confirmation token)

    HARD_HALT is reserved for internal programmatic use and is never exposed
    in get_operator_status().

    db_path: path to the JSON persistence file. Pass None to disable persistence
    (used by unit tests that create isolated instances).
    """

    def __init__(
        self,
        risk_state_manager: Any = None,
        db_path: Optional[str] = None,
    ):
        self.risk_state_manager = risk_state_manager
        self._db_path = db_path
        self._lock = threading.RLock()

        # Core halt state
        self._is_halted: bool = False
        self._current_level: Optional[HaltLevel] = None
        self._current_reason: str = ""
        self._active_event: Optional[HaltEvent] = None
        self._audit_log: List[HaltEvent] = []

        # ACK-token (for halt RESET flow — unchanged from original)
        self._active_ack_token: Optional[str] = None
        self._active_token_operator: Optional[str] = None

        # Flatten confirmation token (for FLATTEN ALL trigger)
        self._flatten_token: Optional[str] = None
        self._flatten_token_expiry: Optional[datetime] = None
        self._flatten_token_operator: Optional[str] = None

        # Restore persisted state on startup
        if self._db_path:
            self.load_halt_state()

    # ── properties (unchanged) ────────────────────────────────────────────────

    @property
    def is_halted(self) -> bool:
        return self._is_halted

    @property
    def current_reason(self) -> str:
        return self._current_reason

    @property
    def current_level(self) -> Optional[HaltLevel]:
        return self._current_level

    # ── disk persistence ──────────────────────────────────────────────────────

    def persist_halt_state(self, path: Optional[str] = None) -> None:
        """
        Atomically write halt state to disk using tmp-then-rename.
        Safe against process kills mid-write.
        """
        target = path or self._db_path
        if not target:
            return
        state = {
            "is_halted": self._is_halted,
            "level": self._current_level.value if self._current_level else None,
            "reason": self._current_reason,
            "triggered_by": self._active_event.triggered_by if self._active_event else "SYSTEM",
            "event_id": self._active_event.event_id if self._active_event else None,
            "timestamp": self._active_event.timestamp.isoformat() if self._active_event else None,
        }
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, target)

    def load_halt_state(self, path: Optional[str] = None) -> bool:
        """
        Restore halt state from disk. Returns True if a halted state was loaded.
        Safe against missing or corrupt files.
        """
        target = path or self._db_path
        if not target or not os.path.exists(target):
            return False
        try:
            with open(target, encoding="utf-8") as f:
                state = json.load(f)
        except Exception as exc:
            logger.warning("[EmergencyHalt] Cannot load halt state from %s: %s", target, exc)
            return False

        if not state.get("is_halted"):
            return False

        with self._lock:
            self._is_halted = True
            level_val = state.get("level")
            try:
                self._current_level = HaltLevel(level_val) if level_val else None
            except ValueError:
                self._current_level = HaltLevel.HARD_HALT
            self._current_reason = state.get("reason", "")
            event_id = state.get("event_id") or f"HALT_{uuid.uuid4().hex[:8].upper()}"
            ts_str = state.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(IST)
            except ValueError:
                ts = datetime.now(IST)
            event = HaltEvent(
                event_id=event_id,
                timestamp=ts,
                reason=self._current_reason,
                level=self._current_level or HaltLevel.HARD_HALT,
                triggered_by=state.get("triggered_by", "UNKNOWN"),
            )
            self._active_event = event
            self._audit_log.append(event)

        logger.warning(
            "[EmergencyHalt] Restored halted state from disk: %s / %s",
            self._current_level,
            self._current_reason,
        )
        return True

    # ── flatten confirmation token ─────────────────────────────────────────────

    def request_flatten_confirmation_token(self, operator_id: str) -> str:
        """
        Generate a one-time token required to execute FLATTEN ALL (FULL_KILL_SWITCH).
        Token expires in 60 seconds. Entirely separate from the ACK reset token.
        Raises ValueError if operator_id is empty.
        """
        if not operator_id or not operator_id.strip():
            raise ValueError("Operator ID is required to generate flatten confirmation token.")
        with self._lock:
            salt = uuid.uuid4().hex[:6]
            token_hash = hashlib.sha256(
                f"flatten:{operator_id}:{salt}:{datetime.now(IST).isoformat()}".encode()
            ).hexdigest()[:12].upper()
            token = f"FLAT-{operator_id.strip().upper()}-{token_hash}"
            self._flatten_token = token
            self._flatten_token_expiry = datetime.now(IST) + timedelta(seconds=60)
            self._flatten_token_operator = operator_id.strip()
        return token

    def _validate_and_consume_flatten_token(self, operator_id: str, token: str) -> None:
        """
        Validate the flatten confirmation token and immediately consume it (one-use).
        Raises PermissionError if the token is missing, wrong, expired, or the
        operator does not match.
        Must be called with the caller holding self._lock.
        """
        if not self._flatten_token or token != self._flatten_token:
            raise PermissionError("Invalid or missing flatten confirmation token.")
        if not operator_id or operator_id.strip() != self._flatten_token_operator:
            raise PermissionError(
                f"Operator ID mismatch for flatten token: expected '{self._flatten_token_operator}'"
            )
        if datetime.now(IST) > self._flatten_token_expiry:
            # Expired — consume it so it can't be replayed
            self._flatten_token = None
            self._flatten_token_expiry = None
            self._flatten_token_operator = None
            raise PermissionError("Flatten confirmation token has expired (60-second window).")
        # Consume
        self._flatten_token = None
        self._flatten_token_expiry = None
        self._flatten_token_operator = None

    # ── operator-facing API ───────────────────────────────────────────────────

    def stop_entry(
        self,
        reason: str,
        operator_id: str = "OPERATOR",
        **kwargs: Any,
    ) -> HaltEvent:
        """
        Operator-facing: block all new order entry while existing positions are held.
        Maps to SOFT_HALT internally.
        No confirmation token required.
        """
        return self.trigger_halt(
            reason=reason,
            level=HaltLevel.SOFT_HALT,
            triggered_by=operator_id,
            **kwargs,
        )

    def flatten_all(
        self,
        operator_id: str,
        token: str,
        reason: str = "Operator FLATTEN ALL",
        **kwargs: Any,
    ) -> HaltEvent:
        """
        Operator-facing: cancel all pending orders and close all open positions.
        Requires a valid flatten confirmation token obtained from
        request_flatten_confirmation_token() within the 60-second window.
        Raises PermissionError if the token is invalid, expired, or operator mismatches.
        Maps to FULL_KILL_SWITCH internally.
        """
        with self._lock:
            self._validate_and_consume_flatten_token(operator_id, token)
        return self.trigger_halt(
            reason=reason,
            level=HaltLevel.FULL_KILL_SWITCH,
            triggered_by=operator_id,
            **kwargs,
        )

    # ── status ────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Full internal status including raw HaltLevel value. Backward-compatible."""
        return {
            "is_halted": self._is_halted,
            "current_level": self._current_level.value if self._current_level else None,
            "current_reason": self._current_reason,
            "pending_acknowledgment": self._is_halted,
            "active_event_id": self._active_event.event_id if self._active_event else None,
            "total_halt_events": len(self._audit_log),
        }

    def get_operator_status(self) -> Dict[str, Any]:
        """
        UI-facing status. Maps internal halt levels to operator-visible labels.
        HARD_HALT is internal — it appears as None in ui_halt_type here.
        """
        ui_label: Optional[str] = None
        if self._current_level == HaltLevel.SOFT_HALT:
            ui_label = "STOP_ENTRY"
        elif self._current_level == HaltLevel.FULL_KILL_SWITCH:
            ui_label = "FLATTEN_ALL"
        # HARD_HALT → ui_label stays None (internal only)

        flatten_token_active = (
            self._flatten_token is not None
            and self._flatten_token_expiry is not None
            and datetime.now(IST) <= self._flatten_token_expiry
        )
        return {
            "is_halted": self._is_halted,
            "halt_type": ui_label,
            "reason": self._current_reason,
            "active_event_id": self._active_event.event_id if self._active_event else None,
            "flatten_token_pending": flatten_token_active,
        }

    # ── original methods (unchanged in signature and behaviour) ───────────────

    def trigger_halt(
        self,
        reason: str,
        level: HaltLevel = HaltLevel.HARD_HALT,
        triggered_by: str = "OPERATOR",
        eod_manager: Any = None,
        broker_router: Any = None,
        positions: Optional[List[Any]] = None,
    ) -> HaltEvent:
        """
        Engages the emergency kill-switch at the specified severity level.
        (signature unchanged from original)
        """
        with self._lock:
            self._is_halted = True
            self._current_level = level
            self._current_reason = reason
            now_ist = datetime.now(IST)

            event = HaltEvent(
                event_id=f"HALT_{uuid.uuid4().hex[:8].upper()}",
                timestamp=now_ist,
                reason=reason,
                level=level,
                triggered_by=triggered_by,
            )
            self._active_event = event
            self._audit_log.append(event)

        print(f"[RiskStateManager] EMERGENCY HALT ACTIVATED: {reason}")
        print(f"[EmergencyHalt] KILL-SWITCH ENGAGED: {reason} (Level: {level.value}, by: {triggered_by})")

        # Synchronize with RiskStateManager if available
        if self.risk_state_manager is not None:
            if hasattr(self.risk_state_manager, "activate_emergency_halt"):
                self.risk_state_manager.activate_emergency_halt(reason)
            elif hasattr(self.risk_state_manager, "emergency_halt"):
                self.risk_state_manager.emergency_halt = True
                self.risk_state_manager.emergency_halt_reason = reason

        # Cancel pending orders for HARD_HALT and FULL_KILL_SWITCH
        if level in (HaltLevel.HARD_HALT, HaltLevel.FULL_KILL_SWITCH) and broker_router is not None:
            if hasattr(broker_router, "cancel_all_pending_orders"):
                broker_router.cancel_all_pending_orders()
            elif hasattr(broker_router, "cancel_all_orders"):
                broker_router.cancel_all_orders()

        # Execute mass liquidation for FULL_KILL_SWITCH
        if level == HaltLevel.FULL_KILL_SWITCH:
            if eod_manager is not None and positions is not None:
                print("[EmergencyHalt] FULL_KILL_SWITCH: Initiating immediate emergency square-off.")
                from .eod_square_off import SquareOffReason
                eod_manager.execute_square_off(
                    positions=positions,
                    reason=SquareOffReason.EMERGENCY_HALT,
                    broker_router=broker_router,
                )

        # Persist state after every halt
        self.persist_halt_state()

        return event

    def request_acknowledgment_token(self, operator_id: str) -> str:
        """
        Generates a one-time cryptographic authorization token required to reset the halt.
        (signature and behaviour unchanged from original)
        """
        if not self._is_halted:
            raise ValueError("Cannot request acknowledgment token: System is not halted.")
        if not operator_id or not operator_id.strip():
            raise ValueError("Operator ID is required to generate acknowledgment token.")

        salt = uuid.uuid4().hex[:6]
        token_hash = hashlib.sha256(
            f"{operator_id}:{salt}:{datetime.now(IST).isoformat()}".encode()
        ).hexdigest()[:12].upper()
        token = f"ACK-{operator_id.strip().upper()}-{token_hash}"
        with self._lock:
            self._active_ack_token = token
            self._active_token_operator = operator_id.strip()
        return token

    def acknowledge_and_reset(
        self,
        operator_id: str,
        token: str,
        resolution_notes: str = "",
    ) -> bool:
        """
        Validates operator authorization token, logs resolution notes, and resets the kill-switch.
        (signature and behaviour unchanged from original)
        """
        if not self._is_halted:
            return True

        if not token or token != self._active_ack_token:
            raise PermissionError(f"Invalid or expired acknowledgment token: '{token}'")

        if not operator_id or operator_id.strip() != self._active_token_operator:
            raise PermissionError(
                f"Operator ID mismatch for token: expected '{self._active_token_operator}'"
            )

        now_ist = datetime.now(IST)
        with self._lock:
            if self._active_event is not None:
                self._active_event.acknowledged = True
                self._active_event.acknowledged_by = operator_id
                self._active_event.acknowledged_at = now_ist
                self._active_event.resolution_notes = resolution_notes

            self._is_halted = False
            self._current_level = None
            self._current_reason = ""
            self._active_ack_token = None
            self._active_token_operator = None

        if self.risk_state_manager is not None:
            if hasattr(self.risk_state_manager, "acknowledge_emergency_halt"):
                self.risk_state_manager.acknowledge_emergency_halt(operator_id)
            elif hasattr(self.risk_state_manager, "emergency_halt"):
                self.risk_state_manager.emergency_halt = False
                self.risk_state_manager.emergency_halt_reason = ""

        # Persist the cleared state
        self.persist_halt_state()

        print(f"[EmergencyHalt] Operator '{operator_id}' acknowledged and reset halt. Notes: {resolution_notes}")
        return True

    def get_audit_log(self) -> List[HaltEvent]:
        return list(self._audit_log)


# ── Global singleton ──────────────────────────────────────────────────────────
# Uses disk persistence by default. Tests create their own instances with db_path=None.

# STRIKE_DESK_HALT_FILE lets pytest (tests/conftest.py) keep halts out of the live state.
emergency_halt_manager = EmergencyHaltManager(
    db_path=os.environ.get("STRIKE_DESK_HALT_FILE") or _DEFAULT_STATE_PATH
)
