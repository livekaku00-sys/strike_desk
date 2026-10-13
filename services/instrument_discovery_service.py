import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from models.instrument import Instrument
from services.broker_discovery import (
    AngelOneDiscovery,
    BaseBrokerDiscovery,
    DhanDiscovery,
    FyersDiscovery,
)
from services.instrument_registry import instrument_registry

logger = logging.getLogger(__name__)


class InstrumentDiscoveryService:
    """
    Unified discovery orchestrator across all brokers with in-memory TTL caching,
    stale-token detection, and automated dynamic metadata cache updating.
    """

    DEFAULT_TTL_SECONDS = 86400  # 24 hours

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        dhan_discovery: Optional[DhanDiscovery] = None,
        angelone_discovery: Optional[AngelOneDiscovery] = None,
        fyers_discovery: Optional[FyersDiscovery] = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

        self._adapters: Dict[str, BaseBrokerDiscovery] = {
            "dhan": dhan_discovery or DhanDiscovery(),
            "angelone": angelone_discovery or AngelOneDiscovery(),
            "fyers": fyers_discovery or FyersDiscovery(),
        }

        # Cache: broker_name -> (cached_at_utc, List[Instrument])
        self._broker_cache: Dict[str, Tuple[datetime, List[Instrument]]] = {}

        # Token tracking: broker_name -> symbol -> token
        self._previous_tokens: Dict[str, Dict[str, str]] = {
            b: {} for b in self._adapters
        }

        # Stale tokens detected per broker in the latest discovery
        self._latest_stale_tokens: Dict[str, List[str]] = {
            b: [] for b in self._adapters
        }

    def set_adapter(self, broker: str, adapter: BaseBrokerDiscovery) -> None:
        self._adapters[broker.lower()] = adapter

    async def discover_all_brokers(
        self, force_refresh: bool = False
    ) -> Dict[str, List[Instrument]]:
        """
        Runs discovery across all brokers. If one fails, logs the error and
        allows other brokers to complete successfully.
        """
        results: Dict[str, List[Instrument]] = {}
        for broker_name in list(self._adapters.keys()):
            try:
                instruments = await self.discover_broker(broker_name, force_refresh=force_refresh)
                results[broker_name] = instruments
            except Exception as exc:
                logger.error("Discovery failed for broker '%s': %s", broker_name, exc, exc_info=True)
                results[broker_name] = []
        return results

    async def discover_broker(
        self, broker: str, force_refresh: bool = False
    ) -> List[Instrument]:
        """
        Discovers instruments for a specific broker with in-memory caching
        and stale-token detection.
        """
        b_key = broker.lower()
        if b_key not in self._adapters:
            raise ValueError(f"Unknown broker '{broker}'. Supported: {list(self._adapters.keys())}")

        adapter = self._adapters[b_key]

        async with self._lock:
            now = datetime.now(timezone.utc)
            # Check cache
            if not force_refresh and b_key in self._broker_cache:
                cached_time, cached_items = self._broker_cache[b_key]
                elapsed = (now - cached_time).total_seconds()
                if elapsed < self.ttl_seconds:
                    logger.info("[%s] Returning %d instruments from in-memory cache (age: %.1fs)", b_key, len(cached_items), elapsed)
                    return cached_items

            logger.info("[%s] Initiating fresh instrument discovery via broker API...", b_key)
            current_instruments = await adapter.fetch_master_contracts()

            # Stale-token detection against previous tokens
            stale: List[str] = []
            prev_token_map = self._previous_tokens.get(b_key, {})
            current_token_map: Dict[str, str] = {}

            for inst in current_instruments:
                if inst.symbol and inst.token:
                    current_token_map[inst.symbol] = inst.token
                    if inst.symbol in prev_token_map:
                        old_tok = prev_token_map[inst.symbol]
                        if old_tok != inst.token:
                            logger.warning(
                                "[%s] Stale token detected for %s: old='%s', new='%s'",
                                b_key,
                                inst.symbol,
                                old_tok,
                                inst.token,
                            )
                            stale.append(inst.symbol)

            self._previous_tokens[b_key] = current_token_map
            self._latest_stale_tokens[b_key] = stale
            self._broker_cache[b_key] = (now, current_instruments)

            # Auto-update dynamic metadata cache
            if current_instruments:
                self.update_metadata_cache(current_instruments)

            return current_instruments

    async def register_discovered_instruments(
        self, instruments: List[Instrument]
    ) -> int:
        """
        Registers discovered instruments into the primary in-memory InstrumentRegistry.
        """
        return await instrument_registry.register_many(instruments)

    def detect_stale_tokens(self, broker: str) -> List[str]:
        """
        Returns list of symbols whose tokens changed in the latest discovery for broker.
        """
        return list(self._latest_stale_tokens.get(broker.lower(), []))

    def update_metadata_cache(
        self,
        instruments: List[Instrument],
        cache_file: Optional[str] = None,
    ) -> None:
        """
        Auto-generates and updates data/metadata_cache.json with non-sensitive
        discovery metadata (isin, name, exchange, last_updated).
        Never edited manually.
        """
        if cache_file is None:
            candidates = [
                "/root/strike_desk/data/metadata_cache.json",
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "metadata_cache.json")),
                os.path.abspath("data/metadata_cache.json"),
            ]
            for c in candidates:
                if os.path.exists(c):
                    cache_file = c
                    break
            if cache_file is None:
                dir_1 = "/root/strike_desk/data"
                if os.path.isdir(dir_1):
                    cache_file = os.path.join(dir_1, "metadata_cache.json")
                else:
                    cache_file = os.path.abspath(
                        os.path.join(os.path.dirname(__file__), "..", "data", "metadata_cache.json")
                    )

        os.makedirs(os.path.dirname(os.path.abspath(cache_file)), exist_ok=True)

        cache_data: Dict[str, Any] = {
            "_warning": "DO NOT EDIT MANUALLY. Auto-populated by broker discovery."
        }

        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
            try:
                with open(cache_file, "r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                    if isinstance(loaded, dict):
                        cache_data.update(loaded)
            except Exception as exc:
                logger.warning("Could not read existing metadata cache: %s. Rewriting.", exc)

        for inst in instruments:
            cache_data[inst.symbol] = {
                "isin": inst.isin,
                "name": inst.name,
                "exchange": inst.exchange.value,
                "last_updated": inst.last_updated.isoformat(),
            }

        tmp_file = f"{cache_file}.tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as fp:
                json.dump(cache_data, fp, indent=2)
            os.replace(tmp_file, cache_file)
            logger.info("Updated dynamic metadata cache at %s with %d entries.", cache_file, len(instruments))
        except Exception as exc:
            logger.error("Failed to write dynamic metadata cache to %s: %s", cache_file, exc)
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass


# Singleton instance
instrument_discovery_service = InstrumentDiscoveryService()
