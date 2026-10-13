"""
Strike Desk V4 - Average Directional Index (ADX) Calculator
Period: 14, Wilder smoothing (alpha = 1/n).
Computes: True Range, +DM, -DM, +DI, -DI, DX, ADX.
Reset at market open 09:15 IST.
Degraded mode: returns None until 14 ticks processed.
"""
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dtime(9, 15)


class ADXCalculator(BaseIndicatorCalculator):
    """
    Stateful ADX calculator using Wilder smoothing for TR, +DM, and -DM.
    Produces first value at tick 14. Returns None in degraded mode (< 14 ticks).
    Uses tick.high / tick.low when non-zero; falls back to tick.ltp otherwise.
    """

    WEAK_THRESHOLD: float = 20.0
    MODERATE_THRESHOLD: float = 25.0
    STRONG_THRESHOLD: float = 40.0

    def __init__(self, symbol: str = "", period: int = 14, **kwargs: Any) -> None:
        actual_symbol = symbol or kwargs.get("symbol", "")
        actual_period = kwargs.get("period", period)
        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.period: int = int(actual_period)
        if self.period <= 0:
            raise ValueError(f"ADX period must be positive, got {self.period}")

        # Interval counter — self-tracked in calculate_indicators so it works
        # whether called via process_tick or called directly by a bundle.
        self._interval_count: int = 0

        # Seed-phase raw buffers
        self._tr_buf: List[float] = []
        self._plus_dm_buf: List[float] = []
        self._minus_dm_buf: List[float] = []

        # Previous tick H/L/C
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None
        self._prev_close: Optional[float] = None

        # Wilder smoothed values (None until seed phase complete)
        self._smoothed_tr: Optional[float] = None
        self._smoothed_plus_dm: Optional[float] = None
        self._smoothed_minus_dm: Optional[float] = None

        # Output values
        self.adx_value: Optional[float] = None
        self.plus_di_value: Optional[float] = None
        self.minus_di_value: Optional[float] = None

        self.last_reset_date: Optional[date] = None

    def _classify_trend(self, adx: float) -> str:
        if adx < self.WEAK_THRESHOLD:
            return "WEAK"
        if adx < self.MODERATE_THRESHOLD:
            return "MODERATE"
        if adx <= self.STRONG_THRESHOLD:
            return "STRONG"
        return "VERY_STRONG"

    def _null_result(self) -> Dict[str, Any]:
        return {
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": None,
            "direction": None,
            "sufficient_data": False,
        }

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        # Extract H/L/C; fall back to ltp when OHLC fields are not populated
        h = tick.high if tick.high > 0.0 else tick.ltp
        l = tick.low if tick.low > 0.0 else tick.ltp
        c = tick.ltp

        # First tick: seed previous values only, no interval to compute yet
        if self._prev_close is None:
            self._prev_high = h
            self._prev_low = l
            self._prev_close = c
            return self._null_result()

        # True Range
        tr = max(h - l, abs(h - self._prev_close), abs(l - self._prev_close))

        # Directional Movement
        up_move = h - self._prev_high
        down_move = self._prev_low - l
        plus_dm = max(up_move, 0.0) if up_move > down_move else 0.0
        minus_dm = max(down_move, 0.0) if down_move > up_move else 0.0

        # Advance prev-tick state
        self._prev_high = h
        self._prev_low = l
        self._prev_close = c

        # Self-tracked interval counter so this method works whether called via
        # process_tick or directly (e.g. from SymbolIndicatorBundle.calculate_indicators).
        self._interval_count += 1

        # Degraded mode: need period-1 intervals before first output
        # (tick 1 seeds prev; ticks 2..13 = 12 intervals; tick 14 = 13th interval → output)
        if self._interval_count < self.period - 1:
            self._tr_buf.append(tr)
            self._plus_dm_buf.append(plus_dm)
            self._minus_dm_buf.append(minus_dm)
            return self._null_result()

        # Seed phase completion: compute Wilder SMA seed from accumulated buffer
        if self._smoothed_tr is None:
            # Append current interval then seed
            self._tr_buf.append(tr)
            self._plus_dm_buf.append(plus_dm)
            self._minus_dm_buf.append(minus_dm)
            n = len(self._tr_buf)
            self._smoothed_tr = sum(self._tr_buf) / n
            self._smoothed_plus_dm = sum(self._plus_dm_buf) / n
            self._smoothed_minus_dm = sum(self._minus_dm_buf) / n
            # Buffers no longer needed after seeding
            self._tr_buf.clear()
            self._plus_dm_buf.clear()
            self._minus_dm_buf.clear()
        else:
            # Wilder smoothing: prev * (n-1)/n + current * 1/n
            self._smoothed_tr = (self._smoothed_tr * (self.period - 1) + tr) / self.period
            self._smoothed_plus_dm = (self._smoothed_plus_dm * (self.period - 1) + plus_dm) / self.period
            self._smoothed_minus_dm = (self._smoothed_minus_dm * (self.period - 1) + minus_dm) / self.period

        if self._smoothed_tr <= 0.0:
            return self._null_result()

        plus_di = 100.0 * self._smoothed_plus_dm / self._smoothed_tr
        minus_di = 100.0 * self._smoothed_minus_dm / self._smoothed_tr

        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0.0 else 0.0

        # ADX: seed on first computation, Wilder smooth thereafter
        if self.adx_value is None:
            self.adx_value = dx
        else:
            self.adx_value = (self.adx_value * (self.period - 1) + dx) / self.period

        self.plus_di_value = round(plus_di, 4)
        self.minus_di_value = round(minus_di, 4)
        self.adx_value = round(self.adx_value, 4)

        direction = "BULLISH" if plus_di >= minus_di else "BEARISH"

        return {
            "adx": self.adx_value,
            "plus_di": self.plus_di_value,
            "minus_di": self.minus_di_value,
            "trend_strength": self._classify_trend(self.adx_value),
            "direction": direction,
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
        self._interval_count = 0
        self._tr_buf.clear()
        self._plus_dm_buf.clear()
        self._minus_dm_buf.clear()
        self._prev_high = None
        self._prev_low = None
        self._prev_close = None
        self._smoothed_tr = None
        self._smoothed_plus_dm = None
        self._smoothed_minus_dm = None
        self.adx_value = None
        self.plus_di_value = None
        self.minus_di_value = None

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
            "adx": self.adx_value,
            "plus_di": self.plus_di_value,
            "minus_di": self.minus_di_value,
            "smoothed_tr": self._smoothed_tr,
            "smoothed_plus_dm": self._smoothed_plus_dm,
            "smoothed_minus_dm": self._smoothed_minus_dm,
            "sufficient_data": self.adx_value is not None,
            "last_reset_date": self.last_reset_date.isoformat() if self.last_reset_date else None,
        })
        return state
