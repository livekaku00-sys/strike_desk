import asyncio
from datetime import date, datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from models.instrument import Instrument
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
from services.instrument_discovery_service import (
    InstrumentDiscoveryService,
    instrument_discovery_service,
)
from services.instrument_registry import InstrumentRegistry, instrument_registry

logger = logging.getLogger(__name__)


class ExpiryAndLotService:
    """
    Unified orchestration service for derivatives expiry discovery,
    strategy-based contract selection, rollover management, and lot-size validation.
    """

    def __init__(
        self,
        discovery: Optional[ExpiryDiscovery] = None,
        selector: Optional[ExpirySelector] = None,
        rollover: Optional[RolloverDetector] = None,
        validator: Optional[LotSizeValidator] = None,
        registry: Optional[InstrumentRegistry] = None,
        discovery_service: Optional[InstrumentDiscoveryService] = None,
    ) -> None:
        self.discovery = discovery or expiry_discovery
        self.selector = selector or expiry_selector
        self.rollover = rollover or rollover_detector
        self.validator = validator or lot_size_validator
        self.registry = registry or instrument_registry
        self.discovery_service = discovery_service or instrument_discovery_service

        self._lock = asyncio.Lock()
        self._last_refresh_time: Optional[datetime] = None

    @property
    def last_refresh_time(self) -> Optional[datetime]:
        return self._last_refresh_time

    async def get_expiry_chain(
        self, underlying: str, force_refresh: bool = False
    ) -> List[date]:
        """
        Retrieves all upcoming expiry dates for an underlying.
        """
        return await self.discovery.get_available_expiries(
            underlying=underlying, force_refresh=force_refresh
        )

    async def select_expiry(
        self, underlying: str, strategy: str = "nearest"
    ) -> Optional[date]:
        """
        Selects an appropriate expiry date for an underlying by strategy.
        """
        return await self.selector.select_expiry_by_strategy(
            underlying=underlying, strategy=strategy
        )

    async def check_rollover(
        self, underlying: str, current_expiry: Optional[date] = None
    ) -> Dict[str, Any]:
        """
        Evaluates rollover conditions for the specified underlying and expiry.
        """
        return await self.rollover.get_rollover_recommendation(
            underlying=underlying, current_expiry=current_expiry
        )

    async def validate_all_lot_sizes(self) -> Dict[str, Any]:
        """
        Scans all registered instruments in memory and validates lot sizes.
        Returns summary counts: valid_count, invalid_count, changes_detected.
        """
        instruments: List[Instrument] = await self.registry.filter(active_only=False)
        valid_count = 0
        invalid_count = 0

        for inst in instruments:
            if self.validator.validate_lot_size(inst):
                valid_count += 1
            else:
                invalid_count += 1

        return {
            "valid_count": valid_count,
            "invalid_count": invalid_count,
            "changes_detected": len(self.validator._history),
        }

    async def refresh_all(self) -> None:
        """
        Daily market close refresh routine. Clears caches and pulls fresh contracts.
        """
        async with self._lock:
            logger.info("Executing daily market close refresh for expiries and lot sizes...")
            self.discovery.clear_cache()
            self._last_refresh_time = datetime.now(timezone.utc)


expiry_and_lot_service = ExpiryAndLotService()
