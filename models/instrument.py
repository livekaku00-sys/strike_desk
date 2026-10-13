from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class AssetClass(str, Enum):
    STOCK_CASH = "STOCK_CASH"
    INDEX_OPTIONS = "INDEX_OPTIONS"
    STOCK_OPTIONS = "STOCK_OPTIONS"
    STOCK_FUTURES = "STOCK_FUTURES"
    INDEX_FUTURES = "INDEX_FUTURES"


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class InstrumentType(str, Enum):
    EQ = "EQ"
    OPT = "OPT"
    FUT = "FUT"
    IDX_OPT = "IDX_OPT"
    IDX_FUT = "IDX_FUT"


@dataclass(slots=True)
class Instrument:
    exchange: Exchange
    symbol: str
    asset_class: AssetClass
    instrument_type: InstrumentType
    broker_symbol: str = ""
    token: str = ""
    underlying: Optional[str] = None
    expiry: Optional[date] = None
    strike_price: Optional[float] = None
    option_type: Optional[str] = None  # "CE" or "PE"
    lot_size: int = 1
    tick_size: float = 0.05
    isin: Optional[str] = None
    name: Optional[str] = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True

    def __post_init__(self) -> None:
        # 1. Exchange validation
        if isinstance(self.exchange, str):
            try:
                self.exchange = Exchange(self.exchange.upper())
            except ValueError:
                raise ValueError(f"Invalid exchange '{self.exchange}'. Must be one of {[e.value for e in Exchange]}")
        elif not isinstance(self.exchange, Exchange):
            raise ValueError(f"Invalid exchange type: {type(self.exchange)}. Expected Exchange enum.")

        # 2. Symbol validation
        if not self.symbol or not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("Instrument symbol cannot be empty.")
        self.symbol = self.symbol.strip().upper()

        # 3. AssetClass validation
        if isinstance(self.asset_class, str):
            try:
                self.asset_class = AssetClass(self.asset_class.upper())
            except ValueError:
                raise ValueError(f"Invalid asset_class '{self.asset_class}'. Must be one of {[a.value for a in AssetClass]}")
        elif not isinstance(self.asset_class, AssetClass):
            raise ValueError(f"Invalid asset_class type: {type(self.asset_class)}. Expected AssetClass enum.")

        # 4. InstrumentType validation
        if isinstance(self.instrument_type, str):
            try:
                self.instrument_type = InstrumentType(self.instrument_type.upper())
            except ValueError:
                raise ValueError(f"Invalid instrument_type '{self.instrument_type}'. Must be one of {[i.value for i in InstrumentType]}")
        elif not isinstance(self.instrument_type, InstrumentType):
            raise ValueError(f"Invalid instrument_type type: {type(self.instrument_type)}. Expected InstrumentType enum.")

        # 5. Lot size and tick size validation
        if not isinstance(self.lot_size, int) or self.lot_size <= 0:
            raise ValueError(f"lot_size must be a positive integer > 0, got {self.lot_size}")
        if self.tick_size <= 0.0:
            raise ValueError(f"tick_size must be > 0.0, got {self.tick_size}")

        # 6. Option validation
        if self.option_type is not None:
            opt_upper = self.option_type.strip().upper()
            if opt_upper not in ("CE", "PE"):
                raise ValueError(f"Invalid option_type '{self.option_type}'. Must be 'CE' or 'PE'.")
            self.option_type = opt_upper

        # 7. Underlying uppercase normalization
        if self.underlying:
            self.underlying = self.underlying.strip().upper()

        # 8. Timezone normalization
        if self.last_updated.tzinfo is None:
            self.last_updated = self.last_updated.replace(tzinfo=timezone.utc)
        else:
            self.last_updated = self.last_updated.astimezone(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exchange": self.exchange.value,
            "symbol": self.symbol,
            "broker_symbol": self.broker_symbol,
            "token": self.token,
            "asset_class": self.asset_class.value,
            "instrument_type": self.instrument_type.value,
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "strike_price": self.strike_price,
            "option_type": self.option_type,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "isin": self.isin,
            "name": self.name,
            "last_updated": self.last_updated.isoformat(),
            "is_active": self.is_active,
        }