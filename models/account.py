from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class Balance:
    broker: str
    available_cash: float = 0.0
    used_margin: float = 0.0
    total_equity: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    currency: str = "INR"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        else:
            self.timestamp = self.timestamp.astimezone(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass(slots=True)
class Position:
    broker: str
    symbol: str
    exchange: str
    product_type: str = "INTRADAY"
    qty: float = 0.0             # Net quantity
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    avg_price: float = 0.0
    current_price: float = 0.0
    pnl: float = 0.0
    pnl_percent: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    is_open: bool = True
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        else:
            self.timestamp = self.timestamp.astimezone(timezone.utc)
        self.is_open = (self.qty != 0.0)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass(slots=True)
class Account:
    broker: str
    client_id: str
    balance: Balance
    positions: List[Position] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None:
            self.updated_at = self.updated_at.replace(tzinfo=timezone.utc)
        else:
            self.updated_at = self.updated_at.astimezone(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "broker": self.broker,
            "client_id": self.client_id,
            "balance": self.balance.to_dict(),
            "positions": [p.to_dict() for p in self.positions],
            "updated_at": self.updated_at.isoformat(),
        }