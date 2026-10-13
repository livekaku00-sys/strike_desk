"""
Strike Desk V4 - Stock Options Strategy
Asset Class: STOCK_OPTIONS (Individual equity stock options)
Setup Type: Underlying Momentum (EMA9 + VWAP) + Option Flow & Liquidity Confirmation

Criteria Weights (Sum = 100):
- ema9_crossover: 30.0 (Underlying stock momentum - primary driver)
- pcr_slope: 20.0 (Derivative sentiment - less weighted due to lower liquidity)
- vwap_alignment: 20.0 (Underlying stock price vs intraday VWAP)
- sentiment_alignment: 15.0 (Composite sentiment score)
- volume_surge: 15.0 (Option volume > average confirming participation)

A+ Setup Definition:
- Confidence score >= 85.0
- Must include ema9_crossover = True
- Must include pcr_slope = True
- Must meet at least 4 of 5 criteria

Risk & Compliance Controls:
- Liquidity gate: Option contracts with volume < 1,000 contracts are rejected as illiquid.
- PCR staleness monitoring (Review Risk 2): If PCR feed > 30s stale, return None immediately.
"""
from datetime import date, datetime, timedelta, timezone
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from models.instrument import AssetClass, Exchange
from models.signal import ConfidenceTier, Direction, Signal, SignalType
from pipeline.indicators.pcr_staleness_monitor import (
    PCRStalenessMonitor,
    pcr_staleness_monitor,
)
from strategies.base_strategy import BaseStrategy
from strategies.confidence import confidence_scorer
from utils.option_contract import resolve_option_contract

logger = logging.getLogger(__name__)

# Suppress repeated "no live contract" warnings to at most once per 5 min per symbol.
_no_contract_warned: Dict[str, float] = {}
_NO_CONTRACT_WARN_INTERVAL = 300.0


class StockOptionsStrategy(BaseStrategy):
    """
    Dedicated strategy for Individual Equity Stock Options.
    Prioritizes underlying stock momentum (EMA9 & VWAP) due to stock options liquidity profiles.
    Enforces minimum contract volume (>= 1,000 contracts) and PCR staleness gating.
    """

    CRITERIA_WEIGHTS: Dict[str, float] = {
        "ema9_crossover": 30.0,
        "pcr_slope": 20.0,
        "vwap_alignment": 20.0,
        "sentiment_alignment": 15.0,
        "volume_surge": 15.0,
    }

    MIN_LIQUIDITY_VOLUME: float = 1000.0

    # Equity option chains share one throttled broker endpoint with the indices, which
    # are refreshed first, so a given equity is only re-polled every few minutes. The
    # index-grade 30s gate would leave stock options switched off almost permanently.
    STALE_THRESHOLD_SECONDS: float = 900.0

    def __init__(
        self,
        name: str = "StockOptionsStrategy",
        signal_expiry_seconds: int = 300,
        duplicate_window_seconds: int = 60,
        pcr_monitor: Optional[PCRStalenessMonitor] = None,
        stale_threshold_seconds: float = STALE_THRESHOLD_SECONDS,
        min_liquidity_volume: float = MIN_LIQUIDITY_VOLUME,
    ) -> None:
        super().__init__(
            name=name,
            asset_class=AssetClass.STOCK_OPTIONS,
            criteria_weights=dict(self.CRITERIA_WEIGHTS),
            signal_expiry_seconds=signal_expiry_seconds,
            duplicate_window_seconds=duplicate_window_seconds,
        )
        self.pcr_monitor = pcr_monitor or pcr_staleness_monitor
        self.stale_threshold_seconds = stale_threshold_seconds
        self.min_liquidity_volume = min_liquidity_volume

    def record_pcr_update(self, underlying: str, timestamp: datetime) -> None:
        """Helper to record a fresh PCR update in the monitor."""
        self.pcr_monitor.record_update(underlying, timestamp)

    def check_pcr_staleness(
        self, underlying: str, current_time: Optional[datetime] = None
    ) -> bool:
        """
        Returns True if PCR is fresh (< 30s), False if stale (> 30s).
        """
        if not underlying:
            return False
        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        sym = underlying.strip().upper()
        last_update = self.pcr_monitor._last_updates.get(sym)
        if last_update is None:
            return True

        staleness = (now - last_update).total_seconds()
        return staleness <= self.stale_threshold_seconds

    def select_strike(
        self,
        underlying_price: float,
        available_strikes: List[float],
        direction: Optional[Direction] = None,
    ) -> float:
        """
        Returns ATM or slightly ITM strike (delta > 0.5) for better execution.
        """
        if not available_strikes:
            return float(underlying_price)

        strikes = sorted([float(s) for s in available_strikes])
        atm_strike = min(strikes, key=lambda s: abs(s - float(underlying_price)))

        if direction == Direction.LONG:
            # Slightly ITM for call option: strike <= underlying_price
            itm_candidates = [s for s in strikes if s <= float(underlying_price)]
            if itm_candidates:
                return itm_candidates[-1]
            return atm_strike
        elif direction == Direction.SHORT:
            # Slightly ITM for put option: strike >= underlying_price
            itm_candidates = [s for s in strikes if s >= float(underlying_price)]
            if itm_candidates:
                return itm_candidates[0]
            return atm_strike

        return atm_strike

    def select_expiry(
        self,
        available_expiries: List[Union[date, datetime, str]],
        reference_date: Optional[date] = None,
    ) -> Optional[date]:
        """
        Prefers monthly expiries (more liquid than weekly for individual stock options).
        """
        if not available_expiries:
            return None

        ref = reference_date or date.today()
        parsed: List[date] = []
        for item in available_expiries:
            if isinstance(item, str):
                parsed.append(date.fromisoformat(item))
            elif isinstance(item, datetime):
                parsed.append(item.date())
            elif isinstance(item, date):
                parsed.append(item)

        future = sorted([d for d in parsed if d >= ref])
        if not future:
            return sorted(parsed)[-1] if parsed else None

        if len(future) == 1:
            return future[0]

        # Prefer monthly candidates (> 7 days away from reference date)
        longer_term = [d for d in future if (d - ref).days > 7]
        if longer_term:
            return longer_term[0]

        return future[-1] if len(future) > 1 else future[0]

    def _determine_direction(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Optional[Direction]:
        """
        Determines intended trade direction (LONG / SHORT).
        In stock options, underlying price momentum (EMA9 crossover / price vs EMA9)
        takes precedence over PCR slope.
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
        prev_price = indicators.get("prev_price")
        prev_ema9 = indicators.get("prev_ema9")

        # 1. Check explicit crossover
        if price is not None and ema9 is not None and prev_price is not None and prev_ema9 is not None:
            p, e, pp, pe = float(price), float(ema9), float(prev_price), float(prev_ema9)
            if pp <= pe and p > e:
                return Direction.LONG
            elif pp >= pe and p < e:
                return Direction.SHORT

        # 2. Check price vs EMA9
        if price is not None and ema9 is not None:
            if float(price) > float(ema9):
                return Direction.LONG
            elif float(price) < float(ema9):
                return Direction.SHORT

        # 3. PCR slope fallback
        pcr_slope = indicators.get("pcr_slope")
        if pcr_slope is not None and not isinstance(pcr_slope, bool):
            slope = float(pcr_slope)
            if slope < 0.0:
                return Direction.LONG
            elif slope > 0.0:
                return Direction.SHORT

        return None

    def get_criteria_checklist(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Dict[str, bool]:
        """
        Evaluates the 5 discrete measurable criteria for Stock Options.
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

        # 2. PCR Slope (20.0)
        pcr_slope_aligned = False
        pcr_slope = indicators.get("pcr_slope")
        if "pcr_slope" in indicators and isinstance(indicators["pcr_slope"], bool):
            pcr_slope_aligned = indicators["pcr_slope"]
        elif pcr_slope is not None:
            slope = float(pcr_slope)
            pcr_slope_aligned = (slope < 0.0) if is_long else (slope > 0.0)

        # 3. VWAP Alignment (20.0)
        vwap_align = False
        vwap = indicators.get("vwap")
        if "vwap_alignment" in indicators:
            vwap_align = bool(indicators["vwap_alignment"])
        elif price is not None and vwap is not None and float(vwap) > 0.0:
            p = float(price)
            v = float(vwap)
            vwap_align = (p > v) if is_long else (p < v)

        # 4. Sentiment Alignment (15.0)
        sent_align = False
        sentiment = indicators.get("sentiment_score") if indicators.get("sentiment_score") is not None else indicators.get("sentiment")
        if "sentiment_alignment" in indicators:
            sent_align = bool(indicators["sentiment_alignment"])
        elif sentiment is not None:
            s_val = float(sentiment)
            sent_align = (s_val > 0.0) if is_long else (s_val < 0.0)

        # 5. Volume Surge (15.0)
        vol_surge = False
        volume = indicators.get("volume") or indicators.get("option_volume")
        avg_volume = indicators.get("avg_volume") or indicators.get("volume_sma")
        if "volume_surge" in indicators:
            vol_surge = bool(indicators["volume_surge"])
        elif volume is not None and avg_volume is not None and float(avg_volume) > 0:
            vol_surge = float(volume) > (1.5 * float(avg_volume))
        elif volume is not None:
            vol_surge = float(volume) >= self.min_liquidity_volume

        return {
            "ema9_crossover": ema9_cross,
            "pcr_slope": pcr_slope_aligned,
            "vwap_alignment": vwap_align,
            "sentiment_alignment": sent_align,
            "volume_surge": vol_surge,
        }

    def evaluate(
        self, indicators: Dict[str, Any], market_state: Dict[str, Any]
    ) -> Optional[Signal]:
        """
        Evaluates indicator payload and returns Signal if actionable, liquid, and PCR fresh.
        """
        underlying = str(
            indicators.get("underlying")
            or market_state.get("underlying")
            or indicators.get("symbol")
            or market_state.get("symbol")
            or "STOCK"
        ).strip().upper()

        current_time = market_state.get("timestamp") or indicators.get("timestamp") or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        # 1. CRITICAL: Review Risk 2 PCR Staleness Check
        is_fresh = self.check_pcr_staleness(underlying, current_time=current_time)
        if market_state.get("is_pcr_stale") or indicators.get("is_pcr_stale") or not is_fresh:
            logger.warning("PCR feed stale, disabling option signals")
            return None

        # 2. Liquidity Check: Option contract volume must meet minimum threshold
        opt_vol = indicators.get("option_volume") if indicators.get("option_volume") is not None else indicators.get("volume")
        if opt_vol is not None and float(opt_vol) < self.min_liquidity_volume:
            logger.info(
                "[%s] Option volume %.0f is below liquidity threshold %.0f. Suppressing signal.",
                self.name,
                float(opt_vol),
                self.min_liquidity_volume,
            )
            return None

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

        macd_cross = indicators.get("macd_crossover")
        if macd_cross == ("BULLISH_CROSS" if is_long_dir else "BEARISH_CROSS"):
            score = min(100.0, score + 5.0)
        if st_aligns:
            score = min(100.0, score + 5.0)
        if rsi_aligns:
            score = min(100.0, score + 5.0)

        raw_tier = confidence_scorer.get_tier(score)

        if score < 55.0 or raw_tier == ConfidenceTier.C:
            logger.debug("[%s] Setup score %.1f is below actionable threshold (55.0).", self.name, score)
            return None

        # A+ Setup Calibration:
        # Score >= 85, ema9_crossover = True, pcr_slope = True, and >= 4 of 5 criteria met
        if score >= 85.0:
            if (
                checklist.get("ema9_crossover", False)
                and checklist.get("pcr_slope", False)
                and sum(checklist.values()) >= 4
            ):
                final_tier = ConfidenceTier.A_PLUS
            else:
                final_tier = ConfidenceTier.A
        else:
            final_tier = raw_tier

        # Contract strike and expiry selection
        available_strikes = market_state.get("available_strikes") or indicators.get("available_strikes") or []
        selected_strike = self.select_strike(float(price), available_strikes, direction=direction)

        available_expiries = market_state.get("available_expiries") or indicators.get("available_expiries") or []
        selected_expiry = self.select_expiry(available_expiries, reference_date=current_time.date())

        # Resolve the actual option contract (premium, symbol, security_id) from the chain
        contract = resolve_option_contract(
            underlying=underlying,
            spot_price=float(price),
            is_long=(direction == Direction.LONG),
            indicators=indicators,
            market_state=market_state,
        )
        if not contract.resolved:
            _now = time.monotonic()
            if _now - _no_contract_warned.get(underlying, 0.0) >= _NO_CONTRACT_WARN_INTERVAL:
                logger.warning(
                    "[%s] No live option contract for %s — suppressing signal rather than "
                    "emitting the underlying spot price as a premium.",
                    self.name, underlying,
                )
                _no_contract_warned[underlying] = _now
            return None

        if contract.strike > 0.0:
            selected_strike = contract.strike

        entry_price = contract.premium
        target = entry_price * 1.35
        # ATR is measured on the underlying, so it cannot be subtracted from an option
        # premium — the two are on different scales. Stops are a fraction of premium.
        stop_loss = round(entry_price * 0.75, 2)

        sig_type = (
            SignalType.EMA9_CROSSOVER_BULLISH
            if direction == Direction.LONG
            else SignalType.EMA9_CROSSOVER_BEARISH
        )

        symbol = contract.symbol

        signal_checklist = [k for k, v in checklist.items() if v]
        if macd_aligns:
            signal_checklist.append("macd_crossover")
        if st_aligns:
            signal_checklist.append("supertrend_direction")
        if rsi_aligns:
            signal_checklist.append("rsi")

        adx_val = indicators.get("adx")
        indicators_triggered = {
            "ema9": checklist.get("ema9_crossover", False),
            "vwap": checklist.get("vwap_alignment", False),
            "pcr_slope": checklist.get("pcr_slope", False),
            "sentiment": checklist.get("sentiment_alignment", False),
            "volume_surge": checklist.get("volume_surge", False),
            "rsi": rsi_aligns,
            "adx": adx_val is not None and float(adx_val) >= 25.0,
            "macd": macd_aligns,
            "bollinger": False,
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
            stop_loss_price=stop_loss,
            confidence_score=score,
            confidence_tier=final_tier,
            checklist=signal_checklist,
            strategy_name=self.name,
            timestamp=current_time,
            metadata={
                "underlying": underlying,
                "underlying_price": float(price),
                "selected_strike": selected_strike,
                "selected_expiry": contract.expiry or (selected_expiry.isoformat() if selected_expiry else None),
                "option_type": "CE" if direction == Direction.LONG else "PE",
                "security_id": contract.security_id,
                "option_premium": contract.premium,
                "criteria_met_count": sum(checklist.values()),
                "total_criteria": len(checklist),
                "is_a_plus": final_tier == ConfidenceTier.A_PLUS,
            },
            indicators_triggered=indicators_triggered,
        )

        if self._store_signal(sig):
            return sig

        return None