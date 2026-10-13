"""
Strike Desk V4 - Phase 4 Integration Service
Unified orchestration service connecting all Phase 4 modules:
- 5 Asset Class Strategies (StockCash, StockFutures, IndexFutures, IndexOptions, StockOptions)
- StrategyOrchestrator (universal signal generation, cascade brake, atomic category switching)
- ExecutionRouter (selective execution gate & zero-routing enforcement)
- AuditLogger (immutable audit trail)
- SignalStore (top-2 recommendations per asset class)
- ConfidenceScorer (measurable, non-probabilistic setup scoring)
- Open Position Policy Manager (Review Gap D)
"""
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Dict, List, Optional

from execution.audit_logger import AuditLogger, audit_logger
from execution.execution_router import ExecutionRouter, execution_router
from models.instrument import AssetClass
from models.signal import ConfidenceTier, Direction, Signal
from strategies.base_strategy import BaseStrategy
from strategies.confidence import ConfidenceScorer, confidence_scorer
from strategies.signal_store import SignalStore, signal_store
from strategies.strategy_orchestrator import StrategyOrchestrator, strategy_orchestrator

logger = logging.getLogger(__name__)


class Phase4IntegrationService:
    """
    Unified end-to-end integration service for Phase 4.
    Runs evaluation cycles across all asset classes, gates executions to the selected category,
    retains top-2 alerts per class, records immutable audit trails, and manages open positions.
    """

    def __init__(
        self,
        orchestrator: Optional[StrategyOrchestrator] = None,
        router: Optional[ExecutionRouter] = None,
        audit_log: Optional[AuditLogger] = None,
        store: Optional[SignalStore] = None,
        scorer: Optional[ConfidenceScorer] = None,
    ) -> None:
        self.signal_store = store or signal_store
        self.audit_logger = audit_log or audit_logger
        self.confidence_scorer = scorer or confidence_scorer

        self.orchestrator = orchestrator or strategy_orchestrator
        self.router = router or execution_router

        self._open_positions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

        self.initialize()

    def initialize(self) -> None:
        """
        Instantiates and binds all components into a cohesive pipeline.
        """
        with self._lock:
            self.orchestrator.signal_store = self.signal_store
            self.orchestrator.audit_logger = self.audit_logger
            self.router.audit = self.audit_logger

            for strat in self.orchestrator.strategies.values():
                strat.signal_store = self.signal_store

        logger.info(
            "[Phase4IntegrationService] Initialized unified service with %d strategies.",
            len(self.orchestrator.strategies),
        )

    def set_selected_asset_class(self, asset_class: AssetClass) -> None:
        """
        Atomically switches the active asset class category.
        Enforces Review Gap D: Existing open positions are strictly preserved.
        """
        with self._lock:
            self.orchestrator.set_selected_asset_class(asset_class)

    def get_selected_asset_class(self) -> AssetClass:
        """Returns the currently active asset class for execution."""
        return self.orchestrator.get_selected_asset_class()

    def add_open_position(
        self, position_id: str, asset_class: AssetClass, details: Dict[str, Any]
    ) -> None:
        """
        Records an active open position.
        Enforces Review Gap D: Switching selected asset class NEVER affects open positions.
        """
        with self._lock:
            self._open_positions[position_id] = {
                "position_id": position_id,
                "asset_class": asset_class,
                "created_at": datetime.now(timezone.utc).isoformat(),
                **details,
            }

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        """Returns snapshot of current open positions."""
        with self._lock:
            return dict(self._open_positions)

    def run_evaluation_cycle(
        self,
        indicators: Dict[str, Any],
        market_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes an end-to-end evaluation cycle:
        1. Orchestrator evaluates indicators across all 5 strategies.
        2. Signals collected from all classes.
        3. Top-2 signals stored in SignalStore.
        4. Cascade brake checked.
        5. Signals routed through ExecutionRouter selective gate.
        6. Actions logged to immutable AuditLogger.
        Returns:
            {
                "signals_generated": int,
                "signals_executed": int,
                "signals_alert_only": int,
                "cascade_brake": bool,
                "audit_entries": int,
                "routed_results": List[Dict[str, Any]]
            }
        """
        state = market_state or {}
        now = state.get("timestamp") or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        with self._lock:
            all_signals_by_class = self.orchestrator.evaluate_all(indicators, state)

            flattened_signals: List[Signal] = []
            for sig_list in all_signals_by_class.values():
                flattened_signals.extend(sig_list)

            cascade_active = self.orchestrator.cascade_brake_active

            selected_class = self.orchestrator.get_selected_asset_class()
            routed_results = self.router.route_signals(
                flattened_signals,
                selected_class,
                current_time=now,
                cascade_brake_active=cascade_active,
            )

            executed_count = sum(1 for r in routed_results if r["action"] == "execute")
            alert_only_count = sum(1 for r in routed_results if r["action"] == "alert_only")
            total_audit = len(self.audit_logger.get_all_entries())

            return {
                "signals_generated": len(flattened_signals),
                "signals_executed": executed_count,
                "signals_alert_only": alert_only_count,
                "cascade_brake": cascade_active,
                "audit_entries": total_audit,
                "routed_results": routed_results,
            }

    def get_dashboard_alerts(self) -> Dict[AssetClass, List[Signal]]:
        """
        Returns top 2 active recommendations per asset class (up to 10 total alerts).
        Enforces Blueprint Rule 10.
        """
        alerts: Dict[AssetClass, List[Signal]] = {}
        for ac in [
            AssetClass.STOCK_CASH,
            AssetClass.STOCK_FUTURES,
            AssetClass.INDEX_FUTURES,
            AssetClass.INDEX_OPTIONS,
            AssetClass.STOCK_OPTIONS,
        ]:
            alerts[ac] = self.signal_store.get_top_signals(ac, n=2)
        return alerts

    def get_status(self) -> Dict[str, Any]:
        """Returns full telemetry of all integrated Phase 4 subsystems."""
        with self._lock:
            return {
                "orchestrator": self.orchestrator.get_status(),
                "selected_asset_class": (
                    self.get_selected_asset_class().value
                    if hasattr(self.get_selected_asset_class(), "value")
                    else str(self.get_selected_asset_class())
                ),
                "open_positions_count": len(self._open_positions),
                "open_positions": list(self._open_positions.values()),
                "signal_store_stats": self.signal_store.get_status(),
                "audit_trail_entries": len(self.audit_logger.get_all_entries()),
            }

    def reset(self) -> None:
        """Resets all internal states, stores, and audit logs."""
        with self._lock:
            self.orchestrator.reset()
            self.signal_store.clear()
            self.audit_logger.clear()
            self._open_positions.clear()


# Global singleton instance
phase4_integration_service = Phase4IntegrationService()