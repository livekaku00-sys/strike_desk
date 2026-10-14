import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from models.order import (
    CommonErrorCode,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    OrderUpdate,
    ProductType,
    TERMINAL_STATES,
)
from utils.order_id_generator import (
    compute_order_signature,
    generate_client_order_id,
)

logger = logging.getLogger(__name__)

# Import lazily to avoid circular imports at module load time.
# _get_audit_logger() is called at call sites, not at module level.
def _get_audit_logger():
    from execution.audit_logger import audit_logger
    return audit_logger


class OrderError(Exception):
    pass


class DuplicateOrderError(OrderError):
    pass


class OrderTimeoutError(OrderError):
    pass


def normalize_broker_error(broker: str, raw_code: Any, raw_message: str) -> Tuple[CommonErrorCode, str]:
    msg = (raw_message or "").strip().lower()
    logger.warning("[%s] Raw broker error: code=%s, message='%s'", broker, raw_code, raw_message)

    if any(k in msg for k in ["margin", "fund", "insufficient", "balance", "credit limit", "ab1004", "-17"]):
        return CommonErrorCode.INSUFFICIENT_MARGIN, raw_message

    if any(k in msg for k in ["circuit", "price", "tick size", "range", "band", "lmt price"]):
        return CommonErrorCode.INVALID_PRICE, raw_message

    if any(k in msg for k in ["quantity", "lot", "qty", "freeze", "multiplier", "min qty", "max qty"]):
        return CommonErrorCode.INVALID_QTY, raw_message

    if any(k in msg for k in ["market closed", "trading hours", "after market", "exchange closed", "amo", "ab1007"]):
        return CommonErrorCode.MARKET_CLOSED, raw_message

    if any(k in msg for k in ["timeout", "network", "connect", "connection", "socket", "gateway", "unreachable", "reset by peer"]):
        return CommonErrorCode.NETWORK_ERROR, raw_message

    return CommonErrorCode.UNKNOWN_ERROR, raw_message


class OrderManager:
    SUBMISSION_TIMEOUT_SECONDS = 30.0
    IDEMPOTENCY_WINDOW_SECONDS = 300.0  # 5 minutes

    def __init__(
        self,
        submission_timeout: float = SUBMISSION_TIMEOUT_SECONDS,
        idempotency_window: float = IDEMPOTENCY_WINDOW_SECONDS,
    ) -> None:
        self.submission_timeout = submission_timeout
        self.idempotency_window = idempotency_window

        self._orders: Dict[str, Order] = {}
        self._broker_to_client_id: Dict[str, str] = {}
        self._recent_signatures: Dict[str, Tuple[str, float]] = {}
        self._listeners: List[Callable[[OrderUpdate], Any]] = []
        self._lock = asyncio.Lock()

    def register_update_listener(self, callback: Callable[[OrderUpdate], Any]) -> None:
        self._listeners.append(callback)

    def get_order(self, client_order_id: str) -> Optional[Order]:
        return self._orders.get(client_order_id)

    def get_order_by_broker_id(self, broker_order_id: str) -> Optional[Order]:
        client_id = self._broker_to_client_id.get(broker_order_id)
        return self._orders.get(client_id) if client_id else None

    def get_all_orders(self) -> List[Order]:
        return list(self._orders.values())

    def get_open_orders(self) -> List[Order]:
        return [
            o for o in self._orders.values()
            if o.state in (
                OrderState.PENDING,
                OrderState.SUBMITTED,
                OrderState.OPEN,
                OrderState.PARTIALLY_FILLED,
                OrderState.CANCEL_PENDING,
                OrderState.UNKNOWN,
                OrderState.TIMEOUT,    # legacy — kept for in-flight orders from prior sessions
            )
        ]

    def _purge_old_signatures(self, now_mono: float) -> None:
        expired_keys = [
            k for k, (_, t) in self._recent_signatures.items()
            if now_mono - t > self.idempotency_window
        ]
        for k in expired_keys:
            del self._recent_signatures[k]

    def create_order(
        self,
        symbol: str,
        exchange: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: float = 0.0,
        trigger_price: float = 0.0,
        product_type: ProductType = ProductType.INTRADAY,
        broker: str = "",
        client_order_id: Optional[str] = None,
    ) -> Order:
        cid = client_order_id or generate_client_order_id(
            symbol=symbol,
            side=side.value if isinstance(side, OrderSide) else str(side),
            quantity=quantity,
            price=price,
        )
        return Order(
            client_order_id=cid,
            symbol=symbol,
            exchange=exchange,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            product_type=product_type,
            broker=broker,
            state=OrderState.PENDING,
        )

    async def submit_order(self, adapter: Any, order: Order) -> OrderUpdate:
        now_mono = time.monotonic()
        side_val = order.side.value if isinstance(order.side, OrderSide) else str(order.side)
        type_val = order.order_type.value if isinstance(order.order_type, OrderType) else str(order.order_type)
        sig = compute_order_signature(order.broker, order.symbol, side_val, order.quantity, order.price, type_val)

        async with self._lock:
            self._purge_old_signatures(now_mono)
            if sig in self._recent_signatures:
                existing_cid, prev_time = self._recent_signatures[sig]
                if now_mono - prev_time <= self.idempotency_window:
                    logger.error(
                        "[%s] Duplicate submission rejected within %ds window (Existing CID: %s)",
                        order.client_order_id,
                        self.idempotency_window,
                        existing_cid,
                    )
                    raise DuplicateOrderError(
                        f"Duplicate order detected for {sig} within {self.idempotency_window}s window (CID: {existing_cid})"
                    )

            self._recent_signatures[sig] = (order.client_order_id, now_mono)
            self._orders[order.client_order_id] = order

        # Transition to SUBMITTED before dispatching — makes the in-flight state explicit
        # and prevents any retry logic from treating the order as unsent.
        submitted_update = order.transition_to(OrderState.SUBMITTED)
        self._dispatch_update(submitted_update)
        _get_audit_logger().log_order_transition(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            broker=order.broker,
            old_state=OrderState.PENDING.value,
            new_state=OrderState.SUBMITTED.value,
            reason="order dispatched to broker",
        )

        try:
            update = await asyncio.wait_for(
                adapter.place_order(order),
                timeout=self.submission_timeout,
            )
            return self.handle_order_update(update)

        except asyncio.TimeoutError:
            logger.warning(
                "[%s] Order submission timed out after %ds. "
                "Transitioning to UNKNOWN — DO NOT resubmit until state is confirmed.",
                order.client_order_id,
                self.submission_timeout,
            )
            # Transition to UNKNOWN immediately so the caller and any monitoring code
            # can see the ambiguous state. The idempotency key is still in the window,
            # which blocks accidental duplicate submission.
            unknown_update = order.transition_to(
                new_state=OrderState.UNKNOWN,
                error_code=CommonErrorCode.NETWORK_ERROR,
                raw_error_message="Submission timed out; state unconfirmed with broker",
            )
            self._dispatch_update(unknown_update)
            _get_audit_logger().log_order_transition(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                broker=order.broker,
                old_state=OrderState.SUBMITTED.value,
                new_state=OrderState.UNKNOWN.value,
                reason="timeout after submission; querying broker to resolve",
                order_id=order.broker_order_id or "",
            )

            # Query broker to resolve — must not resubmit
            try:
                status_update = await asyncio.wait_for(
                    adapter.get_order_status(
                        client_order_id=order.client_order_id,
                        broker_order_id=order.broker_order_id,
                    ),
                    timeout=10.0,
                )
                if status_update and status_update.state not in (OrderState.TIMEOUT, OrderState.UNKNOWN):
                    logger.info(
                        "[%s] Order recovered after timeout. State is %s",
                        order.client_order_id,
                        status_update.state.value,
                    )
                    return self.handle_order_update(status_update)
            except Exception as query_exc:
                logger.error(
                    "[%s] Failed to query status after timeout: %s",
                    order.client_order_id,
                    query_exc,
                )

            return unknown_update

        except Exception as exc:
            prev_state = order.state  # SUBMITTED at this point
            common_err, raw_msg = normalize_broker_error(order.broker, type(exc).__name__, str(exc))
            rej_update = order.transition_to(
                new_state=OrderState.REJECTED,
                error_code=common_err,
                raw_error_message=raw_msg,
            )
            self._dispatch_update(rej_update)
            _get_audit_logger().log_order_transition(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                broker=order.broker,
                old_state=prev_state.value,
                new_state=OrderState.REJECTED.value,
                reason=(raw_msg or "")[:200],
            )
            return rej_update

    async def cancel_order(self, adapter: Any, client_order_id: str) -> OrderUpdate:
        order = self.get_order(client_order_id)
        if not order:
            raise OrderError(f"Order not found: {client_order_id}")

        if order.state in (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED):
            raise OrderError(f"Cannot cancel order in terminal state: {order.state.value}")

        pending_update = order.transition_to(OrderState.CANCEL_PENDING)
        self._dispatch_update(pending_update)

        try:
            update = await adapter.cancel_order(client_order_id=order.client_order_id, broker_order_id=order.broker_order_id)
            return self.handle_order_update(update)
        except Exception as exc:
            common_err, raw_msg = normalize_broker_error(order.broker, type(exc).__name__, str(exc))
            logger.error("[%s] Cancel request failed: %s", client_order_id, exc)
            if order.state == OrderState.CANCEL_PENDING:
                rev_state = OrderState.PARTIALLY_FILLED if order.filled_qty > 0 else OrderState.OPEN
                rev_update = order.transition_to(rev_state, error_code=common_err, raw_error_message=raw_msg)
                self._dispatch_update(rev_update)
                return rev_update
            return order.state_history[-1]

    async def modify_order(
        self,
        adapter: Any,
        client_order_id: str,
        price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> OrderUpdate:
        order = self.get_order(client_order_id)
        if not order:
            raise OrderError(f"Order not found: {client_order_id}")

        if order.state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
            raise OrderError(f"Cannot modify order in state: {order.state.value}")

        update = await adapter.modify_order(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            price=price,
            quantity=quantity,
        )
        if price is not None:
            order.price = price
        if quantity is not None:
            order.quantity = quantity
            order.remaining_qty = max(0.0, order.quantity - order.filled_qty)
        return self.handle_order_update(update)

    def handle_order_update(self, update: OrderUpdate) -> OrderUpdate:
        order = self.get_order(update.client_order_id)
        if not order and update.broker_order_id:
            order = self.get_order_by_broker_id(update.broker_order_id)

        if not order:
            logger.warning("Received update for unknown order: %s (Broker: %s)", update.client_order_id, update.broker_order_id)
            return update

        if update.broker_order_id and not order.broker_order_id:
            order.broker_order_id = update.broker_order_id
            self._broker_to_client_id[update.broker_order_id] = order.client_order_id

        old_state = order.state

        # Drop fill updates for orders already in a terminal state.  A delayed
        # or duplicate fill from the broker must never reopen or overfill an
        # order that has already settled.
        if old_state in TERMINAL_STATES and (
            (update.last_fill_qty > 0 and update.last_fill_price > 0)
            or update.filled_qty > order.filled_qty
        ):
            logger.warning(
                "[%s] Dropping fill update (last_fill_qty=%s, filled_qty=%s) "
                "for order already in terminal state %s.",
                order.client_order_id,
                update.last_fill_qty,
                update.filled_qty,
                old_state.value,
            )
            return update

        # effective_update tracks what was actually booked; replaced by the
        # post-clamp OrderUpdate from apply_fill() in fill branches so that
        # dispatch and callers see accepted quantities, not broker-reported ones.
        effective_update = update

        if update.last_fill_qty > 0 and update.last_fill_price > 0:
            fill_result = order.apply_fill(
                fill_qty=update.last_fill_qty,
                fill_price=update.last_fill_price,
                timestamp=update.timestamp,
                broker_order_id=update.broker_order_id,
            )
            effective_update = fill_result
            accepted_qty = fill_result.last_fill_qty
            reason = "fill received"
            if accepted_qty != update.last_fill_qty:
                reason = (
                    f"fill received; broker reported qty={update.last_fill_qty}, "
                    f"accepted qty={accepted_qty} (clamped)"
                )
            _get_audit_logger().log_order_transition(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                broker=order.broker,
                old_state=old_state.value,
                new_state=order.state.value,
                reason=reason,
                fill_price=update.last_fill_price,
                fill_qty=accepted_qty,
                order_id=update.broker_order_id or "",
            )
        elif update.filled_qty > order.filled_qty:
            additional_fill = update.filled_qty - order.filled_qty
            avg_price = update.avg_fill_price if update.avg_fill_price > 0 else order.price
            fill_result = order.apply_fill(
                fill_qty=additional_fill,
                fill_price=avg_price,
                timestamp=update.timestamp,
                broker_order_id=update.broker_order_id,
            )
            effective_update = fill_result
            accepted_qty = fill_result.last_fill_qty
            reason = "fill delta received"
            if accepted_qty != additional_fill:
                reason = (
                    f"fill delta received; computed delta={additional_fill}, "
                    f"accepted qty={accepted_qty} (clamped)"
                )
            _get_audit_logger().log_order_transition(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                broker=order.broker,
                old_state=old_state.value,
                new_state=order.state.value,
                reason=reason,
                fill_price=avg_price,
                fill_qty=accepted_qty,
                order_id=update.broker_order_id or "",
            )
        elif update.state != order.state:
            order.transition_to(
                new_state=update.state,
                broker_order_id=update.broker_order_id,
                error_code=update.error_code,
                raw_error_message=update.raw_error_message,
                timestamp=update.timestamp,
                metadata=update.metadata,
            )
            _get_audit_logger().log_order_transition(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                broker=order.broker,
                old_state=old_state.value,
                new_state=update.state.value,
                reason=update.raw_error_message or "",
                order_id=update.broker_order_id or "",
            )

        self._dispatch_update(effective_update)
        return effective_update

    def _dispatch_update(self, update: OrderUpdate) -> None:
        for listener in self._listeners:
            try:
                res = listener(update)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.error("Error in order update listener: %s", e)

    async def reconcile_orders(self, adapter: Any) -> Dict[str, Any]:
        logger.info("[%s] Initiating order reconciliation...", adapter.broker_name)
        broker_orders: List[OrderUpdate] = await adapter.get_open_orders()
        broker_map = {o.broker_order_id: o for o in broker_orders if o.broker_order_id}

        matched = 0
        discrepancies_resolved = 0
        missing_local = 0

        # 1. Match broker orders against local book
        for b_id, b_update in broker_map.items():
            local_order = self.get_order_by_broker_id(b_id)
            if not local_order and b_update.client_order_id:
                local_order = self.get_order(b_update.client_order_id)

            if local_order:
                matched += 1
                if local_order.state != b_update.state:
                    logger.warning(
                        "[%s] Reconciliation discrepancy for order %s (Broker ID %s): Local=%s, Broker=%s. Updating local state.",
                        adapter.broker_name,
                        local_order.client_order_id,
                        b_id,
                        local_order.state.value,
                        b_update.state.value,
                    )
                    self.handle_order_update(b_update)
                    discrepancies_resolved += 1
            else:
                missing_local += 1
                logger.warning("[%s] Broker order %s not found in local book. Ingesting.", adapter.broker_name, b_id)
                new_order = Order(
                    client_order_id=b_update.client_order_id or f"EXT_{b_id}",
                    symbol=b_update.metadata.get("symbol", "UNKNOWN"),
                    exchange=b_update.metadata.get("exchange", "NSE"),
                    side=OrderSide.BUY if b_update.metadata.get("side", "BUY").upper() == "BUY" else OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=b_update.remaining_qty + b_update.filled_qty,
                    price=b_update.metadata.get("price", 0.0),
                    broker=adapter.broker_name,
                    broker_order_id=b_id,
                    state=b_update.state,
                    filled_qty=b_update.filled_qty,
                    remaining_qty=b_update.remaining_qty,
                    avg_fill_price=b_update.avg_fill_price,
                )
                self._orders[new_order.client_order_id] = new_order
                self._broker_to_client_id[b_id] = new_order.client_order_id

        # 2. Check local open orders (including UNKNOWN) not reported by broker
        open_local_orders = [
            o for o in self._orders.values()
            if o.broker.lower() == adapter.broker_name.lower()
            and o.state in (
                OrderState.OPEN,
                OrderState.PARTIALLY_FILLED,
                OrderState.CANCEL_PENDING,
                OrderState.UNKNOWN,     # primary: timeout-after-submission orders
                OrderState.SUBMITTED,   # also query if somehow stuck here
                OrderState.TIMEOUT,     # legacy: orders from prior sessions
            )
        ]

        for local_ord in open_local_orders:
            if local_ord.broker_order_id and local_ord.broker_order_id not in broker_map:
                try:
                    final_status = await adapter.get_order_status(
                        client_order_id=local_ord.client_order_id,
                        broker_order_id=local_ord.broker_order_id,
                    )
                    if final_status and final_status.state != local_ord.state:
                        self.handle_order_update(final_status)
                        discrepancies_resolved += 1
                except Exception as e:
                    logger.error("Error querying order status during reconciliation: %s", e)

        return {
            "broker": adapter.broker_name,
            "matched": matched,
            "discrepancies_resolved": discrepancies_resolved,
            "missing_local": missing_local,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


order_manager = OrderManager()
