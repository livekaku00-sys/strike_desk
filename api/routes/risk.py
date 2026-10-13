"""
Strike Desk V4 - Institutional Risk Management Router (Blueprint O5)

P0-05: Added operator-facing endpoints:
  POST /stop-entry               — block new order entry (STOP ENTRY / SOFT_HALT)
  POST /flatten-all/request-token — generate 60s confirmation token for FLATTEN ALL
  POST /flatten-all/confirm       — execute FLATTEN ALL (requires valid token)

Existing endpoints kept for backward compatibility.
"""
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List

import pytz
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..dependencies import get_broker_adapters
from .auth import require_operator

router = APIRouter()
IST = pytz.timezone("Asia/Kolkata")

_risk_audit_log: List[Dict[str, Any]] = []


def _get_risk_manager():
    try:
        from risk.risk_manager import risk_manager
        return risk_manager
    except Exception:
        return None


def _get_halt_manager():
    from risk.emergency_halt import emergency_halt_manager
    return emergency_halt_manager


# ── Request models ────────────────────────────────────────────────────────────

class HaltRequest(BaseModel):
    reason: str = "Manual circuit breaker"
    level: str = "HARD_HALT"
    operator_id: str = "MONUJ"


class TokenRequest(BaseModel):
    operator_id: str = "MONUJ"


class ResetRequest(BaseModel):
    token: str
    operator_id: str = "MONUJ"
    resolution_notes: str = ""


class StopEntryRequest(BaseModel):
    reason: str = "Manual stop entry"
    operator_id: str = "MONUJ"


class FlattenConfirmRequest(BaseModel):
    token: str
    operator_id: str = "MONUJ"
    reason: str = "Operator FLATTEN ALL"


# ── Existing endpoints (backward-compatible) ──────────────────────────────────

@router.get("/state")
async def get_risk_state():
    rm = _get_risk_manager()
    if rm and hasattr(rm, "get_risk_summary"):
        return rm.get_risk_summary()
    return {
        "ist_date": datetime.now(IST).strftime("%Y-%m-%d"),
        "realized_pnl": 0.0,
        "total_risk_pnl": 0.0,
        "daily_loss_ceiling": -2500.0,
        "loss_ceiling_breached": False,
        "trade_count": 0,
        "max_daily_trades": 5,
        "consecutive_losses": 0,
        "cooldown_active": False,
        "cooldown_until": None,
        "emergency_halt_active": False,
        "emergency_halt_reason": None,
        "open_positions_count": 0,
    }


@router.post("/halt")
async def trigger_emergency_halt(
    req: HaltRequest,
    _op: Dict[str, Any] = Depends(require_operator),
):
    hm = _get_halt_manager()
    from risk.emergency_halt import HaltLevel
    try:
        level = HaltLevel(req.level)
    except ValueError:
        level = HaltLevel.HARD_HALT
    hm.trigger_halt(reason=req.reason, level=level, triggered_by=req.operator_id)
    _risk_audit_log.append({
        "action": "HALT",
        "operator": req.operator_id,
        "reason": req.reason,
        "level": req.level,
        "timestamp": datetime.now(IST).isoformat(),
    })
    return {
        "success": True,
        "message": "Emergency halt engaged",
        "status": "HALTED",
        "is_halted": True,
    }


@router.post("/acknowledge/request-token")
async def request_ack_token(
    req: TokenRequest,
    _op: Dict[str, Any] = Depends(require_operator),
):
    hm = _get_halt_manager()
    try:
        token = hm.request_acknowledgment_token(req.operator_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"success": True, "token": token, "operator_id": req.operator_id}


@router.post("/acknowledge/reset")
async def reset_emergency_halt(
    req: ResetRequest,
    _op: Dict[str, Any] = Depends(require_operator),
):
    hm = _get_halt_manager()
    try:
        hm.acknowledge_and_reset(
            operator_id=req.operator_id,
            token=req.token,
            resolution_notes=req.resolution_notes,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    _risk_audit_log.append({
        "action": "RESET",
        "operator": req.operator_id,
        "notes": req.resolution_notes,
        "timestamp": datetime.now(IST).isoformat(),
    })
    return {"success": True, "message": "Emergency halt cleared", "status": "NORMAL", "is_halted": False}


@router.get("/audit-log")
async def get_risk_audit_log():
    return {"events": _risk_audit_log, "count": len(_risk_audit_log)}


# ── New operator-facing endpoints (P0-05) ─────────────────────────────────────

@router.post("/stop-entry")
async def stop_entry(
    req: StopEntryRequest,
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    Block all new order entry while keeping existing positions open.
    Maps to SOFT_HALT internally. No confirmation token required.
    """
    hm = _get_halt_manager()
    event = hm.stop_entry(reason=req.reason, operator_id=req.operator_id)
    _risk_audit_log.append({
        "action": "STOP_ENTRY",
        "operator": req.operator_id,
        "reason": req.reason,
        "event_id": event.event_id,
        "timestamp": datetime.now(IST).isoformat(),
    })
    return {
        "success": True,
        "message": "Stop entry engaged — new orders blocked, positions held.",
        "event_id": event.event_id,
        "halt_type": "STOP_ENTRY",
    }


@router.post("/flatten-all/request-token")
async def request_flatten_token(
    req: TokenRequest,
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    Generate a one-time confirmation token for FLATTEN ALL.
    The token expires in 60 seconds — /flatten-all/confirm must be called before then.
    """
    hm = _get_halt_manager()
    try:
        token = hm.request_flatten_confirmation_token(req.operator_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {
        "success": True,
        "token": token,
        "operator_id": req.operator_id,
        "expires_in_seconds": 60,
        "warning": "FLATTEN ALL will cancel ALL pending orders and close ALL open positions.",
    }


async def _flatten_paper(
    paper: Any,
    positions_db: Dict[str, Any],
    record_fill: Any,
    mc: Any,
    event_id: str,
    now_ist: str,
    _logger: Any,
) -> Dict[str, Any]:
    """
    Close every open paper position.  Never raises.
    """
    from models.order import Order, OrderSide, OrderType

    closes: List[Dict[str, Any]] = []
    open_positions = [
        (sym, pos) for sym, pos in paper.positions.items() if pos.quantity != 0
    ]
    for sym, pos in open_positions:
        exit_side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        exit_px = paper._ltp_provider(sym) or pos.avg_price
        if not exit_px or exit_px <= 0:
            closes.append({
                "symbol": sym,
                "outcome": "FAILED",
                "reason": "no_live_price",
                "quantity": abs(pos.quantity),
            })
            _logger.error("[FlattenAll] PAPER %s: no exit price available", sym)
            continue
        exit_order = Order(
            client_order_id=str(uuid.uuid4()),
            symbol=sym,
            exchange="NSE",
            side=exit_side,
            order_type=OrderType.LIMIT,
            quantity=float(abs(pos.quantity)),
            price=float(exit_px),
            broker="paper",
        )
        if pos.contract:
            exit_order.metadata = dict(pos.contract)
        try:
            update = await paper.place_order(exit_order)
            fill_px = float(update.avg_fill_price)
            oid = update.broker_order_id
            realized = float((update.metadata or {}).get("realized_pnl") or 0.0)
            record_fill(exit_order, update, oid, fill_px, mc.active_mode)
            closes.append({
                "symbol": sym,
                "outcome": "CLOSED",
                "fill_price": round(fill_px, 2),
                "realized_pnl": round(realized, 2),
                "quantity": abs(pos.quantity),
            })
            _logger.warning(
                "[FlattenAll] PAPER %s CLOSED @ %.2f (P&L %.2f) — FLATTEN ALL",
                sym, fill_px, realized,
            )
        except Exception as exc:
            closes.append({
                "symbol": sym,
                "outcome": "FAILED",
                "reason": str(exc)[:200],
                "quantity": abs(pos.quantity),
            })
            _logger.error("[FlattenAll] PAPER %s exit failed: %s", sym, exc)

    # Sync _positions_db once, after all closes
    positions_db.clear()
    for p in paper.get_positions_dict():
        positions_db[p["symbol"]] = p

    unresolved = [c["symbol"] for c in closes if c["outcome"] != "CLOSED"]
    return {
        "mode": "PAPER",
        "paper_closes": closes,
        "unresolved_symbols": unresolved,
        "closed_count": sum(1 for c in closes if c["outcome"] == "CLOSED"),
        "failed_count": len(unresolved),
        "executed_at": now_ist,
        "event_id": event_id,
    }


async def _flatten_live(
    adapters: Dict[str, Any],
    orders_db: Dict[str, Any],
    positions_db: Dict[str, Any],
    record_fill: Any,
    select_adapter: Any,
    mc: Any,
    event_id: str,
    now_ist: str,
    _logger: Any,
) -> Dict[str, Any]:
    """Live order dispatch is permanently prohibited — this function always raises."""
    from utils.live_order_guard import prohibit_live_order
    prohibit_live_order("_flatten_live")


async def _execute_flatten_positions(adapters: Dict[str, Any], event_id: str) -> Dict[str, Any]:
    """
    Flatten all open positions through the paper ledger.
    Live order dispatch is permanently prohibited; the LIVE path raises unconditionally.
    """
    _logger = logging.getLogger(__name__)
    now_ist = datetime.now(IST).isoformat()

    from api.routes.trading import (
        execution_mode as _exec_mode,
        get_paper_broker,
        _orders_db,
        _positions_db,
        record_fill_in_risk,
        _select_active_adapter,
        mode_controller as _mc,
    )

    if _exec_mode.is_paper():
        return await _flatten_paper(
            get_paper_broker(), _positions_db, record_fill_in_risk,
            _mc, event_id, now_ist, _logger,
        )
    # This branch can only be reached if ExecutionModeController.set() is bypassed.
    # _flatten_live raises LiveOrderProhibitedError unconditionally as a second layer.
    return await _flatten_live(
        adapters, _orders_db, _positions_db, record_fill_in_risk,
        _select_active_adapter, _mc, event_id, now_ist, _logger,
    )


@router.post("/flatten-all/confirm")
async def confirm_flatten_all(
    req: FlattenConfirmRequest,
    adapters: Dict[str, Any] = Depends(get_broker_adapters),
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    Execute FLATTEN ALL — cancel all pending orders and close all open positions.
    Requires a valid, unexpired token from /flatten-all/request-token.
    Returns 403 if the token is invalid, expired, or operator does not match.
    """
    hm = _get_halt_manager()
    try:
        event = hm.flatten_all(
            operator_id=req.operator_id,
            token=req.token,
            reason=req.reason,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    _risk_audit_log.append({
        "action": "FLATTEN_ALL",
        "operator": req.operator_id,
        "reason": req.reason,
        "event_id": event.event_id,
        "timestamp": datetime.now(IST).isoformat(),
    })
    execution = await _execute_flatten_positions(adapters, event.event_id)
    return {
        "success": True,
        "message": "FLATTEN ALL executed — halt engaged, positions closed.",
        "event_id": event.event_id,
        "halt_type": "FLATTEN_ALL",
        "execution": execution,
    }


@router.get("/operator-status")
async def get_operator_status():
    """UI-facing halt status. HARD_HALT is internal and not exposed here."""
    hm = _get_halt_manager()
    return hm.get_operator_status()


# ── P0-04: Position Reconciliation endpoints ──────────────────────────────────

def _get_reconciler():
    from services.position_reconciler import position_reconciler
    return position_reconciler


class ReconcileRequest(BaseModel):
    internal: Dict[str, float]
    broker: Dict[str, float]
    broker_name: str = "unknown"


class ReconcileClearRequest(BaseModel):
    operator_id: str = "MONUJ"


@router.post("/reconcile")
async def run_reconciliation(
    req: ReconcileRequest,
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    Compare internal position ledger against broker positions.
    On mismatch, sets an active block on all new order placement (HTTP 409)
    and logs the full diff.  Detection only — does not auto-correct.
    """
    pr = _get_reconciler()
    result = pr.reconcile(req.internal, req.broker, req.broker_name)
    if not result.is_match:
        _risk_audit_log.append({
            "action": "RECONCILE_MISMATCH",
            "broker": req.broker_name,
            "diff_count": len(result.diffs),
            "diffs": [d.to_dict() for d in result.diffs],
            "timestamp": datetime.now(IST).isoformat(),
        })
    return result.to_dict()


@router.post("/reconcile/clear")
async def clear_reconciliation_mismatch(
    req: ReconcileClearRequest,
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    Operator clears the position mismatch flag after manually resolving the
    discrepancy.  Order placement is unblocked immediately.
    """
    pr = _get_reconciler()
    pr.clear_mismatch(req.operator_id)
    _risk_audit_log.append({
        "action": "RECONCILE_CLEAR",
        "operator": req.operator_id,
        "timestamp": datetime.now(IST).isoformat(),
    })
    return {
        "success": True,
        "message": "Position reconciliation mismatch flag cleared. Order placement unblocked.",
        "operator_id": req.operator_id,
    }


@router.get("/reconcile/status")
async def get_reconciliation_status():
    """Current reconciliation state — mismatch flag, last result, diff details."""
    pr = _get_reconciler()
    return pr.get_status()
