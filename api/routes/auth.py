"""
Strike Desk V4 - Single-User Authentication Router
Phase 7: Streamlit Dashboard + Authentication (Prompt 1 of 4)
Decision Maker Memo (2026-09-21): Single-user system (Monuj only), NO multi-user complexity.
"""

from typing import Dict, Any, Optional
from datetime import datetime
import asyncio
import logging
import time
import pytz
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Header, Depends, status

from security.auth import (
    ROLE_GUEST,
    ROLE_OPERATOR,
    authenticate_single_user,
    create_access_token,
    create_guest_token,
    verify_access_token,
    get_single_user,
)

logger = logging.getLogger("strike_desk.api.routes.auth")

IST = pytz.timezone("Asia/Kolkata")
router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    username: str
    full_name: str
    role: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 259200  # 72 hours
    user: UserInfo
    timestamp: datetime


class UserProfileResponse(BaseModel):
    username: str
    full_name: str
    role: str
    status: str = "AUTHENTICATED"
    timestamp: datetime


def get_current_user_payload(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Dependency validating Bearer token from Authorization header."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing. Bearer token required.",
        )
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization scheme. Use 'Bearer <token>'.",
        )
    token = parts[1]
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, tampered, or expired access token.",
        )
    return payload


def require_operator(payload: Dict[str, Any] = Depends(get_current_user_payload)) -> Dict[str, Any]:
    """
    Gate for every state-changing endpoint: placing or squaring off orders, changing
    the execution lock, and broker/risk administration.

    401 means "no usable session"; 403 means "this session is read-only". Keeping
    them distinct matters because the web client force-logs-out on 401, and a guest
    hitting a restricted control should be told no, not signed out.
    """
    role = payload.get("role")
    if role == ROLE_GUEST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Guest sessions are read-only. Sign in as the operator to trade "
                   "or change broker settings.",
        )
    if role != ROLE_OPERATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an operator session.",
        )
    return payload


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """
    POST /api/v1/auth/login: Authenticate single operator (Monuj) and generate JWT.
    """
    t0 = time.monotonic()
    logger.info("[AUTH/login] Request received for username='%s'", req.username)

    # authenticate_single_user may call hashlib.pbkdf2_hmac (100k rounds, ~150ms)
    # when STRIKE_DESK_ADMIN_PASSWORD is not set.  Running it in a thread keeps the
    # event loop free to serve other requests (e.g., health checks, guest login)
    # during the CPU-bound hash computation.
    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, authenticate_single_user, req.username, req.password)
    logger.info("[AUTH/login] authenticate_single_user completed in %.3fs — result=%s",
                time.monotonic() - t0, "ok" if user else "rejected")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operator credentials.",
        )

    token = create_access_token(user["username"], expires_hours=72)
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=72 * 3600,
        user=UserInfo(
            username=user["username"],
            full_name=user["full_name"],
            role=user["role"],
        ),
        timestamp=datetime.now(IST),
    )


@router.post("/guest", response_model=LoginResponse)
async def login_guest():
    """
    POST /api/v1/auth/guest: Issue a read-only session token.

    Guests get a real signed token so the API can distinguish a read-only session
    from an unauthenticated one; every mutating endpoint rejects it with 403.
    This route is intentionally lightweight — no broker calls, no I/O.
    """
    t0 = time.monotonic()
    token = create_guest_token(expires_hours=12)
    logger.info("[AUTH/guest] Token issued in %.4fs", time.monotonic() - t0)
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=12 * 3600,
        user=UserInfo(username="guest", full_name="Guest", role=ROLE_GUEST),
        timestamp=datetime.now(IST),
    )


@router.get("/me", response_model=UserProfileResponse)
async def get_current_operator(payload: Dict[str, Any] = Depends(get_current_user_payload)):
    """
    GET /api/v1/auth/me: Returns current authenticated operator session details.
    """
    return UserProfileResponse(
        username=payload.get("sub", ""),
        full_name=payload.get("name", ""),
        role=payload.get("role", ROLE_GUEST),
        status="AUTHENTICATED",
        timestamp=datetime.now(IST),
    )


@router.post("/logout")
async def logout():
    """
    POST /api/v1/auth/logout: Terminal session logout.
    """
    return {
        "status": "LOGGED_OUT",
        "message": "Operator session terminated.",
        "timestamp": datetime.now(IST).isoformat(),
    }


@router.get("/token-status")
async def get_broker_token_status():
    """
    GET /api/v1/auth/token-status: Real-time adapter auth + feed connection status.
    Reads directly from the live adapter objects, not just token_manager state,
    so 'is_connected' reflects the actual WebSocket feed health.
    """
    from brokers.token_manager import token_manager as _tm
    from adapters.auth_manager import auth_manager as _am

    statuses = []
    broker_names = ["angelone", "dhan", "fyers"]

    for name in broker_names:
        sess = _tm.get_broker_token_status(name) or {}
        auth_status = _am.get_auth_status(name)
        # `expires_in` and `status` are derived from when the token was stored
        # locally, not from whether the broker accepts it — a token the exchange has
        # rejected still reads "ACTIVE, 23h left". AuthManager's result is the only
        # field that reflects a real handshake, so it decides `is_authenticated`.
        stored_ok = bool(sess.get("is_authenticated", False))
        broker_accepted = str(auth_status).lower() == "authenticated"
        statuses.append({
            "broker":          name,
            "display_name":    sess.get("display_name", name.title()),
            "auth_type":       sess.get("auth_type", "MANUAL_REAUTH"),
            "auth_status":     auth_status,          # from AuthManager: authenticated/failed/token_expired
            "is_authenticated": stored_ok and broker_accepted,
            "has_stored_token": stored_ok,
            "token_status":    sess.get("status", "UNKNOWN") if broker_accepted else "REJECTED_BY_BROKER",
            "expires_in":      sess.get("expires_in", "—") if broker_accepted else "—",
            "error_message":   sess.get("error_message"),
            "client_id":       sess.get("client_id", ""),
        })

    return {
        "timestamp": datetime.now(IST).isoformat(),
        "active_broker": _tm.get_active_broker(),
        "brokers": statuses,
    }
