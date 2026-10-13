"""
Strike Desk V4 - EOD Square-Off Subsystem
Blueprint O5: 15:10 IST hard square-off
Review Risk Areas: Failed square-off handling, retry backoff, emergency escalation
"""

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from enum import Enum
from typing import Any, Dict, List, Optional
import pytz

logger = logging.getLogger("EODSquareOff")
IST = pytz.timezone("Asia/Kolkata")


class SquareOffStatus(str, Enum):
    IDLE = "IDLE"
    TRIGGERED = "TRIGGERED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class SquareOffReason(str, Enum):
    EOD_1510_CUTOFF = "EOD_1510_CUTOFF"
    EMERGENCY_HALT = "EMERGENCY_HALT"
    LOSS_CEILING = "LOSS_CEILING"
    STAGNATION = "STAGNATION"
    MANUAL = "MANUAL"


@dataclass
class SquareOffOrder:
    position_id: str
    symbol: str
    quantity: int
    exit_side: str  # "SELL" for long position, "BUY" for short position
    status: str = "PENDING"  # PENDING, SENT, FILLED, REJECTED, FAILED
    attempts: int = 0
    max_attempts: int = 3
    last_error: Optional[str] = None
    order_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(IST))
    executed_at: Optional[datetime] = None


@dataclass
class SquareOffResult:
    status: SquareOffStatus
    reason: SquareOffReason
    total_positions: int
    closed_positions: int
    failed_positions: int
    orders: List[SquareOffOrder] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(IST))


@dataclass
class EODSquareOffConfig:
    cutoff_time_str: str = "15:10:00"  # 15:10 IST hard cutoff
    warning_time_str: str = "15:05:00"  # 15:05 IST warning window
    max_retries: int = 3
    retry_delay: float = 0.05
    escalate_to_emergency_halt_on_failure: bool = True


class EODSquareOffManager:
    """
    Manages End-Of-Day (EOD) hard square-off at 15:10 IST and emergency mass liquidations.
    """
    def __init__(self, config: Optional[EODSquareOffConfig] = None, emergency_halt_manager=None):
        self.config = config or EODSquareOffConfig()
        self.emergency_halt_manager = emergency_halt_manager
        self.status = SquareOffStatus.IDLE
        self.last_result: Optional[SquareOffResult] = None
        self.failed_square_offs: List[SquareOffOrder] = []

    def _parse_ist_time(self, time_str: str) -> dtime:
        parts = [int(p) for p in time_str.split(":")]
        return dtime(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)

    def is_past_cutoff(self, current_time: Optional[datetime] = None) -> bool:
        """Checks if the given time (or current IST time) is >= 15:10:00 IST."""
        if current_time is None:
            current_time = datetime.now(IST)
        elif current_time.tzinfo is None:
            current_time = IST.localize(current_time)
        else:
            current_time = current_time.astimezone(IST)

        cutoff = self._parse_ist_time(self.config.cutoff_time_str)
        return current_time.time() >= cutoff

    def is_warning_window(self, current_time: Optional[datetime] = None) -> bool:
        """Checks if current time is between 15:05 and 15:10 IST."""
        if current_time is None:
            current_time = datetime.now(IST)
        elif current_time.tzinfo is None:
            current_time = IST.localize(current_time)
        else:
            current_time = current_time.astimezone(IST)

        warning = self._parse_ist_time(self.config.warning_time_str)
        cutoff = self._parse_ist_time(self.config.cutoff_time_str)
        return warning <= current_time.time() < cutoff

    def execute_square_off(
        self,
        positions: List[Any],
        reason: SquareOffReason = SquareOffReason.EOD_1510_CUTOFF,
        broker_router: Any = None,
    ) -> SquareOffResult:
        """
        Executes immediate square-off for all open positions with retry and escalation logic.
        """
        self.status = SquareOffStatus.EXECUTING
        orders: List[SquareOffOrder] = []
        errors: List[str] = []

        open_positions = []
        for pos in positions:
            qty = getattr(pos, "quantity", 0)
            if qty == 0 and hasattr(pos, "qty"):
                qty = getattr(pos, "qty", 0)
            if qty != 0:
                open_positions.append(pos)

        if not open_positions:
            self.status = SquareOffStatus.COMPLETED
            res = SquareOffResult(
                status=SquareOffStatus.COMPLETED,
                reason=reason,
                total_positions=0,
                closed_positions=0,
                failed_positions=0,
                orders=[],
            )
            self.last_result = res
            return res

        for pos in open_positions:
            pos_id = getattr(pos, "position_id", getattr(pos, "id", "POS_UNKNOWN"))
            sym = getattr(pos, "symbol", getattr(pos, "trading_symbol", "UNKNOWN"))
            qty = getattr(pos, "quantity", getattr(pos, "qty", 0))

            exit_side = "SELL" if qty > 0 else "BUY"
            abs_qty = abs(qty)

            order = SquareOffOrder(
                position_id=pos_id,
                symbol=sym,
                quantity=abs_qty,
                exit_side=exit_side,
                max_attempts=self.config.max_retries,
            )

            success = False
            for attempt in range(1, self.config.max_retries + 1):
                order.attempts = attempt
                try:
                    if broker_router is not None:
                        resp = broker_router.place_exit_order(
                            position_id=pos_id,
                            symbol=sym,
                            quantity=abs_qty,
                            side=exit_side,
                        )
                        if isinstance(resp, dict) and resp.get("status") == "REJECTED":
                            raise RuntimeError(resp.get("reason", "Broker order rejected"))
                    
                    order.status = "FILLED"
                    order.executed_at = datetime.now(IST)
                    success = True
                    break
                except Exception as exc:
                    order.last_error = str(exc)
                    if attempt < self.config.max_retries:
                        time.sleep(self.config.retry_delay)

            if success:
                if hasattr(pos, "quantity"):
                    pos.quantity = 0
                if hasattr(pos, "is_open"):
                    pos.is_open = False
            else:
                order.status = "FAILED"
                errors.append(f"Position {pos_id} ({sym}) failed after {order.attempts} attempts: {order.last_error}")
                self.failed_square_offs.append(order)

            orders.append(order)

        closed = sum(1 for o in orders if o.status == "FILLED")
        failed = sum(1 for o in orders if o.status == "FAILED")

        if failed == 0:
            final_status = SquareOffStatus.COMPLETED
            print(f"[EODSquareOff] All {closed} open positions successfully squared off ({reason.value}).")
        elif closed > 0:
            final_status = SquareOffStatus.PARTIAL
            print(f"[EODSquareOff] WARNING: Partial square-off: {closed} closed, {failed} failed.")
        else:
            final_status = SquareOffStatus.FAILED
            print(f"[EODSquareOff] CRITICAL: Square-off failed for all {failed} positions!")

        self.status = final_status
        result = SquareOffResult(
            status=final_status,
            reason=reason,
            total_positions=len(open_positions),
            closed_positions=closed,
            failed_positions=failed,
            orders=orders,
            errors=errors,
        )
        self.last_result = result

        # Escalate to emergency halt if any square-off fails
        if failed > 0 and self.config.escalate_to_emergency_halt_on_failure:
            halt_reason = f"CRITICAL: Failed EOD square-off on {failed} position(s): {', '.join(errors)}"
            if self.emergency_halt_manager is not None:
                self.emergency_halt_manager.trigger_halt(
                    reason=halt_reason,
                    triggered_by="EOD_SQUARE_OFF",
                )

        return result

    def check_and_trigger(
        self,
        positions: List[Any],
        broker_router: Any = None,
        current_time: Optional[datetime] = None,
    ) -> Optional[SquareOffResult]:
        if self.is_past_cutoff(current_time):
            print("[EODSquareOff] 15:10 IST hard cutoff reached. Initiating EOD square-off.")
            return self.execute_square_off(
                positions=positions,
                reason=SquareOffReason.EOD_1510_CUTOFF,
                broker_router=broker_router,
            )
        return None


eod_square_off_manager = EODSquareOffManager()
