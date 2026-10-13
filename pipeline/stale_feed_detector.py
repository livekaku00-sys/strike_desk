from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class StaleFeedDetector:
    """
    Monitors market data tick frequency per symbol and alerts on feed stagnation.
    Default threshold: 5.0s for liquid instruments, 30.0s for illiquid/options.
    """

    DEFAULT_THRESHOLD_SECONDS = 5.0

    def __init__(self, default_threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS) -> None:
        self.default_threshold_seconds = default_threshold_seconds
        # symbol -> last_updated_utc
        self._last_updates: Dict[str, datetime] = {}
        # Currently flagged stale symbols
        self._stale_flags: Set[str] = set()

    def update(self, symbol: str, timestamp: Optional[datetime] = None) -> None:
        """
        Updates the latest tick arrival timestamp for a symbol.
        Triggers recovery log if symbol was previously flagged as stale.
        """
        sym = symbol.strip().upper()
        ts = timestamp or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        self._last_updates[sym] = ts

        if sym in self._stale_flags:
            self._stale_flags.remove(sym)
            logger.info(
                "[StaleFeedDetector] Feed recovered for %s (tick received at %s).",
                sym,
                ts.isoformat(),
            )

    def is_stale(
        self,
        symbol: str,
        threshold_seconds: Optional[float] = None,
        as_of: Optional[datetime] = None,
    ) -> bool:
        """
        Checks if a specific symbol's feed has exceeded the staleness threshold.
        Logs WARNING on transition to stale.
        """
        sym = symbol.strip().upper()
        if sym not in self._last_updates:
            return True

        now = as_of or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        last_time = self._last_updates[sym]
        elapsed = (now - last_time).total_seconds()
        threshold = (
            threshold_seconds
            if threshold_seconds is not None
            else self.default_threshold_seconds
        )

        stale = elapsed > threshold
        if stale and sym not in self._stale_flags:
            self._stale_flags.add(sym)
            logger.warning(
                "[StaleFeedDetector] Feed stale for %s! No ticks for %.2fs (threshold: %.1fs).",
                sym,
                elapsed,
                threshold,
            )
        elif not stale and sym in self._stale_flags:
            self._stale_flags.remove(sym)
            logger.info("[StaleFeedDetector] Feed recovered for %s.", sym)

        return stale

    def get_stale_symbols(
        self,
        threshold_seconds: Optional[float] = None,
        as_of: Optional[datetime] = None,
    ) -> List[str]:
        """
        Returns all monitored symbols whose feeds are currently stale.
        """
        return [
            sym
            for sym in list(self._last_updates.keys())
            if self.is_stale(sym, threshold_seconds=threshold_seconds, as_of=as_of)
        ]

    def get_last_update(self, symbol: str) -> Optional[datetime]:
        """Returns the UTC timestamp of the last tick for a symbol."""
        return self._last_updates.get(symbol.strip().upper())

    def reset(self) -> None:
        """Clears all tracking state."""
        self._last_updates.clear()
        self._stale_flags.clear()


stale_feed_detector = StaleFeedDetector()
