import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.websocket_handler import ConnectionState, NetworkError, WebSocketHandler
from utils.connection_monitor import ConnectionHealthMonitor


class TestWebSocket(unittest.IsolatedAsyncioTestCase):
    async def test_a_connection_state_transitions(self):
        transitions = []

        def on_transition(old_state, new_state):
            transitions.append((old_state, new_state))

        handler = WebSocketHandler(broker_name="test_broker", connect_coro=AsyncMock(), disconnect_coro=AsyncMock())
        handler.register_state_change_callback(on_transition)

        self.assertEqual(handler.get_connection_state(), ConnectionState.DISCONNECTED.value)
        await handler.connect()
        self.assertEqual(handler.get_connection_state(), ConnectionState.CONNECTED.value)
        await handler.disconnect()
        self.assertEqual(handler.get_connection_state(), ConnectionState.DISCONNECTED.value)

    async def test_b_reconnect_backoff_timing(self):
        backoff_durations = []

        async def fake_sleep(duration):
            if duration in WebSocketHandler.BACKOFF_SCHEDULE:
                backoff_durations.append(duration)

        attempt_count = 0

        async def failing_then_succeeding_connect():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count <= 4:
                raise NetworkError(f"Connection attempt {attempt_count} failed")
            return None

        handler = WebSocketHandler(broker_name="test_broker", connect_coro=failing_then_succeeding_connect, disconnect_coro=AsyncMock())
        handler.health_monitor.start_monitoring = MagicMock()

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await handler.connect()
            if handler._reconnect_task:
                await handler._reconnect_task
            self.assertEqual(handler.get_connection_state(), ConnectionState.CONNECTED.value)
            await handler.disconnect()

        self.assertEqual(backoff_durations, [1, 2, 4, 8])

    async def test_c_stale_connection_detection(self):
        stale_triggered = asyncio.Event()

        async def on_stale():
            stale_triggered.set()

        monitor = ConnectionHealthMonitor(broker="test_broker", ping_interval=0.01, ping_timeout=0.01, max_missed_heartbeats=3, on_stale_detected=on_stale)
        monitor.start_monitoring()
        try:
            await asyncio.wait_for(stale_triggered.wait(), timeout=1.0)
            self.assertTrue(stale_triggered.is_set())
            self.assertTrue(monitor.is_stale())
        finally:
            monitor.stop_monitoring()

    async def test_d_heartbeat_mechanism(self):
        stale_triggered = asyncio.Event()

        async def on_stale():
            stale_triggered.set()

        monitor = ConnectionHealthMonitor(broker="test_broker", ping_interval=0.02, ping_timeout=0.02, max_missed_heartbeats=3, on_stale_detected=on_stale)
        monitor.start_monitoring()
        for _ in range(10):
            await asyncio.sleep(0.01)
            monitor.record_tick()
        self.assertFalse(monitor.is_stale())
        self.assertEqual(monitor.missed_heartbeats, 0)
        monitor.stop_monitoring()

    async def test_e_max_reconnect_attempts(self):
        backoff_durations = []

        async def fake_sleep(duration):
            if duration in WebSocketHandler.BACKOFF_SCHEDULE:
                backoff_durations.append(duration)

        async def always_failing_connect():
            raise NetworkError("Unreachable gateway")

        handler = WebSocketHandler(broker_name="test_broker", connect_coro=always_failing_connect, disconnect_coro=AsyncMock())
        handler.health_monitor.start_monitoring = MagicMock()

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await handler.connect()
            if handler._reconnect_task:
                await handler._reconnect_task

        self.assertEqual(handler.reconnect_attempts, 10)
        self.assertEqual(len(backoff_durations), 10)
        self.assertEqual(backoff_durations, WebSocketHandler.BACKOFF_SCHEDULE)
        self.assertEqual(handler.get_connection_state(), ConnectionState.DISCONNECTED.value)


if __name__ == "__main__":
    unittest.main()
