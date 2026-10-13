"""
Strike Desk V4 - Circuit Breakers & Cooldown Engine
Enforces Blueprint O5, Review Gap A, and Review Risk 6:
- Daily loss ceiling circuit breaker (₹2,500)
- Daily trade cap circuit breaker (5 trades)
- 45-minute cooldown circuit breaker after 2 consecutive losses
- 15-minute stagnation exit trigger
- Emergency halt kill-switch
- Tick-level P&L monitoring & tripping
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from models.risk_state import RiskState, Trade
from risk.risk_state_manager import RiskStateManager, risk_state_manager
from risk.stagnation_monitor import StagnationAlert, StagnationMonitor, stagnation_monitor

logger = logging.getLogger(__name__)


class BreakerStatus(str, Enum):
    NORMAL = "NORMAL"
    IN_COOLDOWN = "IN_COOLDOWN"
    LOSS_CEILING_TRIPPED = "LOSS_CEILING_TRIPPED"
    TRADE_CAP_TRIPPED = "TRADE_CAP_TRIPPED"
    EMERGENCY_HALTED = "EMERGENCY_HALTED"


@dataclass
class CircuitBreakerConfig:
    """Configurable limits for the Strike Desk V4 Risk Engine."""
    daily_loss_ceiling: float = -2500.0
    daily_trade_cap: int = 5
    consecutive_loss_threshold: int = 2
    cooldown_duration_minutes: int = 45
    stagnation_timeout_minutes: int = 15


class CircuitBreaker:
    """
    Central coordinator of circuit breaker safety gates and cooldown tracking.
    Guarantees that no order executes when safety conditions are violated.
    """

    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        state_manager: Optional[RiskStateManager] = None,
        stag_mon: Optional[StagnationMonitor] = None,
    ) -> None:
        self.config = config or CircuitBreakerConfig()
        self.state_manager = state_manager or risk_state_manager
        self.stagnation_monitor = stag_mon or stagnation_monitor
        self._lock = threading.RLock()

    def get_status(self, current_time: Optional[datetime] = None) -> BreakerStatus:
        """
        Evaluates current risk state and returns active breaker status.
        Priority: EMERGENCY_HALTED -> LOSS_CEILING_TRIPPED -> TRADE_CAP_TRIPPED -> IN_COOLDOWN -> NORMAL.
        """
        with self._lock:
            now = current_time or datetime.now(timezone.utc)
            state = self.state_manager.get_state()

            # 1. Emergency Halt
            if state.is_emergency_halt_active():
                return BreakerStatus.EMERGENCY_HALTED

            # 2. Daily Loss Ceiling (-₹2,500)
            if state.is_daily_loss_ceiling_breached(self.config.daily_loss_ceiling):
                return BreakerStatus.LOSS_CEILING_TRIPPED

            # 3. Daily Trade Cap (5 trades)
            if state.is_trade_cap_breached(self.config.daily_trade_cap):
                return BreakerStatus.TRADE_CAP_TRIPPED

            # 4. Consecutive Loss Cooldown (45 mins)
            if state.is_in_cooldown(now):
                return BreakerStatus.IN_COOLDOWN

            return BreakerStatus.NORMAL

    def can_trade(self, current_time: Optional[datetime] = None) -> Tuple[bool, Optional[str]]:
        """
        Determines if order placement is permitted.
        Returns (allowed: bool, reason: Optional[str]).
        """
        with self._lock:
            now = current_time or datetime.now(timezone.utc)
            status = self.get_status(now)
            state = self.state_manager.get_state()

            if status == BreakerStatus.EMERGENCY_HALTED:
                return False, f"Emergency halt active: {state.emergency_halt_reason or 'Kill-switch triggered'}"
            if status == BreakerStatus.LOSS_CEILING_TRIPPED:
                return False, f"Daily loss ceiling breached: ₹{state.total_risk_pnl:.2f} <= ₹{self.config.daily_loss_ceiling:.2f}"
            if status == BreakerStatus.TRADE_CAP_TRIPPED:
                return False, f"Daily trade cap reached: {state.trade_count} >= {self.config.daily_trade_cap} trades"
            if status == BreakerStatus.IN_COOLDOWN:
                cd_str = state.cooldown_until.isoformat() if state.cooldown_until else "45m"
                return False, f"In cooldown until {cd_str} following {state.consecutive_losses} consecutive losses"

            return True, None

    def on_tick(
        self, symbol: str, price: float, current_time: Optional[datetime] = None
    ) -> Tuple[float, BreakerStatus, bool]:
        """
        Tick-level circuit breaker check (Review Risk 6 compliance).
        Updates mark-to-market P&L and immediately trips breaker if loss ceiling is breached.
        Returns:
            (new_total_risk_pnl: float, breaker_status: BreakerStatus, is_loss_ceiling_tripped: bool)
        """
        with self._lock:
            now = current_time or datetime.now(timezone.utc)
            total_risk = self.state_manager.update_position_pnl(symbol, price)
            self.stagnation_monitor.update_price(symbol, price, now)

            status = self.get_status(now)
            is_loss_tripped = (status == BreakerStatus.LOSS_CEILING_TRIPPED)

            if is_loss_tripped:
                logger.critical(
                    "[CircuitBreaker] TICK BREACH: %s price=₹%.2f pushed Total Risk P&L to ₹%.2f. LOSS CEILING TRIPPED!",
                    symbol, price, total_risk,
                )

            return total_risk, status, is_loss_tripped

    def on_trade_completed(self, trade: Trade) -> BreakerStatus:
        """
        Processes completed trade, updates trade count, checks cooldown, and returns new status.
        """
        with self._lock:
            self.state_manager.record_trade(trade)
            # Remove position from stagnation tracker if position is closed
            self.stagnation_monitor.remove_position(trade.symbol)
            return self.get_status(trade.timestamp)

    def check_stagnant_positions(
        self, current_time: Optional[datetime] = None
    ) -> List[StagnationAlert]:
        """
        Delegates to stagnation monitor to find positions open >= 15 minutes.
        """
        now = current_time or datetime.now(timezone.utc)
        return self.stagnation_monitor.check_stagnant_positions(
            current_time=now,
            threshold_minutes=self.config.stagnation_timeout_minutes,
        )

    def activate_emergency_halt(self, reason: str) -> None:
        """Engages platform-wide emergency halt."""
        self.state_manager.activate_emergency_halt(reason)

    def acknowledge_emergency_halt(self) -> None:
        """Operator acknowledgment clearing emergency halt."""
        self.state_manager.acknowledge_emergency_halt()

    def reset_for_tests(self, test_date: Optional[Any] = None) -> None:
        """Resets all breakers and state manager for test execution."""
        with self._lock:
            self.state_manager.reset_for_tests(test_date=test_date)
            self.stagnation_monitor.clear()


# Global singleton instance
circuit_breaker = CircuitBreaker()
