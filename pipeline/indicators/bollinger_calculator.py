"""
Strike Desk V4 - Bollinger Bands Calculator
Period: 20, Multiplier: 2.0 standard deviations (population std dev).
middle = SMA(20), upper = middle + 2σ, lower = middle - 2σ.
Reset at market open 09:15 IST. Degraded mode: None if < 20 ticks.
"""
from datetime import date, datetime, time as dtime, timedelta, timezone
import math
import logging
from typing import Any, Dict, List, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dtime(9, 15)


class BollingerCalculator(BaseIndicatorCalculator):
    """
    Rolling Bollinger Bands using population std dev.
    Produces first output at tick 20 (full window). Returns None in degraded mode.
    """

    def __init__(
        self,
        symbol: str = "",
        period: int = 20,
        num_std: float = 2.0,
        near_threshold_pct: float = 0.005,
        **kwargs: Any,
    ) -> None:
        actual_symbol = symbol or kwargs.get("symbol", "")
        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.period: int = int(period)
        if self.period <= 0:
            raise ValueError(f"Bollinger period must be positive, got {self.period}")
        self.num_std: float = float(num_std)
        # "near" = within this fraction of the middle price from each band
        self.near_threshold_pct: float = float(near_threshold_pct)

        self._prices: List[float] = []
        self.upper_value: Optional[float] = None
        self.middle_value: Optional[float] = None
        self.lower_value: Optional[float] = None
        self.last_reset_date: Optional[date] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pop_std(prices: List[float]) -> float:
        """Population standard deviation (divide by N)."""
        n = len(prices)
        if n < 2:
            return 0.0
        mean = sum(prices) / n
        variance = sum((p - mean) ** 2 for p in prices) / n
        return math.sqrt(variance)

    def _classify_position(
        self, price: float, lower: float, upper: float, middle: float
    ) -> str:
        near = self.near_threshold_pct * middle  # e.g. 0.5% of middle price
        if price >= upper:
            return "ABOVE_UPPER"
        if price >= upper - near:
            return "NEAR_UPPER"
        if price <= lower:
            return "BELOW_LOWER"
        if price <= lower + near:
            return "NEAR_LOWER"
        return "MIDDLE"

    # ------------------------------------------------------------------
    # BaseIndicatorCalculator interface
    # ------------------------------------------------------------------

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        price = float(tick.ltp)
        self._prices.append(price)
        if len(self._prices) > self.period:
            self._prices.pop(0)

        if len(self._prices) < self.period:
            return {
                "upper": None, "middle": None, "lower": None,
                "bandwidth": None, "percent_b": None, "bb_position": None,
                "sufficient_data": False,
            }

        middle = sum(self._prices) / self.period
        std = self._pop_std(self._prices)
        upper = middle + self.num_std * std
        lower = middle - self.num_std * std

        band_width = upper - lower
        bandwidth = round(band_width / middle, 6) if middle > 0.0 else 0.0

        if band_width > 0.0:
            percent_b = round((price - lower) / band_width, 6)
        else:
            percent_b = 0.5  # degenerate: zero-width bands → treat as midpoint

        position = self._classify_position(price, lower, upper, middle)

        self.upper_value = round(upper, 4)
        self.middle_value = round(middle, 4)
        self.lower_value = round(lower, 4)

        return {
            "upper": self.upper_value,
            "middle": self.middle_value,
            "lower": self.lower_value,
            "bandwidth": bandwidth,
            "percent_b": percent_b,
            "bb_position": position,
            "sufficient_data": True,
        }

    def should_reset(self, tick_timestamp: datetime) -> bool:
        if self.last_reset_date is None:
            return False
        ts = tick_timestamp if tick_timestamp.tzinfo else tick_timestamp.replace(tzinfo=timezone.utc)
        ts_ist = ts.astimezone(IST)
        return ts_ist.date() > self.last_reset_date and ts_ist.time() >= MARKET_OPEN_TIME

    def reset(self) -> None:
        super().reset()
        self._prices.clear()
        self.upper_value = None
        self.middle_value = None
        self.lower_value = None

    def process_tick(self, tick: Tick) -> Dict[str, Any]:
        ts = tick.timestamp if tick.timestamp.tzinfo else tick.timestamp.replace(tzinfo=timezone.utc)
        ts_ist = ts.astimezone(IST)
        if self.should_reset(ts):
            self.reset()
            self.last_reset_date = ts_ist.date()
        elif self.last_reset_date is None:
            self.last_reset_date = ts_ist.date()
        return super().process_tick(tick)

    def get_state(self) -> Dict[str, Any]:
        state = super().get_state()
        state.update({
            "period": self.period,
            "num_std": self.num_std,
            "upper": self.upper_value,
            "middle": self.middle_value,
            "lower": self.lower_value,
            "prices_snapshot": list(self._prices),
            "sufficient_data": self.middle_value is not None,
            "last_reset_date": self.last_reset_date.isoformat() if self.last_reset_date else None,
        })
        return state
