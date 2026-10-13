"""
Strike Desk V4 - PCR Staleness Monitor
Dedicated tracker for derivatives data feed freshness and update frequency.
Enforces Review Risk 2:
- If PCR > 30 seconds stale -> mark as STALE and disable option trading signals.
- Monitor PCR update frequency >= 2/sec for liquid contracts.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PCRStalenessMonitor:
    """
    Tracks PCR update timestamps and frequency per underlying asset.
    Detects feed staleness and sub-nominal throughput for risk controls.
    """

    STALE_THRESHOLD_SECONDS: float = 30.0
    MIN_UPDATE_FREQUENCY_HZ: float = 2.0

    def __init__(
        self,
        stale_threshold_seconds: float = STALE_THRESHOLD_SECONDS,
        min_update_frequency_hz: float = MIN_UPDATE_FREQUENCY_HZ,
    ) -> None:
        self.stale_threshold = stale_threshold_seconds
        self.min_frequency = min_update_frequency_hz
        self._last_updates: Dict[str, datetime] = {}
        self._history: Dict[str, List[datetime]] = defaultdict(list)
        self._symbol_thresholds: Dict[str, float] = {}

    def set_symbol_threshold(self, underlying: str, stale_threshold_seconds: float) -> None:
        """
        Overrides the staleness threshold for one underlying.

        Equity option chains cannot be refreshed as often as index chains (the broker
        throttles the endpoint), so holding them to the index threshold would report
        them as stale for most of every polling cycle.
        """
        self._symbol_thresholds[underlying.strip().upper()] = float(stale_threshold_seconds)

    def get_threshold(self, underlying: str) -> float:
        return self._symbol_thresholds.get(underlying.strip().upper(), self.stale_threshold)

    def record_update(self, underlying: str, timestamp: datetime) -> None:
        sym = underlying.strip().upper()
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        self._last_updates[sym] = ts
        self._history[sym].append(ts)
        cutoff = ts - timedelta(seconds=60)
        self._history[sym] = [t for t in self._history[sym] if t >= cutoff]

    def clear(self) -> None:
        """Clears all tracking history and last update timestamps for clean test runs."""
        self._last_updates.clear()
        self._history.clear()
        self._symbol_thresholds.clear()

    def reset(self) -> None:
        self.clear()

    def get_status(self, underlying: str, current_time: datetime) -> Dict[str, Any]:
        sym = underlying.strip().upper()
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)

        last_update = self._last_updates.get(sym)
        if last_update is None:
            logger.debug("[PCRStalenessMonitor] No update records for %s. Marking untrustable.", sym)
            return {
                "is_stale": True,
                "staleness_seconds": float("inf"),
                "update_frequency_hz": 0.0,
                "is_low_frequency": True,
                "is_trustable": False,
            }

        threshold = self.get_threshold(sym)
        staleness_seconds = max(0.0, (now - last_update).total_seconds())
        is_stale = staleness_seconds > threshold

        if is_stale:
            logger.critical(
                "[PCRStalenessMonitor] CRITICAL ALERT: PCR feed stale for %s! "
                "Staleness: %.2fs exceeds threshold %.1fs. Option trading signals disabled.",
                sym,
                staleness_seconds,
                threshold,
            )

        cutoff = now - timedelta(seconds=60)
        recent_updates = [t for t in self._history.get(sym, []) if t >= cutoff]
        self._history[sym] = recent_updates

        update_count = len(recent_updates)
        update_frequency_hz = update_count / 60.0
        is_low_frequency = update_frequency_hz < self.min_frequency
        is_trustable = (not is_stale) and (not is_low_frequency)

        return {
            "is_stale": is_stale,
            "staleness_seconds": staleness_seconds,
            "update_frequency_hz": update_frequency_hz,
            "is_low_frequency": is_low_frequency,
            "is_trustable": is_trustable,
        }

    def is_trustable(
        self,
        underlying: str,
        current_time: datetime,
        require_min_frequency: bool = True,
    ) -> bool:
        status = self.get_status(underlying, current_time)
        if not require_min_frequency:
            return not status["is_stale"]
        return status["is_trustable"]

    def get_all_stale_underlyings(self, current_time: datetime) -> List[str]:
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        stale_list: List[str] = []
        for sym, last_update in self._last_updates.items():
            if (now - last_update).total_seconds() > self.get_threshold(sym):
                stale_list.append(sym)
        return stale_list

    def cleanup_inactive(
        self, current_time: datetime, max_inactive_seconds: float = 300.0
    ) -> None:
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        inactive = [
            sym
            for sym, last_update in self._last_updates.items()
            if (now - last_update).total_seconds() > max_inactive_seconds
        ]
        for sym in inactive:
            self._last_updates.pop(sym, None)
            self._history.pop(sym, None)


# Global singleton instance
pcr_staleness_monitor = PCRStalenessMonitor()
