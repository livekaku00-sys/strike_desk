"""
Strike Desk V4 - Supertrend Indicator Calculator
Period: 10, Multiplier: 3.0.
Uses ATR (Wilder smoothing, period=10) internally for band computation.
Basic upper/lower bands: hl2 ± multiplier × ATR.
Final bands carry forward unless price breaks through.
Direction: UP (price > upper_band) / DOWN (price < lower_band) with carry-forward.
Reset at market open 09:15 IST. Degraded mode: return None until ATR ready.
"""
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
from typing import Any, Dict, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator
from pipeline.indicators.atr_calculator import ATRCalculator

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dtime(9, 15)


class SupertrendCalculator(BaseIndicatorCalculator):
    """
    Stateful Supertrend indicator using an internal ATR(period) calculator.
    Produces first output once internal ATR is ready.
    Direction is UP when price is above the supertrend line, DOWN otherwise.
    trend_changed is True only on the tick the direction flips.
    """

    def __init__(
        self,
        symbol: str = "",
        period: int = 10,
        multiplier: float = 3.0,
        **kwargs: Any,
    ) -> None:
        actual_symbol = symbol or kwargs.get("symbol", "")
        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.period: int = int(period)
        self.multiplier: float = float(multiplier)
        if self.period <= 0:
            raise ValueError(f"Supertrend period must be positive, got {self.period}")

        # Internal ATR for band computation — self-tracked _interval_count
        self._atr_calc: ATRCalculator = ATRCalculator(symbol=actual_symbol, period=self.period)

        # Previous bar state for band carry-forward and direction carry-forward
        self._prev_close: Optional[float] = None
        self._prev_upper_band: Optional[float] = None
        self._prev_lower_band: Optional[float] = None
        self._prev_direction: Optional[int] = None  # 1 = UP, -1 = DOWN

        # Cached output
        self.supertrend_value: Optional[float] = None
        self.direction_value: Optional[str] = None

        self.last_reset_date: Optional[date] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _null_result(self) -> Dict[str, Any]:
        return {
            "supertrend": None,
            "direction": None,
            "signal": None,
            "trend_changed": False,
            "sufficient_data": False,
        }

    # ------------------------------------------------------------------
    # BaseIndicatorCalculator interface
    # ------------------------------------------------------------------

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        h = tick.high if tick.high > 0.0 else tick.ltp
        l = tick.low if tick.low > 0.0 else tick.ltp
        c = float(tick.ltp)

        # Internal ATR computation (directly called, not via process_tick)
        atr_result = self._atr_calc.calculate_indicators(tick)
        if not atr_result["sufficient_data"]:
            self._prev_close = c
            return self._null_result()

        atr = atr_result["atr"]
        hl2 = (h + l) / 2.0

        # Basic bands
        basic_upper = hl2 + self.multiplier * atr
        basic_lower = hl2 - self.multiplier * atr

        # Adjusted bands with carry-forward
        if self._prev_upper_band is None or self._prev_lower_band is None:
            final_upper = basic_upper
            final_lower = basic_lower
        else:
            prev_c = self._prev_close
            # Upper: carry prev if basic is wider (higher) AND prev close below prev upper
            final_upper = (
                basic_upper
                if (basic_upper < self._prev_upper_band or prev_c > self._prev_upper_band)
                else self._prev_upper_band
            )
            # Lower: carry prev if basic is wider (lower) AND prev close above prev lower
            final_lower = (
                basic_lower
                if (basic_lower > self._prev_lower_band or prev_c < self._prev_lower_band)
                else self._prev_lower_band
            )

        # Direction: determined by current close vs previous final bands
        if self._prev_direction is None:
            # Initialize — convention: start UP
            direction = 1
        elif self._prev_direction == 1:  # was UP
            direction = -1 if c < final_lower else 1
        else:  # was DOWN
            direction = 1 if c > final_upper else -1

        # Supertrend line
        supertrend = final_lower if direction == 1 else final_upper
        trend_changed = (self._prev_direction is not None) and (direction != self._prev_direction)

        # Update state for next tick
        self._prev_close = c
        self._prev_upper_band = final_upper
        self._prev_lower_band = final_lower
        self._prev_direction = direction

        self.supertrend_value = round(supertrend, 4)
        self.direction_value = "UP" if direction == 1 else "DOWN"

        return {
            "supertrend": self.supertrend_value,
            "direction": self.direction_value,
            "signal": "BUY" if direction == 1 else "SELL",
            "trend_changed": trend_changed,
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
        self._atr_calc.reset()
        self._prev_close = None
        self._prev_upper_band = None
        self._prev_lower_band = None
        self._prev_direction = None
        self.supertrend_value = None
        self.direction_value = None

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
            "multiplier": self.multiplier,
            "supertrend": self.supertrend_value,
            "direction": self.direction_value,
            "prev_upper_band": self._prev_upper_band,
            "prev_lower_band": self._prev_lower_band,
            "prev_direction": self._prev_direction,
            "sufficient_data": self.supertrend_value is not None,
            "last_reset_date": self.last_reset_date.isoformat() if self.last_reset_date else None,
        })
        return state
