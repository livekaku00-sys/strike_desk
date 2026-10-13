import asyncio
import logging
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional
from utils.connection_monitor import ConnectionHealthMonitor

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class AuthenticationError(Exception):
    pass


class RateLimitError(Exception):
    pass


class NetworkError(Exception):
    pass


class WebSocketHandler:
    BACKOFF_SCHEDULE = [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]
    MAX_RECONNECT_ATTEMPTS = 10
    STABLE_CONNECTION_THRESHOLD = 300.0

    def __init__(
        self,
        broker_name: str,
        connect_coro: Callable[[], Awaitable[Any]],
        disconnect_coro: Callable[[], Awaitable[None]],
        send_ping_coro: Optional[Callable[[], Awaitable[None]]] = None,
        resubscribe_coro: Optional[Callable[[Dict[str, str]], Awaitable[None]]] = None,
        ping_interval: float = 30.0,
        ping_timeout: float = 10.0,
        max_missed_heartbeats: int = 3,
    ) -> None:
        self.broker_name = broker_name
        self.connect_coro = connect_coro
        self.disconnect_coro = disconnect_coro
        self.send_ping_coro = send_ping_coro
        self.resubscribe_coro = resubscribe_coro

        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._state_callbacks: List[Callable[[ConnectionState, ConnectionState], Any]] = []

        self.reconnect_attempts: int = 0
        self.last_connected_at: Optional[float] = None
        self._is_manual_disconnect: bool = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._stability_task: Optional[asyncio.Task] = None

        self._subscriptions: Dict[str, str] = {}

        self.health_monitor = ConnectionHealthMonitor(
            broker=broker_name,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            max_missed_heartbeats=max_missed_heartbeats,
            on_stale_detected=self._handle_stale_connection,
            send_ping_func=self.send_ping_coro,
        )

    def get_connection_state(self) -> str:
        return self._state.value

    def register_state_change_callback(self, callback: Callable[[ConnectionState, ConnectionState], Any]) -> None:
        self._state_callbacks.append(callback)

    def _set_state(self, new_state: ConnectionState) -> None:
        if self._state == new_state:
            return
        old_state = self._state
        self._state = new_state
        for cb in self._state_callbacks:
            try:
                res = cb(old_state, new_state)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.error("State callback error: %s", e)

    async def connect(self) -> bool:
        self._is_manual_disconnect = False
        self._set_state(ConnectionState.CONNECTING)
        try:
            await self.connect_coro()
            self._on_connected()
            return True
        except AuthenticationError:
            self._set_state(ConnectionState.DISCONNECTED)
            return False
        except Exception as exc:
            self._trigger_reconnect(reason=f"Initial failure: {exc}")
            return False

    def _on_connected(self) -> None:
        self._set_state(ConnectionState.CONNECTED)
        self.last_connected_at = time.monotonic()
        self.health_monitor.start_monitoring()

        if self._stability_task and not self._stability_task.done():
            self._stability_task.cancel()
        self._stability_task = asyncio.create_task(self._monitor_stability())

        if self._subscriptions and self.resubscribe_coro:
            asyncio.create_task(self._restore_subscriptions())

    async def _monitor_stability(self) -> None:
        try:
            await asyncio.sleep(self.STABLE_CONNECTION_THRESHOLD)
            if self._state == ConnectionState.CONNECTED:
                self.reconnect_attempts = 0
        except asyncio.CancelledError:
            pass

    async def _restore_subscriptions(self) -> None:
        try:
            if self.resubscribe_coro:
                await self.resubscribe_coro(dict(self._subscriptions))
        except Exception as e:
            logger.error("Failed to restore subscriptions: %s", e)

    async def disconnect(self) -> None:
        self._is_manual_disconnect = True
        self.health_monitor.stop_monitoring()
        if self._stability_task and not self._stability_task.done():
            self._stability_task.cancel()
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        try:
            await self.disconnect_coro()
        except Exception:
            pass
        self._set_state(ConnectionState.DISCONNECTED)

    def handle_connection_lost(self, reason: str, exception: Optional[Exception] = None) -> None:
        if self._is_manual_disconnect:
            return
        if isinstance(exception, AuthenticationError):
            self.health_monitor.stop_monitoring()
            self._set_state(ConnectionState.DISCONNECTED)
            return
        self._trigger_reconnect(reason=reason)

    def _trigger_reconnect(self, reason: str) -> None:
        if self._is_manual_disconnect:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._set_state(ConnectionState.RECONNECTING)
        self.health_monitor.stop_monitoring()
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(reason))

    async def _reconnect_loop(self, initial_reason: str) -> None:
        reason = initial_reason
        while self.reconnect_attempts < self.MAX_RECONNECT_ATTEMPTS:
            backoff_index = min(self.reconnect_attempts, len(self.BACKOFF_SCHEDULE) - 1)
            backoff_seconds = self.BACKOFF_SCHEDULE[backoff_index]
            self.reconnect_attempts += 1

            await asyncio.sleep(backoff_seconds)
            if self._is_manual_disconnect:
                return

            try:
                try:
                    await self.disconnect_coro()
                except Exception:
                    pass
                self._set_state(ConnectionState.CONNECTING)
                await self.connect_coro()
                self._on_connected()
                return
            except AuthenticationError:
                self._set_state(ConnectionState.DISCONNECTED)
                return
            except Exception as exc:
                reason = f"Error: {exc}"

        self._set_state(ConnectionState.DISCONNECTED)

    async def _handle_stale_connection(self) -> None:
        self.handle_connection_lost(reason="Stale heartbeat timeout")

    def record_subscription(self, symbols: List[str], mode: str) -> None:
        for sym in symbols:
            self._subscriptions[sym] = mode

    def record_unsubscription(self, symbols: List[str]) -> None:
        for sym in symbols:
            self._subscriptions.pop(sym, None)

    def get_subscriptions(self) -> Dict[str, str]:
        return dict(self._subscriptions)
