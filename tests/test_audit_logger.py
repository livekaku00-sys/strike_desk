"""
P0-07 — Structured Audit Log tests

Coverage:
- All 8 new event methods produce correct structured records
- SQLite backend is durable across a close/reopen cycle
- In-memory list is updated synchronously (no flush needed)
- Negative tests: no credential leakage in log_token_event or _sanitize
- Background flush gate works correctly
- Existing methods (log_signal, log_selection_change, log_cascade_brake) preserved
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Disable the global singleton's SQLite writer for all tests so we don't
# pollute data/audit.db during the test run.
os.environ.setdefault("AUDIT_DB_PATH", "")

from execution.audit_logger import AuditLogger, _sanitize


def _make_signal():
    """Return a minimal mock signal compatible with log_signal."""
    sig = MagicMock()
    sig.signal_id = "test-sig-001"
    sig.symbol = "NIFTY"
    sig.asset_class = MagicMock()
    sig.asset_class.value = "INDEX_OPTIONS"
    sig.direction = MagicMock()
    sig.direction.value = "BUY"
    sig.confidence_score = 0.82
    sig.confidence_tier = MagicMock()
    sig.confidence_tier.value = "HIGH"
    return sig


class TestInMemoryBaseline(unittest.TestCase):
    """All new methods update the in-memory list synchronously."""

    def setUp(self):
        self.al = AuditLogger(db_path=None)

    def test_log_order_transition(self):
        r = self.al.log_order_transition(
            client_order_id="C001",
            symbol="BANKNIFTY",
            broker="dhan",
            old_state="PENDING",
            new_state="OPEN",
            reason="submitted",
        )
        self.assertEqual(r["event_type"], "order_transition")
        self.assertEqual(r["client_order_id"], "C001")
        self.assertEqual(r["old_state"], "PENDING")
        self.assertEqual(r["new_state"], "OPEN")
        self.assertEqual(r["action"], "OPEN")
        entries = self.al.get_all_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event_type"], "order_transition")

    def test_log_risk_rejection(self):
        r = self.al.log_risk_rejection(
            symbol="NIFTY",
            broker="angelone",
            rule_violated="DAILY_LOSS_CEILING",
            reason="daily loss ₹45000 exceeds limit ₹40000",
        )
        self.assertEqual(r["event_type"], "risk_rejection")
        self.assertEqual(r["action"], "REJECTED")
        self.assertEqual(r["rule_violated"], "DAILY_LOSS_CEILING")

    def test_log_halt_event_triggered(self):
        r = self.al.log_halt_event(
            event_subtype="HALT_TRIGGERED",
            level="SOFT_HALT",
            reason="manual operator halt",
            operator="admin",
        )
        self.assertEqual(r["event_type"], "halt_event")
        self.assertEqual(r["level"], "SOFT_HALT")
        self.assertEqual(r["event_subtype"], "HALT_TRIGGERED")
        self.assertEqual(r["operator"], "admin")
        self.assertIsNone(r.get("resolution_notes"))

    def test_log_halt_event_reset(self):
        r = self.al.log_halt_event(
            event_subtype="HALT_RESET",
            level="SOFT_HALT",
            reason="market conditions normalized",
            operator="admin",
            resolution_notes="verified positions flat",
        )
        self.assertEqual(r["event_subtype"], "HALT_RESET")
        self.assertEqual(r["resolution_notes"], "verified positions flat")

    def test_log_mode_change(self):
        r = self.al.log_mode_change(
            old_mode="PAPER",
            new_mode="LIVE",
            operator="admin",
            execution_type="REAL",
        )
        self.assertEqual(r["event_type"], "mode_change")
        self.assertEqual(r["old_state"], "PAPER")
        self.assertEqual(r["new_state"], "LIVE")
        self.assertEqual(r["action"], "MODE_CHANGE")
        self.assertEqual(r["operator"], "admin")

    def test_log_broker_change(self):
        r = self.al.log_broker_change(
            old_broker="angelone",
            new_broker="dhan",
            operator="admin",
        )
        self.assertEqual(r["event_type"], "broker_change")
        self.assertEqual(r["old_state"], "angelone")
        self.assertEqual(r["new_state"], "dhan")
        self.assertEqual(r["broker"], "dhan")
        self.assertEqual(r["action"], "BROKER_CHANGE")

    def test_log_token_event_success(self):
        r = self.al.log_token_event(
            broker="angelone",
            event_subtype="refresh_succeeded",
            success=True,
        )
        self.assertEqual(r["event_type"], "token_event")
        self.assertTrue(r["success"])
        self.assertIsNone(r.get("error_message"))

    def test_log_token_event_failure(self):
        r = self.al.log_token_event(
            broker="dhan",
            event_subtype="refresh_failed",
            success=False,
            error_message="HTTP 401 Unauthorized",
        )
        self.assertFalse(r["success"])
        self.assertEqual(r["error_message"], "HTTP 401 Unauthorized")

    def test_log_reconciliation(self):
        r = self.al.log_reconciliation(
            broker="fyers",
            status="MISMATCH",
            diff_summary="local NIFTY qty=2, broker qty=1",
            symbols_checked=5,
        )
        self.assertEqual(r["event_type"], "reconciliation")
        self.assertEqual(r["status"], "MISMATCH")
        self.assertEqual(r["symbols_checked"], 5)
        self.assertEqual(r["action"], "MISMATCH")

    def test_log_preflight_locked(self):
        results = {
            "broker_auth": "PASS",
            "live_feed": "FAIL",
            "risk_limits": "PASS",
        }
        r = self.al.log_preflight(
            check_results=results,
            live_mode_armed=False,
            operator="SYSTEM",
        )
        self.assertEqual(r["event_type"], "preflight")
        self.assertEqual(r["action"], "LOCKED")
        self.assertFalse(r["live_mode_armed"])
        self.assertEqual(r["check_results"]["live_feed"], "FAIL")

    def test_log_preflight_armed(self):
        results = {
            "broker_auth": "PASS",
            "live_feed": "PASS",
            "risk_limits": "PASS",
        }
        r = self.al.log_preflight(
            check_results=results,
            live_mode_armed=True,
            operator="admin",
        )
        self.assertEqual(r["action"], "ARMED")
        self.assertTrue(r["live_mode_armed"])

    def test_timestamp_is_utc_iso(self):
        r = self.al.log_risk_rejection("NIFTY", "dhan", "TRADE_COUNT_CAP", "cap hit")
        ts = r["timestamp"]
        self.assertIn("T", ts)
        self.assertTrue(ts.endswith("+00:00") or ts.endswith("Z"))

    def test_query_by_order_id(self):
        self.al.log_order_transition("X100", "NIFTY", "dhan", "PENDING", "OPEN")
        self.al.log_order_transition("X100", "NIFTY", "dhan", "OPEN", "FILLED", fill_price=21000.0, fill_qty=50)
        self.al.log_order_transition("X200", "BANKNIFTY", "dhan", "PENDING", "REJECTED")
        hits = self.al.query_by_order_id("X100")
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(e["client_order_id"] == "X100" for e in hits))

    def test_query_by_event_type(self):
        self.al.log_mode_change("PAPER", "LIVE", "admin")
        self.al.log_broker_change("angelone", "dhan", "admin")
        self.al.log_mode_change("LIVE", "PAPER", "admin")
        mode_events = self.al.query_by_event_type("mode_change")
        self.assertEqual(len(mode_events), 2)
        broker_events = self.al.query_by_event_type("broker_change")
        self.assertEqual(len(broker_events), 1)

    def test_get_audit_trail_limit(self):
        for i in range(20):
            self.al.log_broker_change("a", "b", "admin")
        trail = self.al.get_audit_trail(limit=5)
        self.assertEqual(len(trail), 5)


class TestSecretLeakage(unittest.TestCase):
    """Zero secret leakage in audit records."""

    def setUp(self):
        self.al = AuditLogger(db_path=None)

    def test_token_event_no_token_param(self):
        """log_token_event signature must not have a token parameter."""
        import inspect
        params = inspect.signature(self.al.log_token_event).parameters
        forbidden = {"token", "access_token", "api_key", "secret", "password"}
        for name in params:
            self.assertNotIn(name, forbidden,
                f"log_token_event must not accept a '{name}' parameter")

    def test_sanitize_bearer_token(self):
        raw = "Auth failed: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = _sanitize(raw)
        self.assertNotIn("eyJ", result)
        self.assertIn("[REDACTED]", result)

    def test_sanitize_key_value_credential(self):
        raw = "Error: access_token=AbCdEfGhIjKlMnOpQrStUvWxYz1234"
        result = _sanitize(raw)
        self.assertNotIn("AbCdEfGhIjKlMnOpQrStUvWxYz1234", result)
        self.assertIn("[REDACTED]", result)

    def test_sanitize_empty_string(self):
        self.assertEqual(_sanitize(""), "")

    def test_sanitize_safe_message_unchanged(self):
        raw = "HTTP 429 Too Many Requests – retry after 30s"
        result = _sanitize(raw)
        self.assertEqual(result, raw)

    def test_token_event_error_message_is_sanitized(self):
        raw = "refresh failed: token=AbcXyz123456789012345678901234567890"
        r = self.al.log_token_event("dhan", "refresh_failed", False, raw)
        stored = r.get("error_message", "")
        if stored:
            self.assertNotIn("AbcXyz123456789012345678901234567890", stored)

    def test_no_secret_keys_in_any_record(self):
        """No record from any log method contains a key called 'token' or 'secret'."""
        forbidden_keys = {"token", "secret", "password", "api_key", "totp"}
        self.al.log_order_transition("C1", "NIFTY", "dhan", "PENDING", "OPEN")
        self.al.log_risk_rejection("NIFTY", "dhan", "MAX_ORDER_VALUE", "too big")
        self.al.log_halt_event("HALT_TRIGGERED", "SOFT_HALT", "test", "admin")
        self.al.log_mode_change("PAPER", "LIVE", "admin")
        self.al.log_broker_change("angelone", "dhan", "admin")
        self.al.log_token_event("angelone", "refresh_succeeded", True)
        self.al.log_reconciliation("fyers", "MATCHED")
        self.al.log_preflight({"check": "PASS"}, True)
        for entry in self.al.get_all_entries():
            for key in entry:
                self.assertNotIn(key, forbidden_keys,
                    f"Forbidden key '{key}' found in {entry['event_type']} record")


class TestSqliteDurability(unittest.TestCase):
    """SQLite backend writes are durable across a close/reopen cycle."""

    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")

    def tearDown(self):
        for path in (self.tmp, self.tmp + "-wal", self.tmp + "-shm"):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def test_records_survive_reopen(self):
        """Write events with one AuditLogger, then verify rows in a fresh SQLite read."""
        al = AuditLogger(db_path=self.tmp)
        al.log_order_transition("C99", "SENSEX", "dhan", "PENDING", "FILLED",
                                 fill_price=71000.0, fill_qty=10)
        al.log_risk_rejection("BANKNIFTY", "angelone", "TRADE_COUNT_CAP", "limit 20")
        al.log_halt_event("HALT_TRIGGERED", "HARD_HALT", "circuit breaker", "SYSTEM")
        al.log_mode_change("PAPER", "LIVE", "admin")
        al.log_broker_change("fyers", "angelone", "admin")
        al.log_token_event("angelone", "refresh_succeeded", True)
        al.log_reconciliation("dhan", "MATCHED", symbols_checked=3)
        al.log_preflight({"broker_auth": "PASS"}, True)

        flushed = al.flush_sqlite(timeout=5.0)
        self.assertTrue(flushed, "SQLite flush timed out — background writer may be stuck")

        with sqlite3.connect(self.tmp) as conn:
            rows = conn.execute(
                "SELECT event_type, raw_json FROM audit_events ORDER BY id"
            ).fetchall()

        expected_types = {
            "order_transition", "risk_rejection", "halt_event",
            "mode_change", "broker_change", "token_event",
            "reconciliation", "preflight",
        }
        found_types = {row[0] for row in rows}
        self.assertEqual(expected_types, found_types)

        # Verify raw_json round-trips correctly
        for event_type, raw in rows:
            decoded = json.loads(raw)
            self.assertEqual(decoded["event_type"], event_type)
            self.assertIn("timestamp", decoded)

    def test_fill_price_and_qty_columns(self):
        al = AuditLogger(db_path=self.tmp)
        al.log_order_transition("C200", "NIFTY", "dhan", "OPEN", "FILLED",
                                 fill_price=21500.50, fill_qty=50)
        self.assertTrue(al.flush_sqlite(timeout=5.0))

        with sqlite3.connect(self.tmp) as conn:
            row = conn.execute(
                "SELECT fill_price, fill_qty FROM audit_events WHERE event_type='order_transition'"
            ).fetchone()
        self.assertAlmostEqual(row[0], 21500.50, places=2)
        self.assertAlmostEqual(row[1], 50.0, places=1)

    def test_operator_column_populated(self):
        al = AuditLogger(db_path=self.tmp)
        al.log_halt_event("HALT_TRIGGERED", "FULL_KILL_SWITCH", "test", "ops_team")
        self.assertTrue(al.flush_sqlite(timeout=5.0))

        with sqlite3.connect(self.tmp) as conn:
            row = conn.execute(
                "SELECT operator FROM audit_events WHERE event_type='halt_event'"
            ).fetchone()
        self.assertEqual(row[0], "ops_team")

    def test_broker_column_populated(self):
        al = AuditLogger(db_path=self.tmp)
        al.log_token_event("fyers", "expiry_alert", False, "token expiring in 30m")
        self.assertTrue(al.flush_sqlite(timeout=5.0))

        with sqlite3.connect(self.tmp) as conn:
            row = conn.execute(
                "SELECT broker FROM audit_events WHERE event_type='token_event'"
            ).fetchone()
        self.assertEqual(row[0], "fyers")

    def test_client_order_id_in_c_order_id_column(self):
        al = AuditLogger(db_path=self.tmp)
        al.log_order_transition("CLIENT-ORDER-XYZ", "NIFTY", "dhan", "PENDING", "OPEN")
        self.assertTrue(al.flush_sqlite(timeout=5.0))

        with sqlite3.connect(self.tmp) as conn:
            row = conn.execute(
                "SELECT c_order_id FROM audit_events WHERE event_type='order_transition'"
            ).fetchone()
        self.assertEqual(row[0], "CLIENT-ORDER-XYZ")

    def test_indexes_exist(self):
        al = AuditLogger(db_path=self.tmp)
        al.log_mode_change("PAPER", "LIVE", "admin")
        self.assertTrue(al.flush_sqlite(timeout=5.0))

        with sqlite3.connect(self.tmp) as conn:
            idx_names = {
                row[1]
                for row in conn.execute("PRAGMA index_list(audit_events)").fetchall()
            }
        self.assertIn("idx_ts", idx_names)
        self.assertIn("idx_etype", idx_names)
        self.assertIn("idx_coid", idx_names)

    def test_multiple_flushes_are_idempotent(self):
        al = AuditLogger(db_path=self.tmp)
        al.log_reconciliation("angelone", "MATCHED")
        self.assertTrue(al.flush_sqlite(timeout=5.0))
        self.assertTrue(al.flush_sqlite(timeout=5.0))  # second flush on empty queue

        with sqlite3.connect(self.tmp) as conn:
            count = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        self.assertEqual(count, 1)


class TestInMemoryNonBlocking(unittest.TestCase):
    """In-memory list is always updated synchronously regardless of SQLite."""

    def test_in_memory_available_before_flush(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = f.name
        try:
            al = AuditLogger(db_path=tmp)
            # Write 5 events and immediately read back — no flush needed
            for i in range(5):
                al.log_risk_rejection(f"SYM{i}", "dhan", "TRADE_COUNT_CAP", "test")
            entries = al.get_all_entries()
            self.assertEqual(len(entries), 5)
        finally:
            for path in (tmp, tmp + "-wal", tmp + "-shm"):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def test_thread_safety_concurrent_writes(self):
        al = AuditLogger(db_path=None)
        errors = []

        def worker(n):
            try:
                for i in range(50):
                    al.log_risk_rejection(f"SYM_{n}", "dhan", "TEST_RULE", f"worker {n} iter {i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread safety error: {errors}")
        self.assertEqual(len(al.get_all_entries()), 500)


class TestExistingMethodsPreserved(unittest.TestCase):
    """The three original methods still work correctly after extension."""

    def setUp(self):
        self.al = AuditLogger(db_path=None)

    def test_log_signal(self):
        sig = _make_signal()
        r = self.al.log_signal(sig, "EXECUTED", "confidence above threshold")
        self.assertEqual(r["event_type"], "signal")
        self.assertEqual(r["signal_id"], "test-sig-001")
        self.assertEqual(r["action"], "EXECUTED")

    def test_log_cascade_brake(self):
        mock_cls = [MagicMock(), MagicMock()]
        mock_cls[0].value = "INDEX_OPTIONS"
        mock_cls[1].value = "STOCK_FUTURES"
        r = self.al.log_cascade_brake(mock_cls)
        self.assertEqual(r["event_type"], "cascade_brake")
        self.assertIn("INDEX_OPTIONS", r["classes"])

    def test_log_selection_change(self):
        old_cls = MagicMock()
        old_cls.value = "INDEX_OPTIONS"
        new_cls = MagicMock()
        new_cls.value = "STOCK_CASH"
        r = self.al.log_selection_change(old_cls, new_cls)
        self.assertEqual(r["event_type"], "selection_change")
        self.assertEqual(r["old_state"], "INDEX_OPTIONS")
        self.assertEqual(r["new_state"], "STOCK_CASH")

    def test_clear_resets_in_memory(self):
        sig = _make_signal()
        self.al.log_signal(sig, "SKIPPED", "below threshold")
        self.assertEqual(len(self.al.get_all_entries()), 1)
        self.al.clear()
        self.assertEqual(len(self.al.get_all_entries()), 0)

    def test_export_to_json(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp_json = f.name
        try:
            self.al.log_mode_change("PAPER", "LIVE", "admin")
            self.al.log_broker_change("angelone", "dhan", "admin")
            self.al.export_to_json(tmp_json)
            with open(tmp_json) as f:
                data = json.load(f)
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["event_type"], "mode_change")
        finally:
            os.remove(tmp_json)


class TestEnableSqlite(unittest.TestCase):
    """enable_sqlite() attaches a writer post-construction."""

    def test_enable_sqlite_after_init(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = f.name
        try:
            al = AuditLogger(db_path=None)
            al.enable_sqlite(tmp)
            al.log_mode_change("PAPER", "LIVE", "admin")
            self.assertTrue(al.flush_sqlite(timeout=5.0))

            with sqlite3.connect(tmp) as conn:
                count = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            for path in (tmp, tmp + "-wal", tmp + "-shm"):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def test_enable_sqlite_is_idempotent(self):
        """Calling enable_sqlite twice should not start a second writer."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = f.name
        try:
            al = AuditLogger(db_path=None)
            al.enable_sqlite(tmp)
            writer1 = al._writer
            al.enable_sqlite(tmp)
            writer2 = al._writer
            self.assertIs(writer1, writer2, "enable_sqlite must not replace an existing writer")
        finally:
            for path in (tmp, tmp + "-wal", tmp + "-shm"):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def test_flush_sqlite_returns_true_when_no_writer(self):
        al = AuditLogger(db_path=None)
        self.assertTrue(al.flush_sqlite(timeout=1.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
