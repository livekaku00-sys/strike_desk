from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class OrderState(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"         # Sent to broker; awaiting exchange acknowledgment
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    EXPIRED = "EXPIRED"
    TIMEOUT = "TIMEOUT"             # Legacy; prefer UNKNOWN for new unconfirmed submissions
    UNKNOWN = "UNKNOWN"             # Submission timed out; state unconfirmed with broker


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_MARKET = "SL_M"


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"
    CARRYFORWARD = "CARRYFORWARD"  # positional F&O (NRML) on AngelOne / Fyers
    MARGIN = "MARGIN"
    BO = "BO"
    CO = "CO"


class CommonErrorCode(str, Enum):
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_QTY = "INVALID_QTY"
    MARKET_CLOSED = "MARKET_CLOSED"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


VALID_TRANSITIONS: Dict[OrderState, List[OrderState]] = {
    OrderState.PENDING: [
        OrderState.SUBMITTED,       # Normal: request sent to broker
        OrderState.UNKNOWN,         # Direct: timeout before SUBMITTED was recorded
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.TIMEOUT,
        OrderState.CANCEL_PENDING,
        OrderState.CANCELLED,
    ],
    OrderState.SUBMITTED: [         # Sent; awaiting ack — can resolve to any confirmed state
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.UNKNOWN,         # Timed out waiting for ack
        OrderState.TIMEOUT,
    ],
    OrderState.OPEN: [
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCEL_PENDING,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    ],
    OrderState.PARTIALLY_FILLED: [
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCEL_PENDING,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    ],
    OrderState.CANCEL_PENDING: [
        OrderState.CANCELLED,
        OrderState.FILLED,          # Race: fill arrived before cancel reached broker
        OrderState.PARTIALLY_FILLED,
        OrderState.OPEN,            # Broker rejected the cancel request
    ],
    OrderState.UNKNOWN: [           # Unconfirmed — resolves once broker is queried
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    ],
    OrderState.TIMEOUT: [           # Legacy; prefer UNKNOWN for unconfirmed submissions
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.UNKNOWN,         # Upgrade to UNKNOWN when status still unresolved
    ],
    OrderState.FILLED: [],
    OrderState.REJECTED: [],
    OrderState.CANCELLED: [],
    OrderState.EXPIRED: [],
}

# States from which no further transition is permitted.
TERMINAL_STATES: frozenset = frozenset({
    OrderState.FILLED,
    OrderState.REJECTED,
    OrderState.CANCELLED,
    OrderState.EXPIRED,
})


@dataclass
class OrderUpdate:
    client_order_id: str
    state: OrderState
    broker_order_id: Optional[str] = None
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_fill_price: float = 0.0
    last_fill_qty: float = 0.0
    last_fill_price: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_code: Optional[CommonErrorCode] = None
    raw_error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        else:
            self.timestamp = self.timestamp.astimezone(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "state": self.state.value if isinstance(self.state, OrderState) else str(self.state),
            "filled_qty": self.filled_qty,
            "remaining_qty": self.remaining_qty,
            "avg_fill_price": self.avg_fill_price,
            "last_fill_qty": self.last_fill_qty,
            "last_fill_price": self.last_fill_price,
            "timestamp": self.timestamp.isoformat(),
            "error_code": self.error_code.value if self.error_code else None,
            "raw_error_message": self.raw_error_message,
            "metadata": self.metadata,
        }


@dataclass
class Order:
    client_order_id: str
    symbol: str
    exchange: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: float = 0.0
    trigger_price: float = 0.0
    product_type: ProductType = ProductType.INTRADAY
    broker: str = ""
    broker_order_id: Optional[str] = None
    state: OrderState = OrderState.PENDING
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_fill_price: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_code: Optional[CommonErrorCode] = None
    raw_error_message: Optional[str] = None
    state_history: List[OrderUpdate] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.remaining_qty == 0.0 and self.quantity > 0:
            self.remaining_qty = self.quantity - self.filled_qty
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)
        if self.updated_at.tzinfo is None:
            self.updated_at = self.updated_at.replace(tzinfo=timezone.utc)

    def can_transition_to(self, new_state: OrderState) -> bool:
        if self.state == new_state:
            return True
        allowed = VALID_TRANSITIONS.get(self.state, [])
        return new_state in allowed

    def transition_to(
        self,
        new_state: OrderState,
        broker_order_id: Optional[str] = None,
        error_code: Optional[CommonErrorCode] = None,
        raw_error_message: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OrderUpdate:
        ts = timestamp or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if not self.can_transition_to(new_state):
            if self.state in TERMINAL_STATES:
                logger.warning(
                    "[%s] Ignoring transition from terminal state %s → %s.",
                    self.client_order_id, self.state.value, new_state.value,
                )
                return OrderUpdate(
                    client_order_id=self.client_order_id,
                    broker_order_id=self.broker_order_id,
                    state=self.state,
                    filled_qty=self.filled_qty,
                    remaining_qty=self.remaining_qty,
                    avg_fill_price=self.avg_fill_price,
                    timestamp=ts,
                )
            logger.warning(
                "[%s] Invalid state transition: %s -> %s. Recording update to prevent desync.",
                self.client_order_id,
                self.state.value,
                new_state.value,
            )

        prev_state = self.state
        self.state = new_state
        self.updated_at = ts
        if broker_order_id:
            self.broker_order_id = broker_order_id
        if error_code:
            self.error_code = error_code
        if raw_error_message:
            self.raw_error_message = raw_error_message

        update = OrderUpdate(
            client_order_id=self.client_order_id,
            broker_order_id=self.broker_order_id,
            state=new_state,
            filled_qty=self.filled_qty,
            remaining_qty=self.remaining_qty,
            avg_fill_price=self.avg_fill_price,
            timestamp=ts,
            error_code=error_code or self.error_code,
            raw_error_message=raw_error_message or self.raw_error_message,
            metadata=metadata or {},
        )
        self.state_history.append(update)
        logger.info(
            "[%s] Order state transitioned: %s -> %s (Broker ID: %s, Filled: %s/%s)",
            self.client_order_id,
            prev_state.value,
            new_state.value,
            self.broker_order_id,
            self.filled_qty,
            self.quantity,
        )
        return update

    def apply_fill(
        self,
        fill_qty: float,
        fill_price: float,
        timestamp: Optional[datetime] = None,
        broker_order_id: Optional[str] = None,
    ) -> OrderUpdate:
        ts = timestamp or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if fill_qty <= 0:
            raise ValueError(f"Fill quantity must be positive, got {fill_qty}")

        if self.state in TERMINAL_STATES:
            logger.warning(
                "[%s] Ignoring fill (qty=%s @ %s) on order already in terminal state %s.",
                self.client_order_id, fill_qty, fill_price, self.state.value,
            )
            return OrderUpdate(
                client_order_id=self.client_order_id,
                broker_order_id=self.broker_order_id or broker_order_id,
                state=self.state,
                filled_qty=self.filled_qty,
                remaining_qty=self.remaining_qty,
                avg_fill_price=self.avg_fill_price,
                last_fill_qty=0.0,
                last_fill_price=0.0,
                timestamp=ts,
            )

        new_filled_qty = self.filled_qty + fill_qty
        if new_filled_qty > self.quantity:
            logger.warning(
                "[%s] Overfill: cumulative fill %.4g would exceed order qty %.4g. Clamping.",
                self.client_order_id, new_filled_qty, self.quantity,
            )
            fill_qty = max(0.0, self.quantity - self.filled_qty)
            if fill_qty == 0.0:
                return OrderUpdate(
                    client_order_id=self.client_order_id,
                    broker_order_id=self.broker_order_id or broker_order_id,
                    state=self.state,
                    filled_qty=self.filled_qty,
                    remaining_qty=self.remaining_qty,
                    avg_fill_price=self.avg_fill_price,
                    last_fill_qty=0.0,
                    last_fill_price=0.0,
                    timestamp=ts,
                )
            new_filled_qty = self.quantity

        total_prev_val = self.filled_qty * self.avg_fill_price
        new_fill_val = fill_qty * fill_price

        self.filled_qty = new_filled_qty
        self.avg_fill_price = round((total_prev_val + new_fill_val) / new_filled_qty, 4)
        self.remaining_qty = max(0.0, self.quantity - self.filled_qty)
        self.updated_at = ts
        if broker_order_id:
            self.broker_order_id = broker_order_id

        if self.remaining_qty == 0.0:
            new_state = OrderState.FILLED
        else:
            new_state = OrderState.PARTIALLY_FILLED

        self.state = new_state
        update = OrderUpdate(
            client_order_id=self.client_order_id,
            broker_order_id=self.broker_order_id,
            state=new_state,
            filled_qty=self.filled_qty,
            remaining_qty=self.remaining_qty,
            avg_fill_price=self.avg_fill_price,
            last_fill_qty=fill_qty,
            last_fill_price=fill_price,
            timestamp=ts,
        )
        self.state_history.append(update)
        logger.info(
            "[%s] Fill applied: +%s @ %s (Total Filled: %s/%s, Avg Price: %s, State: %s)",
            self.client_order_id,
            fill_qty,
            fill_price,
            self.filled_qty,
            self.quantity,
            self.avg_fill_price,
            new_state.value,
        )
        return update

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker": self.broker,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "side": self.side.value if isinstance(self.side, OrderSide) else str(self.side),
            "order_type": self.order_type.value if isinstance(self.order_type, OrderType) else str(self.order_type),
            "product_type": self.product_type.value if isinstance(self.product_type, ProductType) else str(self.product_type),
            "quantity": self.quantity,
            "price": self.price,
            "trigger_price": self.trigger_price,
            "state": self.state.value if isinstance(self.state, OrderState) else str(self.state),
            "filled_qty": self.filled_qty,
            "remaining_qty": self.remaining_qty,
            "avg_fill_price": self.avg_fill_price,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "error_code": self.error_code.value if self.error_code else None,
            "raw_error_message": self.raw_error_message,
            "history_count": len(self.state_history),
        }