"""
Strike Desk V4 - Signal Store
Maintains top-N recommendations per asset class (Blueprint Rule 10).
Provides thread-safe signal storage, pruning of expired signals, and audit logging.
"""
import asyncio
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Dict, List, Optional

from models.instrument import AssetClass
from models.signal import ConfidenceTier, Signal

logger = logging.getLogger(__name__)


class SignalStore:
    """
    Central repository for active strategy recommendations.
    Enforces Blueprint Rule 10: Top 2 recommendations per asset class are displayed as alerts.
    """

    def __init__(self, max_per_asset_class: int = 2) -> None:
        self.max_per_asset_class = max_per_asset_class
        self._top_signals: Dict[AssetClass, List[Signal]] = {}
        self._audit_log: List[Signal] = []
        self._lock = threading.RLock()
        self._async_lock = asyncio.Lock()

    def add_signal(self, signal: Signal, top_n: Optional[int] = None) -> None:
        """
        Adds a signal to the store.
        Maintains top N per asset class (defaults to max_per_asset_class).
        """
        with self._lock:
            self._audit_log.append(signal)

            ac = signal.asset_class
            if ac not in self._top_signals:
                self._top_signals[ac] = []

            current_list = self._top_signals[ac]

            replaced = False
            for idx, existing in enumerate(current_list):
                if (
                    existing.symbol.strip().upper() == signal.symbol.strip().upper()
                    and existing.direction == signal.direction
                ):
                    if signal.confidence_score >= existing.confidence_score:
                        current_list[idx] = signal
                    replaced = True
                    break

            if not replaced:
                current_list.append(signal)

            current_list.sort(
                key=lambda s: (s.confidence_score, s.timestamp), reverse=True
            )

            limit = top_n if top_n is not None else self.max_per_asset_class
            self._top_signals[ac] = current_list[:limit]

            logger.info(
                "[SignalStore] Stored signal for %s: %s %s (Score: %.1f, Tier: %s). Active top count: %d",
                ac.value if hasattr(ac, "value") else str(ac),
                signal.symbol,
                signal.direction.value,
                signal.confidence_score,
                signal.confidence_tier.value,
                len(self._top_signals[ac]),
            )

    async def add_signal_async(self, signal: Signal, top_n: Optional[int] = None) -> None:
        """Async-safe wrapper for add_signal."""
        async with self._async_lock:
            self.add_signal(signal, top_n=top_n)

    def get_top_signals(self, asset_class: AssetClass, n: Optional[int] = None) -> List[Signal]:
        """Returns top N signals for the given asset class."""
        with self._lock:
            limit = n if n is not None else self.max_per_asset_class
            signals = list(self._top_signals.get(asset_class, []))
            signals.sort(key=lambda s: (s.confidence_score, s.timestamp), reverse=True)
            return signals[:limit]

    def get_all_actionable_signals(
        self, current_time: Optional[datetime] = None
    ) -> List[Signal]:
        """Returns all non-expired actionable signals across all asset classes."""
        now = current_time or datetime.now(timezone.utc)
        actionable: List[Signal] = []

        with self._lock:
            for ac_signals in self._top_signals.values():
                for s in ac_signals:
                    if s.is_actionable(now):
                        actionable.append(s)

        actionable.sort(key=lambda s: (s.confidence_score, s.timestamp), reverse=True)
        return actionable

    def prune_expired(self, current_time: Optional[datetime] = None) -> int:
        """Removes expired signals from active recommendations."""
        now = current_time or datetime.now(timezone.utc)
        pruned_count = 0

        with self._lock:
            for ac, signals in list(self._top_signals.items()):
                retained = []
                for s in signals:
                    if s.is_expired(now):
                        pruned_count += 1
                    else:
                        retained.append(s)
                self._top_signals[ac] = retained

        return pruned_count

    def clear(self) -> None:
        """Clears all active signals."""
        with self._lock:
            self._top_signals.clear()

    def get_status(self) -> Dict[str, Any]:
        """Returns snapshot of active top signals and total audit count."""
        now = datetime.now(timezone.utc)
        with self._lock:
            return {
                "max_per_asset_class": self.max_per_asset_class,
                "total_audit_signals": len(self._audit_log),
                "asset_classes": {
                    (ac.value if hasattr(ac, "value") else str(ac)): [
                        s.to_dict() for s in signals
                    ]
                    for ac, signals in self._top_signals.items()
                },
                "total_active_signals": sum(
                    len(s) for s in self._top_signals.values()
                ),
                "total_actionable_signals": len(self.get_all_actionable_signals(now)),
            }


# Global singleton instance
signal_store = SignalStore()
