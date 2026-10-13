from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class OptionContract:
    ltp: float = 0.0
    bid_price: float = 0.0
    ask_price: float = 0.0
    volume: float = 0.0
    oi: float = 0.0
    iv: float = 0.0
    security_id: str = ""


@dataclass(slots=True)
class StrikeData:
    strike_price: float
    call: OptionContract = field(default_factory=OptionContract)
    put: OptionContract = field(default_factory=OptionContract)
    pcr: float = 0.0

    def calculate_pcr(self) -> float:
        if self.call.oi > 0:
            self.pcr = round(self.put.oi / self.call.oi, 4)
        else:
            self.pcr = 0.0
        return self.pcr


@dataclass(slots=True)
class OptionChain:
    underlying: str
    expiry_date: str
    strikes: List[StrikeData] = field(default_factory=list)
    total_call_oi: float = 0.0
    total_put_oi: float = 0.0
    pcr: float = 0.0
    underlying_ltp: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        else:
            self.timestamp = self.timestamp.astimezone(timezone.utc)

    def get_strike(self, strike_price: float) -> Optional[StrikeData]:
        for s in self.strikes:
            if abs(s.strike_price - strike_price) < 1e-4:
                return s
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d
