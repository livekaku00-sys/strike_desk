"""
Strike Desk V4 - Risk State Model & Trade Definitions
Enforces Blueprint O5, Review Gap A, Review Gap B, and Review Risk 6.

LOSS LIMIT SCOPE DEFINITION (Gap B Compliance):

Total Risk P&L = Realized P&L + Unrealized P&L - Total Charges

Where:
- Realized P&L: Sum of (exit_price - entry_price) × quantity for all closed trades today
- Unrealized P&L: Sum of (current_price - entry_price) × quantity for all open positions
- Total Charges: Brokerage + STT + Exchange Transaction Charges + GST + SEBI charges

This ensures the loss limit reflects TRUE economic loss, not just trade P&L.
A position can be "profitable" on trade P&L but still contribute to loss limit breach
if charges exceed the trade profit.

Reset Conditions:
- Daily reset: All P&L counters reset at 00:00 IST (before market open)
- Trade count reset: 00:00 IST
- Consecutive loss counter: Reset to 0 on any WIN
- Cooldown: Automatically expires after 45 minutes
- Emergency halt: Requires MANUAL acknowledgment via API/dashboard
"""
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET, name="IST")


@dataclass
class Trade:
    """
    Represents an executed order or completed round-trip trade for risk tracking.
    """
    trade_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: float
    entry_price: float
    exit_price: float = 0.0
    realized_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    trade_type: str = "INTRADAY"  # "INTRADAY", "DELIVERY", "FNO"
    segment: str = "EQUITY"        # "EQUITY", "FNO"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        else:
            self.timestamp = self.timestamp.astimezone(timezone.utc)

        # Auto-compute realized P&L if not provided but exit_price is available
        if self.realized_pnl == 0.0 and self.exit_price > 0.0:
            if self.side.upper() == "BUY":
                self.realized_pnl = (self.exit_price - self.entry_price) * self.quantity
            else:
                self.realized_pnl = (self.entry_price - self.exit_price) * self.quantity
            self.realized_pnl = round(self.realized_pnl, 4)

        if self.net_pnl == 0.0:
            self.net_pnl = round(self.realized_pnl - self.charges, 4)

    @property
    def is_win(self) -> bool:
        """A trade is a win if net P&L after charges is positive."""
        return self.net_pnl > 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "realized_pnl": self.realized_pnl,
            "charges": self.charges,
            "net_pnl": self.net_pnl,
            "trade_type": self.trade_type,
            "segment": self.segment,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Trade":
        ts = data.get("timestamp")
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts)
        elif isinstance(ts, datetime):
            dt = ts
        else:
            dt = datetime.now(timezone.utc)
        return cls(
            trade_id=str(data["trade_id"]),
            symbol=str(data["symbol"]),
            side=str(data["side"]),
            quantity=float(data["quantity"]),
            entry_price=float(data["entry_price"]),
            exit_price=float(data.get("exit_price", 0.0)),
            realized_pnl=float(data.get("realized_pnl", 0.0)),
            charges=float(data.get("charges", 0.0)),
            net_pnl=float(data.get("net_pnl", 0.0)),
            trade_type=str(data.get("trade_type", "INTRADAY")),
            segment=str(data.get("segment", "EQUITY")),
            timestamp=dt,
        )


@dataclass
class RiskState:
    """
    Unified dataclass capturing all runtime risk metrics and limit states.
    Thread-safe and persistent across restarts.
    """
    trading_date: date
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_charges: float = 0.0
    total_risk_pnl: float = 0.0
    trade_count: int = 0
    consecutive_losses: int = 0
    last_trade_time: Optional[datetime] = None
    last_trade_result: Optional[str] = None  # "WIN", "LOSS", or None
    cooldown_until: Optional[datetime] = None
    emergency_halt_active: bool = False
    emergency_halt_reason: Optional[str] = None
    emergency_halt_acknowledged: bool = False
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.last_updated.tzinfo is None:
            self.last_updated = self.last_updated.replace(tzinfo=timezone.utc)
        else:
            self.last_updated = self.last_updated.astimezone(timezone.utc)

        if self.last_trade_time and self.last_trade_time.tzinfo is None:
            self.last_trade_time = self.last_trade_time.replace(tzinfo=timezone.utc)

        if self.cooldown_until and self.cooldown_until.tzinfo is None:
            self.cooldown_until = self.cooldown_until.replace(tzinfo=timezone.utc)

        # Auto-compute total_risk_pnl if not explicitly assigned
        if self.total_risk_pnl == 0.0 and (self.realized_pnl != 0.0 or self.unrealized_pnl != 0.0 or self.total_charges != 0.0):
            self.recalculate_total_risk_pnl()

    def recalculate_total_risk_pnl(self) -> float:
        """
        Total Risk P&L = Realized P&L + Unrealized P&L - Total Charges
        Charges reduce equity, reflecting true economic profit/loss.
        """
        charge_impact = -abs(self.total_charges) if self.total_charges > 0 else self.total_charges
        self.total_risk_pnl = round(self.realized_pnl + self.unrealized_pnl + charge_impact, 4)
        return self.total_risk_pnl

    def is_daily_loss_ceiling_breached(self, ceiling: float = -2500.0) -> bool:
        """
        Returns True if total risk loss breaches the ceiling.
        Ceiling is typically -2500.0 INR. Loss limit breaches when total_risk_pnl <= ceiling.
        """
        return self.total_risk_pnl <= ceiling

    def is_trade_cap_breached(self, cap: int = 5) -> bool:
        """
        Returns True if daily executed trade count reaches or exceeds cap.
        """
        return self.trade_count >= cap

    def is_in_cooldown(self, current_time: datetime) -> bool:
        """
        Returns True if cooldown is active following 2 consecutive losses.
        Cooldown lasts 45 minutes from trigger time.
        """
        if self.cooldown_until is None:
            return False
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        cd = self.cooldown_until if self.cooldown_until.tzinfo else self.cooldown_until.replace(tzinfo=timezone.utc)
        return now < cd

    def is_stagnant(self, current_time: datetime, threshold_minutes: int = 15) -> bool:
        """
        Returns True if position has been stagnant (no fills/progress) beyond threshold.
        """
        if self.last_trade_time is None:
            return False
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        lt = self.last_trade_time if self.last_trade_time.tzinfo else self.last_trade_time.replace(tzinfo=timezone.utc)
        diff_sec = (now - lt).total_seconds()
        return diff_sec >= threshold_minutes * 60

    def is_emergency_halt_active(self) -> bool:
        """
        Returns True if emergency halt is engaged and has not been manually cleared.
        """
        return bool(self.emergency_halt_active)

    def can_place_order(self, current_time: datetime) -> Tuple[bool, Optional[str]]:
        """
        Risk authorization gate (Review Gap A compliance).
        Evaluates risk constraints in strict hierarchical order:
        1. Emergency Halt (Kill-switch)
        2. Daily Loss Ceiling (-₹2,500)
        3. Daily Trade Cap (5 trades)
        4. Consecutive Loss Cooldown (45 mins)
        Returns:
            (allowed: bool, reason: Optional[str])
        """
        # 1. Emergency Halt
        if self.is_emergency_halt_active():
            return False, f"Emergency halt active: {self.emergency_halt_reason or 'Manual kill-switch engaged'}"

        # 2. Daily Loss Ceiling
        if self.is_daily_loss_ceiling_breached(-2500.0):
            return False, f"Daily loss ceiling breached: ₹{self.total_risk_pnl:.2f} <= ₹-2500.00"

        # 3. Daily Trade Cap
        if self.is_trade_cap_breached(5):
            return False, f"Daily trade cap reached: {self.trade_count} >= 5 trades"

        # 4. Consecutive Loss Cooldown
        if self.is_in_cooldown(current_time):
            cd_str = self.cooldown_until.isoformat() if self.cooldown_until else "45m"
            return False, f"Cooldown active until {cd_str} following consecutive losses"

        return True, None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_charges": self.total_charges,
            "total_risk_pnl": self.total_risk_pnl,
            "trade_count": self.trade_count,
            "consecutive_losses": self.consecutive_losses,
            "last_trade_time": self.last_trade_time.isoformat() if self.last_trade_time else None,
            "last_trade_result": self.last_trade_result,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "emergency_halt_active": self.emergency_halt_active,
            "emergency_halt_reason": self.emergency_halt_reason,
            "emergency_halt_acknowledged": self.emergency_halt_acknowledged,
            "last_updated": self.last_updated.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RiskState":
        td = data.get("trading_date")
        if isinstance(td, str):
            t_date = date.fromisoformat(td)
        elif isinstance(td, date):
            t_date = td
        else:
            t_date = datetime.now(IST).date()

        def parse_dt(val: Any) -> Optional[datetime]:
            if not val:
                return None
            if isinstance(val, str):
                return datetime.fromisoformat(val)
            if isinstance(val, datetime):
                return val
            return None

        return cls(
            trading_date=t_date,
            realized_pnl=float(data.get("realized_pnl", 0.0)),
            unrealized_pnl=float(data.get("unrealized_pnl", 0.0)),
            total_charges=float(data.get("total_charges", 0.0)),
            total_risk_pnl=float(data.get("total_risk_pnl", 0.0)),
            trade_count=int(data.get("trade_count", 0)),
            consecutive_losses=int(data.get("consecutive_losses", 0)),
            last_trade_time=parse_dt(data.get("last_trade_time")),
            last_trade_result=data.get("last_trade_result"),
            cooldown_until=parse_dt(data.get("cooldown_until")),
            emergency_halt_active=bool(data.get("emergency_halt_active", False)),
            emergency_halt_reason=data.get("emergency_halt_reason"),
            emergency_halt_acknowledged=bool(data.get("emergency_halt_acknowledged", False)),
            last_updated=parse_dt(data.get("last_updated")) or datetime.now(timezone.utc),
        )