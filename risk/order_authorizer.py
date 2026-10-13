"""
Strike Desk V4 - Pre-Trade Order Authorization Gate
Enforces Review Gap A (CRITICAL):
- Every order must pass risk authorization before submission.
- Evaluates:
    1. Emergency Halt (Kill-switch)
    2. Daily Loss Ceiling (-₹2,500)
    3. Daily Trade Cap (5 trades)
    4. Consecutive Loss Cooldown (45 mins)
    5. Order Value Sanity Limit (₹200,000 max single order)
    6. Market Session Timing (15:10 IST Hard Entry Cutoff)
"""
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple
import uuid

from models.order import Order, OrderSide
from models.risk_state import IST
from risk.circuit_breaker import BreakerStatus, CircuitBreaker, circuit_breaker

logger = logging.getLogger(__name__)


@dataclass
class AuthorizationResult:
    """Outcome of pre-trade risk authorization gate."""
    is_authorized: bool
    client_order_id: str
    symbol: str
    side: str
    quantity: float
    estimated_value: float
    auth_token: Optional[str] = None
    reason: Optional[str] = None
    rule_violated: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_authorized": self.is_authorized,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "estimated_value": self.estimated_value,
            "auth_token": self.auth_token,
            "reason": self.reason,
            "rule_violated": self.rule_violated,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


class OrderAuthorizer:
    """
    Gateway that must authorize every order prior to broker transmission.
    Prevents unauthorized order leakage and guarantees Review Gap A compliance.
    """

    MAX_SINGLE_ORDER_VALUE: float = 200_000.0  # ₹2 Lakhs per order cap
    ENTRY_CUTOFF_TIME_IST: time = time(15, 10)  # 15:10 IST hard cutoff

    def __init__(
        self,
        breaker: Optional[CircuitBreaker] = None,
        max_order_value: float = MAX_SINGLE_ORDER_VALUE,
    ) -> None:
        self.circuit_breaker = breaker or circuit_breaker
        self.max_order_value = max_order_value
        self._audit_log: List[AuthorizationResult] = []
        self._lock = threading.RLock()

    def authorize_order(
        self,
        order: Order,
        current_time: Optional[datetime] = None,
        current_market_price: Optional[float] = None,
        is_exit: bool = False,
    ) -> AuthorizationResult:
        """
        Validates order against all risk rules and circuit breakers.
        Returns AuthorizationResult with an auth_token if approved.

        is_exit=True (caller has verified the order only reduces an existing
        position): entry-only limits — order value cap, loss ceiling, trade cap,
        cooldown, 15:10 entry cutoff — do not apply, because blocking an exit
        traps the operator in the position those limits exist to cap. The
        emergency halt (operator kill switch) still applies.
        """
        with self._lock:
            now = current_time or datetime.now(timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

            cid = order.client_order_id or str(uuid.uuid4())
            sym = order.symbol.strip().upper()
            side_str = order.side.value if hasattr(order.side, "value") else str(order.side)
            qty = float(order.quantity)

            # Estimate order value
            price = float(order.price) if order.price > 0 else (float(current_market_price or 0.0))
            est_value = round(qty * price, 2)

            # 1. Sanity Check on Quantity
            if qty <= 0:
                result = AuthorizationResult(
                    is_authorized=False,
                    client_order_id=cid,
                    symbol=sym,
                    side=side_str,
                    quantity=qty,
                    estimated_value=est_value,
                    reason=f"Invalid order quantity {qty} <= 0",
                    rule_violated="QUANTITY_SANITY",
                    timestamp=now,
                )
                self._audit_log.append(result)
                return result

            # 2. Maximum Single Order Value Cap (entries only)
            if not is_exit and est_value > self.max_order_value:
                result = AuthorizationResult(
                    is_authorized=False,
                    client_order_id=cid,
                    symbol=sym,
                    side=side_str,
                    quantity=qty,
                    estimated_value=est_value,
                    reason=f"Order value ₹{est_value:.2f} exceeds ₹{self.max_order_value:.2f} limit",
                    rule_violated="MAX_ORDER_VALUE",
                    timestamp=now,
                )
                self._audit_log.append(result)
                return result

            # 3. Circuit Breaker Gate (Emergency Halt, Loss Ceiling, Trade Cap, Cooldown)
            can_trade, reject_reason = self.circuit_breaker.can_trade(now)
            if is_exit and not can_trade and (
                self.circuit_breaker.get_status(now) != BreakerStatus.EMERGENCY_HALTED
            ):
                logger.warning("[OrderAuthorizer] EXIT %s %s allowed despite: %s",
                               side_str, sym, reject_reason)
                can_trade = True
            if not can_trade:
                status = self.circuit_breaker.get_status(now)
                result = AuthorizationResult(
                    is_authorized=False,
                    client_order_id=cid,
                    symbol=sym,
                    side=side_str,
                    quantity=qty,
                    estimated_value=est_value,
                    reason=reject_reason,
                    rule_violated=status.value,
                    timestamp=now,
                )
                self._audit_log.append(result)
                logger.warning("[OrderAuthorizer] Order %s REJECTED: %s", cid, reject_reason)
                return result

            # 4. Session Timing: 15:10 IST Hard Square-Off / Entry Cutoff
            ist_time = now.astimezone(IST).time()
            if not is_exit and ist_time >= self.ENTRY_CUTOFF_TIME_IST:
                result = AuthorizationResult(
                    is_authorized=False,
                    client_order_id=cid,
                    symbol=sym,
                    side=side_str,
                    quantity=qty,
                    estimated_value=est_value,
                    reason=f"Past 15:10 IST cutoff ({ist_time.strftime('%H:%M:%S')} IST). New entries prohibited (Blueprint O5)",
                    rule_violated="TIME_CUTOFF_15_10",
                    timestamp=now,
                )
                self._audit_log.append(result)
                logger.warning("[OrderAuthorizer] Order %s REJECTED: Past 15:10 IST cutoff", cid)
                return result

            # Approved! Generate cryptographically unique authorization token
            auth_token = f"AUTH_{uuid.uuid4().hex[:16].upper()}"
            result = AuthorizationResult(
                is_authorized=True,
                client_order_id=cid,
                symbol=sym,
                side=side_str,
                quantity=qty,
                estimated_value=est_value,
                auth_token=auth_token,
                reason=("Exit authorized (entry-only limits waived)" if is_exit
                        else "All risk and circuit breaker checks passed"),
                timestamp=now,
                metadata={"status": "AUTHORIZED"},
            )
            self._audit_log.append(result)
            logger.info("[OrderAuthorizer] Order %s AUTHORIZED with token %s", cid, auth_token)
            return result

    def get_audit_log(self) -> List[AuthorizationResult]:
        """Returns immutable history of authorization requests."""
        with self._lock:
            return list(self._audit_log)

    def clear_audit_log(self) -> None:
        """Clears authorization audit history."""
        with self._lock:
            self._audit_log.clear()


# Global singleton instance
order_authorizer = OrderAuthorizer()
