"""
Strike Desk V4 - Composite Market Sentiment Scoring Calculator
Combines:
  1. PCR Slope (weight: 0.4) - rate of change of Put-Call Ratio
  2. VWAP Deviation (weight: 0.3) - (LTP - VWAP) / VWAP
  3. EMA9 Trend (weight: 0.2) - (LTP - EMA9) / EMA9
  4. OI Change (weight: 0.1) - % change in total Open Interest

Score Range: -100 (Extremely Bearish) to +100 (Extremely Bullish)
Confidence: (available_components / total_components) * 100%
"""
from typing import Any, Dict, Optional

from models.tick import Tick
from pipeline.indicator_calculator import BaseIndicatorCalculator


class SentimentCalculator(BaseIndicatorCalculator):
    """
    Computes a deterministic, reproducible multi-factor market sentiment score.
    Supports degraded mode with dynamic weight re-normalization when inputs are partially missing.
    """

    DEFAULT_WEIGHTS: Dict[str, float] = {
        "pcr_slope": 0.4,
        "vwap_deviation": 0.3,
        "ema_trend": 0.2,
        "oi_change": 0.1,
    }

    def __init__(
        self,
        symbol_or_weights: Any = "",
        weights: Optional[Dict[str, float]] = None,
        renormalize_weights: bool = True,
        **kwargs: Any,
    ) -> None:
        if isinstance(symbol_or_weights, dict):
            actual_weights = symbol_or_weights
            actual_symbol = kwargs.get("symbol", "")
        else:
            actual_symbol = symbol_or_weights or kwargs.get("symbol", "")
            actual_weights = weights or kwargs.get("weights", self.DEFAULT_WEIGHTS)

        super().__init__(str(actual_symbol).strip().upper() if actual_symbol else "")
        self.weights: Dict[str, float] = dict(actual_weights)
        self.renormalize_weights = renormalize_weights

        self.last_sentiment: float = 0.0
        self.last_confidence: float = 0.0
        self.component_scores: Dict[str, float] = {}

    def calculate_sentiment(
        self,
        pcr_slope: Optional[float] = None,
        vwap_deviation: Optional[float] = None,
        ema_trend: Optional[float] = None,
        oi_change_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Calculates composite sentiment score across available components.
        Parameters:
            pcr_slope: Rate of change of PCR (per min). Clamped [-2.0, +2.0].
            vwap_deviation: (Price - VWAP) / VWAP. Clamped [-5%, +5%].
            ema_trend: (Price - EMA9) / EMA9. Clamped [-3%, +3%].
            oi_change_pct: % change in Open Interest. Clamped [-20%, +20%].
        Returns:
            {"sentiment_score": float, "components": Dict[str, float], "confidence": float}
        """
        raw_components = {
            "pcr_slope": pcr_slope,
            "vwap_deviation": vwap_deviation,
            "ema_trend": ema_trend,
            "oi_change": oi_change_pct,
        }

        # Filter available components
        available = {k: v for k, v in raw_components.items() if v is not None}
        total_factors = len(self.weights)

        if not available:
            self.last_sentiment = 0.0
            self.last_confidence = 0.0
            self.component_scores.clear()
            res = {
                "sentiment_score": 0.0,
                "components": {},
                "confidence": 0.0,
            }
            self.indicator_values.update(res)
            return res

        scores: Dict[str, float] = {}

        # 1. PCR Slope normalization: [-2.0, +2.0] -> [-100, +100]
        # Inverted: positive PCR slope = bearish (negative score), negative slope = bullish (positive score)
        if "pcr_slope" in available:
            slope = float(available["pcr_slope"])
            clamped_slope = max(-2.0, min(2.0, slope))
            scores["pcr_slope"] = - (clamped_slope / 2.0) * 100.0

        # 2. VWAP Deviation normalization: [-5%, +5%] -> [-100, +100]
        if "vwap_deviation" in available:
            dev = float(available["vwap_deviation"])
            if abs(dev) > 0.5:
                dev = dev / 100.0
            clamped_dev = max(-0.05, min(0.05, dev))
            scores["vwap_deviation"] = (clamped_dev / 0.05) * 100.0

        # 3. EMA Trend normalization: [-3%, +3%] -> [-100, +100]
        if "ema_trend" in available:
            trend = float(available["ema_trend"])
            if abs(trend) > 0.3:
                trend = trend / 100.0
            clamped_trend = max(-0.03, min(0.03, trend))
            scores["ema_trend"] = (clamped_trend / 0.03) * 100.0

        # 4. OI Change normalization: [-20%, +20%] -> [-100, +100]
        if "oi_change" in available:
            oi = float(available["oi_change"])
            if abs(oi) > 1.0:
                oi = oi / 100.0
            clamped_oi = max(-0.20, min(0.20, oi))
            scores["oi_change"] = (clamped_oi / 0.20) * 100.0

        self.component_scores = scores

        # Calculate weighted sentiment
        if self.renormalize_weights:
            sum_weights = sum(self.weights.get(k, 0.0) for k in scores)
            if sum_weights > 0:
                sentiment = sum(self.weights.get(k, 0.0) * scores[k] for k in scores) / sum_weights
            else:
                sentiment = 0.0
        else:
            sentiment = sum(self.weights.get(k, 0.0) * scores[k] for k in scores)

        # Ensure bounds [-100.0, +100.0]
        sentiment = max(-100.0, min(100.0, round(sentiment, 4)))
        confidence = round((len(scores) / float(total_factors)) * 100.0, 2)

        self.last_sentiment = sentiment
        self.last_confidence = confidence

        result = {
            "sentiment_score": sentiment,
            "components": dict(scores),
            "confidence": confidence,
        }
        self.indicator_values.update(result)
        return result

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        """
        Inherited from BaseIndicatorCalculator. Returns latest sentiment snapshot.
        """
        return {
            "sentiment_score": self.last_sentiment,
            "components": dict(self.component_scores),
            "confidence": self.last_confidence,
        }

    def reset(self) -> None:
        """Resets sentiment calculator state."""
        super().reset()
        self.last_sentiment = 0.0
        self.last_confidence = 0.0
        self.component_scores.clear()

    def get_state(self) -> Dict[str, Any]:
        """Returns internal state snapshot for telemetry and persistence."""
        state = super().get_state()
        state.update({
            "weights": dict(self.weights),
            "last_sentiment": self.last_sentiment,
            "sentiment_score": self.last_sentiment,
            "components": dict(self.component_scores),
            "confidence": self.last_confidence,
        })
        return state