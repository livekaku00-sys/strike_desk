from abc import ABC, abstractmethod
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional

from models.tick import Tick

logger = logging.getLogger(__name__)


class BaseIndicatorCalculator(ABC):
    """
    Stateful per-symbol base indicator calculator.
    Tracks chronological tick sequence, detects gap anomalies,
    and maintains real-time indicator state.
    """

    MAX_TIME_GAP_SECONDS = 1.0  # Alert gap for liquid instruments

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.strip().upper()
        self.tick_count: int = 0
        self.last_tick_timestamp: Optional[datetime] = None
        self.last_ltp: Optional[float] = None
        self.indicator_values: Dict[str, Any] = {}

    @abstractmethod
    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        """
        Custom calculation logic implemented by concrete indicator classes
        (VWAP, EMA9, PCR slope, Sentiment).
        """
        pass

    def process_tick(self, tick: Tick) -> Dict[str, Any]:
        """
        Validates tick sequence, checks for temporal gaps,
        and computes updated indicator values.
        """
        ts = tick.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        # 0. LTP validity gate — skip ticks whose price is invalid (NaN, Inf, zero).
        #    ltp_valid is set by Tick.__post_init__ from the normalizer output.
        if not tick.ltp_valid:
            logger.warning(
                "[%s] Skipping tick with invalid LTP: %r (ltp_valid=False)",
                self.symbol,
                tick.ltp,
            )
            return dict(self.indicator_values)

        # 1. Out-of-order detection
        if self.last_tick_timestamp is not None:
            if ts < self.last_tick_timestamp:
                logger.warning(
                    "[%s] Out-of-order tick detected! Current ts: %s, Last ts: %s",
                    self.symbol,
                    ts.isoformat(),
                    self.last_tick_timestamp.isoformat(),
                )

            # 2. Missing tick / gap detection (> 1.0s)
            gap = (ts - self.last_tick_timestamp).total_seconds()
            if gap > self.MAX_TIME_GAP_SECONDS:
                logger.warning(
                    "[%s] Feed gap detected! Time delta: %.3fs between ticks (> %.1fs threshold).",
                    self.symbol,
                    gap,
                    self.MAX_TIME_GAP_SECONDS,
                )

        self.last_tick_timestamp = ts
        self.last_ltp = tick.ltp
        self.tick_count += 1

        # 3. Compute indicators
        computed = self.calculate_indicators(tick)
        self.indicator_values.update(computed)

        return dict(self.indicator_values)

    def reset(self) -> None:
        """Resets calculator state (e.g. at daily market open)."""
        self.tick_count = 0
        self.last_tick_timestamp = None
        self.last_ltp = None
        self.indicator_values.clear()
        logger.info("[%s] Indicator calculator state reset.", self.symbol)

    def get_state(self) -> Dict[str, Any]:
        """Returns internal state snapshot for telemetry and persistence."""
        return {
            "symbol": self.symbol,
            "tick_count": self.tick_count,
            "last_tick_timestamp": (
                self.last_tick_timestamp.isoformat()
                if self.last_tick_timestamp
                else None
            ),
            "last_ltp": self.last_ltp,
            "indicator_values": dict(self.indicator_values),
        }


class DefaultIndicatorCalculator(BaseIndicatorCalculator):
    """
    Default baseline indicator calculator storing basic price action metrics
    prior to advanced indicator modules in Prompts 2-3.
    """

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        return {
            "ltp": tick.ltp,
            "volume": tick.volume,
            "processed_ticks": self.tick_count,
        }
