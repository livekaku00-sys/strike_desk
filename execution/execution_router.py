"""
Strike Desk V4 - Selective Execution Gate Router
Enforces Blueprint Rule 9: "Signal generation is universal; execution is selective."
Ensures only signals matching the user-selected active asset class route to execution.
Non-selected classes produce alerts/displays only, with zero orders routed to broker.
"""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from execution.audit_logger import AuditLogger, audit_logger
from models.instrument import AssetClass
from models.signal import Signal

logger = logging.getLogger(__name__)


class ExecutionRouter:
    """
    Bulletproof selective execution gate router.
    Decides with zero ambiguity whether a generated signal routes to the broker or remains alert-only.
    """

    def __init__(self, logger_instance: Optional[AuditLogger] = None) -> None:
        self.audit = logger_instance or audit_logger

    def should_execute(
        self,
        signal: Signal,
        selected_class: AssetClass,
        current_time: Optional[datetime] = None,
    ) -> bool:
        """
        Returns True ONLY if signal.asset_class matches selected_class,
        signal is not expired, and signal is actionable.
        Returns False for all non-selected classes and expired signals.
        """
        if signal is None:
            return False

        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Check expiry
        if signal.is_expired(now):
            return False

        # Check actionability (Tier C is never actionable)
        if not signal.is_actionable(now):
            return False

        # Strict selective gate: only the selected asset class can execute
        will_execute = signal.asset_class == selected_class
        sig_class = signal.asset_class.value if hasattr(signal.asset_class, "value") else str(signal.asset_class)
        sel_class  = selected_class.value  if hasattr(selected_class,  "value") else str(selected_class)
        gate_status = "EXECUTING" if will_execute else "ALERT_ONLY"
        logger.debug(
            "[GATE] Signal %s for %s → Status: %s (selected=%s)",
            signal.signal_id[:8], sig_class, gate_status, sel_class,
        )
        return will_execute

    def route_signal(
        self,
        signal: Signal,
        selected_class: AssetClass,
        current_time: Optional[datetime] = None,
        cascade_brake_active: bool = False,
    ) -> Dict[str, Any]:
        """
        Evaluates a signal against the selective execution gate.
        Returns:
            {
                "action": "execute" | "alert_only",
                "reason": str,
                "signal": Signal,
                "position_size_multiplier": float
            }
        """
        if signal is None:
            return {
                "action": "alert_only",
                "reason": "Signal is None",
                "signal": None,
                "position_size_multiplier": 0.0,
            }

        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # 1. Expiry Check
        if signal.is_expired(now):
            reason = "Signal expired"
            self.audit.log_signal(signal, "alert_only", reason)
            return {
                "action": "alert_only",
                "reason": reason,
                "signal": signal,
                "position_size_multiplier": 0.0,
            }

        # 2. Selective Gate: Match against user-selected category
        if signal.asset_class == selected_class:
            multiplier = 0.5 if cascade_brake_active else 1.0
            reason = (
                "Selected asset class matched (cascade brake: 50% size reduction)"
                if cascade_brake_active
                else "Selected asset class matched"
            )
            self.audit.log_signal(signal, "execute", reason)
            return {
                "action": "execute",
                "reason": reason,
                "signal": signal,
                "position_size_multiplier": multiplier,
            }
        else:
            reason = "Non-selected asset class"
            self.audit.log_signal(signal, "alert_only", reason)
            return {
                "action": "alert_only",
                "reason": reason,
                "signal": signal,
                "position_size_multiplier": 0.0,
            }

    def route_signals(
        self,
        signals: List[Signal],
        selected_class: AssetClass,
        current_time: Optional[datetime] = None,
        cascade_brake_active: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Batch-routes a list of signals.
        """
        return [
            self.route_signal(
                sig,
                selected_class,
                current_time=current_time,
                cascade_brake_active=cascade_brake_active,
            )
            for sig in signals
        ]


# Global singleton instance
execution_router = ExecutionRouter()
