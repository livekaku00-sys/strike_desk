"""
Strike Desk V4 - Unified Pipeline Orchestrator
Integrates:
  - Tick ingestion via TickBuffer & TickConsumer (Phase 1)
  - InstrumentRegistry (Phase 2)
  - Price & Volume indicators: VWAP & EMA9 (Phase 3 Prompt 2)
  - Derivatives & Sentiment indicators: PCR Slope & Sentiment Scoring (Phase 3 Prompt 3)
  - PCR Staleness Monitor (Review Risk 2)
  - Market session validation & Stale feed detection (Phase 3 Prompt 1)
  - State checkpointing & Interruption recovery (Phase 3 Prompt 4)
"""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from models.instrument import Exchange
from models.tick import Tick
from pipeline.consumer import TickConsumer
from pipeline.indicator_calculator import BaseIndicatorCalculator
from pipeline.indicators import (
    ADXCalculator,
    ATRCalculator,
    BollingerCalculator,
    EMACalculator,
    MACDCalculator,
    PCRSlopeCalculator,
    PCRStalenessMonitor,
    RSICalculator,
    SentimentCalculator,
    SupertrendCalculator,
    VWAPCalculator,
    pcr_staleness_monitor,
)
from pipeline.market_session import MarketSession, market_session
from pipeline.stale_feed_detector import StaleFeedDetector, stale_feed_detector
from pipeline.state_persistence import StatePersistence, state_persistence
from pipeline.tick_buffer import TickBuffer, tick_buffer
from strategies.daily_watchlist import daily_watchlist_scanner

try:
    from services.instrument_registry import instrument_registry
except ImportError:
    instrument_registry = None

logger = logging.getLogger(__name__)


class SymbolIndicatorBundle(BaseIndicatorCalculator):
    """
    Unified indicator suite per monitored symbol.
    Synchronously updates VWAP, EMA9, PCR slope, and composite sentiment.
    """

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self.vwap = VWAPCalculator(symbol)
        self.ema9 = EMACalculator(symbol, period=9)
        self.pcr_slope = PCRSlopeCalculator(symbol, window_minutes=5)
        self.sentiment = SentimentCalculator(symbol)
        self.rsi = RSICalculator(symbol, period=14)
        self.adx = ADXCalculator(symbol, period=14)
        self.bollinger = BollingerCalculator(symbol, period=20, num_std=2.0)
        self.atr = ATRCalculator(symbol, period=14)
        self.macd = MACDCalculator(symbol, fast=12, slow=26, signal=9)
        self.supertrend = SupertrendCalculator(symbol, period=10, multiplier=3.0)

    def calculate_indicators(self, tick: Tick) -> Dict[str, Any]:
        """
        Calculates all real-time indicators from a newly processed tick.
        """
        # 1. Price/Volume metrics
        res_vwap = self.vwap.calculate_indicators(tick)
        res_ema = self.ema9.calculate_indicators(tick)

        # 2. Check if tick carries inline PCR
        pcr_val = getattr(tick, "pcr", None)
        if pcr_val is not None:
            self.pcr_slope.process_pcr_update(self.symbol, float(pcr_val), tick.timestamp)

        # 3. Derive Sentiment components
        pcr_slope_val = self.pcr_slope.last_slope
        ema_val = self.ema9.ema_value
        ema_trend = (tick.ltp - ema_val) / ema_val if (ema_val is not None and ema_val > 0.0) else None

        # Only activate price-deviation sentiment once underlying trend/derivatives are established
        if pcr_slope_val is not None or ema_trend is not None:
            vwap_val = self.vwap.vwap
            vwap_dev = (tick.ltp - vwap_val) / vwap_val if vwap_val > 0.0 else None
        else:
            vwap_dev = None

        oi_change = getattr(tick, "oi_change_pct", None)

        res_sent = self.sentiment.calculate_sentiment(
            pcr_slope=pcr_slope_val,
            vwap_deviation=vwap_dev,
            ema_trend=ema_trend,
            oi_change_pct=oi_change,
        )

        # 4. RSI, ADX, Bollinger Bands, ATR, MACD, Supertrend
        res_rsi = self.rsi.calculate_indicators(tick)
        res_adx = self.adx.calculate_indicators(tick)
        res_bb = self.bollinger.calculate_indicators(tick)
        res_atr = self.atr.calculate_indicators(tick)
        res_macd = self.macd.calculate_indicators(tick)
        res_st = self.supertrend.calculate_indicators(tick)

        indicators = {
            "vwap": self.vwap.vwap,
            "cumulative_volume": self.vwap.cumulative_volume,
            "cumulative_pv": self.vwap.cumulative_pv,
            "ema": self.ema9.ema_value,
            "ema9": self.ema9.ema_value,
            "period": self.ema9.period,
            "sufficient_data": self.ema9.ema_value is not None,
            "pcr_slope": self.pcr_slope.last_slope,
            "pcr_current": self.pcr_slope.pcr_history[-1][1] if self.pcr_slope.pcr_history else None,
            "sentiment_score": self.sentiment.last_sentiment,
            "sentiment": self.sentiment.last_sentiment,
            "sentiment_confidence": self.sentiment.last_confidence,
            "confidence": self.sentiment.last_confidence,
            "component_scores": dict(self.sentiment.component_scores),
            "is_pcr_trustable": self.pcr_slope.is_pcr_trustable(self.symbol) if self.pcr_slope.pcr_history else False,
            # RSI
            "rsi": res_rsi.get("rsi"),
            "rsi_zone": res_rsi.get("zone"),
            # ADX
            "adx": res_adx.get("adx"),
            "plus_di": res_adx.get("plus_di"),
            "minus_di": res_adx.get("minus_di"),
            "adx_trend_strength": res_adx.get("trend_strength"),
            "adx_direction": res_adx.get("direction"),
            # Bollinger Bands
            "upper_band": res_bb.get("upper"),
            "middle_band": res_bb.get("middle"),
            "lower_band": res_bb.get("lower"),
            "bandwidth": res_bb.get("bandwidth"),
            "percent_b": res_bb.get("percent_b"),
            "bb_position": res_bb.get("bb_position"),
            # ATR
            "atr": res_atr.get("atr"),
            "atr_percent": res_atr.get("atr_percent"),
            "atr_sl_distance": res_atr.get("atr_sl_distance"),
            "volatility": res_atr.get("volatility"),
            # MACD
            "macd_line": res_macd.get("macd_line"),
            "signal_line": res_macd.get("signal_line"),
            "histogram": res_macd.get("histogram"),
            "macd_crossover": res_macd.get("crossover"),
            "macd_trend": res_macd.get("trend"),
            # Supertrend
            "supertrend": res_st.get("supertrend"),
            "supertrend_direction": res_st.get("direction"),
            "supertrend_signal": res_st.get("signal"),
            "supertrend_trend_changed": res_st.get("trend_changed", False),
        }
        self.indicator_values.update(indicators)
        return indicators

    def reset(self) -> None:
        """Resets all component indicators."""
        super().reset()
        self.vwap.reset()
        self.ema9.reset()
        self.pcr_slope.reset()
        self.sentiment.reset()
        self.rsi.reset()
        self.adx.reset()
        self.bollinger.reset()
        self.atr.reset()
        self.macd.reset()
        self.supertrend.reset()

    def get_state(self) -> Dict[str, Any]:
        """Returns serializable state dictionary."""
        return {
            "vwap": {
                "cumulative_pv": self.vwap.cumulative_pv,
                "cumulative_volume": self.vwap.cumulative_volume,
                "vwap": self.vwap.vwap,
            },
            "ema9": {
                "prices": list(self.ema9.prices),
                "ema_value": self.ema9.ema_value,
                "period": self.ema9.period,
            },
            "pcr_slope": {
                "pcr_history": [
                    (t.isoformat(), val) for t, val in self.pcr_slope.pcr_history
                ],
                "last_slope": self.pcr_slope.last_slope,
                "window_minutes": self.pcr_slope.window_minutes,
            },
            "sentiment": {
                "component_scores": dict(self.sentiment.component_scores),
                "last_sentiment": self.sentiment.last_sentiment,
                "confidence": self.sentiment.last_confidence,
            },
            "rsi": {
                "rsi_value": self.rsi.rsi_value,
                "avg_gain": self.rsi._avg_gain,
                "avg_loss": self.rsi._avg_loss,
            },
            "adx": {
                "adx_value": self.adx.adx_value,
                "plus_di": self.adx.plus_di_value,
                "minus_di": self.adx.minus_di_value,
                "smoothed_tr": self.adx._smoothed_tr,
                "smoothed_plus_dm": self.adx._smoothed_plus_dm,
                "smoothed_minus_dm": self.adx._smoothed_minus_dm,
            },
            "bollinger": {
                "upper": self.bollinger.upper_value,
                "middle": self.bollinger.middle_value,
                "lower": self.bollinger.lower_value,
                "prices_snapshot": list(self.bollinger._prices),
            },
            "atr": {
                "atr_value": self.atr.atr_value,
                "smoothed_tr": self.atr._smoothed_tr,
                "prev_close": self.atr._prev_close,
            },
            "macd": {
                "fast_ema": self.macd._fast_ema,
                "slow_ema": self.macd._slow_ema,
                "signal_ema": self.macd._signal_ema,
                "macd_line": self.macd.macd_line,
                "signal_line": self.macd.signal_line,
                "prev_histogram": self.macd._prev_histogram,
            },
            "supertrend": {
                "supertrend_value": self.supertrend.supertrend_value,
                "direction_value": self.supertrend.direction_value,
                "prev_upper_band": self.supertrend._prev_upper_band,
                "prev_lower_band": self.supertrend._prev_lower_band,
                "prev_direction": self.supertrend._prev_direction,
                "prev_close": self.supertrend._prev_close,
            },
            "last_tick_timestamp": (
                self.last_tick_timestamp.isoformat()
                if self.last_tick_timestamp
                else None
            ),
            "tick_count": self.tick_count,
        }

    def restore_from_state(self, state_dict: Dict[str, Any]) -> None:
        """Restores internal indicators from serialized dictionary."""
        self.tick_count = int(state_dict.get("tick_count", 0))
        ts_str = state_dict.get("last_tick_timestamp")
        self.last_tick_timestamp = (
            datetime.fromisoformat(ts_str) if ts_str else None
        )

        # Restore VWAP
        vwap_state = state_dict.get("vwap", {})
        self.vwap.cumulative_pv = float(vwap_state.get("cumulative_pv", 0.0))
        self.vwap.cumulative_volume = float(vwap_state.get("cumulative_volume", 0.0))
        if self.vwap.cumulative_volume > 0:
            self.vwap.vwap = float(vwap_state.get("vwap", self.vwap.cumulative_pv / self.vwap.cumulative_volume))
        else:
            self.vwap.vwap = float(vwap_state.get("vwap", 0.0))
        self.vwap.tick_count = self.tick_count
        self.vwap.last_tick_timestamp = self.last_tick_timestamp

        # Restore EMA9
        ema_state = state_dict.get("ema9", {})
        self.ema9.prices = [float(p) for p in ema_state.get("prices", [])]
        self.ema9.ema_value = (
            float(ema_state["ema_value"]) if ema_state.get("ema_value") is not None else None
        )
        self.ema9.tick_count = self.tick_count
        self.ema9.last_tick_timestamp = self.last_tick_timestamp

        # Restore PCR Slope
        pcr_state = state_dict.get("pcr_slope", {})
        self.pcr_slope.pcr_history = [
            (datetime.fromisoformat(t), float(v))
            for t, v in pcr_state.get("pcr_history", [])
        ]
        self.pcr_slope.last_slope = (
            float(pcr_state["last_slope"]) if pcr_state.get("last_slope") is not None else None
        )
        self.pcr_slope.window_minutes = int(pcr_state.get("window_minutes", 5))

        # Restore Bollinger Bands
        bb_state = state_dict.get("bollinger", {})
        if bb_state.get("prices_snapshot"):
            self.bollinger._prices = [float(p) for p in bb_state["prices_snapshot"]]
        if bb_state.get("upper") is not None:
            self.bollinger.upper_value = float(bb_state["upper"])
        if bb_state.get("middle") is not None:
            self.bollinger.middle_value = float(bb_state["middle"])
        if bb_state.get("lower") is not None:
            self.bollinger.lower_value = float(bb_state["lower"])

        # Restore ATR
        atr_state = state_dict.get("atr", {})
        if atr_state.get("atr_value") is not None:
            self.atr.atr_value = float(atr_state["atr_value"])
        if atr_state.get("smoothed_tr") is not None:
            self.atr._smoothed_tr = float(atr_state["smoothed_tr"])
        if atr_state.get("prev_close") is not None:
            self.atr._prev_close = float(atr_state["prev_close"])

        # Restore RSI
        rsi_state = state_dict.get("rsi", {})
        if rsi_state.get("rsi_value") is not None:
            self.rsi.rsi_value = float(rsi_state["rsi_value"])
        if rsi_state.get("avg_gain") is not None:
            self.rsi._avg_gain = float(rsi_state["avg_gain"])
        if rsi_state.get("avg_loss") is not None:
            self.rsi._avg_loss = float(rsi_state["avg_loss"])

        # Restore ADX
        adx_state = state_dict.get("adx", {})
        if adx_state.get("adx_value") is not None:
            self.adx.adx_value = float(adx_state["adx_value"])
        if adx_state.get("plus_di") is not None:
            self.adx.plus_di_value = float(adx_state["plus_di"])
        if adx_state.get("minus_di") is not None:
            self.adx.minus_di_value = float(adx_state["minus_di"])
        if adx_state.get("smoothed_tr") is not None:
            self.adx._smoothed_tr = float(adx_state["smoothed_tr"])
        if adx_state.get("smoothed_plus_dm") is not None:
            self.adx._smoothed_plus_dm = float(adx_state["smoothed_plus_dm"])
        if adx_state.get("smoothed_minus_dm") is not None:
            self.adx._smoothed_minus_dm = float(adx_state["smoothed_minus_dm"])

        # Restore MACD
        macd_state = state_dict.get("macd", {})
        if macd_state.get("fast_ema") is not None:
            self.macd._fast_ema = float(macd_state["fast_ema"])
        if macd_state.get("slow_ema") is not None:
            self.macd._slow_ema = float(macd_state["slow_ema"])
        if macd_state.get("signal_ema") is not None:
            self.macd._signal_ema = float(macd_state["signal_ema"])
        if macd_state.get("macd_line") is not None:
            self.macd.macd_line = float(macd_state["macd_line"])
        if macd_state.get("signal_line") is not None:
            self.macd.signal_line = float(macd_state["signal_line"])
        if macd_state.get("prev_histogram") is not None:
            self.macd._prev_histogram = float(macd_state["prev_histogram"])

        # Restore Supertrend
        st_state = state_dict.get("supertrend", {})
        if st_state.get("supertrend_value") is not None:
            self.supertrend.supertrend_value = float(st_state["supertrend_value"])
        if st_state.get("direction_value") is not None:
            self.supertrend.direction_value = str(st_state["direction_value"])
        if st_state.get("prev_upper_band") is not None:
            self.supertrend._prev_upper_band = float(st_state["prev_upper_band"])
        if st_state.get("prev_lower_band") is not None:
            self.supertrend._prev_lower_band = float(st_state["prev_lower_band"])
        if st_state.get("prev_direction") is not None:
            self.supertrend._prev_direction = int(st_state["prev_direction"])
        if st_state.get("prev_close") is not None:
            self.supertrend._prev_close = float(st_state["prev_close"])

        # Restore Sentiment
        sent_state = state_dict.get("sentiment", {})
        self.sentiment.component_scores = {
            k: float(v) for k, v in sent_state.get("component_scores", {}).items()
        }
        self.sentiment.last_sentiment = float(sent_state.get("last_sentiment", 0.0))
        self.sentiment.last_confidence = float(sent_state.get("confidence", 0.0))

        # Re-populate indicator_values
        self.indicator_values.update({
            "vwap": self.vwap.vwap,
            "cumulative_volume": self.vwap.cumulative_volume,
            "cumulative_pv": self.vwap.cumulative_pv,
            "ema": self.ema9.ema_value,
            "ema9": self.ema9.ema_value,
            "period": self.ema9.period,
            "sufficient_data": self.ema9.ema_value is not None,
            "pcr_slope": self.pcr_slope.last_slope,
            "sentiment_score": self.sentiment.last_sentiment,
            "sentiment": self.sentiment.last_sentiment,
            "confidence": self.sentiment.last_confidence,
            "component_scores": dict(self.sentiment.component_scores),
            "rsi": self.rsi.rsi_value,
            "rsi_zone": self.rsi._classify_zone(self.rsi.rsi_value) if self.rsi.rsi_value is not None else None,
            "adx": self.adx.adx_value,
            "plus_di": self.adx.plus_di_value,
            "minus_di": self.adx.minus_di_value,
            "adx_trend_strength": self.adx._classify_trend(self.adx.adx_value) if self.adx.adx_value is not None else None,
            "upper_band": self.bollinger.upper_value,
            "middle_band": self.bollinger.middle_value,
            "lower_band": self.bollinger.lower_value,
            "atr": self.atr.atr_value,
            "macd_line": self.macd.macd_line,
            "signal_line": self.macd.signal_line,
            "macd_crossover": None,
            "macd_trend": None,
            "supertrend": self.supertrend.supertrend_value,
            "supertrend_direction": self.supertrend.direction_value,
            "supertrend_signal": None,
            "supertrend_trend_changed": False,
        })


class PipelineOrchestrator:
    """
    Unified Pipeline Orchestrator coordinating ingestion, indicators,
    session checks, feed staleness, and crash recovery.
    """

    _instance: Optional["PipelineOrchestrator"] = None
    _singleton_lock = asyncio.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "PipelineOrchestrator":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        buffer: Optional[TickBuffer] = None,
        session_validator: Optional[MarketSession] = None,
        stale_detector: Optional[StaleFeedDetector] = None,
        pcr_monitor: Optional[PCRStalenessMonitor] = None,
        persistence: Optional[StatePersistence] = None,
    ) -> None:
        if getattr(self, "_initialized", False):
            return

        self.buffer = buffer or tick_buffer
        self.session = session_validator or market_session
        self.stale_detector = stale_detector or stale_feed_detector
        self.pcr_monitor = pcr_monitor or pcr_staleness_monitor
        self.persistence = persistence or state_persistence
        self.registry = instrument_registry

        self.consumer = TickConsumer(
            buffer=self.buffer,
            session_validator=self.session,
            stale_detector=self.stale_detector,
        )

        self._bundles: Dict[str, SymbolIndicatorBundle] = {}
        self._latest_ticks: Dict[str, Tick] = {}
        self._symbols: List[str] = []
        self._start_time: Optional[datetime] = None
        self._last_save_time: Optional[datetime] = None
        self._last_watchlist_refresh: Optional[datetime] = None
        self._watchlist_refresh_interval_s: float = 300.0  # every 5 minutes
        self._lock = asyncio.Lock()
        self._initialized = True

    async def initialize(self, symbols: List[str]) -> None:
        """
        Initializes symbol tracking, creates per-symbol indicator bundles,
        and connects queues and consumers.
        """
        async with self._lock:
            clean_symbols = [s.strip().upper() for s in symbols]
            self._symbols = clean_symbols
            self._start_time = datetime.now(timezone.utc)

            for sym in clean_symbols:
                if sym not in self._bundles:
                    bundle = SymbolIndicatorBundle(sym)
                    self._bundles[sym] = bundle
                    self.consumer.register_calculator(sym, bundle)

            self.buffer.start()
            await self.consumer.start(self._symbols)
            logger.info("[PipelineOrchestrator] Initialized with %d symbols: %s", len(self._symbols), self._symbols)

    async def start(self) -> None:
        """Starts tick ingestion and background loops."""
        self._start_time = self._start_time or datetime.now(timezone.utc)
        self.buffer.start()
        await self.consumer.start(self._symbols)

    async def stop(self) -> None:
        """Stops consumers and flushes buffer queues."""
        await self.consumer.stop()
        await self.buffer.stop()

    def pause(self) -> None:
        """Pauses tick processing."""
        self.consumer.pause()

    def resume(self) -> None:
        """Resumes tick processing."""
        self.consumer.resume()

    def refresh_watchlist(self) -> List[Any]:
        """
        Rescores all symbol bundles and rebuilds the top-10 watchlist.
        Safe to call from any thread; returns the new list of WatchlistEntry.
        """
        try:
            from utils.cache_manager import option_chain_cache
            entries = daily_watchlist_scanner.refresh(self._bundles, option_chain_cache)
            self._last_watchlist_refresh = datetime.now(timezone.utc)
            return entries
        except Exception as exc:
            logger.warning("[PipelineOrchestrator] Watchlist refresh failed: %s", exc)
            return []

    def get_watchlist(self) -> List[Any]:
        """Returns the last computed watchlist (may be empty before first refresh)."""
        return daily_watchlist_scanner.get_watchlist()

    async def ingest_tick(self, tick: Tick) -> bool:
        """
        Ingests tick into buffer. Returns True if accepted, False if duplicate/skewed.
        Triggers a watchlist refresh every 5 minutes.
        """
        accepted = await self.buffer.push(tick)
        if accepted and tick.symbol:
            self._latest_ticks[tick.symbol.upper()] = tick

            # Periodic watchlist refresh (non-blocking, best-effort)
            now = datetime.now(timezone.utc)
            if (
                self._last_watchlist_refresh is None
                or (now - self._last_watchlist_refresh).total_seconds()
                >= self._watchlist_refresh_interval_s
            ):
                try:
                    self.refresh_watchlist()
                except Exception:
                    pass

        return accepted

    def get_latest_tick(self, symbol: str) -> Optional[Tick]:
        return self._latest_ticks.get(symbol.strip().upper())

    def get_ltp(self, symbol: str) -> Optional[float]:
        """Last traded price from the live feed, or None if that symbol has not ticked."""
        tick = self.get_latest_tick(symbol)
        if tick is None or not tick.ltp or tick.ltp <= 0:
            return None
        return float(tick.ltp)

    def get_all_ticks(self) -> Dict[str, Tick]:
        return dict(self._latest_ticks)

    def process_pcr_update(
        self,
        underlying: str,
        pcr: float,
        timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Dispatches real-time PCR update directly to symbol's indicator bundle.
        """
        sym = underlying.strip().upper()
        bundle = self._bundles.get(sym)
        ts = timestamp or datetime.now(timezone.utc)
        if bundle:
            return bundle.pcr_slope.process_pcr_update(sym, pcr, ts)
        # Record with staleness monitor regardless
        self.pcr_monitor.record_update(sym, ts)
        return {"underlying": sym, "pcr": pcr, "sufficient_data": False}

    def get_indicator_values(self, symbol: str) -> Dict[str, Any]:
        """
        Returns real-time indicator values dictionary for the designated symbol.
        """
        sym = symbol.strip().upper()
        bundle = self._bundles.get(sym)
        if bundle:
            return dict(bundle.indicator_values)
        return {}

    def get_bundle(self, symbol: str) -> Optional[SymbolIndicatorBundle]:
        """Returns direct reference to a symbol's indicator bundle."""
        return self._bundles.get(symbol.strip().upper())

    def get_pipeline_status(self) -> Dict[str, Any]:
        """
        Returns full runtime telemetry across buffer, consumer, feeds, and indicators.
        """
        now = datetime.now(timezone.utc)
        stale_symbols = self.stale_detector.get_stale_symbols()
        pcr_stale = self.pcr_monitor.get_all_stale_underlyings(now)

        return {
            "is_running": self.consumer.is_running,
            "is_paused": self.consumer.is_paused,
            "monitored_symbols": list(self._symbols),
            "consumed_ticks": self.consumer.get_consumed_count(),
            "stale_symbols": stale_symbols,
            "stale_count": len(stale_symbols),
            "pcr_stale_symbols": pcr_stale,
            "last_save_time": (
                self._last_save_time.isoformat()
                if self._last_save_time
                else None
            ),
            "pipeline_start_time": (
                self._start_time.isoformat()
                if self._start_time
                else None
            ),
        }

    def save_state(self, filepath: str = "/root/strike_desk/data/pipeline_state.json") -> None:
        """
        Serializes current orchestrator state to disk.
        """
        now = datetime.now(timezone.utc)
        payload = {
            "metadata": {
                "save_time": now.isoformat(),
                "pipeline_start_time": (
                    self._start_time.isoformat()
                    if self._start_time
                    else now.isoformat()
                ),
            },
            "symbols": {
                sym: bundle.get_state() for sym, bundle in self._bundles.items()
            },
        }
        success = self.persistence.save_state(payload, filepath)
        if success:
            self._last_save_time = now

    def restore_state(self, filepath: str = "/root/strike_desk/data/pipeline_state.json") -> bool:
        """
        Restores state from disk and re-initializes indicator calculators.
        """
        data = self.persistence.load_state(filepath)
        if not data:
            return False

        symbols_data = data.get("symbols", {})
        for sym, sym_state in symbols_data.items():
            sym_up = sym.strip().upper()
            if sym_up not in self._bundles:
                bundle = SymbolIndicatorBundle(sym_up)
                self._bundles[sym_up] = bundle
                self.consumer.register_calculator(sym_up, bundle)
                if sym_up not in self._symbols:
                    self._symbols.append(sym_up)
            else:
                bundle = self._bundles[sym_up]

            bundle.restore_from_state(sym_state)

        metadata = data.get("metadata", {})
        if "pipeline_start_time" in metadata:
            try:
                self._start_time = datetime.fromisoformat(metadata["pipeline_start_time"])
            except Exception:
                pass
        if "save_time" in metadata:
            try:
                self._last_save_time = datetime.fromisoformat(metadata["save_time"])
            except Exception:
                pass

        logger.info("[PipelineOrchestrator] Successfully restored state for %d symbols from %s", len(symbols_data), filepath)
        return True


# Global singleton instance
pipeline_orchestrator = PipelineOrchestrator()