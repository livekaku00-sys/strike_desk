import asyncio
import logging
from typing import Any, Dict, List, Optional

from pipeline.consumer import TickConsumer
from pipeline.indicator_calculator import BaseIndicatorCalculator
from pipeline.market_session import MarketSession, market_session
from pipeline.stale_feed_detector import StaleFeedDetector, stale_feed_detector
from pipeline.tick_buffer import TickBuffer, tick_buffer

logger = logging.getLogger(__name__)


class PipelineManager:
    """
    Unified Pipeline Manager orchestrating:
    - High-throughput asynchronous tick consumption
    - Real-time indicator calculation pipelines
    - Market session validation
    - Stale feed detection & alerts
    - Lifecycle controls (start, stop, pause, resume)
    """

    def __init__(
        self,
        consumer: Optional[TickConsumer] = None,
        buffer: Optional[TickBuffer] = None,
        session_validator: Optional[MarketSession] = None,
        stale_detector: Optional[StaleFeedDetector] = None,
    ) -> None:
        self.buffer = buffer or tick_buffer
        self.market_session = session_validator or market_session
        self.stale_detector = stale_detector or stale_feed_detector
        self.consumer = consumer or TickConsumer(
            buffer=self.buffer,
            session_validator=self.market_session,
            stale_detector=self.stale_detector,
        )

        self._lock = asyncio.Lock()
        self._symbols: List[str] = []

    async def initialize(self, symbols: List[str]) -> None:
        """
        Initializes pipeline tracking for the designated symbols and starts consumption.
        """
        async with self._lock:
            self._symbols = [s.strip().upper() for s in symbols]
            self.buffer.start()
            await self.consumer.start(self._symbols)
            logger.info("PipelineManager initialized with %d symbols: %s", len(self._symbols), self._symbols)

    async def start(self) -> None:
        """Starts tick consumption if stopped."""
        self.buffer.start()
        await self.consumer.start(self._symbols)

    async def stop(self) -> None:
        """Stops tick consumption and buffer loops."""
        await self.consumer.stop()
        await self.buffer.stop()

    def pause(self) -> None:
        self.consumer.pause()

    def resume(self) -> None:
        self.consumer.resume()

    def register_indicator_calculator(
        self, symbol: str, calculator: BaseIndicatorCalculator
    ) -> None:
        self.consumer.register_calculator(symbol, calculator)

    def get_calculator(self, symbol: str) -> Optional[BaseIndicatorCalculator]:
        return self.consumer.get_calculator(symbol)

    def get_indicator_values(self, symbol: str) -> Dict[str, Any]:
        calc = self.consumer.get_calculator(symbol)
        if calc:
            return dict(calc.indicator_values)
        return {}

    def get_status(self) -> Dict[str, Any]:
        """
        Returns comprehensive runtime telemetry across consumers, feeds, and indicators.
        """
        stale_symbols = self.stale_detector.get_stale_symbols()
        return {
            "is_running": self.consumer.is_running,
            "is_paused": self.consumer.is_paused,
            "monitored_symbols": list(self._symbols),
            "consumed_ticks": self.consumer.get_consumed_count(),
            "stale_symbols": stale_symbols,
            "stale_count": len(stale_symbols),
        }


# Global singleton instance
pipeline_manager = PipelineManager()
