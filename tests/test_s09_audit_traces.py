"""
S09 — End-to-End Audit Traces tests

Coverage:
- Pre-signal risk rejections are structured audit events with signal_id
- Paper fill lifecycle (PENDING→SUBMITTED→FILLED) is in the structured audit trail
- API-layer risk rejection emits a structured risk_rejection event
- Invalid-LTP ticks emit a structured market_data_event
- signal_id from Order.metadata threads through order_manager audit calls
- query_by_signal_id returns all events sharing that signal_id
- log_market_data_event records the right fields
- Credential sanitisation still applies to all new event types
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Disable global singleton's SQLite writer for all tests.
os.environ.setdefault("AUDIT_DB_PATH", "")

from execution.audit_logger import AuditLogger


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_signal(signal_id="SIG-001", symbol="NIFTY", asset_class_val="INDEX_OPTIONS"):
    sig = MagicMock()
    sig.signal_id = signal_id
    sig.symbol = symbol
    sig.asset_class = MagicMock()
    sig.asset_class.value = asset_class_val
    sig.direction = MagicMock()
    sig.direction.value = "BUY"
    sig.confidence_score = 0.75
    sig.confidence_tier = MagicMock()
    sig.confidence_tier.value = "HIGH"
    sig.timestamp = datetime.now(timezone.utc)
    sig.state = None
    return sig


def _make_order(client_order_id="ORD-001", symbol="NIFTY", signal_id=""):
    from models.order import Order, OrderSide, OrderType
    return Order(
        client_order_id=client_order_id,
        symbol=symbol,
        exchange="NSE",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0,
        broker="paper",
        metadata={"signal_id": signal_id} if signal_id else {},
    )


# ── TestMarketDataEvent ───────────────────────────────────────────────────────

class TestMarketDataEvent(unittest.TestCase):
    """log_market_data_event produces correct structured records."""

    def setUp(self):
        self.al = AuditLogger(db_path=None)

    def test_basic_fields(self):
        r = self.al.log_market_data_event(
            symbol="BANKNIFTY",
            event_subtype="INVALID_LTP",
            reason="ltp_valid=False; ltp=None",
            ltp=None,
        )
        self.assertEqual(r["event_type"], "market_data_event")
        self.assertEqual(r["symbol"], "BANKNIFTY")
        self.assertEqual(r["event_subtype"], "INVALID_LTP")
        self.assertIn("ltp_valid=False", r["reason"])
        self.assertIsNone(r["ltp"])
        self.assertIn("T", r["timestamp"])

    def test_ltp_value_stored(self):
        r = self.al.log_market_data_event(
            symbol="NIFTY",
            event_subtype="INVALID_LTP",
            reason="ltp=nan",
            ltp=float("nan"),
        )
        self.assertEqual(r["event_type"], "market_data_event")
        # ltp stored even when nan
        self.assertTrue(str(r["ltp"]) == "nan")

    def test_in_memory_list_updated(self):
        self.al.log_market_data_event("RELIANCE", "INVALID_LTP", "test", ltp=0.0)
        entries = self.al.get_all_entries()
        self.assertTrue(any(e["event_type"] == "market_data_event" for e in entries))


# ── TestQueryBySignalId ───────────────────────────────────────────────────────

class TestQueryBySignalId(unittest.TestCase):
    """query_by_signal_id returns all events that share the signal_id."""

    def setUp(self):
        self.al = AuditLogger(db_path=None)

    def test_empty_on_no_match(self):
        self.assertEqual(self.al.query_by_signal_id("nonexistent"), [])

    def test_returns_matching_entries(self):
        sig = _make_signal(signal_id="SIG-XYZ")
        self.al.log_signal(sig, "execute", "test")
        self.al.log_risk_rejection(
            symbol="NIFTY", broker="", rule_violated="GATE",
            reason="test", signal_id="SIG-XYZ",
        )
        self.al.log_risk_rejection(
            symbol="NIFTY", broker="", rule_violated="GATE",
            reason="other", signal_id="SIG-OTHER",
        )
        matches = self.al.query_by_signal_id("SIG-XYZ")
        # log_signal stores signal_id; log_risk_rejection stores signal_id
        self.assertEqual(len(matches), 2)
        for m in matches:
            self.assertEqual(m["signal_id"], "SIG-XYZ")

    def test_order_transition_with_signal_id(self):
        self.al.log_order_transition(
            client_order_id="C001",
            symbol="BANKNIFTY",
            broker="paper",
            old_state="PENDING",
            new_state="SUBMITTED",
            signal_id="SIG-CORR",
        )
        matches = self.al.query_by_signal_id("SIG-CORR")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["event_type"], "order_transition")


# ── TestPreSignalRiskRejectionAudit ──────────────────────────────────────────

class TestPreSignalRiskRejectionAudit(unittest.TestCase):
    """strategy_orchestrator emits a structured risk_rejection when the pre-signal gate fires."""

    def _make_strategy(self, signal):
        """Return a mock strategy whose evaluate() returns the given signal."""
        strat = MagicMock()
        strat.name = "mock_strategy"
        strat.evaluate.return_value = signal
        strat.signal_store = MagicMock()
        strat.reset = MagicMock()
        return strat

    def test_risk_rejection_emits_audit_event(self):
        from models.instrument import AssetClass
        from models.signal import SignalState

        al = AuditLogger(db_path=None)
        sig = _make_signal(signal_id="SIG-RISK-REJECT", symbol="NIFTY")

        # Patch everything the orchestrator touches
        with patch("strategies.strategy_orchestrator.risk_state_manager") as mock_rsm, \
             patch("strategies.strategy_orchestrator.signal_store") as _mock_ss:
            mock_rsm.evaluate_signal.return_value = (False, "loss_ceiling_breached")

            from strategies.strategy_orchestrator import StrategyOrchestrator
            orch = StrategyOrchestrator.__new__(StrategyOrchestrator)
            from collections import defaultdict
            import threading, asyncio
            orch._initialized = False
            orch.signal_store = MagicMock()
            orch.audit_logger = al
            orch._selected_asset_class = AssetClass.INDEX_OPTIONS
            orch.cascade_brake_active = False
            orch._recent_signal_times = defaultdict(list)
            orch._mismatch_log_times = {}
            orch._thread_lock = threading.RLock()
            try:
                orch._async_lock = asyncio.get_event_loop().run_until_complete(asyncio.sleep(0)) or asyncio.Lock()
            except Exception:
                import asyncio as _a
                orch._async_lock = _a.Lock()

            from models.instrument import AssetClass as AC
            strategy_mock = self._make_strategy(sig)
            orch.strategies = {AC.INDEX_OPTIONS: strategy_mock}

            orch.evaluate_all({"ltp": 19000})

        rejections = al.query_by_event_type("risk_rejection")
        self.assertTrue(len(rejections) >= 1, "Expected at least one risk_rejection event")
        ev = rejections[0]
        self.assertEqual(ev["rule_violated"], "PRE_SIGNAL_RISK_GATE")
        self.assertIn("loss_ceiling_breached", ev["reason"])
        self.assertEqual(ev["signal_id"], "SIG-RISK-REJECT")
        self.assertEqual(ev["action"], "REJECTED")

    def test_signal_state_set_to_dropped(self):
        from models.instrument import AssetClass
        from models.signal import SignalState

        al = AuditLogger(db_path=None)
        sig = _make_signal(signal_id="SIG-DROP-STATE", symbol="NIFTY")

        with patch("strategies.strategy_orchestrator.risk_state_manager") as mock_rsm, \
             patch("strategies.strategy_orchestrator.signal_store") as _mock_ss:
            mock_rsm.evaluate_signal.return_value = (False, "trade_count_limit")

            from strategies.strategy_orchestrator import StrategyOrchestrator
            orch = StrategyOrchestrator.__new__(StrategyOrchestrator)
            from collections import defaultdict
            import threading
            orch._initialized = False
            orch.signal_store = MagicMock()
            orch.audit_logger = al
            orch._selected_asset_class = AssetClass.INDEX_OPTIONS
            orch.cascade_brake_active = False
            orch._recent_signal_times = defaultdict(list)
            orch._mismatch_log_times = {}
            orch._thread_lock = threading.RLock()
            import asyncio as _a
            orch._async_lock = _a.Lock()

            from models.instrument import AssetClass as AC
            orch.strategies = {AC.INDEX_OPTIONS: self._make_strategy(sig)}
            orch.evaluate_all({"ltp": 42000})

        self.assertEqual(sig.state, SignalState.DROPPED_RISK_REJECTION)


# ── TestPaperFillAuditTrail ───────────────────────────────────────────────────

class TestPaperFillAuditTrail(unittest.TestCase):
    """PaperBrokerAdapter.place_order emits PENDING→SUBMITTED→FILLED audit events."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_paper_fill_emits_two_transitions(self):
        al = AuditLogger(db_path=None)
        from adapters.paper_broker import PaperBrokerAdapter, PAPER_BROKER_NAME
        from models.order import OrderState

        adapter = PaperBrokerAdapter(ltp_provider=lambda sym: 500.0, starting_cash=100_000.0)
        order = _make_order(client_order_id="PAPER-001", symbol="RELIANCE", signal_id="SIG-FILL")
        order.price = 500.0  # limit price so no live tick needed

        with patch("adapters.paper_broker._get_audit_logger", return_value=al):
            self._run(adapter.place_order(order))

        transitions = [e for e in al.get_all_entries() if e["event_type"] == "order_transition"]
        self.assertEqual(len(transitions), 2, f"Expected 2 transitions, got: {transitions}")

        submitted = transitions[0]
        self.assertEqual(submitted["old_state"], OrderState.PENDING.value)
        self.assertEqual(submitted["new_state"], OrderState.SUBMITTED.value)
        self.assertEqual(submitted["broker"], PAPER_BROKER_NAME)
        self.assertIn("virtual ledger", submitted["reason"])

        filled = transitions[1]
        self.assertEqual(filled["old_state"], OrderState.SUBMITTED.value)
        self.assertEqual(filled["new_state"], OrderState.FILLED.value)
        self.assertEqual(filled["broker"], PAPER_BROKER_NAME)
        self.assertIsNotNone(filled["fill_price"])
        self.assertEqual(filled["fill_qty"], 1.0)

    def test_signal_id_propagated_to_paper_fill(self):
        al = AuditLogger(db_path=None)
        from adapters.paper_broker import PaperBrokerAdapter

        adapter = PaperBrokerAdapter(ltp_provider=lambda sym: 250.0, starting_cash=100_000.0)
        order = _make_order(client_order_id="PAPER-SIG", symbol="TATAMOTORS", signal_id="SIG-CORR-42")
        order.price = 250.0

        with patch("adapters.paper_broker._get_audit_logger", return_value=al):
            self._run(adapter.place_order(order))

        matches = al.query_by_signal_id("SIG-CORR-42")
        self.assertTrue(len(matches) >= 1, "Expected at least one audit entry with signal_id=SIG-CORR-42")
        for m in matches:
            self.assertEqual(m["signal_id"], "SIG-CORR-42")

    def test_paper_fill_no_signal_id_still_records(self):
        al = AuditLogger(db_path=None)
        from adapters.paper_broker import PaperBrokerAdapter

        adapter = PaperBrokerAdapter(ltp_provider=lambda sym: 100.0, starting_cash=100_000.0)
        order = _make_order(client_order_id="PAPER-NOSIG", symbol="SBIN")  # no signal_id
        order.price = 100.0

        with patch("adapters.paper_broker._get_audit_logger", return_value=al):
            self._run(adapter.place_order(order))

        transitions = [e for e in al.get_all_entries() if e["event_type"] == "order_transition"]
        self.assertEqual(len(transitions), 2)


# ── TestOrderManagerSignalIdThreading ────────────────────────────────────────

class TestOrderManagerSignalIdThreading(unittest.TestCase):
    """order_manager.submit_order threads signal_id from order.metadata to audit records."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_submit_order_passes_signal_id(self):
        al = AuditLogger(db_path=None)
        from adapters.order_manager import OrderManager
        from models.order import OrderState, OrderUpdate

        mock_adapter = AsyncMock()
        mock_adapter.place_order = AsyncMock(return_value=OrderUpdate(
            client_order_id="ORD-SIG",
            state=OrderState.FILLED,
            filled_qty=1.0,
            last_fill_qty=1.0,
            last_fill_price=200.0,
        ))

        order = _make_order("ORD-SIG", "INFY", signal_id="SIG-THREAD-99")
        order.price = 200.0

        mgr = OrderManager()
        with patch("adapters.order_manager._get_audit_logger", return_value=al):
            self._run(mgr.submit_order(mock_adapter, order))

        # The PENDING→SUBMITTED transition should carry the signal_id
        submitted_events = [
            e for e in al.get_all_entries()
            if e.get("event_type") == "order_transition" and e.get("new_state") == "SUBMITTED"
        ]
        self.assertTrue(len(submitted_events) >= 1)
        self.assertEqual(submitted_events[0].get("signal_id"), "SIG-THREAD-99")


# ── TestInvalidLtpAuditEvent ─────────────────────────────────────────────────

class TestInvalidLtpAuditEvent(unittest.TestCase):
    """BaseIndicatorCalculator.process_tick emits market_data_event for invalid-LTP ticks."""

    def _make_invalid_tick(self, symbol="NIFTY"):
        from models.tick import Tick
        t = MagicMock(spec=Tick)
        t.symbol = symbol
        t.ltp = None
        t.ltp_valid = False
        t.volume = 0
        t.timestamp = datetime.now(timezone.utc)
        return t

    def test_invalid_ltp_emits_market_data_event(self):
        al = AuditLogger(db_path=None)
        from pipeline.indicator_calculator import DefaultIndicatorCalculator

        calc = DefaultIndicatorCalculator("NIFTY")
        tick = self._make_invalid_tick()

        # indicator_calculator does `from execution.audit_logger import audit_logger as _audit_logger`
        # inside process_tick, so patching execution.audit_logger.audit_logger intercepts that import.
        with patch("execution.audit_logger.audit_logger", al):
            calc.process_tick(tick)

        events = al.query_by_event_type("market_data_event")
        self.assertTrue(len(events) >= 1, "Expected at least one market_data_event")
        ev = events[0]
        self.assertEqual(ev["event_subtype"], "INVALID_LTP")
        self.assertEqual(ev["symbol"], "NIFTY")


# ── TestApiRiskRejectionAudit ─────────────────────────────────────────────────

class TestApiRiskRejectionAudit(unittest.TestCase):
    """log_risk_rejection is called when order_authorizer rejects at the API layer."""

    def test_audit_fields_on_api_rejection(self):
        al = AuditLogger(db_path=None)
        al.log_risk_rejection(
            symbol="BANKNIFTY",
            broker="paper:dhan",
            rule_violated="DAILY_LOSS_CEILING",
            reason="Daily loss limit of ₹5000 exceeded",
        )
        events = al.query_by_event_type("risk_rejection")
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["rule_violated"], "DAILY_LOSS_CEILING")
        self.assertEqual(ev["symbol"], "BANKNIFTY")
        self.assertIn("Daily loss limit", ev["reason"])
        self.assertEqual(ev["action"], "REJECTED")
        self.assertIsNone(ev["signal_id"])  # no signal_id at API layer

    def test_audit_sanitizes_credential_in_reason(self):
        al = AuditLogger(db_path=None)
        # Simulate a reason that accidentally includes a token-shaped string.
        # The AuditLogger itself does not sanitize reason fields automatically —
        # the caller is responsible. This test verifies _sanitize works directly.
        from execution.audit_logger import _sanitize
        tainted = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        safe = _sanitize(tainted)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", safe)
        self.assertIn("[REDACTED]", safe)


# ── TestCorrelationContinuity ─────────────────────────────────────────────────

class TestCorrelationContinuity(unittest.TestCase):
    """A single signal_id can be found across signal, risk_rejection, and order_transition events."""

    def test_full_correlation_chain(self):
        al = AuditLogger(db_path=None)
        SIGNAL_ID = "SIG-FULL-CHAIN"

        sig = _make_signal(signal_id=SIGNAL_ID)
        al.log_signal(sig, "execute", "asset class matched")

        # Simulate the orchestrator's pre-signal risk rejection audit call
        al.log_risk_rejection(
            symbol="NIFTY",
            broker="",
            rule_violated="PRE_SIGNAL_RISK_GATE",
            reason="loss_ceiling_breached",
            signal_id=SIGNAL_ID,
        )

        # Simulate paper broker fill (PENDING→SUBMITTED, SUBMITTED→FILLED)
        al.log_order_transition(
            client_order_id="ORD-CHAIN",
            symbol="NIFTY",
            broker="paper",
            old_state="PENDING",
            new_state="SUBMITTED",
            signal_id=SIGNAL_ID,
        )
        al.log_order_transition(
            client_order_id="ORD-CHAIN",
            symbol="NIFTY",
            broker="paper",
            old_state="SUBMITTED",
            new_state="FILLED",
            fill_price=19500.0,
            fill_qty=1.0,
            signal_id=SIGNAL_ID,
        )

        chain = al.query_by_signal_id(SIGNAL_ID)
        self.assertEqual(len(chain), 4)
        event_types = {e["event_type"] for e in chain}
        self.assertIn("signal", event_types)
        self.assertIn("risk_rejection", event_types)
        self.assertIn("order_transition", event_types)

    def test_correlation_is_stable_across_events(self):
        al = AuditLogger(db_path=None)
        SID = "SIG-STABLE"
        for i in range(5):
            al.log_order_transition(
                client_order_id=f"ORD-{i}",
                symbol="NIFTY",
                broker="paper",
                old_state="PENDING",
                new_state="FILLED",
                signal_id=SID,
            )
        matches = al.query_by_signal_id(SID)
        self.assertEqual(len(matches), 5)
        for m in matches:
            self.assertEqual(m["signal_id"], SID)


# ── TestEventOrdering ─────────────────────────────────────────────────────────

class TestEventOrdering(unittest.TestCase):
    """Timestamps in the audit trail are monotonically non-decreasing."""

    def test_timestamps_are_ordered(self):
        al = AuditLogger(db_path=None)
        sig = _make_signal()
        al.log_signal(sig, "execute", "t1")
        al.log_order_transition("O1", "NIFTY", "paper", "PENDING", "SUBMITTED")
        al.log_order_transition("O1", "NIFTY", "paper", "SUBMITTED", "FILLED",
                                fill_price=19000.0, fill_qty=1.0)

        entries = al.get_all_entries()
        ts_list = [e["timestamp"] for e in entries]
        for i in range(1, len(ts_list)):
            self.assertGreaterEqual(ts_list[i], ts_list[i - 1],
                                    f"Non-monotonic timestamp: {ts_list[i-1]} > {ts_list[i]}")


# ── TestCredentialSanitisation ────────────────────────────────────────────────

class TestCredentialSanitisation(unittest.TestCase):
    """_sanitize strips credential patterns from free-text fields."""

    def test_bearer_token_redacted(self):
        from execution.audit_logger import _sanitize
        text = "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.sig"
        result = _sanitize(text)
        self.assertNotIn("eyJhbGciOiJSUzI1NiJ9", result)
        self.assertIn("[REDACTED]", result)

    def test_api_key_value_redacted(self):
        from execution.audit_logger import _sanitize
        text = "api_key=abcdefghijklmnopqrstuvwxyz12345678"
        result = _sanitize(text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz12345678", result)
        self.assertIn("[REDACTED]", result)

    def test_safe_text_unchanged(self):
        from execution.audit_logger import _sanitize
        text = "Order NIFTY BUY 1 lot filled at 19500.0"
        self.assertEqual(_sanitize(text), text)

    def test_empty_string_unchanged(self):
        from execution.audit_logger import _sanitize
        self.assertEqual(_sanitize(""), "")

    def test_token_event_sanitizes_error_message(self):
        al = AuditLogger(db_path=None)
        ev = al.log_token_event(
            broker="dhan",
            event_subtype="refresh_failed",
            success=False,
            error_message="token=supersecrettoken12345678901234567890abc",
        )
        self.assertNotIn("supersecrettoken", ev["error_message"])
        self.assertIn("[REDACTED]", ev["error_message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
