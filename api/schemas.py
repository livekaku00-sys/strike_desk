"""
Strike Desk V4 - API Pydantic Response & Request Schemas
Blueprint L6, O8, O5 & O7: Strong typing, real data or explicit error/empty states only.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field


# --- Market Data Schemas ---

class InstrumentResponse(BaseModel):
    symbol: str
    name: str
    exchange: str
    segment: str
    lot_size: int = 1
    tick_size: float = 0.05
    strike_step: Optional[float] = None
    token: Optional[str] = None
    expiry: Optional[str] = None


class TickResponse(BaseModel):
    symbol: str
    ltp: float
    timestamp: datetime
    volume: Optional[int] = 0
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None


class IndicatorResponse(BaseModel):
    symbol: str
    timeframe: str = "15m"
    vwap: Optional[float] = None
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    adx: Optional[float] = None
    atr: Optional[float] = None
    regime: Optional[str] = None
    timestamp: datetime


class RSIData(BaseModel):
    rsi: Optional[float] = None
    zone: Optional[str] = None


class ADXData(BaseModel):
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    trend_strength: Optional[str] = None


class MACDData(BaseModel):
    macd_line: Optional[float] = None
    signal_line: Optional[float] = None
    histogram: Optional[float] = None
    crossover: Optional[str] = None


class BollingerData(BaseModel):
    upper: Optional[float] = None
    middle: Optional[float] = None
    lower: Optional[float] = None
    percent_b: Optional[float] = None
    position: Optional[str] = None


class ATRData(BaseModel):
    atr: Optional[float] = None
    atr_percent: Optional[float] = None
    volatility: Optional[str] = None


class SupertrendData(BaseModel):
    supertrend: Optional[float] = None
    direction: Optional[str] = None
    signal: Optional[str] = None
    trend_changed: Optional[bool] = None


class SentimentData(BaseModel):
    score: Optional[float] = None
    components: Optional[Dict[str, Any]] = None
    confidence: Optional[float] = None


class FullIndicatorResponse(BaseModel):
    symbol: str
    timestamp: datetime
    ema9: Optional[float] = None
    vwap: Optional[float] = None
    rsi: Optional[RSIData] = None
    adx: Optional[ADXData] = None
    macd: Optional[MACDData] = None
    bollinger: Optional[BollingerData] = None
    atr: Optional[ATRData] = None
    supertrend: Optional[SupertrendData] = None
    sentiment: Optional[SentimentData] = None
    pcr_slope: Optional[float] = None
    volume_surge: Optional[bool] = None
    oi_change_pct: Optional[float] = None


class OptionStrikeData(BaseModel):
    strike: float
    ce_symbol: Optional[str] = None
    ce_ltp: Optional[float] = None
    ce_oi: Optional[int] = None
    ce_iv: Optional[float] = None
    pe_symbol: Optional[str] = None
    pe_ltp: Optional[float] = None
    pe_oi: Optional[int] = None
    pe_iv: Optional[float] = None


class OptionChainResponse(BaseModel):
    underlying: str
    spot_price: float
    atm_strike: float
    expiry: Optional[str] = None
    strikes: List[OptionStrikeData]
    timestamp: datetime


# --- Trading & Signal Schemas ---

class ModeUpdateRequest(BaseModel):
    mode: str
    operator_id: Optional[str] = "ADMIN"


class ModeResponse(BaseModel):
    active_mode: str
    allowed_category: str
    last_updated: datetime


class SignalResponse(BaseModel):
    signal_id: str
    symbol: str
    instrument_type: str
    direction: str  # BUY or SELL
    score: float
    expected_value: float
    status: str  # APPROVED_FOR_EXECUTION or ALERT_ONLY
    timestamp: datetime


class OrderCreateRequest(BaseModel):
    symbol: str
    side: str  # BUY or SELL
    quantity: int
    price: Optional[float] = None
    order_type: str = "MARKET"
    instrument_type: str = "AUTO_OPTIONS"
    broker: Optional[str] = "angelone"


class OrderResponse(BaseModel):
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    status: str
    broker: str
    placed_at: datetime
    filled_at: Optional[datetime] = None


class PositionResponse(BaseModel):
    position_id: str
    symbol: str
    quantity: int
    entry_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float = 0.0
    is_open: bool = True
    entry_time: datetime


class SquareOffRequest(BaseModel):
    reason: Optional[str] = "MANUAL_OPERATOR"


# --- Risk & Emergency Halt Schemas ---

class RiskStateResponse(BaseModel):
    ist_date: str
    realized_pnl: float
    total_risk_pnl: float
    daily_loss_ceiling: float
    loss_ceiling_breached: bool
    trade_count: int
    max_daily_trades: int
    consecutive_losses: int
    cooldown_active: bool
    cooldown_until: Optional[str] = None
    emergency_halt_active: bool
    emergency_halt_reason: Optional[str] = None
    open_positions_count: int


class HaltTriggerRequest(BaseModel):
    reason: str
    level: str = "HARD_HALT"
    operator_id: str = "OPERATOR"


class HaltResponse(BaseModel):
    event_id: str
    is_halted: bool
    level: str
    reason: str
    triggered_by: str
    timestamp: datetime


class AckTokenRequest(BaseModel):
    operator_id: str


class AckTokenResponse(BaseModel):
    token: str
    operator_id: str
    expires_in_seconds: int = 300


class AckResetRequest(BaseModel):
    operator_id: str
    token: str
    resolution_notes: str = ""


class AckResetResponse(BaseModel):
    success: bool
    message: str
    reset_at: datetime


class AuditLogResponse(BaseModel):
    events: List[Dict[str, Any]]


# --- System Schemas ---

class BrokerStatusResponse(BaseModel):
    broker_name: str
    is_connected: bool
    latency_ms: Optional[float] = None
    last_heartbeat: Optional[datetime] = None
    error_message: Optional[str] = None


class ErrorResponse(BaseModel):
    error: str
    message: str
    status_code: int
    timestamp: datetime
    details: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    version: str = "4.0.0"
    services: Dict[str, str]


class ReadinessResponse(BaseModel):
    ready: bool
    timestamp: datetime
    checks: Dict[str, bool]
    message: str
