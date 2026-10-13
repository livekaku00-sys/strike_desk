import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class ConnectionHealthMonitor:
    def __init__(
        self,
        broker: str,
        ping_interval: float = 30.0,
        ping_timeout: float = 10.0,
        max_missed_heartbeats: int = 3,
        on_stale_detected: Optional[Callable[[], Awaitable[None]]] = None,
        send_ping_func: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.broker = broker
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.max_missed_heartbeats = max_missed_heartbeats
        self.on_stale_detected = on_stale_detected
        self.send_ping_func = send_ping_func

        self.last_heartbeat_sent: Optional[float] = None
        self.last_heartbeat_received: Optional[float] = None
        self.last_tick_received: Optional[float] = None
        self.missed_heartbeats: int = 0
        self.is_running: bool = False
        self._monitor_task: Optional[asyncio.Task] = None

    def record_heartbeat_sent(self) -> None:
        self.last_heartbeat_sent = time.monotonic()

    def record_heartbeat_received(self) -> None:
        self.last_heartbeat_received = time.monotonic()
        self.missed_heartbeats = 0

    def record_tick(self) -> None:
        self.last_tick_received = time.monotonic()
        self.missed_heartbeats = 0

    def is_stale(self) -> bool:
        return self.missed_heartbeats >= self.max_missed_heartbeats

    def start_monitoring(self) -> None:
        if self.is_running and self._monitor_task and not self._monitor_task.done():
            return
        self.is_running = True
        self.missed_heartbeats = 0
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name=f"health_monitor_{self.broker}")

    def stop_monitoring(self) -> None:
        self.is_running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None

    def reset(self) -> None:
        self.last_heartbeat_sent = None
        self.last_heartbeat_received = None
        self.last_tick_received = None
        self.missed_heartbeats = 0

    async def _monitor_loop(self) -> None:
        try:
            while self.is_running:
                await asyncio.sleep(self.ping_interval)
                if not self.is_running:
                    break

                self.record_heartbeat_sent()
                ping_sent_time = self.last_heartbeat_sent

                if self.send_ping_func:
                    try:
                        await asyncio.wait_for(self.send_ping_func(), timeout=self.ping_timeout)
                    except Exception as e:
                        logger.warning("[%s] Failed to dispatch ping message: %s", self.broker, e)

                await asyncio.sleep(self.ping_timeout)
                if not self.is_running:
                    break

                pong_received = (self.last_heartbeat_received is not None and self.last_heartbeat_received >= ping_sent_time)
                tick_received = (self.last_tick_received is not None and self.last_tick_received >= ping_sent_time)

                if not (pong_received or tick_received):
                    self.missed_heartbeats += 1
                    if self.is_stale():
                        if self.on_stale_detected:
                            await self.on_stale_detected()
                        break
                else:
                    self.missed_heartbeats = 0
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[%s] Unexpected error in health monitor loop: %s", self.broker, exc)
