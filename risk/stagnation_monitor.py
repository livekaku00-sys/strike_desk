"""
Strike Desk V4 - 15-Minute Stagnation Exit Monitor
Enforces Blueprint O5:
- 15-minute stagnation exit: Identifies positions open for >= 15 minutes without progress
  and triggers protective square-off recommendations to prevent capital tie-up.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StagnantPositionRecord:
    """Tracks position entry, duration, and price history."""
    symbol: str
    entry_time: datetime
    entry_price: float
    quantity: float
    current_price: float
    last_move_time: datetime
    highest_price: float
    lowest_price: float

    @property
    def unrealized_pnl(self) -> float:
        if self.quantity > 0:
            return round((self.current_price - self.entry_price) * self.quantity, 4)
        else:
            return round((self.entry_price - self.current_price) * abs(self.quantity), 4)

    def elapsed_minutes(self, current_time: datetime) -> float:
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        et = self.entry_time if self.entry_time.tzinfo else self.entry_time.replace(tzinfo=timezone.utc)
        return max(0.0, (now - et).total_seconds() / 60.0)


@dataclass
class StagnationAlert:
    """Alert issued when a position violates the 15-minute stagnation ceiling."""
    symbol: str
    entry_time: datetime
    entry_price: float
    current_price: float
    quantity: float
    elapsed_minutes: float
    unrealized_pnl: float
    reason: str
    recommended_action: str = "SQUARE_OFF"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "quantity": self.quantity,
            "elapsed_minutes": round(self.elapsed_minutes, 2),
            "unrealized_pnl": self.unrealized_pnl,
            "reason": self.reason,
            "recommended_action": self.recommended_action,
            "timestamp": self.timestamp.isoformat(),
        }


class StagnationMonitor:
    """
    Monitors all open positions for stagnation beyond the 15-minute limit.
    Thread-safe and deterministic.
    """

    DEFAULT_STAGNATION_TIMEOUT_MINUTES: float = 15.0

    def __init__(self, timeout_minutes: float = DEFAULT_STAGNATION_TIMEOUT_MINUTES) -> None:
        self.timeout_minutes = timeout_minutes
        self._positions: Dict[str, StagnantPositionRecord] = {}
        self._lock = threading.RLock()

    def register_position(
        self,
        symbol: str,
        entry_time: datetime,
        entry_price: float,
        quantity: float,
    ) -> None:
        """Registers a new or updated open position to monitor."""
        with self._lock:
            sym = symbol.strip().upper()
            t = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
            self._positions[sym] = StagnantPositionRecord(
                symbol=sym,
                entry_time=t,
                entry_price=float(entry_price),
                quantity=float(quantity),
                current_price=float(entry_price),
                last_move_time=t,
                highest_price=float(entry_price),
                lowest_price=float(entry_price),
            )
            logger.info("[StagnationMonitor] Tracking position %s (qty: %.2f @ ₹%.2f)", sym, quantity, entry_price)

    def update_price(self, symbol: str, current_price: float, current_time: datetime) -> None:
        """Updates price and logs movement."""
        with self._lock:
            sym = symbol.strip().upper()
            pos = self._positions.get(sym)
            if pos is None:
                return

            p = float(current_price)
            t = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)

            pos.current_price = p
            pos.highest_price = max(pos.highest_price, p)
            pos.lowest_price = min(pos.lowest_price, p)

            # Check if price moved more than 0.25% from entry
            price_change_pct = abs(p - pos.entry_price) / pos.entry_price * 100.0 if pos.entry_price > 0 else 0.0
            if price_change_pct > 0.25:
                pos.last_move_time = t

    def remove_position(self, symbol: str) -> None:
        """Stops tracking when position is closed."""
        with self._lock:
            sym = symbol.strip().upper()
            self._positions.pop(sym, None)

    def is_position_stagnant(
        self, symbol: str, current_time: datetime, threshold_minutes: Optional[float] = None
    ) -> bool:
        """Checks if a specific position has exceeded the stagnation threshold."""
        with self._lock:
            sym = symbol.strip().upper()
            pos = self._positions.get(sym)
            if pos is None:
                return False
            limit = threshold_minutes or self.timeout_minutes
            return pos.elapsed_minutes(current_time) >= limit

    def check_stagnant_positions(
        self, current_time: datetime, threshold_minutes: Optional[float] = None
    ) -> List[StagnationAlert]:
        """
        Scans all tracked positions and generates alerts for those exceeding stagnation limit.
        """
        limit = threshold_minutes or self.timeout_minutes
        alerts: List[StagnationAlert] = []

        with self._lock:
            t = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
            for sym, pos in self._positions.items():
                elapsed = pos.elapsed_minutes(t)
                if elapsed >= limit:
                    alert = StagnationAlert(
                        symbol=sym,
                        entry_time=pos.entry_time,
                        entry_price=pos.entry_price,
                        current_price=pos.current_price,
                        quantity=pos.quantity,
                        elapsed_minutes=elapsed,
                        unrealized_pnl=pos.unrealized_pnl,
                        reason=f"Position stagnant for {elapsed:.1f}m exceeding {limit:.0f}m ceiling (Blueprint O5)",
                        recommended_action="SQUARE_OFF",
                        timestamp=t,
                    )
                    alerts.append(alert)
                    logger.warning("[StagnationMonitor] Stagnation alert triggered: %s", alert.reason)

        return alerts

    def clear(self) -> None:
        """Clears all tracked positions."""
        with self._lock:
            self._positions.clear()


# Global singleton instance
stagnation_monitor = StagnationMonitor()
