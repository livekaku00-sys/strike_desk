import asyncio
from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional, Tuple

from models.instrument import AssetClass, Exchange, Instrument
from services.indices.index_catalog import IndexCatalog, IndexDefinition, index_catalog
from services.instrument_registry import InstrumentRegistry, instrument_registry

logger = logging.getLogger(__name__)


class ConstituentResolver:
    """
    Dynamically resolves index constituents from broker master contracts and registry.
    Per Immutable Rule #6: Zero hardcoded constituent symbols.
    """

    DEFAULT_TTL_SECONDS = 86400  # 24 hours

    def __init__(
        self,
        catalog: Optional[IndexCatalog] = None,
        registry: Optional[InstrumentRegistry] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.catalog = catalog or index_catalog
        self.registry = registry or instrument_registry
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

        # Cache: (index_name, broker) -> (cached_at_utc, List[Instrument])
        self._cache: Dict[Tuple[str, str], Tuple[datetime, List[Instrument]]] = {}

    def is_cached_fresh(self, index_name: str, broker: str = "dhan") -> bool:
        key = (index_name.upper(), broker.lower())
        if key not in self._cache:
            return False
        cached_at, _ = self._cache[key]
        elapsed = (datetime.now(timezone.utc) - cached_at).total_seconds()
        return elapsed < self.ttl_seconds

    def get_cached(self, index_name: str, broker: str = "dhan") -> Optional[List[Instrument]]:
        key = (index_name.upper(), broker.lower())
        if self.is_cached_fresh(index_name, broker):
            return self._cache[key][1]
        return None

    def clear_cache(self) -> None:
        self._cache.clear()

    async def resolve(
        self,
        index_name: str,
        broker: str = "dhan",
        master_contracts: Optional[List[Instrument]] = None,
        force_refresh: bool = False,
    ) -> List[Instrument]:
        """
        Resolves the list of constituent Instrument objects for a given index.
        """
        norm_name = index_name.upper()
        norm_broker = broker.lower()
        key = (norm_name, norm_broker)

        async with self._lock:
            if not force_refresh and key in self._cache:
                cached_at, cached_list = self._cache[key]
                elapsed = (datetime.now(timezone.utc) - cached_at).total_seconds()
                if elapsed < self.ttl_seconds:
                    logger.debug("Returning %d cached constituents for %s (%s)", len(cached_list), norm_name, norm_broker)
                    return cached_list

            definition = self.catalog.get_index(norm_name)
            if not definition:
                raise ValueError(f"Unknown index '{index_name}'. Available: {self.catalog.list_names()}")

            # 1. Gather candidate instruments
            candidates: List[Instrument] = []
            if master_contracts is not None:
                candidates = master_contracts
            else:
                candidates = await self.registry.filter(exchange=definition.exchange, active_only=True)

            if not candidates:
                return []

            # 2. Filter candidates matching the index criteria dynamically
            matched = self._match_constituents(definition, candidates)

            # 3. Validate count against expected_count within tolerance
            expected = definition.expected_count
            tolerance = definition.tolerance_pct
            min_allowed = int(expected * (1.0 - tolerance))
            max_allowed = int(expected * (1.0 + tolerance))

            if len(candidates) >= min_allowed and (len(matched) < min_allowed or len(matched) > max_allowed):
                logger.warning(
                    "Index %s constituent count (%d) outside expected range [%d - %d] (expected %d ± %.0f%%).",
                    norm_name,
                    len(matched),
                    min_allowed,
                    max_allowed,
                    expected,
                    tolerance * 100,
                )

            # 4. Cache result
            now = datetime.now(timezone.utc)
            self._cache[key] = (now, matched)
            logger.info("Resolved %d dynamic constituents for %s (%s).", len(matched), norm_name, norm_broker)
            return matched

    def _match_constituents(
        self, definition: IndexDefinition, candidates: List[Instrument]
    ) -> List[Instrument]:
        """
        Filters candidates dynamically using index tag, underlying, and exchange.
        Zero hardcoded constituent tickers.
        """
        matched: List[Instrument] = []
        raw_tag = (definition.tag or definition.name).upper()
        clean_tag = raw_tag.replace("_", "")
        clean_def_name = definition.name.replace("_", "")

        for inst in candidates:
            if inst.exchange != definition.exchange:
                continue

            und = (inst.underlying or "").upper()
            sym = inst.symbol.upper()
            name = (inst.name or "").upper()
            bsym = inst.broker_symbol.upper()

            matches = False
            # Direct tag or name equality
            if und in (raw_tag, clean_tag, definition.name, clean_def_name):
                matches = True
            elif raw_tag in ("NIFTY", "NIFTY50", "NIFTY_50") and und == "NIFTY":
                # Ensure it is core Nifty 50 and not sub-indices with distinct tags
                if not any(k in und or k in sym for k in ("BANK", "100", "IT", "PHARMA", "FMCG")):
                    matches = True
            elif raw_tag in sym or clean_tag in sym or raw_tag in name or raw_tag in bsym:
                if raw_tag in ("NIFTY", "NIFTY50", "NIFTY_50") and any(k in sym for k in ("BANK", "100", "IT", "PHARMA", "FMCG")):
                    matches = False
                else:
                    matches = True

            if matches:
                matched.append(inst)

        unique_matched: List[Instrument] = []
        seen_keys = set()
        for inst in matched:
            k = (inst.exchange, inst.symbol)
            if k not in seen_keys:
                seen_keys.add(k)
                unique_matched.append(inst)

        return unique_matched

    async def resolve_all(
        self,
        index_name: str,
        brokers: Optional[List[str]] = None,
        master_contracts_by_broker: Optional[Dict[str, List[Instrument]]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, List[Instrument]]:
        """
        Resolves constituents for an index across all specified brokers.
        """
        target_brokers = brokers or ["dhan", "angelone", "fyers"]
        results: Dict[str, List[Instrument]] = {}

        for b in target_brokers:
            contracts = master_contracts_by_broker.get(b) if master_contracts_by_broker else None
            try:
                constituents = await self.resolve(
                    index_name=index_name,
                    broker=b,
                    master_contracts=contracts,
                    force_refresh=force_refresh,
                )
                results[b] = constituents
            except Exception as exc:
                logger.error("Failed to resolve index %s for broker %s: %s", index_name, b, exc)
                results[b] = []

        return results

    async def get_member_symbols(self, index_name: str, broker: str = "dhan") -> List[str]:
        """
        Returns list of member symbol strings for an index.
        """
        instruments = await self.resolve(index_name=index_name, broker=broker)
        return [inst.symbol for inst in instruments]


constituent_resolver = ConstituentResolver()
