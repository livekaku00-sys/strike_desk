import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, List, Optional, Set
from models.tick import Tick

logger = logging.getLogger(__name__)


class TickBuffer:
    MAX_QUEUE_SIZE = 10000
    CLOCK_SKEW_TOLERANCE_MS = 100
    BUFFER_WINDOW_MS = 500

    def __init__(
        self,
        buffer_window_ms: int = BUFFER_WINDOW_MS,
        clock_skew_tolerance_ms: int = CLOCK_SKEW_TOLERANCE_MS,
        max_queue_size: int = MAX_QUEUE_SIZE,
    ) -> None:
        self.buffer_window_ms = buffer_window_ms
        self.clock_skew_tolerance = timedelta(milliseconds=clock_skew_tolerance_ms)
        self.max_queue_size = max_queue_size

        self._last_timestamps: Dict[str, datetime] = {}
        self._seen_signatures: Dict[str, Set[tuple]] = {}
        self._buffer: Dict[str, List[Tick]] = {}
        self._consumers: Dict[str, Set[asyncio.Queue]] = {}

        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

    def start(self) -> None:
        if self._is_running:
            return
        self._is_running = True
        self._flush_task = asyncio.create_task(self._periodic_flush_loop(), name="tick_buffer_flush_loop")

    async def stop(self) -> None:
        self._is_running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        await self.flush()

    def subscribe(self, symbol: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue_size)
        sym = symbol.upper()
        if sym not in self._consumers:
            self._consumers[sym] = set()
        self._consumers[sym].add(q)
        return q

    def unsubscribe(self, symbol: str, queue: asyncio.Queue) -> None:
        sym = symbol.upper()
        if sym in self._consumers:
            self._consumers[sym].discard(queue)
            if not self._consumers[sym]:
                del self._consumers[sym]

    async def push(self, tick: Tick) -> bool:
        sym = tick.symbol.upper()
        ts = tick.timestamp

        async with self._lock:
            last_ts = self._last_timestamps.get(sym)

            if last_ts is not None:
                sig = (ts, tick.ltp, tick.volume, tick.bid_price, tick.ask_price)
                if sym not in self._seen_signatures:
                    self._seen_signatures[sym] = set()

                if sig in self._seen_signatures[sym]:
                    return False

                if ts < (last_ts - self.clock_skew_tolerance):
                    return False

                if ts < last_ts:
                    tick.late = True

            if last_ts is None or ts > last_ts:
                self._last_timestamps[sym] = ts

            if sym not in self._seen_signatures:
                self._seen_signatures[sym] = set()
            self._seen_signatures[sym].add((ts, tick.ltp, tick.volume, tick.bid_price, tick.ask_price))
            if len(self._seen_signatures[sym]) > 1000:
                self._seen_signatures[sym].clear()

            if sym not in self._buffer:
                self._buffer[sym] = []
            self._buffer[sym].append(tick)

        return True

    async def flush(self) -> int:
        async with self._lock:
            if not self._buffer:
                return 0
            current_buffers = self._buffer
            self._buffer = {}

        total_distributed = 0
        for sym, ticks in current_buffers.items():
            if not ticks:
                continue
            ticks.sort(key=lambda t: t.timestamp)
            consumers = self._consumers.get(sym, set())
            for tick in ticks:
                for q in list(consumers):
                    self._dispatch_with_backpressure(q, tick, sym)
                total_distributed += 1

        return total_distributed

    def _dispatch_with_backpressure(self, queue: asyncio.Queue, tick: Tick, symbol: str) -> None:
        if queue.full():
            try:
                queue.get_nowait()
                logger.warning("[%s] Queue overflow (capacity %d reached). Dropped oldest tick.", symbol, self.max_queue_size)
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(tick)
        except asyncio.QueueFull:
            logger.error("[%s] Failed to enqueue tick despite overflow drop.", symbol)

    async def _periodic_flush_loop(self) -> None:
        interval = self.buffer_window_ms / 1000.0
        try:
            while self._is_running:
                await asyncio.sleep(interval)
                await self.flush()
        except asyncio.CancelledError:
            pass


tick_buffer = TickBuffer()
