"""
Strike Desk V4 - Trading Signal Data Models & Enums
Defines Signal, SignalType, Direction, and ConfidenceTier.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

from models.instrument import AssetClass, Exchange


class SignalType(str, Enum):
    EMA9_CROSSOVER_BULLISH = "EMA9_CROSSOVER_BULLISH"
    EMA9_CROSSOVER_BEARISH = "EMA9_CROSSOVER_BEARISH"
    VWAP_DEVIATION_LONG = "VWAP_DEVIATION_LONG"
    VWAP_DEVIATION_SHORT = "VWAP_DEVIATION_SHORT"
    PCR_SLOPE_BULLISH = "PCR_SLOPE_BULLISH"
    PCR_SLOPE_BEARISH = "PCR_SLOPE_BEARISH"
    SENTIMENT_BULLISH = "SENTIMENT_BULLISH"
    SENTIMENT_BEARISH = "SENTIMENT_BEARISH"
    COMPOSITE_BULLISH = "COMPOSITE_BULLISH"
    COMPOSITE_BEARISH = "COMPOSITE_BEARISH"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ConfidenceTier(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"

    @classmethod
    def from_score(cls, score: float) -> "ConfidenceTier":
        if score >= 85.0:
            return cls.A_PLUS
        elif score >= 70.0:
            return cls.A
        elif score >= 55.0:
            return cls.B
        else:
            return cls.C


class SignalState(str, Enum):
    GENERATED              = "GENERATED"               # freshly created, not yet stored
    ACTIVE                 = "ACTIVE"                  # stored in SignalStore, actionable
    COOLDOWN               = "COOLDOWN"                # same instrument in cooldown window
    EXPIRED                = "EXPIRED"                 # TTL elapsed
    DROPPED_CLASS_MISMATCH = "DROPPED_CLASS_MISMATCH"  # execution class lock mismatch
    DROPPED_DUPLICATE      = "DROPPED_DUPLICATE"       # duplicate suppression window
    DROPPED_RISK_REJECTION = "DROPPED_RISK_REJECTION"  # rejected by pre-signal risk gate


@dataclass(slots=True)
class Signal:
    symbol: str
    exchange: Exchange
    asset_class: AssetClass
    signal_type: SignalType
    direction: Direction
    entry_price: float
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    target_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    confidence_score: float = 0.0
    confidence_tier: ConfidenceTier = ConfidenceTier.C
    checklist: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    strategy_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    indicators_triggered: Dict[str, bool] = field(default_factory=dict)
    state: "SignalState" = field(default_factory=lambda: SignalState.GENERATED)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        if self.expires_at is None:
            self.expires_at = self.timestamp + timedelta(seconds=30)
        elif self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=timezone.utc)

        if self.confidence_tier == ConfidenceTier.C and self.confidence_score >= 55.0:
            self.confidence_tier = ConfidenceTier.from_score(self.confidence_score)

    def is_expired(self, current_time: Optional[datetime] = None) -> bool:
        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now >= self.expires_at

    def is_actionable(self, current_time: Optional[datetime] = None) -> bool:
        if self.is_expired(current_time):
            return False
        return self.confidence_tier in (
            ConfidenceTier.A_PLUS,
            ConfidenceTier.A,
            ConfidenceTier.B,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "exchange": self.exchange.value if hasattr(self.exchange, "value") else str(self.exchange),
            "asset_class": self.asset_class.value if hasattr(self.asset_class, "value") else str(self.asset_class),
            "signal_type": self.signal_type.value if hasattr(self.signal_type, "value") else str(self.signal_type),
            "direction": self.direction.value if hasattr(self.direction, "value") else str(self.direction),
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "stop_loss_price": self.stop_loss_price,
            "confidence_score": self.confidence_score,
            "confidence_tier": self.confidence_tier.value,
            "checklist": list(self.checklist),
            "timestamp": self.timestamp.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "strategy_name": self.strategy_name,
            "metadata": dict(self.metadata),
            "indicators_triggered": dict(self.indicators_triggered),
            "state": self.state.value if hasattr(self.state, "value") else str(self.state),
        }