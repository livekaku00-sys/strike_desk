import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from config.settings import generate_totp
from utils.token_store import InMemoryTokenStore, token_store
from adapters.auth_manager import AuthManager, AuthStatus


class TestAuth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        token_store.clear_all()

    def test_a_totp_generation_with_known_secret(self):
        known_secret = "JBSWY3DPEHPK3PXP"
        totp_code = generate_totp(known_secret)
        self.assertIsInstance(totp_code, str)
        self.assertEqual(len(totp_code), 6)
        self.assertTrue(totp_code.isdigit())

    def test_b_token_expiry_detection(self):
        store = InMemoryTokenStore()
        broker = "test_broker"
        store.store_token(broker=broker, token="fresh_token_123", expires_in_seconds=3600)
        self.assertFalse(store.is_expired(broker, buffer_seconds=300))
        self.assertEqual(store.get_token(broker), "fresh_token_123")

        store.store_token(broker=broker, token="expiring_token_456", expires_in_seconds=200)
        self.assertTrue(store.is_expired(broker, buffer_seconds=300))

        past_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        store.store_token(broker=broker, token="expired_token_789", expires_at=past_time)
        self.assertTrue(store.is_expired(broker, buffer_seconds=300))

        mgr = AuthManager(store=store, expiry_buffer_seconds=300)
        status = mgr.get_auth_status(broker)
        self.assertEqual(status, AuthStatus.TOKEN_EXPIRED.value)

    async def test_c_retry_logic_with_mocked_failures(self):
        store = InMemoryTokenStore()
        mgr = AuthManager(store=store, max_retries=3, base_backoff=0.01, max_backoff=0.05, request_timeout=2.0)
        call_count_success = 0

        async def failing_then_succeeding_auth():
            nonlocal call_count_success
            call_count_success += 1
            if call_count_success < 3:
                raise ConnectionError(f"Temporary network failure #{call_count_success}")
            return True

        result_success = await mgr.execute_with_retry("broker_s1", failing_then_succeeding_auth)
        self.assertTrue(result_success)
        self.assertEqual(call_count_success, 3)

        call_count_fail = 0

        async def always_failing_auth():
            nonlocal call_count_fail
            call_count_fail += 1
            raise TimeoutError("Broker gateway timeout")

        result_fail = await mgr.execute_with_retry("broker_s2", always_failing_auth)
        self.assertFalse(result_fail)
        self.assertEqual(call_count_fail, 3)

    def test_d_verifies_no_tokens_written_to_disk(self):
        tracked_dirs = [os.getcwd(), tempfile.gettempdir()]
        before_files = set()
        for d in tracked_dirs:
            if os.path.exists(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        before_files.add(os.path.join(root, f))

        store = InMemoryTokenStore()
        store.store_token("dhan", "secret_dhan_token_xyz", expires_in_seconds=86400)
        store.store_token("angelone", "secret_angel_jwt_abc", expires_in_seconds=86400)
        store.store_token("fyers", "secret_fyers_bearer_123", expires_in_seconds=86400)

        self.assertEqual(store.get_token("dhan"), "secret_dhan_token_xyz")
        self.assertEqual(store.get_token("angelone"), "secret_angel_jwt_abc")
        self.assertEqual(store.get_token("fyers"), "secret_fyers_bearer_123")

        after_files = set()
        for d in tracked_dirs:
            if os.path.exists(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        after_files.add(os.path.join(root, f))

        new_files = after_files - before_files
        for new_f in new_files:
            if os.path.isfile(new_f):
                try:
                    with open(new_f, "r", errors="ignore") as fp:
                        content = fp.read()
                        self.assertNotIn("secret_dhan_token_xyz", content)
                except Exception:
                    pass

        self.assertTrue(store.has_token("dhan"))
        store.clear_all()
        self.assertFalse(store.has_token("dhan"))


if __name__ == "__main__":
    unittest.main()
