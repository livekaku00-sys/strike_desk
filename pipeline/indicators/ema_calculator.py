"""
Strike Desk V4 - Exponential Moving Average (EMA) Calculator
Standard EMA formula with smoothing factor α = 2/(period+1).
Initial EMA calculated as Simple Moving Average (SMA) of first N periods.
"""
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator

logger = logging.getLogger(__name__)

# Indian Standard Time: UTC+05:30
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dtime(9, 15)


class EMACalculator(BaseIndicatorCalculator):
    """
    Stateful Exponential Moving Average (EMA) calculator with configurable period.
    Default period = 9 (alpha = 2/(9+1) = 0.2).
    Initial value is SMA of the first `period` observations.
    """

    def __init__(
        self,
        symbol: Any = "",
        period: int = 9,
        **kwargs: Any,
    ) -> None:
        if isinstance(symbol, int):
            actual_period = symbol
            actual_symbol = kwargs.get("symbol", "")
        else:
            actual_symbol = symbol or kwargs.get("symbol", "")
            actual_period = kwargs.get("period", period)

        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.period: int = int(actual_period)
        if self.period <= 0:
            raise ValueError(f"EMA period must be positive, got {self.period}")

        self.alpha: float = 2.0 / (self.period + 1.0)
        self.prices: List[float] = []
        self.ema_value: Optional[float] = None
        self.last_reset_date: Optional[date] = None

    def calculate_ema(self, new_price: float) -> Optional[float]:
        """
        Updates price history and calculates EMA.
        - If len(prices) < period: append price, return None (insufficient data)
        - If len(prices) == period: calculate initial SMA, set as first EMA
        - If len(prices) > period: apply EMA formula
        """
        price = float(new_price)
        self.prices.append(price)

        if len(self.prices) > self.period:
            self.prices.pop(0)

        if self.ema_value is None:
            if len(self.prices) == self.period:
                self.ema_value = sum(self.prices) / float(self.period)
                return self.ema_value
            else:
                return None
        else:
            self.ema_value = (price * self.alpha) + (self.ema_value * (1.0 - self.alpha))
            return self.ema_value

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        """
        Computes EMA from tick.ltp.
        Returns indicator dictionary.
        """
        ema = self.calculate_ema(tick.ltp)
        return {
            "ema": ema,
            "period": self.period,
            "sufficient_data": ema is not None,
        }

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
        Clears price history and EMA value.
        """
        super().reset()
        self.prices.clear()
        self.ema_value = None

    def process_tick(self, tick: Tick) -> Dict[str, Any]:
        """
        Validates tick sequence, checks if daily reset is needed,
        and computes updated EMA values.
        Returns: {"ema": Optional[float], "period": int, "sufficient_data": bool}
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
            "period": self.period,
            "alpha": self.alpha,
            "prices": list(self.prices),
            "ema_value": self.ema_value,
            "ema": self.ema_value,
            "sufficient_data": self.ema_value is not None,
            "last_reset_date": (
                self.last_reset_date.isoformat()
                if self.last_reset_date
                else None
            ),
        })
        return state