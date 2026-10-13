"""
Strike Desk V4 - Base Strategy Architecture
Abstract Base Class for all 5 asset-class strategies:
1. Stock Cash (Momentum + Mean Reversion)
2. Index Options (Scalping + Directional)
3. Stock Options (Breakout + Momentum)
4. Stock Futures (Trend Following + VWAP Pullback)
5. Index Futures (Intraday Momentum + Range Breakout)
"""
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

from models.instrument import AssetClass
from models.signal import ConfidenceTier, Signal, SignalState
from strategies.confidence import ConfidenceScorer, confidence_scorer

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Abstract Base Class establishing standard strategy contract:
    - Rule-based entry criteria evaluation
    - Standardized signal generation with A+, A, B, C setup tiers
    - Duplicate signal detection (same symbol + direction within 60s)
    - Expiry lifecycle tracking (default 30s)
    - Independent testability prior to orchestration
    """

    def __init__(
        self,
        name: str,
        asset_class: AssetClass,
        criteria_weights: Dict[str, float],
        signal_expiry_seconds: int = 300,
        duplicate_window_seconds: int = 60,
    ) -> None:
        self.name = name
        self.asset_class = asset_class
        self.criteria_weights = criteria_weights
        self.signal_expiry_seconds = signal_expiry_seconds
        self.duplicate_window_seconds = duplicate_window_seconds

        # Validate weights configuration
        total_w = sum(self.criteria_weights.values())
        if abs(total_w - 100.0) > 1e-4:
            raise ValueError(
                f"[{self.name}] Criteria weights must sum to 100.0, got {total_w:.2f}"
            )

        self._recent_signals: List[Signal] = []
        self._signals: List[Signal] = []

    @abstractmethod
    def evaluate(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Optional[Signal]:
        """
        Evaluates current indicator snapshot and market state to generate a Signal.
        Returns a Signal if criteria meet actionable thresholds, else None.
        """
        pass

    @abstractmethod
    def get_criteria_checklist(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Dict[str, bool]:
        """
        Evaluates discrete checklist criteria.
        Returns dictionary of criterion_name -> bool.
        """
        pass

    def _check_cooldown(self, new_signal: Signal) -> Optional[float]:
        """
        Returns seconds remaining in the cooldown window if the same symbol+direction
        was already signalled within duplicate_window_seconds, else None.
        """
        now = new_signal.timestamp
        window = timedelta(seconds=self.duplicate_window_seconds)
        for s in self._recent_signals:
            if (
                s.symbol.strip().upper() == new_signal.symbol.strip().upper()
                and s.direction == new_signal.direction
                and abs(now - s.timestamp) <= window
            ):
                elapsed = abs((now - s.timestamp).total_seconds())
                return max(0.0, self.duplicate_window_seconds - elapsed)
        return None

    def _is_duplicate(self, new_signal: Signal) -> bool:
        """Backward-compat wrapper around _check_cooldown."""
        return self._check_cooldown(new_signal) is not None

    def _store_signal(self, signal: Signal) -> bool:
        """
        Stores signal if not in cooldown and actionable (A+, A, or B tier).
        Prunes old entries from cooldown history.
        Returns True if stored, False if in cooldown or below tier threshold.
        Sets signal.state for every outcome.
        """
        cooldown_remaining = self._check_cooldown(signal)
        if cooldown_remaining is not None:
            signal.state = SignalState.DROPPED_DUPLICATE
            logger.info(
                "[Signal] COOLDOWN — %s %s: %.0fs remaining in %ds window [strategy=%s]",
                signal.symbol,
                signal.direction.value,
                cooldown_remaining,
                self.duplicate_window_seconds,
                self.name,
            )
            return False

        now = signal.timestamp
        cutoff = now - timedelta(seconds=self.duplicate_window_seconds * 2)
        self._recent_signals = [s for s in self._recent_signals if s.timestamp >= cutoff]
        self._recent_signals.append(signal)

        # Apply strategy-configured expiry
        signal.expires_at = signal.timestamp + timedelta(seconds=self.signal_expiry_seconds)

        if signal.confidence_tier in (
            ConfidenceTier.A_PLUS,
            ConfidenceTier.A,
            ConfidenceTier.B,
        ):
            signal.state = SignalState.ACTIVE
            self._signals.append(signal)
            logger.info(
                "[Signal] ACTIVE — %s %s %s @ %.2f | score=%.1f tier=%s id=%s [strategy=%s]",
                signal.symbol,
                signal.direction.value,
                signal.signal_type.value if hasattr(signal.signal_type, "value") else signal.signal_type,
                signal.entry_price,
                signal.confidence_score,
                signal.confidence_tier.value,
                signal.signal_id[:8],
                self.name,
            )
            return True

        return False

    def get_top_n_signals(self, n: int = 2) -> List[Signal]:
        """
        Returns top-N actionable (non-expired) signals sorted by confidence score (descending).
        Tie-breaker: latest timestamp descending.
        """
        now = datetime.now(timezone.utc)
        active = [s for s in self._signals if s.is_actionable(now)]
        active.sort(key=lambda s: (s.confidence_score, s.timestamp), reverse=True)
        return active[:n]

    def reset(self) -> None:
        """Clears all stored signals and duplicate cache."""
        self._recent_signals.clear()
        self._signals.clear()
        logger.info("[%s] Strategy signals and cache reset.", self.name)

    def get_status(self) -> Dict[str, Any]:
        """Returns strategy telemetry and active recommendation count."""
        now = datetime.now(timezone.utc)
        actionable = [s for s in self._signals if s.is_actionable(now)]
        return {
            "name": self.name,
            "asset_class": (
                self.asset_class.value
                if hasattr(self.asset_class, "value")
                else str(self.asset_class)
            ),
            "signal_expiry_seconds": self.signal_expiry_seconds,
            "duplicate_window_seconds": self.duplicate_window_seconds,
            "total_stored_signals": len(self._signals),
            "active_actionable_signals": len(actionable),
            "top_2_recommendations": [s.to_dict() for s in self.get_top_n_signals(2)],
        }