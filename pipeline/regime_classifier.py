"""
Strike Desk V4 - Market Regime Classifier (S12)

PDF status: Strike_Desk_Upgrade_Prompt_Series.pdf unavailable.
Taxonomy and thresholds are PROVISIONAL — derived from existing project indicators.

Classifies market conditions into one of five mutually exclusive labels using
rule-based logic over validated indicator outputs. The classifier is stateless
and deterministic: identical inputs always produce the same label, confidence
score, and reason text (the classified_at timestamp is metadata only).

Regime labels
-------------
TRENDING_UP   : ADX >= 25, +DI >= −DI  (strong bullish directional conviction)
TRENDING_DOWN : ADX >= 25, −DI > +DI   (strong bearish directional conviction)
RANGING       : ADX < 20, Bollinger bandwidth < VOLATILE threshold  (sideways)
VOLATILE      : ADX < 25, Bollinger bandwidth >= 0.04  (elevated volatility)
UNKNOWN       : ADX not ready (< 14 ticks) or all ADX values are None

Transition zone (20 ≤ ADX < 25): directional bias (+DI vs −DI) still applied,
so the label may be TRENDING_UP or TRENDING_DOWN at moderate ADX levels; the
confidence score will be lower because the primary ADX gate is not fully met.
VOLATILE overrides the transition zone when Bollinger bandwidth is elevated.

Confidence score
----------------
0–100 percentage of available secondary indicators that agree with the primary
ADX-derived regime. ADX alone (no secondary indicators available) yields 0.
Secondaries used: RSI, Bollinger percent_b, MACD histogram, Supertrend signal,
price vs VWAP.

CRITICAL NOTE: Confidence is NOT a probability of profit. It measures secondary
signal agreement only. Regime labels are descriptive, not predictive.

Input format
------------
The classifier reads from the flat merged indicator dict produced by
CompositeIndicatorCalculator (or any equivalent). Only unambiguous field names
are used; keys that collide across indicators ('sufficient_data', 'direction')
are deliberately not read. Per-indicator readiness is inferred from None-ness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
import logging

logger = logging.getLogger(__name__)


class RegimeLabel(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class RegimeResult:
    """
    Output of a single RegimeClassifier.classify() call.

    Fields
    ------
    label           : Classified regime.
    confidence_score: 0–100; fraction of secondary signals that confirm the label.
    sufficient_data : True iff ADX (the primary input) is ready.
    reason          : Human-readable explanation of the classification decision.
    inputs_used     : Snapshot of the relevant inputs extracted from the indicator dict.
    classified_at   : UTC timestamp of classification (non-deterministic metadata).
    """
    label: RegimeLabel
    confidence_score: float
    sufficient_data: bool
    reason: str
    inputs_used: Dict[str, Any]
    classified_at: datetime


class RegimeClassifier:
    """
    Rule-based stateless market regime classifier.

    All public methods are static/class methods — no instance state.
    """

    # ------------------------------------------------------------------ #
    # Thresholds — all documented here so they appear together            #
    # ------------------------------------------------------------------ #

    # ADX primary classification
    ADX_TREND_THRESHOLD: float = 25.0
    """ADX >= this: trending regime (UP or DOWN determined by +DI/-DI)."""

    ADX_RANGING_THRESHOLD: float = 20.0
    """ADX < this: ranging regime (no significant directional conviction)."""
    # ADX in [20, 25) is a transition zone; directional bias still applied.

    # Bollinger volatile detection
    BOLLINGER_VOLATILE_BANDWIDTH: float = 0.04
    """Bollinger bandwidth >= this flags elevated volatility (4 % of middle price).
    Takes priority over RANGING when ADX < ADX_TREND_THRESHOLD."""

    # RSI alignment thresholds
    RSI_TRENDING_UP_MIN: float = 50.0
    """RSI >= this confirms up-trend (bullish momentum)."""

    RSI_TRENDING_DOWN_MAX: float = 50.0
    """RSI < this confirms down-trend (bearish momentum)."""

    RSI_RANGING_LOW: float = 40.0
    RSI_RANGING_HIGH: float = 60.0
    """RSI in [40, 60] confirms ranging (neutral momentum)."""

    RSI_VOLATILE_EXTREME_LOW: float = 30.0
    RSI_VOLATILE_EXTREME_HIGH: float = 70.0
    """RSI outside [30, 70] confirms volatile (momentum at an extreme)."""

    # Bollinger percent_b alignment
    PERCENT_B_UP_MIN: float = 0.5
    """percent_b >= 0.5: price above midpoint, confirms up-trend."""

    PERCENT_B_DOWN_MAX: float = 0.5
    """percent_b < 0.5: price below midpoint, confirms down-trend."""

    PERCENT_B_RANGING_LOW: float = 0.3
    PERCENT_B_RANGING_HIGH: float = 0.7
    """percent_b in [0.3, 0.7]: price within mid-band, confirms ranging."""

    PERCENT_B_VOLATILE_OUTER: float = 0.2
    """percent_b outside [0.2, 0.8]: price near/beyond bands, confirms volatile."""

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    @classmethod
    def classify(
        cls,
        indicators: Dict[str, Any],
        *,
        _now: Optional[datetime] = None,
    ) -> RegimeResult:
        """
        Classify the current market regime from a flattened indicator dictionary.

        Parameters
        ----------
        indicators : Dict[str, Any]
            Flat merged indicator dictionary (output of CompositeIndicatorCalculator
            or equivalent). All keys are optional; the method degrades gracefully
            to UNKNOWN when the required ADX inputs are None.

        _now : Optional[datetime]
            Override the classification timestamp — for deterministic unit tests.
            Production callers should omit this.

        Returns
        -------
        RegimeResult
        """
        now = _now if _now is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # ---------------------------------------------------------------- #
        # Extract primary input: ADX                                        #
        # 'direction' and 'sufficient_data' collide with Supertrend/other   #
        # keys in the flat merged dict; do NOT read them here.              #
        # ---------------------------------------------------------------- #
        adx = indicators.get("adx")
        plus_di = indicators.get("plus_di")
        minus_di = indicators.get("minus_di")
        trend_strength = indicators.get("trend_strength")

        if adx is None or plus_di is None or minus_di is None:
            return RegimeResult(
                label=RegimeLabel.UNKNOWN,
                confidence_score=0.0,
                sufficient_data=False,
                reason=(
                    "ADX not ready: insufficient tick history for primary classification. "
                    f"adx={adx!r}, plus_di={plus_di!r}, minus_di={minus_di!r}."
                ),
                inputs_used={
                    "adx": adx,
                    "plus_di": plus_di,
                    "minus_di": minus_di,
                },
                classified_at=now,
            )

        try:
            adx_f = float(adx)
            plus_di_f = float(plus_di)
            minus_di_f = float(minus_di)
        except (TypeError, ValueError) as exc:
            return RegimeResult(
                label=RegimeLabel.UNKNOWN,
                confidence_score=0.0,
                sufficient_data=False,
                reason=f"ADX values not numeric: {exc}",
                inputs_used={"adx": adx, "plus_di": plus_di, "minus_di": minus_di},
                classified_at=now,
            )

        import math
        if not (math.isfinite(adx_f) and math.isfinite(plus_di_f) and math.isfinite(minus_di_f)):
            return RegimeResult(
                label=RegimeLabel.UNKNOWN,
                confidence_score=0.0,
                sufficient_data=False,
                reason=(
                    f"ADX values non-finite: adx={adx_f}, plus_di={plus_di_f}, "
                    f"minus_di={minus_di_f}."
                ),
                inputs_used={"adx": adx, "plus_di": plus_di, "minus_di": minus_di},
                classified_at=now,
            )

        # ---------------------------------------------------------------- #
        # Extract secondary inputs (all optional)                           #
        # ---------------------------------------------------------------- #
        rsi = indicators.get("rsi")
        bandwidth = indicators.get("bandwidth")
        percent_b = indicators.get("percent_b")
        histogram = indicators.get("histogram")
        supertrend_signal = indicators.get("signal")  # "BUY"/"SELL" from Supertrend
        ltp = indicators.get("ltp")
        vwap = indicators.get("vwap")

        inputs_used: Dict[str, Any] = {
            "adx": adx_f,
            "plus_di": plus_di_f,
            "minus_di": minus_di_f,
            "trend_strength": trend_strength,
            "rsi": rsi,
            "bandwidth": bandwidth,
            "percent_b": percent_b,
            "histogram": histogram,
            "supertrend_signal": supertrend_signal,
            "ltp": ltp,
            "vwap": vwap,
        }

        # ---------------------------------------------------------------- #
        # Volatile flag: Bollinger bandwidth elevated                       #
        # ---------------------------------------------------------------- #
        volatile_flag = (
            bandwidth is not None
            and _safe_numeric(bandwidth) is not None
            and _safe_numeric(bandwidth) >= cls.BOLLINGER_VOLATILE_BANDWIDTH
        )

        # ---------------------------------------------------------------- #
        # Primary classification from ADX                                   #
        # ---------------------------------------------------------------- #
        if adx_f >= cls.ADX_TREND_THRESHOLD:
            # Strong directional conviction — volatile override does not apply
            # when ADX confirms a trend.
            if plus_di_f >= minus_di_f:
                primary_label = RegimeLabel.TRENDING_UP
            else:
                primary_label = RegimeLabel.TRENDING_DOWN

        elif adx_f < cls.ADX_RANGING_THRESHOLD:
            # Low directional conviction
            if volatile_flag:
                primary_label = RegimeLabel.VOLATILE
            else:
                primary_label = RegimeLabel.RANGING

        else:
            # Transition zone: 20 <= ADX < 25
            if volatile_flag:
                primary_label = RegimeLabel.VOLATILE
            elif plus_di_f >= minus_di_f:
                primary_label = RegimeLabel.TRENDING_UP
            else:
                primary_label = RegimeLabel.TRENDING_DOWN

        # ---------------------------------------------------------------- #
        # Confidence: secondary signal agreement rate                       #
        # ---------------------------------------------------------------- #
        confirms = 0
        available = 0

        # RSI
        rsi_f = _safe_numeric(rsi)
        if rsi_f is not None:
            available += 1
            if primary_label == RegimeLabel.TRENDING_UP and rsi_f >= cls.RSI_TRENDING_UP_MIN:
                confirms += 1
            elif primary_label == RegimeLabel.TRENDING_DOWN and rsi_f < cls.RSI_TRENDING_DOWN_MAX:
                confirms += 1
            elif (
                primary_label == RegimeLabel.RANGING
                and cls.RSI_RANGING_LOW <= rsi_f <= cls.RSI_RANGING_HIGH
            ):
                confirms += 1
            elif primary_label == RegimeLabel.VOLATILE and (
                rsi_f < cls.RSI_VOLATILE_EXTREME_LOW
                or rsi_f > cls.RSI_VOLATILE_EXTREME_HIGH
            ):
                confirms += 1

        # Bollinger percent_b
        pb_f = _safe_numeric(percent_b)
        if pb_f is not None:
            available += 1
            if primary_label == RegimeLabel.TRENDING_UP and pb_f >= cls.PERCENT_B_UP_MIN:
                confirms += 1
            elif primary_label == RegimeLabel.TRENDING_DOWN and pb_f < cls.PERCENT_B_DOWN_MAX:
                confirms += 1
            elif (
                primary_label == RegimeLabel.RANGING
                and cls.PERCENT_B_RANGING_LOW <= pb_f <= cls.PERCENT_B_RANGING_HIGH
            ):
                confirms += 1
            elif primary_label == RegimeLabel.VOLATILE and (
                pb_f < cls.PERCENT_B_VOLATILE_OUTER
                or pb_f > (1.0 - cls.PERCENT_B_VOLATILE_OUTER)
            ):
                confirms += 1

        # MACD histogram (sign-based; trending only — no reliable zero threshold for RANGING)
        hist_f = _safe_numeric(histogram)
        if hist_f is not None:
            if primary_label in (RegimeLabel.TRENDING_UP, RegimeLabel.TRENDING_DOWN):
                available += 1
                if primary_label == RegimeLabel.TRENDING_UP and hist_f > 0.0:
                    confirms += 1
                elif primary_label == RegimeLabel.TRENDING_DOWN and hist_f < 0.0:
                    confirms += 1

        # Supertrend signal ("BUY" = up-trend, "SELL" = down-trend)
        if supertrend_signal is not None:
            if primary_label in (RegimeLabel.TRENDING_UP, RegimeLabel.TRENDING_DOWN):
                available += 1
                st = str(supertrend_signal).upper().strip()
                if primary_label == RegimeLabel.TRENDING_UP and st == "BUY":
                    confirms += 1
                elif primary_label == RegimeLabel.TRENDING_DOWN and st == "SELL":
                    confirms += 1

        # Price vs VWAP
        ltp_f = _safe_numeric(ltp)
        vwap_f = _safe_numeric(vwap)
        if ltp_f is not None and vwap_f is not None and vwap_f > 0.0:
            if primary_label in (RegimeLabel.TRENDING_UP, RegimeLabel.TRENDING_DOWN):
                available += 1
                price_vs_vwap = ltp_f - vwap_f
                if primary_label == RegimeLabel.TRENDING_UP and price_vs_vwap > 0.0:
                    confirms += 1
                elif primary_label == RegimeLabel.TRENDING_DOWN and price_vs_vwap < 0.0:
                    confirms += 1

        confidence_score = (
            round((confirms / available) * 100.0, 1) if available > 0 else 0.0
        )

        # ---------------------------------------------------------------- #
        # Build reason string                                               #
        # ---------------------------------------------------------------- #
        parts = [
            f"ADX={adx_f:.2f}",
            f"+DI={plus_di_f:.2f}",
            f"-DI={minus_di_f:.2f}",
        ]
        if trend_strength:
            parts.append(f"trend_strength={trend_strength}")
        if rsi_f is not None:
            parts.append(f"RSI={rsi_f:.2f}")
        if bandwidth is not None:
            bw_f = _safe_numeric(bandwidth)
            if bw_f is not None:
                parts.append(f"BB_bandwidth={bw_f:.4f}")
        if volatile_flag:
            parts.append(f"volatile_flag=True(bw>={cls.BOLLINGER_VOLATILE_BANDWIDTH})")
        parts.append(f"secondary_confirms={confirms}/{available}")
        reason = (
            f"Regime={primary_label.value}; "
            + ", ".join(parts)
            + f"; confidence={confidence_score}%"
        )

        logger.debug(
            "[RegimeClassifier] %s | adx=%.2f | +DI=%.2f | -DI=%.2f | "
            "confidence=%.1f%% | secondary=%d/%d",
            primary_label.value,
            adx_f,
            plus_di_f,
            minus_di_f,
            confidence_score,
            confirms,
            available,
        )

        return RegimeResult(
            label=primary_label,
            confidence_score=confidence_score,
            sufficient_data=True,
            reason=reason,
            inputs_used=inputs_used,
            classified_at=now,
        )


# ------------------------------------------------------------------ #
# Module-level helpers                                                 #
# ------------------------------------------------------------------ #

def _safe_numeric(value: Any) -> Optional[float]:
    """Return float(value) when finite and convertible; None otherwise."""
    if value is None:
        return None
    try:
        import math as _math
        f = float(value)
        return f if _math.isfinite(f) else None
    except (TypeError, ValueError):
        return None
