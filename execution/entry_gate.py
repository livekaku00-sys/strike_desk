"""
Entry slot gate for one-position-at-a-time enforcement.

The auto-execute loop already checks RiskManager.open_positions_count (Layer 1).
This module provides Layer 2: block when a BUY entry order is in-flight (SUBMITTED,
OPEN, PARTIALLY_FILLED, CANCEL_PENDING, UNKNOWN, or TIMEOUT) but no ActivePosition
record exists yet in RiskManager — which happens because the position record is only
created after the fill WebSocket callback fires.

Gap closed: a SUBMITTED order that times out to UNKNOWN has no matching ActivePosition.
Without this check, _open_count == 0 and the loop would attempt a second entry before
the first order's fate is confirmed by the broker.

Only BUY orders are checked. SELL orders are exits and never occupy the entry slot.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from adapters.order_manager import OrderManager

logger = logging.getLogger(__name__)

# States where a BUY entry order is in-flight but has no position record yet.
# Populated lazily to avoid circular imports at module load time.
_INFLIGHT_ENTRY_STATES: frozenset | None = None


def _get_inflight_states() -> frozenset:
    global _INFLIGHT_ENTRY_STATES
    if _INFLIGHT_ENTRY_STATES is None:
        from models.order import OrderState
        _INFLIGHT_ENTRY_STATES = frozenset({
            OrderState.SUBMITTED,
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.UNKNOWN,
            OrderState.TIMEOUT,
        })
    return _INFLIGHT_ENTRY_STATES


def is_entry_blocked_by_inflight_orders(order_manager: "OrderManager") -> Tuple[bool, str]:
    """
    Returns (blocked: bool, reason: str).

    blocked=True when at least one BUY order is in a limbo state that indicates
    an entry is in progress but its fill (or cancellation) has not yet been
    confirmed by the broker and recorded in RiskManager.

    This function does NOT check open_positions_count — the caller (auto execute
    loop) already handles that via Layer 1.
    """
    from models.order import OrderSide

    inflight_states = _get_inflight_states()
    try:
        inflight: List = [
            o for o in order_manager.get_open_orders()
            if getattr(o, "side", None) == OrderSide.BUY
            and getattr(o, "state", None) in inflight_states
        ]
    except Exception as exc:
        logger.warning("[EntryGate] in-flight order query failed: %s", exc)
        return False, ""

    if not inflight:
        return False, ""

    summary = ", ".join(
        f"{str(getattr(o, 'client_order_id', '?'))[:12]}:{getattr(o, 'state', '?')}"
        for o in inflight
    )
    return True, f"in-flight BUY entries ({len(inflight)}): {summary}"
