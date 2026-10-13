"""
Strike Desk V4 - Relative Strength Index (RSI) Calculator
Period: 14, Wilder smoothing (alpha = 1/n).
Initial seed: SMA of first available gains/losses (period - 1 changes at tick 14).
Reset at market open 09:15 IST.
"""
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dtime(9, 15)


class RSICalculator(BaseIndicatorCalculator):
    """
    Stateful RSI calculator using Wilder smoothing.
    Produces first value at tick 14 (seeded from 13 price changes).
    Returns None in degraded mode (< 14 ticks).
    """

    OVERSOLD_THRESHOLD: float = 30.0
    OVERBOUGHT_THRESHOLD: float = 70.0

    def __init__(self, symbol: str = "", period: int = 14, **kwargs: Any) -> None:
        actual_symbol = symbol or kwargs.get("symbol", "")
        actual_period = kwargs.get("period", period)
        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.period: int = int(actual_period)
        if self.period <= 0:
            raise ValueError(f"RSI period must be positive, got {self.period}")

        self._prices: List[float] = []
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self.rsi_value: Optional[float] = None
        self.last_reset_date: Optional[date] = None

    def _classify_zone(self, rsi: float) -> str:
        if rsi < self.OVERSOLD_THRESHOLD:
            return "OVERSOLD"
        if rsi > self.OVERBOUGHT_THRESHOLD:
            return "OVERBOUGHT"
        return "NEUTRAL"

    def _compute_rsi(self, price: float) -> Optional[float]:
        self._prices.append(float(price))
        # Keep last period+1 prices (sliding window, enough for one Wilder step)
        if len(self._prices) > self.period + 1:
            self._prices.pop(0)

        if len(self._prices) < self.period:
            # Degraded: fewer than `period` prices → not enough for seed
            return None

        if self._avg_gain is None:
            # Seed phase: SMA of all available changes (period-1 changes at exactly period prices)
            gains, losses = [], []
            for i in range(1, len(self._prices)):
                delta = self._prices[i] - self._prices[i - 1]
                gains.append(max(delta, 0.0))
                losses.append(max(-delta, 0.0))
            n = len(gains)
            self._avg_gain = sum(gains) / n
            self._avg_loss = sum(losses) / n
        else:
            # Wilder smoothing: (prev * (n-1) + current) / n
            delta = self._prices[-1] - self._prices[-2]
            gain = max(delta, 0.0)
            loss = max(-delta, 0.0)
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0.0:
            self.rsi_value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.rsi_value = 100.0 - (100.0 / (1.0 + rs))

        return round(self.rsi_value, 4)

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        rsi = self._compute_rsi(tick.ltp)
        zone = self._classify_zone(rsi) if rsi is not None else None
        return {
            "rsi": rsi,
            "zone": zone,
            "sufficient_data": rsi is not None,
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
        self._avg_gain = None
        self._avg_loss = None
        self.rsi_value = None

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
            "rsi": self.rsi_value,
            "avg_gain": self._avg_gain,
            "avg_loss": self._avg_loss,
            "sufficient_data": self.rsi_value is not None,
            "last_reset_date": self.last_reset_date.isoformat() if self.last_reset_date else None,
        })
        return state
