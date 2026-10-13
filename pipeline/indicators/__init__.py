"""
Strike Desk V4 - Pipeline Indicators Package
Provides real-time mathematical indicators:
- VWAP (Volume Weighted Average Price)
- EMA (Exponential Moving Average)
- PCR Slope & PCR Staleness Monitoring
- Composite Market Sentiment Scoring
- RSI (Relative Strength Index, Wilder smoothing)
- ADX (Average Directional Index, +DI / -DI)
- Bollinger Bands (SMA-20, 2σ population std dev)
- ATR (Average True Range, Wilder smoothing)
- MACD (Fast 12 / Slow 26 / Signal 9, standard EMA)
- Supertrend (Period 10, Multiplier 3.0)
- Composite Multiplexers
"""
from typing import Any, Dict, List

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator
from .vwap_calculator import VWAPCalculator
from .ema_calculator import EMACalculator
from .pcr_staleness_monitor import PCRStalenessMonitor, pcr_staleness_monitor
from .pcr_slope_calculator import PCRSlopeCalculator
from .sentiment_calculator import SentimentCalculator
from .rsi_calculator import RSICalculator
from .adx_calculator import ADXCalculator
from .bollinger_calculator import BollingerCalculator
from .atr_calculator import ATRCalculator
from .macd_calculator import MACDCalculator
from .supertrend_calculator import SupertrendCalculator


class CompositeIndicatorCalculator(BaseIndicatorCalculator):
    """
    Multiplexes multiple indicators (e.g., VWAP, EMA9, PCR Slope, Sentiment) under a single symbol.
    Dispatches incoming ticks across all registered sub-calculators.
    """

    def __init__(self, symbol: str, calculators: List[BaseIndicatorCalculator]) -> None:
        super().__init__(str(symbol).strip().upper() if symbol else "")
        self.calculators = calculators

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for c in self.calculators:
            res = c.process_tick(tick)
            merged.update(res)
        return merged

    def reset(self) -> None:
        super().reset()
        for c in self.calculators:
            c.reset()

    def get_state(self) -> Dict[str, Any]:
        state = super().get_state()
        state["calculators"] = [c.get_state() for c in self.calculators]
        return state


__all__ = [
    "VWAPCalculator",
    "EMACalculator",
    "PCRStalenessMonitor",
    "pcr_staleness_monitor",
    "PCRSlopeCalculator",
    "SentimentCalculator",
    "RSICalculator",
    "ADXCalculator",
    "BollingerCalculator",
    "ATRCalculator",
    "MACDCalculator",
    "SupertrendCalculator",
    "CompositeIndicatorCalculator",
]
