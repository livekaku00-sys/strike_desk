"""
P0-05 — Emergency Halt Semantics tests

Coverage:
- Disk persistence survives simulated process restart (tmp-then-rename)
- STOP ENTRY blocks new orders (SOFT_HALT) without a confirmation token
- FLATTEN ALL strictly requires a valid, unexpired confirmation token (PermissionError otherwise)
- Expired token raises PermissionError
- Wrong operator raises PermissionError
- Token is single-use (consumed on successful flatten_all)
- HARD_HALT is not exposed in get_operator_status()
- Existing methods (trigger_halt, request_acknowledgment_token, acknowledge_and_reset) unaffected
- Thread safety: concurrent stop_entry calls converge safely
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from risk.emergency_halt import EmergencyHaltManager, HaltLevel, HaltEvent

IST = pytz.timezone("Asia/Kolkata")


def _make_manager(db_path=None) -> EmergencyHaltManager:
    """Return a fresh in-memory manager (no disk side-effects) unless db_path given."""
    return EmergencyHaltManager(db_path=db_path)


class TestDiskPersistence(unittest.TestCase):
    """Halt state survives a simulated process kill / restart."""

    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".json")

    def tearDown(self):
        for p in (self.tmp, self.tmp + ".tmp"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def test_halted_state_survives_restart(self):
        m1 = _make_manager(db_path=self.tmp)
        m1.trigger_halt("network fault detected", level=HaltLevel.SOFT_HALT, triggered_by="RMS")
        self.assertTrue(os.path.exists(self.tmp))

        # Simulate restart: fresh instance loads from same path
        m2 = _make_manager(db_path=self.tmp)
        self.assertTrue(m2.is_halted, "Restarted manager must detect persisted halt")
        self.assertEqual(m2.current_level, HaltLevel.SOFT_HALT)
        self.assertEqual(m2.current_reason, "network fault detected")

    def test_cleared_state_survives_restart(self):
        m1 = _make_manager(db_path=self.tmp)
        m1.trigger_halt("test halt")
        token = m1.request_acknowledgment_token("op1")
        m1.acknowledge_and_reset("op1", token)
        self.assertFalse(m1.is_halted)

        # Persisted state must show is_halted=False
        with open(self.tmp) as f:
            state = json.load(f)
        self.assertFalse(state["is_halted"])

        m2 = _make_manager(db_path=self.tmp)
        self.assertFalse(m2.is_halted, "Restarted manager must start clean after reset")

    def test_persist_uses_tmp_then_rename(self):
        """The .tmp file must not persist after a successful write."""
        m = _make_manager(db_path=self.tmp)
        m.trigger_halt("atomic write test")
        self.assertFalse(os.path.exists(self.tmp + ".tmp"),
                         ".tmp file must be renamed away after persist_halt_state")
        self.assertTrue(os.path.exists(self.tmp))

    def test_load_missing_file_returns_false(self):
        m = _make_manager()
        result = m.load_halt_state("/nonexistent/path/halt.json")
        self.assertFalse(result)
        self.assertFalse(m.is_halted)

    def test_load_corrupt_file_returns_false(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{ invalid json @@@@")
            bad_path = f.name
        try:
            m = _make_manager()
            result = m.load_halt_state(bad_path)
            self.assertFalse(result)
        finally:
            os.remove(bad_path)

    def test_explicit_persist_and_load(self):
        """persist_halt_state / load_halt_state can be called manually with an explicit path."""
        m1 = _make_manager()  # no auto-persist
        m1.trigger_halt("manual persist test", level=HaltLevel.FULL_KILL_SWITCH, triggered_by="OPS")
        m1.persist_halt_state(self.tmp)

        m2 = _make_manager()
        m2.load_halt_state(self.tmp)
        self.assertTrue(m2.is_halted)
        self.assertEqual(m2.current_level, HaltLevel.FULL_KILL_SWITCH)
        self.assertEqual(m2.current_reason, "manual persist test")


class TestStopEntry(unittest.TestCase):
    """stop_entry() maps to SOFT_HALT and requires no confirmation token."""

    def test_stop_entry_sets_soft_halt(self):
        m = _make_manager()
        event = m.stop_entry("risk limit breached", operator_id="admin")
        self.assertTrue(m.is_halted)
        self.assertEqual(m.current_level, HaltLevel.SOFT_HALT)
        self.assertEqual(event.triggered_by, "admin")
        self.assertEqual(event.reason, "risk limit breached")

    def test_stop_entry_no_token_required(self):
        """stop_entry must succeed without any flatten confirmation token."""
        m = _make_manager()
        # No token setup — must still work
        event = m.stop_entry("market disruption")
        self.assertTrue(m.is_halted)
        self.assertIsNotNone(event.event_id)

    def test_stop_entry_operator_status_shows_stop_entry(self):
        m = _make_manager()
        m.stop_entry("test")
        s = m.get_operator_status()
        self.assertTrue(s["is_halted"])
        self.assertEqual(s["halt_type"], "STOP_ENTRY")

    def test_stop_entry_get_status_pending_ack(self):
        m = _make_manager()
        m.stop_entry("test")
        s = m.get_status()
        self.assertTrue(s["pending_acknowledgment"])
        self.assertEqual(s["current_level"], "SOFT_HALT")


class TestFlattenAll(unittest.TestCase):
    """flatten_all() requires a valid, unexpired confirmation token."""

    def test_flatten_all_succeeds_with_valid_token(self):
        m = _make_manager()
        token = m.request_flatten_confirmation_token("admin")
        event = m.flatten_all(operator_id="admin", token=token, reason="emergency flatten")
        self.assertTrue(m.is_halted)
        self.assertEqual(m.current_level, HaltLevel.FULL_KILL_SWITCH)
        self.assertEqual(event.triggered_by, "admin")

    def test_flatten_all_rejected_without_token(self):
        m = _make_manager()
        with self.assertRaises(PermissionError):
            m.flatten_all(operator_id="admin", token="")

    def test_flatten_all_rejected_with_wrong_token(self):
        m = _make_manager()
        m.request_flatten_confirmation_token("admin")
        with self.assertRaises(PermissionError):
            m.flatten_all(operator_id="admin", token="FLAT-ADMIN-WRONGTOKEN")

    def test_flatten_all_rejected_with_operator_mismatch(self):
        m = _make_manager()
        token = m.request_flatten_confirmation_token("admin")
        with self.assertRaises(PermissionError):
            m.flatten_all(operator_id="other_user", token=token)

    def test_flatten_all_rejected_after_expiry(self):
        m = _make_manager()
        token = m.request_flatten_confirmation_token("admin")
        # Wind the expiry back to the past
        m._flatten_token_expiry = datetime.now(IST) - timedelta(seconds=1)
        with self.assertRaises(PermissionError) as ctx:
            m.flatten_all(operator_id="admin", token=token)
        self.assertIn("expired", str(ctx.exception).lower())

    def test_flatten_all_token_is_single_use(self):
        """After a successful flatten_all, the same token must be rejected."""
        m = _make_manager()
        token = m.request_flatten_confirmation_token("admin")
        m.flatten_all(operator_id="admin", token=token)
        # Reset the halt so we can try again
        ack = m.request_acknowledgment_token("admin")
        m.acknowledge_and_reset("admin", ack)
        # Try to reuse the consumed token
        token2 = m.request_flatten_confirmation_token("admin")
        with self.assertRaises(PermissionError):
            m.flatten_all(operator_id="admin", token=token)  # original (consumed) token

    def test_flatten_token_invalidated_on_expiry_check(self):
        """Expiry check must consume the token so it cannot be replayed."""
        m = _make_manager()
        token = m.request_flatten_confirmation_token("admin")
        m._flatten_token_expiry = datetime.now(IST) - timedelta(seconds=1)
        try:
            m.flatten_all(operator_id="admin", token=token)
        except PermissionError:
            pass
        # Internal token state must be cleared
        self.assertIsNone(m._flatten_token)
        self.assertIsNone(m._flatten_token_expiry)

    def test_request_flatten_token_rejects_empty_operator(self):
        m = _make_manager()
        with self.assertRaises(ValueError):
            m.request_flatten_confirmation_token("")

    def test_flatten_token_has_correct_prefix(self):
        m = _make_manager()
        token = m.request_flatten_confirmation_token("ops_user")
        self.assertTrue(token.startswith("FLAT-OPS_USER-"),
                        f"Token must start with 'FLAT-OPS_USER-', got: {token}")

    def test_operator_status_shows_flatten_all(self):
        m = _make_manager()
        token = m.request_flatten_confirmation_token("admin")
        m.flatten_all(operator_id="admin", token=token)
        s = m.get_operator_status()
        self.assertEqual(s["halt_type"], "FLATTEN_ALL")


class TestHardHaltHidden(unittest.TestCase):
    """HARD_HALT is internal and must not appear in get_operator_status()."""

    def test_hard_halt_hidden_from_operator_status(self):
        m = _make_manager()
        m.trigger_halt("internal circuit", level=HaltLevel.HARD_HALT, triggered_by="SYSTEM")
        s = m.get_operator_status()
        self.assertTrue(s["is_halted"])
        self.assertIsNone(s["halt_type"],
            "HARD_HALT must not be exposed as a UI-facing halt_type")

    def test_hard_halt_still_visible_in_get_status(self):
        """Internal get_status() must still report HARD_HALT for backward compat."""
        m = _make_manager()
        m.trigger_halt("internal", level=HaltLevel.HARD_HALT)
        s = m.get_status()
        self.assertEqual(s["current_level"], "HARD_HALT")


class TestFlattenTokenPending(unittest.TestCase):
    """get_operator_status flatten_token_pending reflects real expiry."""

    def test_flatten_token_pending_true_within_window(self):
        m = _make_manager()
        m.request_flatten_confirmation_token("admin")
        s = m.get_operator_status()
        self.assertTrue(s["flatten_token_pending"])

    def test_flatten_token_pending_false_after_expiry(self):
        m = _make_manager()
        m.request_flatten_confirmation_token("admin")
        m._flatten_token_expiry = datetime.now(IST) - timedelta(seconds=1)
        s = m.get_operator_status()
        self.assertFalse(s["flatten_token_pending"])

    def test_flatten_token_pending_false_initially(self):
        m = _make_manager()
        s = m.get_operator_status()
        self.assertFalse(s["flatten_token_pending"])


class TestExistingBehaviourPreserved(unittest.TestCase):
    """All existing methods work exactly as before."""

    def test_trigger_halt_hard_halt(self):
        m = _make_manager()
        event = m.trigger_halt("existing test", level=HaltLevel.HARD_HALT, triggered_by="ADMIN")
        self.assertTrue(m.is_halted)
        self.assertEqual(m.current_level, HaltLevel.HARD_HALT)
        self.assertEqual(event.triggered_by, "ADMIN")

    def test_trigger_halt_full_kill_switch_direct(self):
        m = _make_manager()
        m.trigger_halt("flash crash", level=HaltLevel.FULL_KILL_SWITCH)
        self.assertTrue(m.is_halted)
        self.assertEqual(m.current_level, HaltLevel.FULL_KILL_SWITCH)

    def test_request_ack_token_prefix(self):
        m = _make_manager()
        m.trigger_halt("test")
        token = m.request_acknowledgment_token("OPERATOR_1")
        self.assertTrue(token.startswith("ACK-OPERATOR_1-"))

    def test_request_ack_token_not_halted_raises(self):
        m = _make_manager()
        with self.assertRaises(ValueError):
            m.request_acknowledgment_token("op1")

    def test_acknowledge_and_reset_cycle(self):
        m = _make_manager()
        m.trigger_halt("test halt")
        token = m.request_acknowledgment_token("op_abc")
        result = m.acknowledge_and_reset("op_abc", token, "all clear")
        self.assertTrue(result)
        self.assertFalse(m.is_halted)
        self.assertIsNone(m.current_level)

    def test_acknowledge_wrong_token_raises(self):
        m = _make_manager()
        m.trigger_halt("test")
        with self.assertRaises(PermissionError):
            m.acknowledge_and_reset("op1", "WRONG-TOKEN")

    def test_audit_log_accumulates(self):
        m = _make_manager()
        m.trigger_halt("event 1")
        tok = m.request_acknowledgment_token("op")
        m.acknowledge_and_reset("op", tok)
        m.trigger_halt("event 2")
        log = m.get_audit_log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0].reason, "event 1")
        self.assertEqual(log[1].reason, "event 2")

    def test_get_status_structure(self):
        m = _make_manager()
        m.trigger_halt("status test", level=HaltLevel.SOFT_HALT)
        s = m.get_status()
        self.assertIn("is_halted", s)
        self.assertIn("current_level", s)
        self.assertIn("pending_acknowledgment", s)
        self.assertTrue(s["is_halted"])
        self.assertTrue(s["pending_acknowledgment"])


class TestThreadSafety(unittest.TestCase):
    """Concurrent halt calls must not corrupt state."""

    def test_concurrent_stop_entry_converges(self):
        m = _make_manager()
        errors = []

        def worker(i):
            try:
                m.stop_entry(f"concurrent halt {i}", operator_id=f"op{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread safety error: {errors}")
        self.assertTrue(m.is_halted)
        self.assertEqual(m.current_level, HaltLevel.SOFT_HALT)

    def test_token_request_and_flatten_are_atomic(self):
        """Requesting a token and then immediately calling flatten_all must not race."""
        m = _make_manager()
        results = []
        errors = []

        def try_flatten():
            try:
                tok = m.request_flatten_confirmation_token("admin")
                ev = m.flatten_all(operator_id="admin", token=tok)
                results.append(ev.event_id)
            except PermissionError:
                pass  # another thread already consumed the token — expected
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=try_flatten) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        # At least one flatten must have succeeded
        self.assertGreater(len(results), 0)
        # The manager must be in FULL_KILL_SWITCH
        self.assertEqual(m.current_level, HaltLevel.FULL_KILL_SWITCH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
