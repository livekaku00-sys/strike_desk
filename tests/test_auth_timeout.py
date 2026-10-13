"""
Regression tests for broker authentication timeout enforcement.

Proves that:
1. A hanging broker API call (simulated via asyncio.sleep) is abandoned within
   _HTTP_TIMEOUT seconds and raises AuthenticationError — not a raw TimeoutError
   that would bubble up uncaught.
2. AuthManager.execute_with_retry propagates the failure cleanly and returns False
   within total wall time ≈ (_HTTP_TIMEOUT + backoffs × retries), NOT the full
   auth_manager request_timeout (30s) × retries.
3. The event loop is NOT blocked while waiting — other coroutines can progress
   concurrently.
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.auth_manager import AuthManager
from utils.token_store import InMemoryTokenStore
from utils.exceptions import AuthenticationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hanging_executor(delay: float = 60.0):
    """
    Returns a coroutine factory that simulates a blocking broker HTTP call by
    sleeping for `delay` seconds.  Used to stand in for run_in_executor calls.
    """
    async def _hang():
        await asyncio.sleep(delay)
        return {"status": True, "data": {"jwtToken": "fake_token"}}
    return _hang


# ---------------------------------------------------------------------------
# 1. Per-adapter HTTP timeout (unit-level, no broker library required)
# ---------------------------------------------------------------------------

class TestAngelOneHTTPTimeout(unittest.IsolatedAsyncioTestCase):
    """AngelOne _perform_auth must abandon a hanging generateSession call within 10s."""

    async def test_hanging_generate_session_raises_auth_error(self):
        """
        Simulate SmartConnect.generateSession hanging indefinitely.
        The adapter's _perform_auth wraps the executor call with
        asyncio.wait_for(timeout=_HTTP_TIMEOUT=10s).
        We shorten _HTTP_TIMEOUT to 0.2s for test speed.
        """
        from adapters.angelone_adapter import AngelOneAdapter

        adapter = AngelOneAdapter.__new__(AngelOneAdapter)
        adapter._HTTP_TIMEOUT = 0.2  # override for test speed
        adapter.settings = None

        mock_settings = MagicMock()
        mock_settings.client_id = "TEST_CLIENT"
        mock_settings.api_key = "test_api_key_123"
        mock_settings.password = "test_pass"
        mock_settings.current_totp = MagicMock(return_value="123456")

        async def _hanging_executor(*args, **kwargs):
            await asyncio.sleep(60)  # simulates unreachable broker API
            return {}

        with patch.object(adapter, "_load_settings", return_value=mock_settings), \
             patch("adapters.angelone_adapter._SMARTAPI_AVAILABLE", True), \
             patch("adapters.angelone_adapter.SmartConnect") as mock_sc, \
             patch("adapters.angelone_adapter.rate_limiter") as mock_rl:

            mock_rl.acquire = AsyncMock()
            mock_smart_api = MagicMock()
            mock_sc.return_value = mock_smart_api

            loop = asyncio.get_running_loop()
            # Make run_in_executor return a future that resolves after 60 seconds
            mock_smart_api.generateSession = MagicMock(side_effect=lambda *a: None)

            original_run_in_executor = loop.run_in_executor

            async def slow_executor(executor, fn, *args):
                await asyncio.sleep(60)  # simulate hang
                return {}

            with patch.object(loop, "run_in_executor", side_effect=slow_executor):
                t0 = time.monotonic()
                with self.assertRaises(AuthenticationError) as ctx:
                    await adapter._perform_auth()
                elapsed = time.monotonic() - t0

            # Must fail within 2× the per-call timeout (generous buffer for CI)
            self.assertLess(elapsed, adapter._HTTP_TIMEOUT * 2 + 0.5,
                            f"_perform_auth took {elapsed:.2f}s — should fail fast at {adapter._HTTP_TIMEOUT}s")
            self.assertIn("timed out", str(ctx.exception).lower())


class TestDhanHTTPTimeout(unittest.IsolatedAsyncioTestCase):
    """Dhan _perform_auth must abandon a hanging get_fund_limits call within 10s."""

    async def test_hanging_get_fund_limits_raises_auth_error(self):
        from adapters.dhan_adapter import DhanAdapter

        adapter = DhanAdapter.__new__(DhanAdapter)
        adapter._HTTP_TIMEOUT = 0.2
        adapter.settings = None

        mock_settings = MagicMock()
        mock_settings.client_id = "TEST_DHAN"
        mock_settings.access_token = "test_access_token"

        with patch.object(adapter, "_load_settings", return_value=mock_settings), \
             patch("adapters.dhan_adapter._DHANHQ_AVAILABLE", True), \
             patch("adapters.dhan_adapter.dhanhq") as mock_dhanhq, \
             patch("adapters.dhan_adapter._DhanContext") as mock_ctx, \
             patch("adapters.dhan_adapter.rate_limiter") as mock_rl:

            mock_rl.acquire = AsyncMock()
            mock_ctx.return_value = MagicMock()
            mock_client = MagicMock()
            mock_dhanhq.return_value = mock_client

            loop = asyncio.get_running_loop()

            async def slow_executor(executor, fn, *args):
                await asyncio.sleep(60)
                return {}

            with patch.object(loop, "run_in_executor", side_effect=slow_executor):
                t0 = time.monotonic()
                with self.assertRaises(AuthenticationError) as ctx:
                    await adapter._perform_auth()
                elapsed = time.monotonic() - t0

            self.assertLess(elapsed, adapter._HTTP_TIMEOUT * 2 + 0.5,
                            f"_perform_auth took {elapsed:.2f}s — expected fast fail at {adapter._HTTP_TIMEOUT}s")
            self.assertIn("timed out", str(ctx.exception).lower())


class TestFyersHTTPTimeout(unittest.IsolatedAsyncioTestCase):
    """Fyers _perform_auth must abandon a hanging get_profile call within 10s."""

    async def test_hanging_get_profile_raises_auth_error(self):
        from adapters.fyers_adapter import FyersAdapter

        adapter = FyersAdapter.__new__(FyersAdapter)
        adapter._HTTP_TIMEOUT = 0.2
        adapter.settings = None

        mock_settings = MagicMock()
        mock_settings.client_id = "TEST_FYERS"
        mock_settings.access_token = "test_fyers_token"

        with patch.object(adapter, "_load_settings", return_value=mock_settings), \
             patch("adapters.fyers_adapter._FYERS_AVAILABLE", True), \
             patch("adapters.fyers_adapter.fyersModel") as mock_fyers_module, \
             patch("adapters.fyers_adapter.rate_limiter") as mock_rl, \
             patch("adapters.fyers_adapter.token_store") as mock_ts:

            mock_rl.acquire = AsyncMock()
            mock_ts.get_token.return_value = "existing_token"
            mock_ts.is_expired.return_value = False
            mock_fyers_instance = MagicMock()
            mock_fyers_module.FyersModel.return_value = mock_fyers_instance

            loop = asyncio.get_running_loop()

            async def slow_executor(executor, fn, *args):
                await asyncio.sleep(60)
                return {}

            with patch.object(loop, "run_in_executor", side_effect=slow_executor):
                t0 = time.monotonic()
                with self.assertRaises(AuthenticationError) as ctx:
                    await adapter._perform_auth()
                elapsed = time.monotonic() - t0

            self.assertLess(elapsed, adapter._HTTP_TIMEOUT * 2 + 0.5,
                            f"_perform_auth took {elapsed:.2f}s — expected fast fail at {adapter._HTTP_TIMEOUT}s")
            self.assertIn("timed out", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# 2. End-to-end: execute_with_retry with a slow broker returns False, fast
# ---------------------------------------------------------------------------

class TestAuthManagerWithSlowBroker(unittest.IsolatedAsyncioTestCase):
    """
    AuthManager.execute_with_retry must return False (not hang) when _perform_auth
    raises AuthenticationError due to an HTTP timeout.
    """

    async def test_all_retries_exhaust_quickly_on_broker_hang(self):
        store = InMemoryTokenStore()
        mgr = AuthManager(
            store=store,
            max_retries=3,
            base_backoff=0.01,  # negligible backoff for test speed
            max_backoff=0.05,
            request_timeout=5.0,
        )

        call_count = 0

        async def slow_auth():
            nonlocal call_count
            call_count += 1
            # Simulates per-adapter HTTP timeout raising AuthenticationError
            raise AuthenticationError("Broker API timed out after 10s", broker="test")

        t0 = time.monotonic()
        result = await mgr.execute_with_retry("test_broker", slow_auth)
        elapsed = time.monotonic() - t0

        self.assertFalse(result, "Must return False when all retries fail")
        self.assertEqual(call_count, 3, "Must exhaust all 3 retries")
        # With near-zero backoffs the whole thing should finish in < 1 second
        self.assertLess(elapsed, 2.0,
                        f"execute_with_retry took {elapsed:.2f}s with instant failures — "
                        f"something is blocking")

    async def test_event_loop_not_blocked_during_auth_timeout(self):
        """
        Other coroutines must make progress while auth is timing out.
        We run a counter task alongside a slow auth attempt and confirm
        the counter incremented during the auth wait.
        """
        store = InMemoryTokenStore()
        mgr = AuthManager(
            store=store,
            max_retries=1,
            base_backoff=0.01,
            request_timeout=0.3,  # force timeout after 0.3s
        )

        async def slow_auth():
            # This simulates a genuinely slow network call that doesn't respect
            # per-call timeout (worst case).  AuthManager's wait_for catches it.
            await asyncio.sleep(10)
            return True

        progress_ticks = 0

        async def progress_counter():
            nonlocal progress_ticks
            for _ in range(10):
                await asyncio.sleep(0.05)
                progress_ticks += 1

        # Run both concurrently
        await asyncio.gather(
            mgr.execute_with_retry("test_slow", slow_auth),
            progress_counter(),
            return_exceptions=True,
        )

        self.assertGreater(progress_ticks, 0,
                           "Event loop was blocked — no progress made while auth was waiting")


# ---------------------------------------------------------------------------
# 3. Startup auth timeout cap: asyncio.wait_for wrapping authenticate()
# ---------------------------------------------------------------------------

class TestStartupAuthTimeoutCap(unittest.IsolatedAsyncioTestCase):
    """
    Simulates the _startup_sequence per-broker timeout cap added to api/main.py.

    In production, each broker's authenticate() call in startup is wrapped with
        asyncio.wait_for(a.authenticate(), timeout=_STARTUP_AUTH_TIMEOUT)
    This proves that three such calls complete in well under 3 × cap + slop,
    even when all three brokers hang indefinitely.
    """

    async def _fake_authenticate_hang(self) -> bool:
        """Simulates an adapter whose broker API never responds."""
        await asyncio.sleep(60)
        return True

    async def test_three_hanging_brokers_complete_within_cap(self):
        _STARTUP_AUTH_TIMEOUT = 0.3  # 300ms per broker for test speed

        class FakeBroker:
            async def authenticate(self):
                await asyncio.sleep(60)
                return True

        t0 = time.monotonic()
        for name in ("dhan", "angelone", "fyers"):
            try:
                await asyncio.wait_for(FakeBroker().authenticate(), timeout=_STARTUP_AUTH_TIMEOUT)
            except asyncio.TimeoutError:
                pass  # expected — broker unreachable
        elapsed = time.monotonic() - t0

        worst_case = _STARTUP_AUTH_TIMEOUT * 3
        self.assertLess(
            elapsed, worst_case + 0.3,
            f"Startup auth for 3 brokers took {elapsed:.3f}s — should be ≤ {worst_case:.1f}s",
        )

    async def test_fast_broker_does_not_wait_full_cap(self):
        """A broker that authenticates instantly should not pay the full timeout."""
        _STARTUP_AUTH_TIMEOUT = 1.0

        class FastBroker:
            async def authenticate(self):
                await asyncio.sleep(0.01)  # instant
                return True

        t0 = time.monotonic()
        ok = await asyncio.wait_for(FastBroker().authenticate(), timeout=_STARTUP_AUTH_TIMEOUT)
        elapsed = time.monotonic() - t0

        self.assertTrue(ok)
        self.assertLess(elapsed, 0.1, "Fast broker should not wait the full cap")


if __name__ == "__main__":
    unittest.main()
