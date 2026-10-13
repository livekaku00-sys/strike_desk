"""
Strike Desk V4 - MACD (Moving Average Convergence Divergence) Calculator
Fast EMA: 12, Slow EMA: 26, Signal EMA: 9.
Standard EMA smoothing: alpha = 2 / (period + 1).
EMAs are seeded from SMA(period) of the first `period` prices.
Reset at market open 09:15 IST. Degraded mode: return None if < 26 ticks.
"""
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dtime(9, 15)


class MACDCalculator(BaseIndicatorCalculator):
    """
    Stateful MACD calculator with standard EMA smoothing.
    Produces first complete output (with signal line) when enough MACD values have
    accumulated for the signal EMA seed (tick 26 + 8 = tick 34).
    macd_line is available from tick 26; signal_line from tick 34.
    Crossover detection uses histogram sign change (macd_line - signal_line).
    """

    def __init__(
        self,
        symbol: str = "",
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        **kwargs: Any,
    ) -> None:
        actual_symbol = symbol or kwargs.get("symbol", "")
        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.fast_period: int = int(fast)
        self.slow_period: int = int(slow)
        self.signal_period: int = int(signal)

        if self.fast_period <= 0 or self.slow_period <= 0 or self.signal_period <= 0:
            raise ValueError("All MACD periods must be positive")
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be less than slow_period")

        self._fast_alpha: float = 2.0 / (self.fast_period + 1)
        self._slow_alpha: float = 2.0 / (self.slow_period + 1)
        self._signal_alpha: float = 2.0 / (self.signal_period + 1)

        # Price buffer for seeding EMAs (cleared once slow EMA is seeded)
        self._prices: List[float] = []
        self._fast_ema: Optional[float] = None
        self._slow_ema: Optional[float] = None

        # MACD history buffer for seeding signal EMA
        self._macd_history: List[float] = []
        self._signal_ema: Optional[float] = None

        # For crossover detection (sign of prev histogram)
        self._prev_histogram: Optional[float] = None

        # Cached output values
        self.macd_line: Optional[float] = None
        self.signal_line: Optional[float] = None

        self.last_reset_date: Optional[date] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _null_result(self) -> Dict[str, Any]:
        return {
            "macd_line": None,
            "signal_line": None,
            "histogram": None,
            "crossover": None,
            "trend": None,
            "sufficient_data": False,
        }

    # ------------------------------------------------------------------
    # BaseIndicatorCalculator interface
    # ------------------------------------------------------------------

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        price = float(tick.ltp)

        # --- Phase 1: accumulate prices until slow EMA can be seeded ---
        if self._slow_ema is None:
            self._prices.append(price)

            # Seed or step fast EMA
            if self._fast_ema is None:
                if len(self._prices) >= self.fast_period:
                    # Seed fast EMA from SMA of first fast_period prices
                    self._fast_ema = sum(self._prices[:self.fast_period]) / self.fast_period
                    # Catch up for any prices appended after the seed point
                    for p in self._prices[self.fast_period:]:
                        self._fast_ema = self._fast_alpha * p + (1.0 - self._fast_alpha) * self._fast_ema
            else:
                # Fast EMA running: step with current price
                self._fast_ema = self._fast_alpha * price + (1.0 - self._fast_alpha) * self._fast_ema

            # Try to seed slow EMA once we have enough prices
            if len(self._prices) < self.slow_period:
                return self._null_result()

            # Seed slow EMA from SMA(slow_period)
            self._slow_ema = sum(self._prices) / self.slow_period
            self._prices.clear()
            # fast_ema was already stepped for this price above; proceed to MACD computation

        else:
            # --- Phase 2: both EMAs running, step both ---
            self._fast_ema = self._fast_alpha * price + (1.0 - self._fast_alpha) * self._fast_ema
            self._slow_ema = self._slow_alpha * price + (1.0 - self._slow_alpha) * self._slow_ema

        # --- Compute MACD line ---
        macd = self._fast_ema - self._slow_ema
        self.macd_line = round(macd, 4)
        trend = "BULLISH" if macd > 0.0 else ("BEARISH" if macd < 0.0 else "NEUTRAL")

        # --- Seed or step signal EMA ---
        if self._signal_ema is None:
            self._macd_history.append(macd)
            if len(self._macd_history) < self.signal_period:
                # Signal not ready yet
                return {
                    "macd_line": self.macd_line,
                    "signal_line": None,
                    "histogram": None,
                    "crossover": None,
                    "trend": trend,
                    "sufficient_data": False,
                }
            # Seed signal EMA from SMA of first signal_period MACD values
            self._signal_ema = sum(self._macd_history) / self.signal_period
            self._macd_history.clear()
        else:
            self._signal_ema = self._signal_alpha * macd + (1.0 - self._signal_alpha) * self._signal_ema

        signal = self._signal_ema
        self.signal_line = round(signal, 4)
        histogram = round(macd - signal, 4)

        # --- Crossover detection via histogram sign change (use rounded value) ---
        crossover = "NO_CROSS"
        if self._prev_histogram is not None:
            if self._prev_histogram <= 0.0 and histogram > 0.0:
                crossover = "BULLISH_CROSS"
            elif self._prev_histogram >= 0.0 and histogram < 0.0:
                crossover = "BEARISH_CROSS"
        self._prev_histogram = histogram

        return {
            "macd_line": self.macd_line,
            "signal_line": self.signal_line,
            "histogram": histogram,
            "crossover": crossover,
            "trend": trend,
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
        self._fast_ema = None
        self._slow_ema = None
        self._macd_history.clear()
        self._signal_ema = None
        self._prev_histogram = None
        self.macd_line = None
        self.signal_line = None

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
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "signal_period": self.signal_period,
            "fast_ema": self._fast_ema,
            "slow_ema": self._slow_ema,
            "signal_ema": self._signal_ema,
            "macd_line": self.macd_line,
            "signal_line": self.signal_line,
            "sufficient_data": self.signal_line is not None,
            "last_reset_date": self.last_reset_date.isoformat() if self.last_reset_date else None,
        })
        return state
