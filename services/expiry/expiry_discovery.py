import asyncio
from datetime import date, datetime, timezone
import logging
from typing import Dict, List, Optional, Tuple

from models.instrument import Exchange, Instrument
from services.instrument_registry import InstrumentRegistry, instrument_registry

logger = logging.getLogger(__name__)


class ExpiryDiscovery:
    """
    Dynamic expiry discovery service for derivatives contracts.
    Zero hardcoded dates (Immutable Rule #6).
    """

    DEFAULT_TTL_SECONDS = 86400  # 24 hours

    def __init__(
        self,
        registry: Optional[InstrumentRegistry] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.registry = registry or instrument_registry
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

        # Cache: (underlying, exchange_str) -> (cached_at_utc, List[date])
        self._cache: Dict[Tuple[str, str], Tuple[datetime, List[date]]] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    async def get_available_expiries(
        self,
        underlying: str,
        exchange: Optional[Exchange] = None,
        as_of: Optional[date] = None,
        force_refresh: bool = False,
    ) -> List[date]:
        """
        Discovers and returns all future expiry dates for an underlying,
        sorted chronologically (earliest first).
        """
        norm_und = underlying.strip().upper()
        exch_key = exchange.value if exchange else "ALL"
        cache_key = (norm_und, exch_key)
        today = as_of or datetime.now(timezone.utc).date()

        async with self._lock:
            now = datetime.now(timezone.utc)
            if not force_refresh and cache_key in self._cache:
                cached_at, cached_dates = self._cache[cache_key]
                elapsed = (now - cached_at).total_seconds()
                if elapsed < self.ttl_seconds:
                    return [d for d in cached_dates if d >= today]

            # Query registry
            instruments: List[Instrument] = await self.registry.list_by_underlying(norm_und)
            if not instruments:
                candidates = await self.registry.filter(exchange=exchange, active_only=True)
                instruments = [
                    inst for inst in candidates
                    if (inst.underlying and inst.underlying.upper() == norm_und)
                    or (norm_und in inst.symbol.upper())
                ]

            expiries_set = set()
            for inst in instruments:
                if exchange is not None and inst.exchange != exchange:
                    continue
                if inst.expiry is not None:
                    if inst.expiry >= today:
                        expiries_set.add(inst.expiry)

            sorted_expiries = sorted(list(expiries_set))
            self._cache[cache_key] = (now, sorted_expiries)
            logger.info(
                "Discovered %d future expiries for %s (%s): %s",
                len(sorted_expiries),
                norm_und,
                exch_key,
                [d.isoformat() for d in sorted_expiries],
            )
            return sorted_expiries

    async def get_expiry_chain(
        self,
        underlying: str,
        exchange: Optional[Exchange] = None,
        as_of: Optional[date] = None,
        force_refresh: bool = False,
    ) -> List[date]:
        """Convenience alias for get_available_expiries."""
        return await self.get_available_expiries(
            underlying=underlying,
            exchange=exchange,
            as_of=as_of,
            force_refresh=force_refresh,
        )


expiry_discovery = ExpiryDiscovery()
