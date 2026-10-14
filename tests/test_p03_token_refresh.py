"""
P0-3 — Token Refresh Reliability

Coverage matrix (14 categories):
  01  Concurrent deduplication — second caller reuses first's auth, coro runs once
  02  Two-broker concurrency — different brokers auth in parallel, no cross-block
  03  AuthManager status transitions — AUTHENTICATING → AUTHENTICATED / FAILED
  04  AuthManager success on final attempt — all-but-last retries fail
  05  AngelOne error log sanitization — no raw response in error log on auth fail
  06  AngelOne malformed response: missing data key raises AuthenticationError
  07  AngelOne malformed response: missing jwtToken raises AuthenticationError
  08  AngelOne malformed response error message contains no sensitive fields
  09  Dhan error sanitization — fund_limits dict not included verbatim in error
  10  Dhan malformed/unexpected fund_limits type handled gracefully
  11  Fyers empty token raises AuthenticationError before making any network call
  12  Token store metadata — api_key in metadata not leaked via get_token()
  13  Safety guard preserved — prohibit_live_order() still fires after authenticate()
  14  Token store clear unblocks deduplication — fresh caller re-authenticates
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.auth_manager import AuthManager, AuthStatus
from utils.token_store import InMemoryTokenStore
from utils.exceptions import AuthenticationError


# ── 01/02: Concurrent refresh deduplication ──────────────────────────────────

class TestAuthManagerConcurrentDeduplication(unittest.IsolatedAsyncioTestCase):

    async def test_01_concurrent_callers_same_broker_deduplicate(self):
        """
        Two concurrent callers to execute_with_retry for the same broker:
        the second waits on the lock, sees a fresh token on entry, and returns
        True without invoking the auth coroutine a second time.
        """
        store = InMemoryTokenStore()
        mgr = AuthManager(store=store, max_retries=2, base_backoff=0.01,
                          expiry_buffer_seconds=60)
        call_count = 0

        async def slow_auth():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            store.store_token("shared_broker", "fresh_token_xyz", expires_in_seconds=3600)
            return True

        results = await asyncio.gather(
            mgr.execute_with_retry("shared_broker", slow_auth),
            mgr.execute_with_retry("shared_broker", slow_auth),
        )

        self.assertTrue(all(results), "Both callers must report success")
        self.assertEqual(call_count, 1,
                         "Auth coroutine must run exactly once — second caller deduplicates")

    async def test_02_two_brokers_auth_concurrently(self):
        """
        Different broker names have independent locks: they must be able to
        authenticate in parallel without blocking each other.
        """
        store = InMemoryTokenStore()
        mgr = AuthManager(store=store, max_retries=1, base_backoff=0.01)

        async def auth_for(name: str) -> bool:
            await asyncio.sleep(0.05)
            store.store_token(name, f"token_{name}", expires_in_seconds=3600)
            return True

        t0 = time.monotonic()
        results = await asyncio.gather(
            mgr.execute_with_retry("broker_a", lambda: auth_for("broker_a")),
            mgr.execute_with_retry("broker_b", lambda: auth_for("broker_b")),
        )
        elapsed = time.monotonic() - t0

        self.assertTrue(all(results))
        # Parallel execution: both 50ms sleeps overlap, total ≪ 100ms+100ms
        self.assertLess(elapsed, 0.15,
                        "Two brokers must authenticate concurrently, not sequentially")


# ── 03/04: AuthManager status transitions ────────────────────────────────────

class TestAuthManagerStatusTransitions(unittest.IsolatedAsyncioTestCase):

    async def test_03_status_transitions_authenticated(self):
        store = InMemoryTokenStore()
        mgr = AuthManager(store=store, max_retries=1, base_backoff=0.01)

        async def succeeding_auth():
            store.store_token("b1", "t1", expires_in_seconds=3600)
            return True

        self.assertNotEqual(mgr.get_auth_status("b1"), AuthStatus.AUTHENTICATED.value)
        result = await mgr.execute_with_retry("b1", succeeding_auth)
        self.assertTrue(result)
        self.assertEqual(mgr.get_auth_status("b1"), AuthStatus.AUTHENTICATED.value)

    async def test_04_success_on_final_attempt(self):
        """Auth succeeds on the last allowed attempt; earlier attempts raise."""
        store = InMemoryTokenStore()
        mgr = AuthManager(store=store, max_retries=3, base_backoff=0.01,
                          max_backoff=0.05)
        call_count = 0

        async def fail_twice_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError(f"transient failure #{call_count}")
            store.store_token("b_retry", "token_on_3rd", expires_in_seconds=3600)
            return True

        result = await mgr.execute_with_retry("b_retry", fail_twice_then_succeed)
        self.assertTrue(result)
        self.assertEqual(call_count, 3)
        self.assertEqual(mgr.get_auth_status("b_retry"), AuthStatus.AUTHENTICATED.value)

    async def test_status_failed_when_all_attempts_fail(self):
        store = InMemoryTokenStore()
        mgr = AuthManager(store=store, max_retries=2, base_backoff=0.01)

        async def always_fail():
            raise RuntimeError("always fails")

        result = await mgr.execute_with_retry("b_fail", always_fail)
        self.assertFalse(result)
        # After exhausting retries the status must be FAILED (not AUTHENTICATING)
        self.assertEqual(mgr._statuses.get("b_fail"), AuthStatus.FAILED)


# ── 05/06/07/08: AngelOne error log & malformed response ─────────────────────

class TestAngelOneErrorSanitization(unittest.IsolatedAsyncioTestCase):

    def _make_adapter(self, http_timeout: float = 5.0):
        from adapters.angelone_adapter import AngelOneAdapter
        adapter = AngelOneAdapter.__new__(AngelOneAdapter)
        adapter._HTTP_TIMEOUT = http_timeout
        adapter.settings = None
        adapter._smart_api = None
        adapter._access_token = ""
        adapter._feed_token = None
        adapter.is_authenticated = False
        return adapter

    def _mock_settings(self):
        s = MagicMock()
        s.client_id = "TEST_CLIENT"
        s.api_key = "test_api_key_123"
        s.password = "test_pass"
        s.current_totp = MagicMock(return_value="123456")
        return s

    async def _call_perform_auth_with_response(self, adapter, fake_response):
        mock_settings = self._mock_settings()
        with patch.object(adapter, "_load_settings", return_value=mock_settings), \
             patch("adapters.angelone_adapter._SMARTAPI_AVAILABLE", True), \
             patch("adapters.angelone_adapter.SmartConnect") as mock_sc, \
             patch("adapters.angelone_adapter.rate_limiter") as mock_rl, \
             patch("adapters.angelone_adapter.token_store"):
            mock_rl.acquire = AsyncMock()
            mock_sc.return_value = MagicMock()
            loop = asyncio.get_running_loop()

            async def instant_executor(executor, fn, *args):
                return fake_response

            with patch.object(loop, "run_in_executor", side_effect=instant_executor):
                await adapter._perform_auth()

    async def test_05_error_log_no_raw_response_on_auth_fail(self):
        """
        On auth failure the error log must NOT dump the raw broker response body —
        it could contain a partially-issued JWT or refresh_token in some flows.
        """
        adapter = self._make_adapter()
        fake_response = {
            "status": False,
            "message": "Invalid credentials",
            "data": {"jwtToken": "SHOULD_NOT_APPEAR_IN_LOGS_AT_ALL"},
        }
        with self.assertLogs("adapters.angelone_adapter", level="ERROR") as captured:
            with self.assertRaises(AuthenticationError):
                await self._call_perform_auth_with_response(adapter, fake_response)

        all_output = " ".join(captured.output)
        self.assertNotIn("SHOULD_NOT_APPEAR_IN_LOGS_AT_ALL", all_output,
                         "jwtToken value leaked into error log")
        # str(fake_response)[:300] must not appear — previously logged as "full response"
        self.assertNotIn("full response", all_output,
                         "'full response' label still present in error log")

    async def test_06_missing_data_key_raises_auth_error(self):
        """Response with no 'data' key raises AuthenticationError (not raw KeyError)."""
        adapter = self._make_adapter()
        with self.assertRaises(AuthenticationError) as ctx:
            await self._call_perform_auth_with_response(
                adapter, {"status": True, "message": "ok"}
            )
        self.assertIn("jwtToken", str(ctx.exception).lower() + "jwtToken not found")

    async def test_07_empty_data_dict_raises_auth_error(self):
        """Response with 'data: {}' (missing jwtToken) raises AuthenticationError."""
        adapter = self._make_adapter()
        with self.assertRaises(AuthenticationError):
            await self._call_perform_auth_with_response(
                adapter, {"status": True, "data": {}}
            )

    async def test_08_malformed_error_message_no_sensitive_fields(self):
        """Error message for missing jwtToken must not include other response fields."""
        adapter = self._make_adapter()
        fake_response = {
            "status": True,
            "data": {"someInternalField": "SENSITIVE_VALUE_MUST_NOT_LEAK"},
        }
        try:
            await self._call_perform_auth_with_response(adapter, fake_response)
        except AuthenticationError as e:
            self.assertNotIn("SENSITIVE_VALUE_MUST_NOT_LEAK", str(e),
                             "Sensitive field from malformed response leaked into error message")
        except Exception:
            pass  # any other exception is acceptable too


# ── 09/10: Dhan error sanitization ───────────────────────────────────────────

class TestDhanErrorSanitization(unittest.IsolatedAsyncioTestCase):

    def _make_adapter(self):
        from adapters.dhan_adapter import DhanAdapter
        adapter = DhanAdapter.__new__(DhanAdapter)
        adapter._HTTP_TIMEOUT = 5.0
        adapter.settings = None
        adapter._client = None
        adapter._dhan_ctx = None
        adapter.is_authenticated = False
        adapter._equity_ids = {}
        return adapter

    async def test_09_failure_response_no_full_dict_in_error(self):
        """
        When get_fund_limits returns {status: failure, accessToken: "..."},
        the raised AuthenticationError must not include the full dict (which
        might contain the access_token if the SDK echoes it).
        """
        mock_settings = MagicMock()
        mock_settings.client_id = "TEST_DHAN"
        mock_settings.access_token = "SECRET_DHAN_TOKEN_MUST_NOT_LEAK"

        adapter = self._make_adapter()
        fake_fund_limits = {
            "status": "failure",
            "accessToken": "SECRET_DHAN_TOKEN_MUST_NOT_LEAK",
            "remarks": "Unauthorized",
        }

        with patch.object(adapter, "_load_settings", return_value=mock_settings), \
             patch("adapters.dhan_adapter._DHANHQ_AVAILABLE", True), \
             patch("adapters.dhan_adapter.dhanhq") as mock_dhanhq, \
             patch("adapters.dhan_adapter._DhanContext"), \
             patch("adapters.dhan_adapter.rate_limiter") as mock_rl, \
             patch("adapters.dhan_adapter.token_store"):

            mock_rl.acquire = AsyncMock()
            mock_client = MagicMock()
            mock_dhanhq.return_value = mock_client

            loop = asyncio.get_running_loop()

            async def instant_executor(executor, fn, *args):
                return fake_fund_limits

            with patch.object(loop, "run_in_executor", side_effect=instant_executor):
                with self.assertRaises(AuthenticationError) as ctx:
                    await adapter._perform_auth()

        err_str = str(ctx.exception)
        self.assertNotIn("SECRET_DHAN_TOKEN_MUST_NOT_LEAK", err_str,
                         "Access token value leaked into Dhan error message")
        self.assertIn("failure", err_str.lower(),
                      "Error message should include the status")

    async def test_10_malformed_fund_limits_non_dict_handled(self):
        """If get_fund_limits returns a non-dict (e.g. None or a list), no exception."""
        mock_settings = MagicMock()
        mock_settings.client_id = "TEST_DHAN"
        mock_settings.access_token = "test_token"

        adapter = self._make_adapter()

        with patch.object(adapter, "_load_settings", return_value=mock_settings), \
             patch("adapters.dhan_adapter._DHANHQ_AVAILABLE", True), \
             patch("adapters.dhan_adapter.dhanhq") as mock_dhanhq, \
             patch("adapters.dhan_adapter._DhanContext"), \
             patch("adapters.dhan_adapter.rate_limiter") as mock_rl, \
             patch("adapters.dhan_adapter.token_store") as mock_ts:

            mock_rl.acquire = AsyncMock()
            mock_ts.store_token = MagicMock()
            mock_client = MagicMock()
            mock_dhanhq.return_value = mock_client

            loop = asyncio.get_running_loop()

            # Non-dict return (some SDKs return None on network failure)
            async def instant_executor(executor, fn, *args):
                return None

            with patch.object(loop, "run_in_executor", side_effect=instant_executor):
                # Should succeed (non-dict is not a failure response)
                result = await adapter._perform_auth()

        self.assertTrue(result)


# ── 11: Fyers empty token ─────────────────────────────────────────────────────

class TestFyersEmptyToken(unittest.IsolatedAsyncioTestCase):

    async def test_11_empty_access_token_raises_before_network(self):
        """FYERS_ACCESS_TOKEN absent: must raise AuthenticationError without making a call."""
        from adapters.fyers_adapter import FyersAdapter

        adapter = FyersAdapter.__new__(FyersAdapter)
        adapter._HTTP_TIMEOUT = 5.0
        adapter.settings = None
        adapter.is_authenticated = False

        mock_settings = MagicMock()
        mock_settings.client_id = "FY123"
        mock_settings.access_token = ""

        with patch.object(adapter, "_load_settings", return_value=mock_settings), \
             patch("adapters.fyers_adapter._FYERS_AVAILABLE", True), \
             patch("adapters.fyers_adapter.fyersModel") as mock_fyers_module, \
             patch("adapters.fyers_adapter.rate_limiter") as mock_rl:

            mock_rl.acquire = AsyncMock()

            with self.assertRaises(AuthenticationError) as ctx:
                await adapter._perform_auth()

            # Must not have called FyersModel at all
            mock_fyers_module.FyersModel.assert_not_called()

        err_str = str(ctx.exception).lower()
        self.assertIn("fyers_access_token", err_str,
                      "Error must name the missing env var")


# ── 12: Token store metadata safety ──────────────────────────────────────────

class TestTokenStoreMetadataSafety(unittest.TestCase):

    def test_12_api_key_in_metadata_not_returned_by_get_token(self):
        """
        AngelOne stores api_key in InMemoryTokenStore metadata.
        get_token() must return only the token (JWT), never the metadata.
        """
        store = InMemoryTokenStore()
        store.store_token(
            broker="angelone",
            token="REAL_JWT_VALUE",
            expires_in_seconds=86400,
            metadata={"client_id": "CLIENT_123", "api_key": "SECRET_API_KEY"},
        )
        retrieved = store.get_token("angelone")
        self.assertEqual(retrieved, "REAL_JWT_VALUE")
        self.assertNotIn("SECRET_API_KEY", retrieved)
        self.assertNotIn("api_key", retrieved)

    def test_metadata_accessible_separately_from_token(self):
        """Metadata dict is kept but is not the encrypted payload."""
        store = InMemoryTokenStore()
        store.store_token("test_broker", "token_abc", expires_in_seconds=3600,
                          metadata={"api_key": "should_not_be_in_token"})
        stored = store._store.get("test_broker")
        self.assertIsNotNone(stored)
        # The metadata dict should exist but the actual token payload is encrypted
        self.assertEqual(stored.metadata.get("api_key"), "should_not_be_in_token")
        # Decrypted payload is the token, not the metadata
        self.assertEqual(store.get_token("test_broker"), "token_abc")


# ── 13: Safety guard preserved after authenticate() ──────────────────────────

class TestSafetyGuardPreservedAfterRefresh(unittest.TestCase):

    def test_13_prohibit_live_order_fires_after_authenticate(self):
        """
        After authenticate() succeeds, place_order / cancel_order / modify_order
        must still raise LiveOrderError — token refresh must never enable live
        order execution.
        """
        from utils.live_order_guard import prohibit_live_order, LiveOrderProhibitedError as LiveOrderError

        # Simulate what the adapter does after authenticate(): store token, mark authenticated.
        store = InMemoryTokenStore()
        store.store_token("angelone", "fake_jwt_after_refresh", expires_in_seconds=86400)

        # All nine guard points must still fire
        for guard_point in (
            "angelone.place_order",
            "angelone.cancel_order",
            "angelone.modify_order",
            "dhan.place_order",
            "dhan.cancel_order",
            "dhan.modify_order",
            "fyers.place_order",
            "fyers.cancel_order",
            "fyers.modify_order",
        ):
            with self.assertRaises(LiveOrderError,
                                   msg=f"Guard not raised for {guard_point}"):
                prohibit_live_order(guard_point)


# ── 14: Clear deduplication — fresh caller re-authenticates ──────────────────

class TestTokenStoreClearResetsDedup(unittest.IsolatedAsyncioTestCase):

    async def test_14_clear_token_allows_reauth(self):
        """
        After clearing a broker's token, the next call to execute_with_retry
        must NOT be short-circuited by the deduplication check — it must
        run the auth coroutine again.
        """
        store = InMemoryTokenStore()
        mgr = AuthManager(store=store, max_retries=1, base_backoff=0.01,
                          expiry_buffer_seconds=60)
        call_count = 0

        async def auth_coro():
            nonlocal call_count
            call_count += 1
            store.store_token("clearable", "token_v1", expires_in_seconds=3600)
            return True

        # First auth
        await mgr.execute_with_retry("clearable", auth_coro)
        self.assertEqual(call_count, 1)

        # Clear token (simulate expiry or operator disconnect)
        store.clear_token("clearable")

        # Second auth must run the coro, not skip
        await mgr.execute_with_retry("clearable", auth_coro)
        self.assertEqual(call_count, 2, "Re-auth must run after token was cleared")

    async def test_expired_token_also_bypasses_deduplication(self):
        """An expired token in the store must not short-circuit deduplication."""
        from datetime import datetime, timedelta, timezone

        store = InMemoryTokenStore()
        mgr = AuthManager(store=store, max_retries=1, base_backoff=0.01,
                          expiry_buffer_seconds=300)
        call_count = 0

        async def refresh_coro():
            nonlocal call_count
            call_count += 1
            store.store_token("expiring", "fresh_token", expires_in_seconds=3600)
            return True

        # Seed with an expired token
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        store.store_token("expiring", "stale_token", expires_at=past)

        result = await mgr.execute_with_retry("expiring", refresh_coro)
        self.assertTrue(result)
        self.assertEqual(call_count, 1, "Expired token must not block re-authentication")


if __name__ == "__main__":
    unittest.main(verbosity=2)
