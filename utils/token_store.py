import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

try:
    from cryptography.fernet import Fernet
    _FERNET_AVAILABLE = True
except ImportError:
    _FERNET_AVAILABLE = False
    import secrets
    import hashlib

logger = logging.getLogger(__name__)


@dataclass
class StoredToken:
    encrypted_payload: bytes
    created_at: datetime
    expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._store: Dict[str, StoredToken] = {}
        if _FERNET_AVAILABLE:
            self._fernet_key = Fernet.generate_key()
            self._cipher = Fernet(self._fernet_key)
        else:
            self._raw_key = secrets.token_bytes(32)
            self._cipher = None

    def _encrypt(self, token: str) -> bytes:
        raw_bytes = token.encode("utf-8")
        if _FERNET_AVAILABLE and self._cipher:
            return self._cipher.encrypt(raw_bytes)
        keystream = hashlib.sha256(self._raw_key).digest()
        return bytes(b ^ keystream[i % len(keystream)] for i, b in enumerate(raw_bytes))

    def _decrypt(self, payload: bytes) -> str:
        if _FERNET_AVAILABLE and self._cipher:
            return self._cipher.decrypt(payload).decode("utf-8")
        keystream = hashlib.sha256(self._raw_key).digest()
        decrypted = bytes(b ^ keystream[i % len(keystream)] for i, b in enumerate(payload))
        return decrypted.decode("utf-8")

    def store_token(
        self,
        broker: str,
        token: str,
        expires_in_seconds: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        target_expiry = expires_at
        if expires_in_seconds is not None:
            target_expiry = now + timedelta(seconds=expires_in_seconds)

        encrypted = self._encrypt(token)
        self._store[broker.lower()] = StoredToken(
            encrypted_payload=encrypted,
            created_at=now,
            expires_at=target_expiry,
            metadata=metadata or {},
        )

    def get_token(self, broker: str) -> Optional[str]:
        stored = self._store.get(broker.lower())
        if not stored:
            return None
        try:
            return self._decrypt(stored.encrypted_payload)
        except Exception:
            return None

    def is_expired(self, broker: str, buffer_seconds: int = 300) -> bool:
        stored = self._store.get(broker.lower())
        if not stored:
            return True
        if stored.expires_at is None:
            return False
        now = datetime.now(timezone.utc)
        return (now + timedelta(seconds=buffer_seconds)) >= stored.expires_at

    def get_expiry(self, broker: str) -> Optional[datetime]:
        stored = self._store.get(broker.lower())
        return stored.expires_at if stored else None

    def has_token(self, broker: str) -> bool:
        return broker.lower() in self._store

    def clear_token(self, broker: str) -> None:
        self._store.pop(broker.lower(), None)

    def clear_all(self) -> None:
        self._store.clear()


token_store = InMemoryTokenStore()
