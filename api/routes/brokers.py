"""
Strike Desk V4 - Broker Telemetry, Selectors & Multi-Path Token Management Router
Phase 7: Streamlit Dashboard + Authentication (Prompt 3 of 4)
Decision Maker Memo (2026-09-21):
- AngelOne supports auto-refresh via refresh_token.
- Dhan and Fyers require manual re-auth (username/password/TOTP) via dashboard button.
- NO TOTP secrets are to be stored in the database.
"""

import logging
from typing import Any, Dict, List, Optional
from datetime import datetime
import pytz
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Depends, status, Request

from brokers.token_manager import token_manager, BrokerTokenManager
from .auth import require_operator

IST = pytz.timezone("Asia/Kolkata")
router = APIRouter()
logger = logging.getLogger("strike_desk.api.routes.brokers")


_ADAPTER_CLASSES = {
    "dhan": ("adapters.dhan_adapter", "DhanAdapter"),
    "fyers": ("adapters.fyers_adapter", "FyersAdapter"),
    "angelone": ("adapters.angelone_adapter", "AngelOneAdapter"),
}


async def _register_new_adapter(adapters: Dict[str, Any], broker: str) -> Dict[str, Any]:
    """A broker that failed at startup has no adapter; build, authenticate and wire one."""
    import importlib
    from api.main import startup_in_progress, wire_late_adapter

    result: Dict[str, Any] = {"live": True, "authenticated": False, "reconnected": False}
    if startup_in_progress():
        # Startup iterates this same dict; adding to it now could break that pass.
        result["retry_later"] = True
        result["detail"] = ("Server is still starting up. The token is saved — "
                            "submit it again once startup completes (about a minute).")
        return result

    module_name, cls_name = _ADAPTER_CLASSES[broker]
    adapter = getattr(importlib.import_module(module_name), cls_name)()
    if not await adapter.authenticate():
        result["rejected"] = True
        result["detail"] = f"{broker} rejected the new token (authentication failed)."
        return result
    result["authenticated"] = True

    adapters[broker] = adapter
    from api.dependencies import service_container
    service_container.set_broker_adapters(adapters)
    logger.info("[%s] Registered new live adapter after re-auth", broker)

    try:
        summary = await wire_late_adapter(broker, adapter)
        result["reconnected"] = bool(getattr(adapter, "is_connected", False))
        result["detail"] = f"adapter registered without restart: {summary}"
    except Exception as exc:
        logger.error("[%s] late adapter wiring failed: %s", broker, exc)
        result["detail"] = f"authenticated, but feed wiring failed: {exc}"
    return result


async def _reconnect_adapter(request: Request, broker: str) -> Dict[str, Any]:
    """
    Re-authenticate and reconnect the live adapter after a token update, creating
    it first if the broker failed at startup, so a pasted token never needs a restart.
    """
    adapters = getattr(request.app.state, "adapters", None)
    if adapters is None:
        # The app never ran its startup sequence (tests, tooling): nothing is live.
        return {"live": False, "authenticated": False, "reconnected": False,
                "detail": "no live adapter registry (server startup not run)"}

    adapter = adapters.get(broker)
    if adapter is None:
        try:
            return await _register_new_adapter(adapters, broker)
        except Exception as exc:
            logger.error("[%s] live adapter registration failed: %s", broker, exc)
            return {"live": True, "authenticated": False, "reconnected": False,
                    "detail": f"adapter registration error: {exc}"}

    result: Dict[str, Any] = {"live": True, "authenticated": False, "reconnected": False,
                              "detail": ""}
    try:
        # Drop the cached frozen settings so from_env() re-reads os.environ.
        if hasattr(adapter, "settings"):
            adapter.settings = None

        try:
            await adapter.disconnect()
        except Exception as exc:
            logger.warning("[%s] disconnect before reconnect failed: %s", broker, exc)

        if not await adapter.authenticate():
            result["rejected"] = True
            result["detail"] = f"{broker} rejected the new token (authentication failed)."
            return result
        result["authenticated"] = True

        await adapter.connect()
        connected = bool(getattr(adapter, "is_connected", False))
        result.update(
            reconnected=connected,
            detail="adapter re-authenticated and feed reconnected" if connected
            else "adapter re-authenticated; feed NOT connected yet (WebSocket reconnecting)",
        )
    except Exception as exc:
        logger.error("[%s] live adapter reconnect failed: %s", broker, exc)
        result["detail"] = f"reconnect error: {exc}"

    return result


def _raise_if_not_live(mgr: BrokerTokenManager, broker: str, live: Dict[str, Any]) -> None:
    """Fail the request when a running server could not log in with the new token."""
    if not live.get("live") or live.get("authenticated"):
        return
    msg = f"Token saved, but it is not in use: {live.get('detail', '')}"
    mgr.record_live_auth_failure(broker, msg)
    if live.get("retry_later"):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif live.get("rejected"):
        code = status.HTTP_400_BAD_REQUEST
    else:
        code = status.HTTP_502_BAD_GATEWAY
    raise HTTPException(status_code=code, detail=msg)


class ActiveBrokerRequest(BaseModel):
    broker: str = Field(..., description="Target active broker (angelone, dhan, fyers)")


class AngelOneRefreshRequest(BaseModel):
    refresh_token: Optional[str] = Field(None, description="Optional override refresh token")


class DhanReauthRequest(BaseModel):
    """Dhan has no TOTP flow — the operator pastes a token from api.dhan.co."""
    access_token: str = Field(..., min_length=1, description="Access token from api.dhan.co")
    client_id: Optional[str] = Field(None, description="Defaults to DHAN_CLIENT_ID from env")


class FyersReauthRequest(BaseModel):
    """Fyers has no TOTP flow — the operator pastes a token from myapi.fyers.in."""
    access_token: str = Field(..., min_length=1, description="Access token from myapi.fyers.in")
    client_id: Optional[str] = Field(None, description="Defaults to FYERS_CLIENT_ID from env")


class BrokerStatusResponse(BaseModel):
    broker_name: str
    is_connected: bool
    latency_ms: Optional[float] = None
    last_heartbeat: Optional[datetime] = None
    error_message: Optional[str] = None
    # Token lifecycle, so the dashboard can distinguish a healthy token from
    # one that actually needs the operator to paste a new one.
    token_status: Optional[str] = None
    token_expires_in: Optional[str] = None
    auth_type: Optional[str] = None
    auto_refresh_supported: bool = False


def get_token_mgr() -> BrokerTokenManager:
    return token_manager


# ---------------------------------------------------------------------
# Active Broker Selector Endpoints (Prompt 3 of 4)
# ---------------------------------------------------------------------
@router.get("/active")
async def get_active_broker(mgr: BrokerTokenManager = Depends(get_token_mgr)):
    """GET /api/v1/brokers/active: Returns currently active routing broker."""
    curr = mgr.get_active_broker()
    info = mgr.get_broker_token_status(curr) or {}
    return {
        "active_broker": curr,
        "display_name": info.get("display_name", curr.upper()),
        "is_authenticated": info.get("is_authenticated", False),
        "status": info.get("status", "UNKNOWN"),
        "latency_ms": info.get("latency_ms", 15.0),
        "timestamp": datetime.now(IST).isoformat(),
    }


@router.post("/active")
async def set_active_broker(
    req: ActiveBrokerRequest,
    mgr: BrokerTokenManager = Depends(get_token_mgr),
    _op: Dict[str, Any] = Depends(require_operator),
):
    """POST /api/v1/brokers/active: Switches active routing broker."""
    ok, msg = mgr.set_active_broker(req.broker)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    return {
        "success": True,
        "active_broker": req.broker.lower(),
        "message": msg,
        "timestamp": datetime.now(IST).isoformat(),
    }


# ---------------------------------------------------------------------
# Phase 6 Compatible Telemetry Endpoints
# ---------------------------------------------------------------------
@router.get("", response_model=List[BrokerStatusResponse])
async def list_brokers(mgr: BrokerTokenManager = Depends(get_token_mgr)):
    """GET /api/v1/brokers: Returns list of configured brokers and connection status."""
    statuses = mgr.get_all_token_statuses()
    results = []
    for s in statuses:
        results.append(
            BrokerStatusResponse(
                broker_name=s["broker_name"],
                is_connected=s.get("is_authenticated", False),
                latency_ms=s.get("latency_ms", 15.0),
                last_heartbeat=datetime.now(IST),
                error_message=s.get("error_message"),
                token_status=s.get("status"),
                token_expires_in=s.get("expires_in"),
                auth_type=s.get("auth_type"),
                auto_refresh_supported=bool(s.get("auto_refresh_supported", False)),
            )
        )
    return results


# ---------------------------------------------------------------------
# Multi-Path Token Management Endpoints
# ---------------------------------------------------------------------
@router.get("/tokens/status")
async def get_token_statuses(request: Request, mgr: BrokerTokenManager = Depends(get_token_mgr)):
    """
    GET /api/v1/brokers/tokens/status: Returns detailed multi-path session metadata,
    distinguishing AUTO_REFRESH (AngelOne) from MANUAL_REAUTH (Dhan & Fyers).
    Also cross-references the live adapter is_connected flag so the status reflects
    the actual feed state, not just the token lifecycle state.
    """
    statuses = mgr.get_all_token_statuses()

    # Augment with real-time adapter feed state
    adapters = getattr(request.app.state, "adapters", None) or {}
    for s in statuses:
        adapter = adapters.get(s["broker_name"])
        if adapter is not None:
            feed_alive = getattr(adapter, "is_connected", None)
            s["feed_alive"] = feed_alive
            # If token looks ACTIVE but feed is down, surface that
            if feed_alive is False and s.get("status") in ("ACTIVE", "EXPIRING"):
                s["status"] = "ERROR"
                if not s.get("error_message"):
                    s["error_message"] = "Feed disconnected — adapter not streaming"
        else:
            s["feed_alive"] = None

    return {
        "timestamp": datetime.now(IST).isoformat(),
        "active_broker": mgr.get_active_broker(),
        "brokers": statuses,
    }


@router.post("/angelone/refresh")
async def refresh_angelone(
    request: Request,
    req: Optional[AngelOneRefreshRequest] = None,
    mgr: BrokerTokenManager = Depends(get_token_mgr),
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    POST /api/v1/brokers/angelone/refresh:
    Path 1: Automatic token renewal using refresh_token (No user/TOTP prompt required).
    Also reconnects the live adapter and WS feed so ticks resume immediately.
    """
    rf = req.refresh_token if req else None
    success, message, data = mgr.refresh_angelone_token(refresh_token=rf)
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    live = await _reconnect_adapter(request, "angelone")
    _raise_if_not_live(mgr, "angelone", live)

    return {
        "success": True,
        "broker": "angelone",
        "auth_type": "AUTO_REFRESH",
        "message": message,
        "data": data,
        "feed_reconnected": live.get("reconnected", False),
        "feed_detail": live.get("detail", ""),
        "timestamp": datetime.now(IST).isoformat(),
    }


@router.post("/dhan/reauth")
async def reauth_dhan(
    req: DhanReauthRequest,
    request: Request,
    mgr: BrokerTokenManager = Depends(get_token_mgr),
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    POST /api/v1/brokers/dhan/reauth — body: {"access_token": "..."}
    Dhan has no TOTP flow; the operator pastes a token from api.dhan.co.
    The token is written to .env + os.environ and the live adapter is
    re-authenticated and reconnected.
    """
    success, message, data = mgr.reauth_dhan(
        client_id=req.client_id,
        access_token=req.access_token,
    )
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    live = await _reconnect_adapter(request, "dhan")
    _raise_if_not_live(mgr, "dhan", live)

    # Clear any live-mode block that was set because the previous token expired.
    try:
        from services.token_scheduler import token_scheduler as _ts
        _ts.clear_live_mode_block("dhan")
    except Exception:
        pass

    return {
        "success": True,
        "broker": "dhan",
        "auth_type": "MANUAL_REAUTH",
        "message": message,
        "data": data,
        "live_adapter": live,
        "timestamp": datetime.now(IST).isoformat(),
    }


@router.post("/fyers/reauth")
async def reauth_fyers(
    req: FyersReauthRequest,
    request: Request,
    mgr: BrokerTokenManager = Depends(get_token_mgr),
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    POST /api/v1/brokers/fyers/reauth — body: {"access_token": "..."}
    Fyers has no TOTP flow; the operator pastes a token from myapi.fyers.in.
    The token is written to .env + os.environ and the live adapter is
    re-authenticated and reconnected.
    """
    success, message, data = mgr.reauth_fyers(
        client_id=req.client_id,
        access_token=req.access_token,
    )
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    live = await _reconnect_adapter(request, "fyers")
    _raise_if_not_live(mgr, "fyers", live)

    # Clear any live-mode block that was set because the previous token expired.
    try:
        from services.token_scheduler import token_scheduler as _ts
        _ts.clear_live_mode_block("fyers")
    except Exception:
        pass

    return {
        "success": True,
        "broker": "fyers",
        "auth_type": "MANUAL_REAUTH",
        "message": message,
        "data": data,
        "live_adapter": live,
        "timestamp": datetime.now(IST).isoformat(),
    }


@router.get("/health")
async def get_broker_health(mgr: BrokerTokenManager = Depends(get_token_mgr)):
    """
    GET /api/v1/brokers/health

    Per-broker token age, remaining lifetime, refresh status, and LIVE-mode block flag.
    Safe for monitoring dashboards — no token values are returned.
    """
    try:
        from services.token_scheduler import token_scheduler as _ts
        return _ts.get_health_status()
    except Exception as exc:
        logger.warning("[BrokerHealth] token_scheduler unavailable: %s", exc)
        # Fallback: derive health from token_manager alone.
        import pytz as _pytz
        _IST = _pytz.timezone("Asia/Kolkata")
        _now = datetime.now(_IST)
        brokers = []
        for sess in mgr.get_all_token_statuses():
            brokers.append({
                "broker": sess.get("broker_name"),
                "token_status": sess.get("status", "UNKNOWN"),
                "auth_type": sess.get("auth_type"),
                "auto_refresh_supported": sess.get("auto_refresh_supported", False),
                "expires_in": sess.get("expires_in"),
                "live_mode_blocked": False,
                "block_reason": None,
                "token_age_seconds": None,
                "token_remaining_seconds": None,
            })
        return {
            "checked_at": _now.isoformat(),
            "last_daily_check": None,
            "next_check_in_seconds": None,
            "any_live_mode_blocked": False,
            "brokers": brokers,
        }


@router.post("/{broker_name}/disconnect")
async def disconnect_broker(
    broker_name: str,
    mgr: BrokerTokenManager = Depends(get_token_mgr),
    _op: Dict[str, Any] = Depends(require_operator),
):
    """POST /api/v1/brokers/{broker_name}/disconnect: Clean session termination."""
    success, message = mgr.disconnect_broker(broker_name)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)

    return {
        "success": True,
        "broker": broker_name.lower(),
        "message": message,
        "timestamp": datetime.now(IST).isoformat(),
    }


@router.get("/{broker_name}/status", response_model=BrokerStatusResponse)
async def get_single_broker_status(
    broker_name: str,
    mgr: BrokerTokenManager = Depends(get_token_mgr),
):
    """GET /api/v1/brokers/{broker_name}/status: Single broker detail."""
    b_stat = mgr.get_broker_token_status(broker_name)
    if not b_stat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Broker '{broker_name}' not configured in Strike Desk.",
        )

    return BrokerStatusResponse(
        broker_name=b_stat["broker_name"],
        is_connected=b_stat.get("is_authenticated", False),
        latency_ms=b_stat.get("latency_ms", 15.0),
        last_heartbeat=datetime.now(IST),
        error_message=b_stat.get("error_message"),
    )
