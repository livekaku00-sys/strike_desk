"""
Strike Desk V4 - Volume Weighted Average Price (VWAP) Calculator
Cumulative intraday calculation resetting at market open (09:15 IST).
Formula: VWAP = Σ(Price × Volume) / Σ(Volume)
"""
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
from typing import Any, Dict, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator

logger = logging.getLogger(__name__)

# Indian Standard Time: UTC+05:30
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dtime(9, 15)


class VWAPCalculator(BaseIndicatorCalculator):
    """
    Cumulative intraday Volume Weighted Average Price (VWAP) calculator.
    Resets at market open (09:15 IST) on a new trading day.
    """

    def __init__(self, symbol: str = "") -> None:
        super().__init__(str(symbol).strip().upper() if symbol else "")
        self.cumulative_pv: float = 0.0
        self.cumulative_volume: float = 0.0
        self.vwap: float = 0.0
        self.last_reset_date: Optional[date] = None

    def should_reset(self, tick_timestamp: datetime) -> bool:
        """
        Returns True if tick is from a new trading day (after 09:15 IST).
        """
        if self.last_reset_date is None:
            return False

        ts = tick_timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_ist = ts.astimezone(IST)

        return ts_ist.date() > self.last_reset_date and ts_ist.time() >= MARKET_OPEN_TIME

    def reset(self) -> None:
        """
        Resets cumulative values to 0.
        """
        super().reset()
        self.cumulative_pv = 0.0
        self.cumulative_volume = 0.0
        self.vwap = 0.0

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        """
        Calculates cumulative PV, cumulative volume, and VWAP.
        When volume=0 (indices have no traded volume), uses weight=1 per tick
        so VWAP degrades gracefully to a simple running average of price.
        """
        price = float(tick.ltp)
        vol = float(tick.volume)
        # Indices report volume=0; fall back to tick-weight=1 so VWAP = avg(LTP)
        weight = vol if vol > 0.0 else 1.0

        self.cumulative_pv += price * weight
        self.cumulative_volume += weight

        if self.cumulative_volume > 0.0:
            self.vwap = self.cumulative_pv / self.cumulative_volume
        else:
            self.vwap = 0.0

        return {
            "vwap": self.vwap,
            "cumulative_volume": self.cumulative_volume,
            "tick_count": self.tick_count,
        }

    def process_tick(self, tick: Tick) -> Dict[str, Any]:
        """
        Validates tick sequence, checks if daily reset is needed,
        and computes updated indicator values.
        Returns: {"vwap": float, "cumulative_volume": float, "tick_count": int}
        """
        ts = tick.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_ist = ts.astimezone(IST)

        if self.should_reset(ts):
            self.reset()
            self.last_reset_date = ts_ist.date()
        elif self.last_reset_date is None:
            self.last_reset_date = ts_ist.date()

        return super().process_tick(tick)

    def get_state(self) -> Dict[str, Any]:
        """
        Returns internal state snapshot for telemetry and persistence.
        """
        state = super().get_state()
        state.update({
            "cumulative_pv": self.cumulative_pv,
            "cumulative_volume": self.cumulative_volume,
            "vwap": self.vwap,
            "last_reset_date": (
                self.last_reset_date.isoformat()
                if self.last_reset_date
                else None
            ),
        })
        return state