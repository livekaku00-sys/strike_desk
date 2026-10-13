"""
Strike Desk V4 - Single-User Authentication & JWT Security Module
Phase 7: Streamlit Dashboard + Authentication (Prompt 1 of 4)
Decision Maker Memo (2026-09-21): Single-user system (Monuj only), NO multi-user complexity.
Pure standard library implementation (RFC 7519 HMAC-SHA256 JWT & PBKDF2).
"""

from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

# Secret key for signing JWT tokens
SECRET_KEY = os.environ.get(
    "STRIKE_DESK_SECRET_KEY",
    "strike_desk_monuj_quant_jwt_secret_2026_secured_institution"
)

# Machine-readable authorization roles. The human-facing job title lives in
# `role_label`; authorization decisions must only ever read `role`.
ROLE_OPERATOR = "operator"
ROLE_GUEST = "guest"

GUEST_USERNAME = "guest"

# Single authorized operator profile
SINGLE_USER = {
    "username": "monuj",
    "aliases": ["monuj", "admin"],
    "full_name": "Monuj Phukon",
    "role": ROLE_OPERATOR,
    "role_label": "Lead Quant / Operator",
}

# Configurable master password (default: StrikeDesk@2026)
DEFAULT_PASSWORD = os.environ.get("STRIKE_DESK_ADMIN_PASSWORD", "StrikeDesk@2026")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = len(s) % 4
    if pad != 0:
        s += "=" * (4 - pad)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def hash_password(password: str) -> str:
    """Hash password using PBKDF2 with 100,000 rounds of HMAC-SHA256."""
    salt = os.urandom(16)
    pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return f"{salt.hex()}${pw_hash.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verifies a plain password against the stored salt$hash."""
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
        actual_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return hmac.compare_digest(expected_hash, actual_hash)
    except Exception:
        return False


# In-memory salted hash of default password for constant-time verification
_MASTER_PASSWORD_HASH = hash_password(DEFAULT_PASSWORD)


def create_access_token(
    username: str,
    expires_hours: int = 72,
    role: str = ROLE_OPERATOR,
    full_name: Optional[str] = None,
) -> str:
    """Generate a standard RFC 7519 HMAC-SHA256 JWT access token."""
    header = {"alg": "HS256", "typ": "JWT"}
    is_guest = role == ROLE_GUEST
    payload = {
        "sub": username.lower(),
        "name": full_name or ("Guest" if is_guest else SINGLE_USER["full_name"]),
        "role": role,
        "role_label": "Read-only Guest" if is_guest else SINGLE_USER["role_label"],
        "iat": int(time.time()),
        "exp": int(time.time()) + (expires_hours * 3600),
    }

    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    message = f"{h_b64}.{p_b64}".encode("utf-8")
    signature = hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)

    return f"{h_b64}.{p_b64}.{sig_b64}"


def verify_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Validates JWT signature and expiration. Returns payload if valid, None if invalid/expired."""
    try:
        parts = token.strip().split(".")
        if len(parts) != 3:
            return None
        h_b64, p_b64, sig_b64 = parts

        message = f"{h_b64}.{p_b64}".encode("utf-8")
        expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload_bytes = _b64url_decode(p_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))

        if payload.get("exp", 0) < time.time():
            return None

        sub = payload.get("sub", "").lower()
        role = payload.get("role")

        # A guest token authenticates a read-only session and carries no operator
        # identity. Anything claiming a role other than guest must be the operator.
        if role == ROLE_GUEST:
            return payload if sub == GUEST_USERNAME else None
        if role != ROLE_OPERATOR or sub not in SINGLE_USER["aliases"]:
            return None

        return payload
    except Exception:
        return None


def authenticate_single_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Authenticate single-user credentials (Monuj).
    Checks against allowed aliases and master password.
    """
    u = username.strip().lower()
    if u not in SINGLE_USER["aliases"]:
        return None

    # Verify against live environment password or stored hash
    current_admin_pw = os.environ.get("STRIKE_DESK_ADMIN_PASSWORD")
    if current_admin_pw:
        if not hmac.compare_digest(password.encode("utf-8"), current_admin_pw.encode("utf-8")):
            return None
    else:
        if not verify_password(password, _MASTER_PASSWORD_HASH):
            return None

    return {
        "username": SINGLE_USER["username"],
        "full_name": SINGLE_USER["full_name"],
        "role": SINGLE_USER["role"],
        "role_label": SINGLE_USER["role_label"],
    }


def create_guest_token(expires_hours: int = 12) -> str:
    """
    Issues a read-only session token.

    Guests are a real, server-issued identity rather than a client-side flag, so
    the API can tell a read-only session apart from an unauthenticated one and
    answer 403 rather than 401.
    """
    return create_access_token(
        GUEST_USERNAME, expires_hours=expires_hours, role=ROLE_GUEST, full_name="Guest"
    )


def get_guest_user() -> Dict[str, Any]:
    return {
        "username": GUEST_USERNAME,
        "full_name": "Guest",
        "role": ROLE_GUEST,
        "role_label": "Read-only Guest",
    }


def get_single_user() -> Dict[str, Any]:
    """Returns metadata for the single authorized operator."""
    return {
        "username": SINGLE_USER["username"],
        "full_name": SINGLE_USER["full_name"],
        "role": SINGLE_USER["role"],
        "role_label": SINGLE_USER["role_label"],
    }
