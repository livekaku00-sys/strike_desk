import asyncio
from datetime import date, datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from models.instrument import Exchange, Instrument
from services.expiry import (
    ExpiryDiscovery,
    ExpirySelector,
    LotSizeValidator,
    RolloverDetector,
    expiry_discovery,
    expiry_selector,
    lot_size_validator,
    rollover_detector,
)
from services.expiry_and_lot_service import (
    ExpiryAndLotService,
    expiry_and_lot_service,
)
from services.indices import (
    ConstituentResolver,
    IndexCatalog,
    MembershipTracker,
    constituent_resolver,
    index_catalog,
    membership_tracker,
)
from services.index_constituent_service import (
    IndexConstituentService,
    index_constituent_service,
)
from services.instrument_discovery_service import (
    InstrumentDiscoveryService,
    instrument_discovery_service,
)
from services.instrument_registry import InstrumentRegistry, instrument_registry

logger = logging.getLogger(__name__)


class InstrumentService:
    """
    Unified InstrumentService orchestrating:
    - Master Instrument Registry (Prompt 1/5)
    - Dynamic Broker Discovery & Metadata Cache (Prompt 2/5)
    - Dynamic Index Constituent Resolution (Prompt 3/5)
    - Expiry Discovery, Strategy Selection & Rollover Tracking (Prompt 4/5)
    - Lot-size validation and exchange revisions
    """

    def __init__(
        self,
        registry: Optional[InstrumentRegistry] = None,
        discovery_service: Optional[InstrumentDiscoveryService] = None,
        constituent_service: Optional[IndexConstituentService] = None,
        expiry_lot_service: Optional[ExpiryAndLotService] = None,
        catalog: Optional[IndexCatalog] = None,
        tracker: Optional[MembershipTracker] = None,
    ) -> None:
        self.registry = registry or instrument_registry
        self.discovery_service = discovery_service or instrument_discovery_service
        self.constituent_service = constituent_service or index_constituent_service
        self.expiry_lot_service = expiry_lot_service or expiry_and_lot_service
        self.catalog = catalog or index_catalog
        self.tracker = tracker or membership_tracker

        self._lock = asyncio.Lock()
        self._is_initialized = False
        self._last_initialized_at: Optional[datetime] = None

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    async def initialize(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Bootstraps instrument discovery across all supported brokers, ingests
        master contracts, validates lot sizes, and resolves indices.
        """
        async with self._lock:
            if self._is_initialized and not force_refresh:
                return self.get_status()

            logger.info("Initializing Strike Desk Unified InstrumentService...")
            now = datetime.now(timezone.utc)

            # 1. Discover all brokers
            discovered_by_broker = await self.discovery_service.discover_all_brokers(
                force_refresh=force_refresh
            )

            # 2. Register discovered instruments into master registry
            total_registered = 0
            for broker, instruments in discovered_by_broker.items():
                if instruments:
                    cnt = await self.registry.register_many(instruments)
                    total_registered += cnt
                    logger.info("[%s] Registered %d instruments.", broker, cnt)

            # 3. Resolve all indices
            indices_result = await self.constituent_service.fetch_all_indices(
                force_refresh=force_refresh
            )

            # 4. Validate lot sizes
            lot_summary = await self.expiry_lot_service.validate_all_lot_sizes()

            self._is_initialized = True
            self._last_initialized_at = now

            status_summary = {
                "status": "initialized",
                "total_instruments": self.registry.count(),
                "indices_resolved": len(indices_result),
                "brokers": list(discovered_by_broker.keys()),
                "lot_summary": lot_summary,
                "initialized_at": now.isoformat(),
            }
            logger.info("Unified InstrumentService initialization complete: %s", status_summary)
            return status_summary

    async def get_instrument(self, exchange: Exchange, symbol: str) -> Optional[Instrument]:
        """Retrieves a single instrument by exchange and symbol."""
        return await self.registry.get(exchange=exchange, symbol=symbol)

    async def get_instruments_by_index(
        self, index_name: str, force_refresh: bool = False
    ) -> List[Instrument]:
        """Retrieves all constituent instruments for an index dynamically."""
        return await self.constituent_service.fetch_index(
            index_name=index_name, force_refresh=force_refresh
        )

    async def get_expiry_chain(
        self, underlying: str, force_refresh: bool = False
    ) -> List[date]:
        """Retrieves all upcoming valid future expiry dates for an underlying."""
        return await self.expiry_lot_service.get_expiry_chain(
            underlying=underlying, force_refresh=force_refresh
        )

    async def select_expiry(
        self, underlying: str, strategy: str = "nearest"
    ) -> Optional[date]:
        """Selects an expiry date dynamically based on strategy."""
        return await self.expiry_lot_service.select_expiry(
            underlying=underlying, strategy=strategy
        )

    async def check_rollover(
        self, underlying: str, current_expiry: Optional[date] = None
    ) -> Dict[str, Any]:
        """Evaluates rollover conditions for the underlying and current contract."""
        return await self.expiry_lot_service.check_rollover(
            underlying=underlying, current_expiry=current_expiry
        )

    async def refresh_all(self) -> None:
        """
        Daily refresh executed at market close.
        Forces re-discovery across brokers, indices, and expiries.
        """
        logger.info("Triggering market-close refresh across all instrument domains...")
        await self.initialize(force_refresh=True)

    def get_status(self) -> Dict[str, Any]:
        """Returns runtime status and freshness of the instrument service."""
        return {
            "initialized": self._is_initialized,
            "total_instruments": self.registry.count(),
            "registry_fresh": self.registry.is_fresh(),
            "supported_indices": self.catalog.list_names(),
            "last_initialized_at": (
                self._last_initialized_at.isoformat()
                if self._last_initialized_at
                else None
            ),
        }


# Global unified singleton
instrument_service = InstrumentService()
