import asyncio
from datetime import date, datetime, timezone
import logging
from typing import Any, Dict, Optional, Tuple

from services.expiry.expiry_discovery import ExpiryDiscovery, expiry_discovery
from services.expiry.expiry_selector import ExpirySelector, expiry_selector

logger = logging.getLogger(__name__)


class RolloverDetector:
    """
    Detects contract lifecycle transitions and recommends rollover
    when contracts approach expiry.
    """

    DEFAULT_TTL_SECONDS = 3600  # 1 hour

    def __init__(
        self,
        discovery: Optional[ExpiryDiscovery] = None,
        selector: Optional[ExpirySelector] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.discovery = discovery or expiry_discovery
        self.selector = selector or expiry_selector
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

        # Cache: (underlying, str(current_expiry)) -> (cached_at_utc, Dict[str, Any])
        self._cache: Dict[Tuple[str, str], Tuple[datetime, Dict[str, Any]]] = {}

    def should_rollover(
        self,
        current_expiry: date,
        as_of: Optional[date] = None,
        days_threshold: int = 3,
    ) -> bool:
        """
        Returns True if current_expiry is within days_threshold days of today (or past).
        """
        today = as_of or datetime.now(timezone.utc).date()
        days_remaining = (current_expiry - today).days
        return days_remaining <= days_threshold

    def detect_expiry_approaching(
        self,
        current_expiry: date,
        as_of: Optional[date] = None,
        days_threshold: int = 3,
    ) -> bool:
        """Alias for should_rollover."""
        return self.should_rollover(
            current_expiry=current_expiry,
            as_of=as_of,
            days_threshold=days_threshold,
        )

    async def get_rollover_recommendation(
        self,
        underlying: str,
        current_expiry: Optional[date] = None,
        as_of: Optional[date] = None,
        days_threshold: int = 3,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Generates structured rollover recommendation.
        Returns dict containing: current, recommended, should_rollover, days_remaining, reason.
        """
        norm_und = underlying.strip().upper()
        today = as_of or datetime.now(timezone.utc).date()

        if current_expiry is None:
            nearest = await self.selector.select_nearest_expiry(norm_und, as_of=today)
            if nearest is None:
                return {
                    "underlying": norm_und,
                    "current_expiry": None,
                    "recommended_expiry": None,
                    "should_rollover": False,
                    "days_remaining": None,
                    "reason": "No active expiries found for underlying.",
                }
            current_expiry = nearest

        cache_key = (norm_und, current_expiry.isoformat())

        async with self._lock:
            now = datetime.now(timezone.utc)
            if not force_refresh and cache_key in self._cache:
                cached_at, cached_rec = self._cache[cache_key]
                if (now - cached_at).total_seconds() < self.ttl_seconds:
                    return cached_rec

            days_remaining = (current_expiry - today).days
            rollover_needed = days_remaining <= days_threshold

            # Find recommended next expiry
            chain = await self.discovery.get_available_expiries(norm_und, as_of=today)
            future_after_current = [d for d in chain if d > current_expiry]
            recommended_expiry = future_after_current[0] if future_after_current else None

            if days_remaining < 0:
                reason = f"Contract expired {abs(days_remaining)} day(s) ago."
            elif days_remaining == 0:
                reason = "Contract reaches final expiry today. Immediate rollover required."
            elif rollover_needed:
                reason = (
                    f"Contract expires in {days_remaining} day(s) (within threshold of {days_threshold} days). "
                    "Recommend rolling over to next cycle."
                )
            else:
                reason = (
                    f"Contract has {days_remaining} day(s) remaining (outside threshold of {days_threshold} days). "
                    "Rollover not required yet."
                )

            rec = {
                "underlying": norm_und,
                "current_expiry": current_expiry.isoformat(),
                "recommended_expiry": (
                    recommended_expiry.isoformat() if recommended_expiry else None
                ),
                "should_rollover": rollover_needed,
                "days_remaining": days_remaining,
                "reason": reason,
            }

            self._cache[cache_key] = (now, rec)
            return rec


rollover_detector = RolloverDetector()
