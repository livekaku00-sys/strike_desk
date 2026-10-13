import asyncio
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

from utils.token_store import InMemoryTokenStore, token_store

logger = logging.getLogger(__name__)


class AuthStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    AUTHENTICATING = "authenticating"
    FAILED = "failed"
    TOKEN_EXPIRED = "token_expired"


class AuthManager:
    def __init__(
        self,
        store: Optional[InMemoryTokenStore] = None,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        max_backoff: float = 8.0,
        request_timeout: float = 30.0,
        expiry_buffer_seconds: int = 300,
    ) -> None:
        self.store = store or token_store
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.request_timeout = request_timeout
        self.expiry_buffer_seconds = expiry_buffer_seconds

        self._statuses: Dict[str, AuthStatus] = {}
        self._auth_locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, broker: str) -> asyncio.Lock:
        broker_key = broker.lower()
        if broker_key not in self._auth_locks:
            self._auth_locks[broker_key] = asyncio.Lock()
        return self._auth_locks[broker_key]

    def get_auth_status(self, broker: str) -> str:
        broker_key = broker.lower()
        current_status = self._statuses.get(broker_key)

        if current_status == AuthStatus.AUTHENTICATING:
            return AuthStatus.AUTHENTICATING.value

        if not self.store.has_token(broker_key):
            return current_status.value if current_status else AuthStatus.FAILED.value

        if self.store.is_expired(broker_key, buffer_seconds=self.expiry_buffer_seconds):
            self._statuses[broker_key] = AuthStatus.TOKEN_EXPIRED
            return AuthStatus.TOKEN_EXPIRED.value

        return AuthStatus.AUTHENTICATED.value

    async def execute_with_retry(
        self,
        broker: str,
        auth_coro_func: Callable[[], Awaitable[Any]],
    ) -> bool:
        broker_key = broker.lower()
        lock = self._get_lock(broker_key)

        async with lock:
            self._statuses[broker_key] = AuthStatus.AUTHENTICATING
            attempt = 0

            while attempt < self.max_retries:
                attempt += 1
                try:
                    result = await asyncio.wait_for(auth_coro_func(), timeout=self.request_timeout)
                    if result:
                        self._statuses[broker_key] = AuthStatus.AUTHENTICATED
                        return True
                    else:
                        raise RuntimeError(f"Auth returned falsy: {result}")
                except Exception as exc:
                    logger.error("Auth attempt %d failed for %s: %s", attempt, broker, exc)

                if attempt < self.max_retries:
                    delay = min(self.base_backoff * (2 ** (attempt - 1)), self.max_backoff)
                    await asyncio.sleep(delay)

            self._statuses[broker_key] = AuthStatus.FAILED
            return False


auth_manager = AuthManager()
