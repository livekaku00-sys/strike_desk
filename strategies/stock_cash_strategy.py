"""
Strike Desk V4 - Stock Cash Strategy
Asset Class: STOCK_CASH (Equity cash market)
Setup Type: Momentum + Mean Reversion / EMA9 Crossover

Criteria Weights (Sum = 100):
- ema9_crossover: 30.0 (Price crosses above/below EMA9)
- vwap_alignment: 25.0 (Price on correct side of VWAP)
- volume_surge: 20.0 (Volume > 1.5x average volume)
- pcr_alignment: 15.0 (PCR slope aligns with trade direction)
- sentiment_alignment: 10.0 (Composite sentiment aligns)

A+ Setup Definition:
- Confidence score >= 85.0
- Must include ema9_crossover = True
- Must meet at least 4 of 5 criteria
"""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional

from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal, SignalType
from strategies.base_strategy import BaseStrategy
from strategies.confidence import confidence_scorer

logger = logging.getLogger(__name__)


class StockCashStrategy(BaseStrategy):
    """
    Dedicated strategy for the Equity Cash market.
    Evaluates EMA9 crossover momentum supported by VWAP, volume surge, PCR, and sentiment.
    """

    CRITERIA_WEIGHTS: Dict[str, float] = {
        "ema9_crossover": 30.0,
        "vwap_alignment": 25.0,
        "volume_surge": 20.0,
        "pcr_alignment": 15.0,
        "sentiment_alignment": 10.0,
    }

    def __init__(
        self,
        name: str = "StockCashStrategy",
        signal_expiry_seconds: int = 300,
        duplicate_window_seconds: int = 60,
    ) -> None:
        super().__init__(
            name=name,
            asset_class=AssetClass.STOCK_CASH,
            criteria_weights=dict(self.CRITERIA_WEIGHTS),
            signal_expiry_seconds=signal_expiry_seconds,
            duplicate_window_seconds=duplicate_window_seconds,
        )

    def _determine_direction(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Optional[Direction]:
        """
        Determines the intended trade direction (LONG / SHORT).
        Precedence:
        1. Explicit direction passed in market_state or indicators.
        2. Inferred from crossover indicator or price vs EMA9.
        """
        raw_dir = market_state.get("direction") or indicators.get("direction")
        if raw_dir is not None:
            if isinstance(raw_dir, Direction):
                return raw_dir
            d_str = str(raw_dir).strip().upper()
            if d_str in ("LONG", "BUY", "BULLISH"):
                return Direction.LONG
            if d_str in ("SHORT", "SELL", "BEARISH"):
                return Direction.SHORT

        if indicators.get("ema9_crossover_bullish") or indicators.get("bullish"):
            return Direction.LONG
        if indicators.get("ema9_crossover_bearish") or indicators.get("bearish"):
            return Direction.SHORT

        price = indicators.get("price") or indicators.get("ltp")
        ema9 = indicators.get("ema9") or indicators.get("ema")
        if price is not None and ema9 is not None:
            price_val = float(price)
            ema_val = float(ema9)
            if price_val > ema_val:
                return Direction.LONG
            elif price_val < ema_val:
                return Direction.SHORT

        return None

    def get_criteria_checklist(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Dict[str, bool]:
        """
        Evaluates the 5 discrete measurable criteria for Stock Cash.
        """
        direction = self._determine_direction(indicators, market_state) or Direction.LONG
        is_long = direction == Direction.LONG

        price = indicators.get("price") if indicators.get("price") is not None else indicators.get("ltp")
        ema9 = indicators.get("ema9") if indicators.get("ema9") is not None else indicators.get("ema")
        prev_price = indicators.get("prev_price")
        prev_ema9 = indicators.get("prev_ema9")

        # 1. EMA9 Crossover (30.0)
        ema9_cross = False
        if "ema9_crossover" in indicators:
            ema9_cross = bool(indicators["ema9_crossover"])
        elif "ema_crossover" in indicators:
            ema9_cross = bool(indicators["ema_crossover"])
        elif "ema_cross" in indicators:
            ema9_cross = bool(indicators["ema_cross"])
        elif price is not None and ema9 is not None:
            p = float(price)
            e = float(ema9)
            if prev_price is not None and prev_ema9 is not None:
                pp = float(prev_price)
                pe = float(prev_ema9)
                if is_long:
                    ema9_cross = pp <= pe and p > e
                else:
                    ema9_cross = pp >= pe and p < e
            else:
                ema9_cross = (p > e) if is_long else (p < e)

        # 2. VWAP Alignment (25.0)
        vwap_align = False
        vwap = indicators.get("vwap")
        if "vwap_alignment" in indicators:
            vwap_align = bool(indicators["vwap_alignment"])
        elif price is not None and vwap is not None and float(vwap) > 0.0:
            p = float(price)
            v = float(vwap)
            vwap_align = (p > v) if is_long else (p < v)

        # 3. Volume Surge (20.0)
        vol_surge = False
        volume = indicators.get("volume")
        avg_volume = indicators.get("avg_volume") or indicators.get("volume_sma")
        if "volume_surge" in indicators:
            vol_surge = bool(indicators["volume_surge"])
        elif volume is not None and avg_volume is not None:
            vol = float(volume)
            avg_vol = float(avg_volume)
            if avg_vol > 0.0:
                vol_surge = vol > (1.5 * avg_vol)
            else:
                vol_surge = False

        # 4. PCR Alignment (15.0)
        pcr_align = False
        pcr_slope = indicators.get("pcr_slope")
        if "pcr_alignment" in indicators:
            pcr_align = bool(indicators["pcr_alignment"])
        elif pcr_slope is not None:
            slope = float(pcr_slope)
            pcr_align = (slope < 0.0) if is_long else (slope > 0.0)

        # 5. Sentiment Alignment (10.0)
        sent_align = False
        sentiment = indicators.get("sentiment_score") if indicators.get("sentiment_score") is not None else indicators.get("sentiment")
        if "sentiment_alignment" in indicators:
            sent_align = bool(indicators["sentiment_alignment"])
        elif sentiment is not None:
            s_val = float(sentiment)
            sent_align = (s_val > 0.0) if is_long else (s_val < 0.0)

        return {
            "ema9_crossover": ema9_cross,
            "vwap_alignment": vwap_align,
            "volume_surge": vol_surge,
            "pcr_alignment": pcr_align,
            "sentiment_alignment": sent_align,
        }

    def evaluate(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Optional[Signal]:
        """
        Evaluates indicator payload and returns Signal if actionable (score >= 55.0).
        """
        price = indicators.get("price") if indicators.get("price") is not None else indicators.get("ltp")
        ema9 = indicators.get("ema9") if indicators.get("ema9") is not None else indicators.get("ema")
        if price is None or ema9 is None:
            logger.debug("[%s] Insufficient data: price or ema9 missing.", self.name)
            return None

        direction = self._determine_direction(indicators, market_state)
        if direction is None:
            logger.debug("[%s] Unable to determine direction from indicators.", self.name)
            return None

        checklist = self.get_criteria_checklist(indicators, market_state)
        score = confidence_scorer.calculate_score(checklist, self.criteria_weights)

        is_long_dir = (direction == Direction.LONG)
        ml = indicators.get("macd_line")
        sl_macd = indicators.get("signal_line")
        macd_aligns = (ml is not None and sl_macd is not None and
                       (float(ml) > float(sl_macd) if is_long_dir else float(ml) < float(sl_macd)))
        st_dir = indicators.get("supertrend_direction")
        st_aligns = (st_dir is not None and
                     (st_dir == "UP" if is_long_dir else st_dir == "DOWN"))
        rsi_val = indicators.get("rsi")
        rsi_aligns = (rsi_val is not None and
                      (float(rsi_val) < 30.0 if is_long_dir else float(rsi_val) > 70.0))
        bb_pos = indicators.get("bb_position")
        bb_aligns = (bb_pos is not None and
                     (bb_pos in ("NEAR_LOWER", "BELOW_LOWER") if is_long_dir
                      else bb_pos in ("NEAR_UPPER", "ABOVE_UPPER")))

        # Separate +5 bonus for each aligning indicator
        macd_cross = indicators.get("macd_crossover")
        if macd_cross == ("BULLISH_CROSS" if is_long_dir else "BEARISH_CROSS"):
            score = min(100.0, score + 5.0)
        if st_aligns:
            score = min(100.0, score + 5.0)
        if rsi_aligns:
            score = min(100.0, score + 5.0)
        if bb_aligns:
            score = min(100.0, score + 5.0)

        raw_tier = confidence_scorer.get_tier(score)

        if score < 55.0 or raw_tier == ConfidenceTier.C:
            logger.debug("[%s] Setup score %.1f is below actionable threshold (55.0).", self.name, score)
            return None

        # A+ Setup Calibration: score >= 85.0, ema9_crossover = True, >= 4 criteria met
        if score >= 85.0:
            if checklist.get("ema9_crossover", False) and sum(checklist.values()) >= 4:
                final_tier = ConfidenceTier.A_PLUS
            else:
                final_tier = ConfidenceTier.A
        else:
            final_tier = raw_tier

        entry_price = float(price)
        target = entry_price * (1.015 if direction == Direction.LONG else 0.985)
        stop_loss = entry_price * (0.9925 if direction == Direction.LONG else 1.0075)

        sig_type = (
            SignalType.EMA9_CROSSOVER_BULLISH
            if direction == Direction.LONG
            else SignalType.EMA9_CROSSOVER_BEARISH
        )

        symbol = str(indicators.get("symbol") or market_state.get("symbol") or "UNKNOWN").strip().upper()
        ts = market_state.get("timestamp") or indicators.get("timestamp") or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        signal_checklist = [k for k, v in checklist.items() if v]
        if macd_aligns:
            signal_checklist.append("macd_crossover")
        if st_aligns:
            signal_checklist.append("supertrend_direction")
        if rsi_aligns:
            signal_checklist.append("rsi")
        if bb_aligns:
            signal_checklist.append("bollinger")

        adx_val = indicators.get("adx")
        indicators_triggered = {
            "ema9": checklist.get("ema9_crossover", False),
            "vwap": checklist.get("vwap_alignment", False),
            "pcr_slope": checklist.get("pcr_alignment", False),
            "sentiment": checklist.get("sentiment_alignment", False),
            "volume_surge": checklist.get("volume_surge", False),
            "rsi": rsi_aligns,
            "adx": adx_val is not None and float(adx_val) >= 25.0,
            "macd": macd_aligns,
            "bollinger": bb_aligns,
            "supertrend": st_aligns,
        }

        sig = Signal(
            symbol=symbol,
            exchange=Exchange.NSE,
            asset_class=self.asset_class,
            signal_type=sig_type,
            direction=direction,
            entry_price=entry_price,
            target_price=round(target, 2),
            stop_loss_price=round(stop_loss, 2),
            confidence_score=score,
            confidence_tier=final_tier,
            checklist=signal_checklist,
            strategy_name=self.name,
            timestamp=ts,
            metadata={
                "criteria_met_count": sum(checklist.values()),
                "total_criteria": len(checklist),
                "is_a_plus": final_tier == ConfidenceTier.A_PLUS,
            },
            indicators_triggered=indicators_triggered,
        )

        if self._store_signal(sig):
            return sig

        return None