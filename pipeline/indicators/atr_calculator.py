"""
Strike Desk V4 - Average True Range (ATR) Calculator
Period: 14, Wilder smoothing (alpha = 1/n).
True Range = max(high-low, |high-prev_close|, |low-prev_close|).
Output includes atr_sl_distance = ATR × 1.5 for dynamic stop-loss sizing.
Reset at market open 09:15 IST. Degraded mode: None if < 14 ticks.
"""
from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = dtime(9, 15)

# Dynamic SL multiplier: ATR × SL_MULTIPLIER = stop loss distance
SL_MULTIPLIER: float = 1.5


class ATRCalculator(BaseIndicatorCalculator):
    """
    Wilder-smoothed ATR with volatility classification and dynamic SL sizing.
    Uses self-tracked _interval_count so it works whether called via process_tick
    or directly from SymbolIndicatorBundle.calculate_indicators.
    """

    LOW_THRESHOLD: float = 0.5       # ATR% < 0.5  → LOW
    NORMAL_THRESHOLD: float = 1.5    # ATR% < 1.5  → NORMAL
    HIGH_THRESHOLD: float = 3.0      # ATR% < 3.0  → HIGH; ≥ 3.0 → EXTREME

    def __init__(self, symbol: str = "", period: int = 14, **kwargs: Any) -> None:
        actual_symbol = symbol or kwargs.get("symbol", "")
        actual_period = kwargs.get("period", period)
        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.period: int = int(actual_period)
        if self.period <= 0:
            raise ValueError(f"ATR period must be positive, got {self.period}")

        # Self-tracked interval counter (see ADXCalculator for rationale)
        self._interval_count: int = 0

        # Seed-phase buffer
        self._tr_buf: List[float] = []

        # Previous close for TR calculation
        self._prev_close: Optional[float] = None

        # Wilder smoothed TR
        self._smoothed_tr: Optional[float] = None

        # Output values
        self.atr_value: Optional[float] = None

        self.last_reset_date: Optional[date] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_volatility(self, atr_pct: float) -> str:
        if atr_pct < self.LOW_THRESHOLD:
            return "LOW"
        if atr_pct < self.NORMAL_THRESHOLD:
            return "NORMAL"
        if atr_pct < self.HIGH_THRESHOLD:
            return "HIGH"
        return "EXTREME"

    def _null_result(self) -> Dict[str, Any]:
        return {
            "atr": None,
            "atr_percent": None,
            "atr_sl_distance": None,
            "volatility": None,
            "sufficient_data": False,
        }

    # ------------------------------------------------------------------
    # BaseIndicatorCalculator interface
    # ------------------------------------------------------------------

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        h = tick.high if tick.high > 0.0 else tick.ltp
        l = tick.low if tick.low > 0.0 else tick.ltp
        c = float(tick.ltp)

        # First tick: seed previous close, no TR to compute
        if self._prev_close is None:
            self._prev_close = c
            return self._null_result()

        # True Range
        tr = max(h - l, abs(h - self._prev_close), abs(l - self._prev_close))

        self._interval_count += 1
        self._prev_close = c

        # Degraded: need period - 1 intervals before first output
        # (tick 1 seeds prev; tick 14 gives the 13th interval → first output)
        if self._interval_count < self.period - 1:
            self._tr_buf.append(tr)
            return self._null_result()

        # Seed phase: on first qualifying interval, build SMA seed from buffer
        if self._smoothed_tr is None:
            self._tr_buf.append(tr)
            n = len(self._tr_buf)
            self._smoothed_tr = sum(self._tr_buf) / n
            self._tr_buf.clear()
        else:
            # Wilder smoothing: prev * (n-1)/n + current * 1/n
            self._smoothed_tr = (self._smoothed_tr * (self.period - 1) + tr) / self.period

        self.atr_value = round(self._smoothed_tr, 4)
        atr_pct = round((self.atr_value / c) * 100.0, 4) if c > 0.0 else 0.0
        atr_sl_distance = round(self.atr_value * SL_MULTIPLIER, 4)

        return {
            "atr": self.atr_value,
            "atr_percent": atr_pct,
            "atr_sl_distance": atr_sl_distance,
            "volatility": self._classify_volatility(atr_pct),
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
        self._prev_close = None
        self._smoothed_tr = None
        self.atr_value = None

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
            "atr": self.atr_value,
            "smoothed_tr": self._smoothed_tr,
            "prev_close": self._prev_close,
            "sufficient_data": self.atr_value is not None,
            "last_reset_date": self.last_reset_date.isoformat() if self.last_reset_date else None,
        })
        return state
