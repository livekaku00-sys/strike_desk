"""
Strike Desk V4 — Phase 2 Regression Suite: Pre-Signal Risk Execution Gate

Tests:
  [a] Signal within all limits is allowed (evaluate_signal returns True)
  [b] Signal exceeding max trade size is rejected → DROPPED_RISK_REJECTION
  [c] Signal that would breach max daily loss is rejected → DROPPED_RISK_REJECTION
  [d] Signal that would breach max daily loss — boundary: projected loss exactly at ceiling
  [e] Signal with no stop_loss set skips daily-loss check (no false rejection)
  [f] Position limit reached → rejected → DROPPED_RISK_REJECTION
  [g] Rejection logged at INFO with "[Risk] Signal Dropped" via orchestrator
  [h] Rejected signal does NOT appear in orchestrator signal store
  [i] SignalState has DROPPED_RISK_REJECTION value
  [j] Allowed signal (within limits) proceeds to ACTIVE state in store
"""
import logging
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from execution.audit_logger import AuditLogger
from models.instrument import AssetClass, Exchange
from models.signal import (
    ConfidenceTier,
    Direction,
    Signal,
    SignalState,
    SignalType,
)
from pipeline.indicators.pcr_staleness_monitor import pcr_staleness_monitor
from risk.risk_state_manager import risk_state_manager
from strategies.signal_store import SignalStore
from strategies.strategy_orchestrator import StrategyOrchestrator
from tests.option_chain_fixture import seed_many

UTC = timezone.utc


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_signal(
    entry_price: float = 200.0,
    stop_loss_price: float | None = 180.0,
    direction: Direction = Direction.LONG,
    confidence_score: float = 80.0,
    confidence_tier: ConfidenceTier = ConfidenceTier.A,
) -> Signal:
    return Signal(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        asset_class=AssetClass.INDEX_OPTIONS,
        signal_type=SignalType.PCR_SLOPE_BULLISH,
        direction=direction,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        confidence_score=confidence_score,
        confidence_tier=confidence_tier,
        timestamp=datetime(2026, 9, 30, 9, 30, 0, tzinfo=UTC),
    )


def _portfolio(
    current_daily_pnl: float = 0.0,
    open_positions_count: int = 0,
    max_capital_per_trade: float = 50_000.0,
    daily_loss_ceiling: float = -2_500.0,
    max_open_positions: int = 5,
    quantity: float | None = None,
) -> dict:
    ps: dict = {
        "current_daily_pnl": current_daily_pnl,
        "open_positions_count": open_positions_count,
        "max_capital_per_trade": max_capital_per_trade,
        "daily_loss_ceiling": daily_loss_ceiling,
        "max_open_positions": max_open_positions,
    }
    if quantity is not None:
        ps["quantity"] = quantity
    return ps


def _make_orchestrator() -> StrategyOrchestrator:
    pcr_staleness_monitor.clear()
    seed_many({"NIFTY": 24520.0, "RELIANCE": 2520.0})
    store = SignalStore()
    al = AuditLogger()
    orch = StrategyOrchestrator(signal_store_instance=store, audit_logger_instance=al)
    orch.reset()
    return orch


# ── Unit tests: evaluate_signal() ────────────────────────────────────────────

class TestEvaluateSignal(unittest.TestCase):

    # [a] All limits respected → allowed
    def test_a_signal_within_limits_is_allowed(self):
        # entry=200, sl=180, qty=5 → trade_risk=5*20=100
        # projected_pnl = 0 - 100 = -100 > -2500 → allowed
        sig = _make_signal(entry_price=200.0, stop_loss_price=180.0)
        allowed, reason = risk_state_manager.evaluate_signal(
            sig, _portfolio(current_daily_pnl=0.0, quantity=5)
        )
        self.assertTrue(allowed, f"Expected allowed=True, got reason: {reason}")
        self.assertIsNone(reason)

    # [b] Max trade size exceeded → rejected
    def test_b_max_trade_size_rejected(self):
        sig = _make_signal(entry_price=60_000.0)
        allowed, reason = risk_state_manager.evaluate_signal(
            sig, _portfolio(max_capital_per_trade=50_000.0)
        )
        self.assertFalse(allowed)
        self.assertIsNotNone(reason)
        self.assertIn("Max trade size exceeded", reason,
                      f"Unexpected rejection reason: {reason}")
        self.assertIn("60000", reason)
        self.assertIn("50000", reason)

    # [c] Daily loss ceiling would be breached → rejected
    def test_c_max_daily_loss_breach_rejected(self):
        # entry=200, sl=180, qty=5 → sl_distance=20, trade_risk=5*20=100
        # projected = -2400 - 100 = -2500 <= -2500 → reject
        sig = _make_signal(entry_price=200.0, stop_loss_price=180.0, direction=Direction.LONG)
        allowed, reason = risk_state_manager.evaluate_signal(sig, _portfolio(
            current_daily_pnl=-2_400.0,
            daily_loss_ceiling=-2_500.0,
            quantity=5,
        ))
        self.assertFalse(allowed, "Expected rejection when projected loss hits ceiling")
        self.assertIsNotNone(reason)
        self.assertIn("Max daily loss", reason, f"Unexpected reason: {reason}")

    # [d] Boundary: projected loss exactly at ceiling → rejected
    def test_d_daily_loss_boundary_is_inclusive(self):
        # entry=500, sl=400, LONG, qty=1 → sl_distance=100, trade_risk=100
        # projected = -2400 - 100 = -2500 <= -2500 → reject
        sig = _make_signal(entry_price=500.0, stop_loss_price=400.0, direction=Direction.LONG)
        allowed, reason = risk_state_manager.evaluate_signal(sig, _portfolio(
            current_daily_pnl=-2_400.0,
            daily_loss_ceiling=-2_500.0,
            quantity=1,
        ))
        self.assertFalse(allowed, "Exactly at ceiling should be rejected (<=)")

    # [e] No stop_loss → daily-loss check skipped (no false rejection)
    def test_e_no_stop_loss_skips_daily_loss_check(self):
        sig = _make_signal(entry_price=200.0, stop_loss_price=None)
        allowed, reason = risk_state_manager.evaluate_signal(sig, _portfolio(
            current_daily_pnl=-2_490.0,
            daily_loss_ceiling=-2_500.0,
            quantity=100,
        ))
        # No stop_loss → daily-loss check is skipped regardless of quantity
        self.assertTrue(allowed, f"Expected allowed when no stop_loss set, got: {reason}")

    # [f] Position limit reached → rejected
    def test_f_position_limit_rejected(self):
        # No stop_loss so daily-loss check is skipped; entry_price is well below trade cap
        # This isolates the position-limit check exclusively.
        sig = _make_signal(entry_price=200.0, stop_loss_price=None)
        allowed, reason = risk_state_manager.evaluate_signal(sig, _portfolio(
            open_positions_count=5,
            max_open_positions=5,
        ))
        self.assertFalse(allowed)
        self.assertIn("Max open positions", reason, f"Unexpected reason: {reason}")
        self.assertIn("5/5", reason)

    # SHORT direction: sl_distance computed correctly
    def test_short_direction_sl_distance(self):
        # SHORT: sl_distance = stop_loss - entry = 220 - 200 = 20, qty=5, trade_risk=100
        # projected = -2400 - 100 = -2500 <= -2500 → rejected
        sig = _make_signal(entry_price=200.0, stop_loss_price=220.0, direction=Direction.SHORT)
        allowed, reason = risk_state_manager.evaluate_signal(sig, _portfolio(
            current_daily_pnl=-2_400.0,
            daily_loss_ceiling=-2_500.0,
            quantity=5,
        ))
        self.assertFalse(allowed, "SHORT direction risk should be computed and rejected")

    # Signal below ceiling after trade risk → allowed
    def test_trade_risk_within_remaining_budget_allowed(self):
        # entry=200, sl=180, qty=5 → sl_dist=20, trade_risk=100
        # current_pnl=-2300, projected = -2300-100 = -2400 > -2500 → allowed
        sig = _make_signal(entry_price=200.0, stop_loss_price=180.0)
        allowed, reason = risk_state_manager.evaluate_signal(sig, _portfolio(
            current_daily_pnl=-2_300.0,
            daily_loss_ceiling=-2_500.0,
            quantity=5,
        ))
        self.assertTrue(allowed, f"Should be allowed when budget remains: {reason}")


# ── SignalState enum completeness ─────────────────────────────────────────────

class TestSignalStateDroppedRiskRejection(unittest.TestCase):

    # [i] DROPPED_RISK_REJECTION is in SignalState
    def test_i_dropped_risk_rejection_in_enum(self):
        self.assertIn(
            "DROPPED_RISK_REJECTION",
            {s.value for s in SignalState},
            "SignalState must include DROPPED_RISK_REJECTION",
        )

    def test_state_can_be_assigned_to_signal(self):
        sig = _make_signal()
        sig.state = SignalState.DROPPED_RISK_REJECTION
        self.assertEqual(sig.state, SignalState.DROPPED_RISK_REJECTION)
        self.assertEqual(sig.to_dict()["state"], "DROPPED_RISK_REJECTION")


# ── Orchestrator integration tests ────────────────────────────────────────────

class TestOrchestratorRiskGate(unittest.TestCase):

    def setUp(self):
        self.orch = _make_orchestrator()
        self.t0 = datetime(2026, 9, 30, 9, 30, 0, tzinfo=UTC)
        self.indicators = {
            AssetClass.INDEX_OPTIONS: {
                "symbol": "NIFTY",
                "underlying": "NIFTY",
                "price": 24520.0, "prev_price": 24490.0,
                "ema9": 24500.0, "prev_ema9": 24480.0,
                "vwap": 24480.0, "pcr_slope": -0.12,
                "oi_change": 5.0, "sentiment": 30.0,
            },
        }

    # [g] Rejection logs at INFO with "[Risk] Signal Dropped"
    def test_g_rejection_logged_at_info(self):
        with patch.object(
            risk_state_manager,
            "evaluate_signal",
            return_value=(False, "Max trade size exceeded (Signal=60000, Limit=50000)"),
        ):
            with self.assertLogs("strategies.strategy_orchestrator", level="INFO") as cm:
                self.orch.evaluate_all(
                    self.indicators,
                    market_state={"timestamp": self.t0},
                    locked_class=AssetClass.INDEX_OPTIONS,
                )
        risk_logs = [line for line in cm.output if "[Risk] Signal Dropped" in line]
        self.assertGreater(
            len(risk_logs), 0,
            "Expected at least one INFO log containing '[Risk] Signal Dropped'.\n"
            "Got:\n" + "\n".join(cm.output),
        )
        self.assertTrue(
            any("Max trade size exceeded" in line for line in risk_logs),
            "Risk rejection reason must appear in the log line",
        )

    # [h] Rejected signal does NOT appear in signal store
    def test_h_rejected_signal_not_in_store(self):
        with patch.object(
            risk_state_manager,
            "evaluate_signal",
            return_value=(False, "Max trade size exceeded (Signal=60000, Limit=50000)"),
        ):
            self.orch.evaluate_all(
                self.indicators,
                market_state={"timestamp": self.t0},
                locked_class=AssetClass.INDEX_OPTIONS,
            )
        idx_opt_signals = self.orch.signal_store.get_top_signals(AssetClass.INDEX_OPTIONS)
        self.assertEqual(
            len(idx_opt_signals), 0,
            f"Risk-rejected signal must not reach the signal store. "
            f"Got {len(idx_opt_signals)} signals.",
        )

    # [j] Allowed signal (within limits) proceeds to ACTIVE state in store
    def test_j_allowed_signal_reaches_store_as_active(self):
        with patch.object(
            risk_state_manager,
            "evaluate_signal",
            return_value=(True, None),
        ):
            self.orch.evaluate_all(
                self.indicators,
                market_state={"timestamp": self.t0},
                locked_class=AssetClass.INDEX_OPTIONS,
            )
        idx_opt_signals = self.orch.signal_store.get_top_signals(AssetClass.INDEX_OPTIONS)
        self.assertGreater(
            len(idx_opt_signals), 0,
            "Signal passing risk gate must reach the signal store.",
        )
        for sig in idx_opt_signals:
            self.assertEqual(
                sig.state, SignalState.ACTIVE,
                f"Stored signal must be ACTIVE, got {sig.state}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
