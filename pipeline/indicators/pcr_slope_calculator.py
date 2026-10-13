"""
Strike Desk V4 - PCR Slope Calculator & Staleness Monitoring
Computes the rate of change of Put-Call Ratio (PCR) over a rolling window.
Formula: PCR_slope = (PCR_current - PCR_window_start) / window_duration_minutes

Market Interpretation:
- Positive slope = increasing put activity (bearish sentiment)
- Negative slope = decreasing put activity (bullish sentiment)
"""
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple

from models.option_chain import OptionChain
from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator

try:
    from .pcr_staleness_monitor import (
        PCRStalenessMonitor,
        pcr_staleness_monitor,
    )
except ImportError:
    from pipeline.indicators.pcr_staleness_monitor import (
        PCRStalenessMonitor,
        pcr_staleness_monitor,
    )

from utils.pcr_calculator import pcr_calculator

logger = logging.getLogger(__name__)


class PCRSlopeCalculator(BaseIndicatorCalculator):
    """
    Calculates PCR rate of change over a configurable rolling time window (default 5 minutes).
    Integrates with PCRStalenessMonitor to track feed freshness and throughput.
    """

    def __init__(
        self,
        symbol_or_window: Any = "",
        window_minutes: int = 5,
        staleness_monitor: Optional[PCRStalenessMonitor] = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(symbol_or_window, int):
            actual_window = symbol_or_window
            actual_symbol = kwargs.get("symbol", "")
        else:
            actual_symbol = symbol_or_window or kwargs.get("symbol", "")
            actual_window = kwargs.get("window_minutes", window_minutes)

        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.window_minutes: int = int(actual_window)
        if self.window_minutes <= 0:
            raise ValueError(f"window_minutes must be positive, got {self.window_minutes}")

        self.pcr_history: List[Tuple[datetime, float]] = []
        self.last_slope: Optional[float] = None
        self.staleness_monitor = staleness_monitor or pcr_staleness_monitor

    def calculate_slope(self) -> Optional[float]:
        """
        Calculates PCR slope over the configured rolling window.
        Returns:
            - None if insufficient data (< 2 points in window)
            - float slope = (pcr_latest - pcr_oldest_in_window) / window_minutes
        """
        if len(self.pcr_history) < 2:
            self.last_slope = None
            return None

        oldest_ts, oldest_pcr = self.pcr_history[0]
        latest_ts, latest_pcr = self.pcr_history[-1]

        delta_pcr = latest_pcr - oldest_pcr
        self.last_slope = delta_pcr / float(self.window_minutes)
        return self.last_slope

    def process_pcr_update(
        self,
        underlying: str,
        pcr: float,
        timestamp: datetime,
    ) -> Dict[str, Any]:
        """
        Ingests a new PCR data point, updates staleness monitor,
        prunes points outside the rolling window, and computes slope.
        Returns:
            {"pcr_slope": Optional[float], "pcr_current": float, "window_minutes": int, "sufficient_data": bool}
        """
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        sym = underlying.strip().upper()
        if not self.symbol:
            self.symbol = sym

        # 1. Update staleness monitor
        self.staleness_monitor.record_update(sym, ts)

        # 2. Append new PCR point
        pcr_val = float(pcr)
        self.pcr_history.append((ts, pcr_val))

        # 3. Prune history outside window
        cutoff = ts - timedelta(minutes=self.window_minutes)
        self.pcr_history = [(t, v) for t, v in self.pcr_history if t >= cutoff]

        # 4. Calculate slope
        slope = self.calculate_slope()
        sufficient = slope is not None

        result = {
            "pcr_slope": slope,
            "pcr_current": pcr_val,
            "window_minutes": self.window_minutes,
            "sufficient_data": sufficient,
        }
        self.indicator_values.update(result)
        return result

    def process_option_chain(
        self, chain: OptionChain, timestamp: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Integrates with Phase 1 OptionChain: calculates/extracts PCR and updates slope.
        """
        ts = timestamp or datetime.now(timezone.utc)
        pcr = chain.pcr
        if pcr == 0.0 and chain.strikes:
            pcr, _ = pcr_calculator.calculate(chain)

        return self.process_pcr_update(chain.underlying, pcr, ts)

    def is_pcr_trustable(
        self,
        underlying: Optional[str] = None,
        current_time: Optional[datetime] = None,
        require_min_frequency: bool = False,
    ) -> bool:
        """
        Checks if PCR for the underlying is fresh (<= 30s) and optionally meets frequency criteria.
        By default verifies that the PCR feed is not stale (> 30s), enforcing Review Risk 2.
        """
        sym = (underlying or self.symbol).strip().upper()
        now = current_time or datetime.now(timezone.utc)
        return self.staleness_monitor.is_trustable(
            sym, now, require_min_frequency=require_min_frequency
        )

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        """
        Inherited from BaseIndicatorCalculator. Returns current PCR indicator state.
        """
        pcr_val = getattr(tick, "pcr", None)
        if pcr_val is not None:
            self.process_pcr_update(self.symbol, float(pcr_val), tick.timestamp)

        return {
            "pcr_slope": self.last_slope,
            "pcr_current": self.pcr_history[-1][1] if self.pcr_history else None,
            "window_minutes": self.window_minutes,
            "sufficient_data": self.last_slope is not None,
        }

    def reset(self) -> None:
        """Resets PCR history and slope state."""
        super().reset()
        self.pcr_history.clear()
        self.last_slope = None

    def get_state(self) -> Dict[str, Any]:
        """Returns internal state snapshot for telemetry and persistence."""
        state = super().get_state()
        state.update({
            "window_minutes": self.window_minutes,
            "last_slope": self.last_slope,
            "pcr_current": self.pcr_history[-1][1] if self.pcr_history else None,
            "sufficient_data": self.last_slope is not None,
            "pcr_history_len": len(self.pcr_history),
        })
        return state
