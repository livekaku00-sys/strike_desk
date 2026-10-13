import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from models.instrument import AssetClass, Exchange, Instrument, InstrumentType

logger = logging.getLogger(__name__)


def load_symbol_map(file_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """
    FALLBACK ONLY — never used as primary discovery source.
    Per Immutable Rule #6: All instrument discovery is dynamic via broker API.
    This symbol map is loaded strictly for offline testing or fallback metadata.
    """
    if file_path is None:
        candidates = [
            "/root/strike_desk/data/symbol_map.json",
            os.path.join(os.path.dirname(__file__), "..", "data", "symbol_map.json"),
            "data/symbol_map.json",
        ]
        for c in candidates:
            if os.path.exists(c):
                file_path = c
                break

    if not file_path or not os.path.exists(file_path):
        logger.warning("Fallback symbol_map.json not found at candidate paths.")
        return {}

    logger.info("Loading fallback symbol map from %s (FALLBACK ONLY — not auto-registered)", file_path)
    try:
        with open(file_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as exc:
        logger.error("Failed to load fallback symbol map from %s: %s", file_path, exc)
        return {}


class InstrumentRegistry:
    """
    Master in-memory Instrument Registry with TTL caching and fast indexing.
    Thread-safe async operations via asyncio.Lock.
    """

    DEFAULT_TTL_SECONDS = 14400  # 4 hours

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

        # Primary store: (Exchange, canonical_symbol) -> Instrument
        self._instruments: Dict[Tuple[Exchange, str], Instrument] = {}

        # Fast lookup indices
        self._token_index: Dict[Tuple[Exchange, str], Instrument] = {}
        self._isin_index: Dict[str, Instrument] = {}
        self._asset_class_index: Dict[AssetClass, Set[Tuple[Exchange, str]]] = {
            ac: set() for ac in AssetClass
        }
        self._underlying_index: Dict[str, Set[Tuple[Exchange, str]]] = {}

        self._last_updated: Optional[datetime] = None

    @property
    def last_updated(self) -> Optional[datetime]:
        return self._last_updated

    def is_fresh(self, max_age_seconds: Optional[int] = None) -> bool:
        if self._last_updated is None or len(self._instruments) == 0:
            return False
        max_age = max_age_seconds if max_age_seconds is not None else self.ttl_seconds
        now = datetime.now(timezone.utc)
        elapsed = (now - self._last_updated).total_seconds()
        return elapsed < max_age

    def count(self) -> int:
        return len(self._instruments)

    async def clear(self) -> None:
        async with self._lock:
            self._instruments.clear()
            self._token_index.clear()
            self._isin_index.clear()
            for ac in self._asset_class_index:
                self._asset_class_index[ac].clear()
            self._underlying_index.clear()
            self._last_updated = None
            logger.info("InstrumentRegistry cleared.")

    async def register(self, instrument: Instrument) -> None:
        async with self._lock:
            self._register_unlocked(instrument)

    async def register_many(self, instruments: List[Instrument]) -> int:
        count = 0
        async with self._lock:
            for inst in instruments:
                self._register_unlocked(inst)
                count += 1
        logger.info("Successfully registered %d instruments in registry.", count)
        return count

    def _register_unlocked(self, inst: Instrument) -> None:
        key = (inst.exchange, inst.symbol)
        self._instruments[key] = inst

        # Token index
        if inst.token:
            self._token_index[(inst.exchange, inst.token)] = inst

        # ISIN index
        if inst.isin:
            self._isin_index[inst.isin.strip().upper()] = inst

        # Asset Class index
        self._asset_class_index[inst.asset_class].add(key)

        # Underlying index
        if inst.underlying:
            und = inst.underlying.strip().upper()
            if und not in self._underlying_index:
                self._underlying_index[und] = set()
            self._underlying_index[und].add(key)

        self._last_updated = datetime.now(timezone.utc)
        logger.debug("Registered instrument: %s:%s (Token: %s)", inst.exchange.value, inst.symbol, inst.token)

    async def get(self, exchange: Exchange, symbol: str) -> Optional[Instrument]:
        key = (exchange, symbol.strip().upper())
        async with self._lock:
            return self._instruments.get(key)

    async def get_by_token(self, exchange: Exchange, token: str) -> Optional[Instrument]:
        key = (exchange, str(token).strip())
        async with self._lock:
            return self._token_index.get(key)

    async def get_by_isin(self, isin: str) -> Optional[Instrument]:
        norm_isin = isin.strip().upper()
        async with self._lock:
            return self._isin_index.get(norm_isin)

    async def list_by_asset_class(self, asset_class: AssetClass) -> List[Instrument]:
        async with self._lock:
            keys = self._asset_class_index.get(asset_class, set())
            return [self._instruments[k] for k in keys if k in self._instruments]

    async def list_by_underlying(self, underlying: str) -> List[Instrument]:
        norm_und = underlying.strip().upper()
        async with self._lock:
            keys = self._underlying_index.get(norm_und, set())
            return [self._instruments[k] for k in keys if k in self._instruments]

    async def filter(
        self,
        exchange: Optional[Exchange] = None,
        asset_class: Optional[AssetClass] = None,
        underlying: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Instrument]:
        norm_und = underlying.strip().upper() if underlying else None
        async with self._lock:
            results: List[Instrument] = []
            for inst in self._instruments.values():
                if active_only and not inst.is_active:
                    continue
                if exchange is not None and inst.exchange != exchange:
                    continue
                if asset_class is not None and inst.asset_class != asset_class:
                    continue
                if norm_und is not None and inst.underlying != norm_und:
                    continue
                results.append(inst)
            return results


# Global master registry instance
instrument_registry = InstrumentRegistry()