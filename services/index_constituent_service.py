import asyncio
from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional

from models.instrument import Instrument
from services.indices import (
    ConstituentResolver,
    IndexCatalog,
    MembershipTracker,
    constituent_resolver,
    index_catalog,
    membership_tracker,
)
from services.instrument_discovery_service import (
    InstrumentDiscoveryService,
    instrument_discovery_service,
)
from services.instrument_registry import InstrumentRegistry, instrument_registry

logger = logging.getLogger(__name__)


class IndexConstituentService:
    """
    Unified index constituent orchestration service.
    Integrates dynamic broker discovery, master instrument registry,
    constituent resolution, and membership change tracking.
    """

    def __init__(
        self,
        catalog: Optional[IndexCatalog] = None,
        resolver: Optional[ConstituentResolver] = None,
        tracker: Optional[MembershipTracker] = None,
        discovery_service: Optional[InstrumentDiscoveryService] = None,
        registry: Optional[InstrumentRegistry] = None,
    ) -> None:
        self.catalog = catalog or index_catalog
        self.resolver = resolver or constituent_resolver
        self.tracker = tracker or membership_tracker
        self.discovery_service = discovery_service or instrument_discovery_service
        self.registry = registry or instrument_registry

        self._lock = asyncio.Lock()
        self._last_refresh_times: Dict[str, datetime] = {}

    def get_last_refresh_time(self, index_name: str) -> Optional[datetime]:
        return self._last_refresh_times.get(index_name.upper())

    def get_cached_constituents(self, index_name: str) -> Optional[List[Instrument]]:
        return self.resolver.get_cached(index_name)

    async def fetch_index(
        self,
        index_name: str,
        force_refresh: bool = False,
        preferred_broker: str = "dhan",
    ) -> List[Instrument]:
        """
        Discovers broker master contracts if needed, resolves index constituents,
        tracks membership modifications, and updates the refresh timestamp.
        """
        norm_name = index_name.upper()

        async with self._lock:
            if not force_refresh and self.resolver.is_cached_fresh(norm_name, preferred_broker):
                cached = self.resolver.get_cached(norm_name, preferred_broker)
                if cached is not None:
                    return cached

            try:
                discovered = await self.discovery_service.discover_broker(
                    preferred_broker, force_refresh=force_refresh
                )
                if discovered:
                    await self.registry.register_many(discovered)
            except Exception as exc:
                logger.warning(
                    "Broker discovery failed for '%s' during index fetch (%s): %s. Falling back to existing registry.",
                    preferred_broker,
                    norm_name,
                    exc,
                )

            constituents = await self.resolver.resolve(
                index_name=norm_name,
                broker=preferred_broker,
                force_refresh=force_refresh,
            )

            self.tracker.detect_changes(norm_name, constituents)

            now = datetime.now(timezone.utc)
            self._last_refresh_times[norm_name] = now

            logger.info("Successfully fetched %d constituents for %s.", len(constituents), norm_name)
            return constituents

    async def fetch_all_indices(
        self, force_refresh: bool = False, preferred_broker: str = "dhan"
    ) -> Dict[str, List[Instrument]]:
        """
        Fetches constituents across all cataloged indices.
        If any index fails, logs an error and allows others to proceed.
        """
        results: Dict[str, List[Instrument]] = {}
        for idx_def in self.catalog.list_indices():
            name = idx_def.name
            try:
                constituents = await self.fetch_index(
                    name, force_refresh=force_refresh, preferred_broker=preferred_broker
                )
                results[name] = constituents
            except Exception as exc:
                logger.error("Failed to fetch index '%s': %s (continuing with remaining indices)", name, exc)
                results[name] = []
        return results

    async def refresh_all_indices(self) -> None:
        """
        Daily scheduled task executed at market close.
        Forces refresh of all constituent lists.
        """
        logger.info("Executing daily market close constituent refresh for all indices...")
        await self.fetch_all_indices(force_refresh=True)


index_constituent_service = IndexConstituentService()
