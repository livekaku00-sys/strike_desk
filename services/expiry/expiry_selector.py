from datetime import date, datetime, timezone
import logging
from typing import List, Optional

from models.instrument import Exchange
from services.expiry.expiry_discovery import ExpiryDiscovery, expiry_discovery

logger = logging.getLogger(__name__)


class ExpirySelector:
    """
    Selects contract expiries dynamically based on strategy requirements.
    (Nearest / Next / Far / Monthly / Weekly).
    """

    def __init__(self, discovery: Optional[ExpiryDiscovery] = None) -> None:
        self.discovery = discovery or expiry_discovery

    async def select_nearest_expiry(
        self,
        underlying: str,
        exchange: Optional[Exchange] = None,
        as_of: Optional[date] = None,
    ) -> Optional[date]:
        """Returns the nearest upcoming expiry (current week or nearest month)."""
        chain = await self.discovery.get_available_expiries(
            underlying=underlying, exchange=exchange, as_of=as_of
        )
        return chain[0] if chain else None

    async def select_next_expiry(
        self,
        underlying: str,
        exchange: Optional[Exchange] = None,
        as_of: Optional[date] = None,
    ) -> Optional[date]:
        """Returns the next upcoming expiry (second in chain, e.g. next week or next month)."""
        chain = await self.discovery.get_available_expiries(
            underlying=underlying, exchange=exchange, as_of=as_of
        )
        return chain[1] if len(chain) > 1 else None

    async def select_far_expiry(
        self,
        underlying: str,
        exchange: Optional[Exchange] = None,
        as_of: Optional[date] = None,
    ) -> Optional[date]:
        """Returns the furthest available expiry in the chain (e.g. far-month/quarterly)."""
        chain = await self.discovery.get_available_expiries(
            underlying=underlying, exchange=exchange, as_of=as_of
        )
        return chain[-1] if chain else None

    async def select_expiry_by_strategy(
        self,
        underlying: str,
        strategy: str = "nearest",
        exchange: Optional[Exchange] = None,
        as_of: Optional[date] = None,
    ) -> Optional[date]:
        """
        Selects an expiry based on strategy name:
        - 'nearest' or 'current': nearest upcoming expiry
        - 'next': second upcoming expiry
        - 'far': furthest expiry
        - 'monthly': month-end expiry
        """
        strat = strategy.strip().lower()
        chain = await self.discovery.get_available_expiries(
            underlying=underlying, exchange=exchange, as_of=as_of
        )
        if not chain:
            return None

        if strat in ("nearest", "current", "weekly"):
            return chain[0]
        elif strat == "next":
            return chain[1] if len(chain) > 1 else chain[0]
        elif strat == "far":
            return chain[-1]
        elif strat == "monthly":
            current_month = chain[0].month
            month_expiries = [d for d in chain if d.month == current_month]
            return month_expiries[-1] if month_expiries else chain[0]
        else:
            logger.warning("Unrecognized strategy '%s', defaulting to 'nearest'.", strategy)
            return chain[0]


expiry_selector = ExpirySelector()
