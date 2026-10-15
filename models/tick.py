from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any, Dict


@dataclass(slots=True)
class Tick:
    symbol: str
    exchange: str
    instrument_type: str
    timestamp: datetime
    ltp: float
    bid_price: float = 0.0
    ask_price: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    volume: float = 0.0
    total_buy_qty: float = 0.0
    total_sell_qty: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    vwap: float = 0.0
    oi: float = 0.0
    oi_change: float = 0.0
    late: bool = False
    source: str = "live"
    # True only when bid_price and ask_price are both finite, positive, and from
    # an instrument type that has an order book.  False for index instruments and
    # any tick where bid/ask were absent, zero, or non-finite in the raw payload.
    bid_ask_valid: bool = False
    # True only when ltp is finite and strictly positive.
    # False for any tick where ltp was absent, zero, NaN, or non-finite in the raw
    # payload.  Indicator calculators must skip ticks where ltp_valid is False.
    ltp_valid: bool = False

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        else:
            self.timestamp = self.timestamp.astimezone(timezone.utc)
        # Derive ltp_valid from the stored ltp value.
        self.ltp_valid = math.isfinite(self.ltp) and self.ltp > 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d
