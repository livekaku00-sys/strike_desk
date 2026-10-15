"""
Strike Desk V4 - Structured Append-Only Audit Logger
P0-07: Extends the in-memory logger with a SQLite background writer.

Design contract:
- In-memory list is always updated synchronously (zero latency on the hot path).
- SQLite writes are dispatched to a daemon thread via a queue — the calling
  thread is never blocked by disk I/O.
- Secrets are never accepted as parameters by any log method. The _sanitize()
  helper strips common credential patterns from free-text error messages.
"""
from datetime import datetime, timezone
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from models.instrument import AssetClass
from models.signal import Signal

logger = logging.getLogger(__name__)

# ── Secret sanitiser ──────────────────────────────────────────────────────────

_SECRET_RE = [
    # JWT / Bearer tokens
    re.compile(r'Bearer\s+[A-Za-z0-9._\-]{20,}', re.IGNORECASE),
    # key=value patterns
    re.compile(r'(?:access_token|token|jwt|api_key|secret|password|totp)[=:\s]+[A-Za-z0-9._\-]{10,}', re.IGNORECASE),
    # Raw base64-ish blobs > 40 chars that look like credentials (not file paths)
    re.compile(r'(?<![/\\.])\b[A-Za-z0-9+/]{40,}={0,2}\b'),
]


def _sanitize(text: str) -> str:
    """Strip common credential patterns from a free-text string before logging."""
    if not text:
        return text
    for pat in _SECRET_RE:
        text = pat.sub("[REDACTED]", text)
    return text


# ── SQLite background writer ──────────────────────────────────────────────────

class _SqliteWriter:
    """
    Daemon thread that batches audit records into SQLite.
    The caller enqueues a dict; the thread owns the connection exclusively.
    WAL mode keeps reads non-blocking during writes.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS audit_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL,
            symbol      TEXT,
            broker      TEXT,
            order_id    TEXT,
            c_order_id  TEXT,
            signal_id   TEXT,
            action      TEXT,
            reason      TEXT,
            old_state   TEXT,
            new_state   TEXT,
            fill_price  REAL,
            fill_qty    REAL,
            operator    TEXT,
            raw_json    TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ts    ON audit_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_oid   ON audit_events(order_id);
        CREATE INDEX IF NOT EXISTS idx_etype ON audit_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_coid  ON audit_events(c_order_id);
    """

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="audit-sqlite"
        )
        self._thread.start()

    def enqueue(self, record: Dict[str, Any]) -> None:
        self._queue.put(record)

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until all currently queued records are written. Used by tests."""
        done = threading.Event()
        self._queue.put({"_sentinel": done})
        return done.wait(timeout=timeout)

    # ── thread body ──

    def _run(self) -> None:
        while True:
            try:
                with sqlite3.connect(self._db_path, check_same_thread=True) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(self._SCHEMA)
                    conn.commit()
                    self._loop(conn)
            except Exception as exc:
                logger.error("[AuditSqlite] Writer crashed, restarting in 1s: %s", exc)
                time.sleep(1.0)

    def _loop(self, conn: sqlite3.Connection) -> None:
        while True:
            batch: List[Dict[str, Any]] = []
            events: List[threading.Event] = []

            item = self._queue.get()  # blocks until something arrives
            if "_sentinel" in item:
                item["_sentinel"].set()
            else:
                batch.append(item)

            while True:
                try:
                    nxt = self._queue.get_nowait()
                    if "_sentinel" in nxt:
                        events.append(nxt["_sentinel"])
                    else:
                        batch.append(nxt)
                except queue.Empty:
                    break

            if batch:
                self._write_batch(conn, batch)

            for ev in events:
                ev.set()

    def _write_batch(self, conn: sqlite3.Connection, batch: List[Dict[str, Any]]) -> None:
        rows = [
            (
                r.get("event_type", ""),
                r.get("timestamp", ""),
                r.get("symbol"),
                r.get("broker"),
                r.get("order_id") or r.get("broker_order_id"),
                r.get("client_order_id"),
                r.get("signal_id"),
                r.get("action"),
                r.get("reason"),
                r.get("old_state"),
                r.get("new_state"),
                r.get("fill_price"),
                r.get("fill_qty"),
                r.get("operator"),
                json.dumps(r),
            )
            for r in batch
        ]
        conn.executemany(
            "INSERT INTO audit_events "
            "(event_type,timestamp,symbol,broker,order_id,c_order_id,"
            "signal_id,action,reason,old_state,new_state,fill_price,fill_qty,"
            "operator,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()


# ── AuditLogger ───────────────────────────────────────────────────────────────

class AuditLogger:
    """
    Structured append-only audit logger.

    In-memory list is updated synchronously; SQLite (if configured) is
    written asynchronously by a background daemon thread.

    Pass db_path=None to disable SQLite (unit tests, REPL use).
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._entries: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._writer: Optional[_SqliteWriter] = (
            _SqliteWriter(db_path) if db_path else None
        )

    def enable_sqlite(self, db_path: str) -> None:
        """Attach a SQLite backend after construction. Idempotent."""
        if self._writer is None:
            self._writer = _SqliteWriter(db_path)

    def flush_sqlite(self, timeout: float = 5.0) -> bool:
        """Wait until all queued SQLite writes complete. Used by tests."""
        if self._writer is None:
            return True
        return self._writer.flush(timeout=timeout)

    # ── internal helper ──

    def _record(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._entries.append(entry)
        if self._writer is not None:
            self._writer.enqueue(entry)
        return entry

    # ── existing methods (unchanged) ─────────────────────────────────────────

    def log_signal(self, signal: Signal, action: str, reason: str) -> Dict[str, Any]:
        entry = {
            "event_type": "signal",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "asset_class": signal.asset_class.value if hasattr(signal.asset_class, "value") else str(signal.asset_class),
            "direction": signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
            "confidence_score": signal.confidence_score,
            "confidence_tier": signal.confidence_tier.value if hasattr(signal.confidence_tier, "value") else str(signal.confidence_tier),
            "action": action,
            "reason": reason,
        }
        return self._record(entry)

    def log_selection_change(
        self, old_class: Optional[AssetClass], new_class: AssetClass
    ) -> Dict[str, Any]:
        old_val = old_class.value if hasattr(old_class, "value") else (str(old_class) if old_class else None)
        new_val = new_class.value if hasattr(new_class, "value") else str(new_class)
        entry = {
            "event_type": "selection_change",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "old_class": old_val,
            "new_state": new_val,
            "old_state": old_val,
            "new_class": new_val,
            "action": "SELECTION_CHANGE",
        }
        logger.info("[AuditLogger] Asset class selection changed from %s to %s", old_val, new_val)
        return self._record(entry)

    def log_cascade_brake(self, triggered_classes: List[AssetClass]) -> Dict[str, Any]:
        classes_str = [c.value if hasattr(c, "value") else str(c) for c in triggered_classes]
        entry = {
            "event_type": "cascade_brake",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "classes": classes_str,
            "action": "CASCADE_BRAKE",
        }
        return self._record(entry)

    # ── new event methods ────────────────────────────────────────────────────

    def log_order_transition(
        self,
        client_order_id: str,
        symbol: str,
        broker: str,
        old_state: str,
        new_state: str,
        reason: str = "",
        fill_price: float = 0.0,
        fill_qty: float = 0.0,
        order_id: str = "",
        signal_id: str = "",
    ) -> Dict[str, Any]:
        """Record every order state transition. Called from order_manager and place_order."""
        entry = {
            "event_type": "order_transition",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_order_id": client_order_id,
            "symbol": symbol,
            "broker": broker,
            "old_state": old_state,
            "new_state": new_state,
            "reason": reason,
            "fill_price": fill_price if fill_price else None,
            "fill_qty": fill_qty if fill_qty else None,
            "order_id": order_id or None,
            "signal_id": signal_id or None,
            "action": new_state,
        }
        return self._record(entry)

    def log_risk_rejection(
        self,
        symbol: str,
        broker: str,
        rule_violated: str,
        reason: str,
        order_value: float = 0.0,
        signal_id: str = "",
    ) -> Dict[str, Any]:
        """Record every risk gate rejection — loss ceiling, trade cap, cutoff, etc."""
        entry = {
            "event_type": "risk_rejection",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "broker": broker,
            "rule_violated": rule_violated,
            "reason": reason,
            "order_value": order_value if order_value else None,
            "signal_id": signal_id or None,
            "action": "REJECTED",
        }
        return self._record(entry)

    def log_halt_event(
        self,
        event_subtype: str,
        level: str,
        reason: str,
        operator: str,
        resolution_notes: str = "",
    ) -> Dict[str, Any]:
        """
        Record emergency halt triggers and resets.
        event_subtype: 'HALT_TRIGGERED' | 'HALT_RESET'
        level: 'SOFT_HALT' | 'HARD_HALT' | 'FULL_KILL_SWITCH'
        """
        entry = {
            "event_type": "halt_event",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_subtype": event_subtype,
            "level": level,
            "reason": reason,
            "operator": operator,
            "resolution_notes": resolution_notes or None,
            "action": event_subtype,
        }
        return self._record(entry)

    def log_mode_change(
        self,
        old_mode: str,
        new_mode: str,
        operator: str,
        execution_type: str = "",
    ) -> Dict[str, Any]:
        """Record Paper ↔ Live mode switches and asset-class selection changes."""
        entry = {
            "event_type": "mode_change",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "old_state": old_mode,
            "new_state": new_mode,
            "operator": operator,
            "execution_type": execution_type or None,
            "action": "MODE_CHANGE",
        }
        return self._record(entry)

    def log_broker_change(
        self,
        old_broker: str,
        new_broker: str,
        operator: str,
    ) -> Dict[str, Any]:
        """Record active broker selection changes."""
        entry = {
            "event_type": "broker_change",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "old_state": old_broker,
            "new_state": new_broker,
            "broker": new_broker,
            "operator": operator,
            "action": "BROKER_CHANGE",
        }
        return self._record(entry)

    def log_token_event(
        self,
        broker: str,
        event_subtype: str,
        success: bool,
        error_message: str = "",
    ) -> Dict[str, Any]:
        """
        Record token lifecycle events. Never accepts token values as parameters.
        error_message is sanitized before storage — any credential-shaped string
        is replaced with [REDACTED].

        event_subtype: 'refresh_succeeded' | 'refresh_failed' |
                       'expiry_alert' | 'reauth_succeeded' | 'reauth_failed'
        """
        safe_msg = _sanitize(error_message)
        entry = {
            "event_type": "token_event",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "broker": broker,
            "event_subtype": event_subtype,
            "success": success,
            "error_message": safe_msg or None,
            "action": event_subtype,
        }
        return self._record(entry)

    def log_reconciliation(
        self,
        broker: str,
        status: str,
        diff_summary: str = "",
        symbols_checked: int = 0,
    ) -> Dict[str, Any]:
        """
        Record position reconciliation results.
        status: 'MATCHED' | 'MISMATCH' | 'ERROR'
        """
        entry = {
            "event_type": "reconciliation",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "broker": broker,
            "status": status,
            "diff_summary": diff_summary or None,
            "symbols_checked": symbols_checked,
            "action": status,
        }
        return self._record(entry)

    def log_preflight(
        self,
        check_results: Dict[str, str],
        live_mode_armed: bool,
        operator: str = "SYSTEM",
    ) -> Dict[str, Any]:
        """
        Record pre-flight check outcomes.
        check_results maps check_name → 'PASS' | 'FAIL' | 'PENDING'
        """
        entry = {
            "event_type": "preflight",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operator": operator,
            "live_mode_armed": live_mode_armed,
            "check_results": check_results,
            "action": "ARMED" if live_mode_armed else "LOCKED",
        }
        return self._record(entry)

    # ── query helpers ─────────────────────────────────────────────────────────

    def get_audit_trail(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._entries[-limit:])

    def get_all_entries(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def log_market_data_event(
        self,
        symbol: str,
        event_subtype: str,
        reason: str,
        ltp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record market-data pipeline events: invalid LTP, out-of-order tick, feed gap."""
        entry = {
            "event_type": "market_data_event",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "event_subtype": event_subtype,
            "reason": reason,
            "ltp": ltp,
            "action": event_subtype,
        }
        return self._record(entry)

    def query_by_order_id(self, client_order_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                e for e in self._entries
                if e.get("client_order_id") == client_order_id
            ]

    def query_by_signal_id(self, signal_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                e for e in self._entries
                if e.get("signal_id") == signal_id
            ]

    def query_by_event_type(self, event_type: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            matches = [e for e in self._entries if e.get("event_type") == event_type]
            return matches[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def export_to_json(self, filepath: str) -> None:
        with self._lock:
            data = list(self._entries)
        dirname = os.path.dirname(filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        temp_path = f"{filepath}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, filepath)


# ── Global singleton ──────────────────────────────────────────────────────────
# SQLite path comes from env so tests can override with AUDIT_DB_PATH=""
# to disable the background writer (empty string → None → in-memory only).

_db_env = os.environ.get("AUDIT_DB_PATH", "data/audit.db")
audit_logger = AuditLogger(db_path=_db_env if _db_env else None)
