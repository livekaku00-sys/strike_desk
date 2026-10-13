from abc import ABC, abstractmethod
import logging
from typing import Any, Dict, List, Optional

from models.instrument import Instrument

logger = logging.getLogger(__name__)


class BaseBrokerDiscovery(ABC):
    """
    Abstract base class for broker-specific master contract discovery.
    """

    @abstractmethod
    def get_broker_name(self) -> str:
        """Returns the unique name of the broker (e.g. 'dhan', 'angelone', 'fyers')."""
        pass

    @abstractmethod
    async def fetch_master_contracts(self) -> List[Instrument]:
        """Fetches and normalizes all master contracts from the broker API."""
        pass

    @abstractmethod
    def normalize_instrument(self, raw_data: Dict[str, Any]) -> Optional[Instrument]:
        """Normalizes a raw broker contract record into a unified Instrument model."""
        pass

    def detect_stale_tokens(
        self, current: List[Instrument], previous: List[Instrument]
    ) -> List[str]:
        """
        Detects instruments whose broker tokens have changed between discoveries.
        Returns a list of symbols with changed tokens and logs a warning for each.
        """
        prev_map = {
            (inst.exchange, inst.symbol): inst.token
            for inst in previous
            if inst.token
        }
        stale_symbols: List[str] = []

        for curr in current:
            key = (curr.exchange, curr.symbol)
            if key in prev_map:
                prev_token = prev_map[key]
                if curr.token and prev_token != curr.token:
                    logger.warning(
                        "[%s] Stale/changed token detected for %s:%s! Previous: '%s', Current: '%s'",
                        self.get_broker_name(),
                        curr.exchange.value,
                        curr.symbol,
                        prev_token,
                        curr.token,
                    )
                    stale_symbols.append(curr.symbol)

        return stale_symbols
