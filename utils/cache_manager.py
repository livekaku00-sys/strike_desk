from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class DataFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"


@dataclass
class CacheEntry:
    data: Any
    underlying: str
    expiry_date: str
    cached_at: float
    last_update_time: datetime


class OptionChainCacheManager:
    DEFAULT_TTL_SECONDS = 60.0
    FRESH_THRESHOLD_SECONDS = 30.0
    STALE_WARNING_SECONDS = 120.0

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[Tuple[str, str], CacheEntry] = {}
        self._active_expiries: Dict[str, str] = {}

    def _make_key(self, underlying: str, expiry_date: str) -> Tuple[str, str]:
        return (underlying.strip().upper(), expiry_date.strip().upper())

    def get(self, underlying: str, expiry_date: str, force_refresh: bool = False) -> Optional[Any]:
        key = self._make_key(underlying, expiry_date)
        if force_refresh:
            return None
        entry = self._cache.get(key)
        if not entry:
            return None
        age = time.monotonic() - entry.cached_at
        if age > self.STALE_WARNING_SECONDS:
            logger.warning("[Cache ALERT] Severely stale data for %s (%s)", underlying, expiry_date)
        if age < self.ttl_seconds:
            return entry.data
        return None

    def put(self, underlying: str, expiry_date: str, data: Any) -> None:
        u = underlying.strip().upper()
        exp = expiry_date.strip().upper()
        key = (u, exp)

        prev_exp = self._active_expiries.get(u)
        if prev_exp is not None and prev_exp != exp:
            self.invalidate(u, prev_exp)

        self._active_expiries[u] = exp
        now_mono = time.monotonic()
        now_utc = datetime.now(timezone.utc)

        self._cache[key] = CacheEntry(
            data=data,
            underlying=u,
            expiry_date=exp,
            cached_at=now_mono,
            last_update_time=now_utc,
        )

    def get_freshness(self, underlying: str, expiry_date: str) -> str:
        key = self._make_key(underlying, expiry_date)
        entry = self._cache.get(key)
        if not entry:
            return DataFreshness.EXPIRED.value
        age = time.monotonic() - entry.cached_at
        if age < self.FRESH_THRESHOLD_SECONDS:
            return DataFreshness.FRESH.value
        if age < self.ttl_seconds:
            return DataFreshness.STALE.value
        return DataFreshness.EXPIRED.value

    def get_last_update_time(self, underlying: str, expiry_date: str) -> Optional[datetime]:
        key = self._make_key(underlying, expiry_date)
        entry = self._cache.get(key)
        return entry.last_update_time if entry else None

    def invalidate(self, underlying: str, expiry_date: Optional[str] = None) -> None:
        u = underlying.strip().upper()
        if expiry_date:
            key = self._make_key(u, expiry_date)
            self._cache.pop(key, None)
        else:
            keys_to_del = [k for k in self._cache if k[0] == u]
            for k in keys_to_del:
                self._cache.pop(k, None)

    def clear(self) -> None:
        self._cache.clear()
        self._active_expiries.clear()


option_chain_cache = OptionChainCacheManager()
