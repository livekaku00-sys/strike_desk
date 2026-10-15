"""
Strike Desk V4 - Unified Strategy Orchestrator
Coordinates all 5 asset class strategies:
1. STOCK_CASH (StockCashStrategy)
2. STOCK_FUTURES (StockFuturesStrategy)
3. INDEX_FUTURES (IndexFuturesStrategy)
4. INDEX_OPTIONS (IndexOptionsStrategy)
5. STOCK_OPTIONS (StockOptionsStrategy)

Enforces:
- Universal signal generation across all 5 classes (Blueprint Rule 9)
- Top 2 signal retention per asset class (Blueprint Rule 10)
- Cascade brake for multi-asset correlation (Review Risk 3)
- Atomic asset class selection switching (Review Gap D)
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Union

from execution.audit_logger import AuditLogger, audit_logger
from models.instrument import AssetClass
from models.signal import Signal, SignalState
from risk.risk_state_manager import risk_state_manager
from strategies.base_strategy import BaseStrategy
from strategies.index_futures_strategy import IndexFuturesStrategy
from strategies.index_options_strategy import IndexOptionsStrategy
from strategies.signal_store import SignalStore, signal_store
from strategies.stock_cash_strategy import StockCashStrategy
from strategies.stock_futures_strategy import StockFuturesStrategy
from strategies.stock_options_strategy import StockOptionsStrategy

logger = logging.getLogger(__name__)

_MISMATCH_LOG_INTERVAL_S = 60


class AwaitableDict(dict):
    """Dict subclass that allows both synchronous usage and async await."""
    def __await__(self):
        async def _coro():
            return self
        return _coro().__await__()


class AwaitableValue:
    """Wrapper that allows both synchronous assignment and async await."""
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def __await__(self):
        async def _coro():
            return self.value
        return _coro().__await__()


class StrategyOrchestrator:
    """
    Unified Orchestrator coordinating all 5 asset class strategies.
    Thread-safe and async-safe.
    """

    _instance: Optional["StrategyOrchestrator"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super(StrategyOrchestrator, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(
        self,
        signal_store_instance: Optional[SignalStore] = None,
        audit_logger_instance: Optional[AuditLogger] = None,
    ) -> None:
        if getattr(self, "_initialized", False):
            if signal_store_instance is not None:
                self.signal_store = signal_store_instance
                for strat in self.strategies.values():
                    strat.signal_store = signal_store_instance
            if audit_logger_instance is not None:
                self.audit_logger = audit_logger_instance
            return

        self.signal_store = signal_store_instance or signal_store
        self.audit_logger = audit_logger_instance or audit_logger

        self._selected_asset_class: AssetClass = AssetClass.STOCK_CASH
        self.cascade_brake_active: bool = False

        self.strategies: Dict[AssetClass, BaseStrategy] = {}
        self._recent_signal_times: Dict[AssetClass, List[datetime]] = defaultdict(list)
        self._mismatch_log_times: Dict[tuple, float] = {}

        self._thread_lock = threading.RLock()
        self._async_lock = asyncio.Lock()

        self.initialize()
        self._initialized = True

    def initialize(self) -> None:
        """
        Instantiates all 5 concrete asset class strategies and binds to signal store.
        """
        with self._thread_lock:
            self.strategies = {
                AssetClass.STOCK_CASH: StockCashStrategy(),
                AssetClass.STOCK_FUTURES: StockFuturesStrategy(),
                AssetClass.INDEX_FUTURES: IndexFuturesStrategy(),
                AssetClass.INDEX_OPTIONS: IndexOptionsStrategy(),
                AssetClass.STOCK_OPTIONS: StockOptionsStrategy(),
            }
            for strat in self.strategies.values():
                strat.signal_store = self.signal_store

        logger.info(
            "[StrategyOrchestrator] Initialized all 5 asset class strategies: %s",
            list(self.strategies.keys()),
        )

    @property
    def selected_asset_class(self) -> AssetClass:
        with self._thread_lock:
            return self._selected_asset_class

    @selected_asset_class.setter
    def selected_asset_class(self, val: AssetClass) -> None:
        self.set_selected_asset_class(val)

    def set_selected_asset_class(self, asset_class: AssetClass) -> AwaitableValue:
        """
        Atomically changes the active asset class category.
        Takes effect immediately with no restart required.
        Thread-safe and async-safe.
        """
        with self._thread_lock:
            old = self._selected_asset_class
            self._selected_asset_class = asset_class
            self.audit_logger.log_selection_change(old, asset_class)

        return AwaitableValue(None)

    def get_selected_asset_class(self) -> AssetClass:
        """Returns currently selected asset class."""
        with self._thread_lock:
            return self._selected_asset_class

    def _record_signal_timestamp(self, asset_class: AssetClass, timestamp: datetime) -> None:
        """Records timestamp of generated signal for cascade brake evaluation."""
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        with self._thread_lock:
            self._recent_signal_times[asset_class].append(ts)
            cutoff = ts - timedelta(seconds=60)
            self._recent_signal_times[asset_class] = [
                t for t in self._recent_signal_times[asset_class] if t >= cutoff
            ]

    def check_cascade_brake(self, current_time: Optional[datetime] = None) -> bool:
        """
        Review Risk 3: Evaluates multi-asset class correlation.
        If 3+ asset classes generated signals within last 30s:
          - Sets cascade_brake_active = True
          - Logs warning
          - Returns True (downstream orders reduce size by 50%)
        """
        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        with self._thread_lock:
            cutoff_30 = now - timedelta(seconds=30)
            triggered_classes = [
                c
                for c, times in self._recent_signal_times.items()
                if any(t >= cutoff_30 for t in times)
            ]

            if len(triggered_classes) >= 3:
                self.cascade_brake_active = True
                classes_str = [c.value if hasattr(c, "value") else str(c) for c in triggered_classes]
                logger.warning(
                    "Cascade brake triggered: %s signaled within 30s",
                    classes_str,
                )
                self.audit_logger.log_cascade_brake(triggered_classes)
                return True
            else:
                cutoff_60 = now - timedelta(seconds=60)
                classes_60 = [
                    c
                    for c, times in self._recent_signal_times.items()
                    if any(t >= cutoff_60 for t in times)
                ]
                if len(classes_60) < 3:
                    self.cascade_brake_active = False
                return self.cascade_brake_active

    def _get_indicators_for_class(
        self, indicators: Dict[str, Any], asset_class: AssetClass
    ) -> Dict[str, Any]:
        """Extracts indicators specific to an asset class or returns full dictionary."""
        if asset_class in indicators and isinstance(indicators[asset_class], dict):
            return indicators[asset_class]
        val = asset_class.value if hasattr(asset_class, "value") else str(asset_class)
        if val in indicators and isinstance(indicators[val], dict):
            return indicators[val]
        if val.lower() in indicators and isinstance(indicators[val.lower()], dict):
            return indicators[val.lower()]
        return indicators

    def evaluate_all(
        self,
        indicators: Dict[str, Any],
        market_state: Optional[Dict[str, Any]] = None,
        locked_class: Optional[Union[AssetClass, str]] = None,
    ) -> AwaitableDict:
        """
        Universal signal generation:
        Continuously evaluates market indicators across all 5 asset classes.
        Retains top 2 recommendations per class in the SignalStore.
        Evaluates cascade brake trigger.
        Returns Dict[AssetClass, List[Signal]].

        locked_class: when set, ONLY the matching asset class is evaluated;
        all others are dropped with a visible [Signal] Dropped: Class mismatch log.
        This is the Execution Class Lock — it must be set from the operator's
        active mode on every evaluation call.
        """
        state = market_state or {}
        now = state.get("timestamp") or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        results: Dict[AssetClass, List[Signal]] = {c: [] for c in self.strategies.keys()}

        # Resolve locked_class to AssetClass
        _locked_ac: Optional[AssetClass] = None
        if locked_class is not None:
            if isinstance(locked_class, AssetClass):
                _locked_ac = locked_class
            else:
                try:
                    _locked_ac = AssetClass(str(locked_class))
                except (ValueError, KeyError):
                    logger.warning(
                        "[Signal] Invalid locked_class %r — class lock disabled for this evaluation",
                        locked_class,
                    )

        # Backward-compat: market_state.asset_class filter (used internally by pipeline)
        _requested_ac_str: Optional[str] = state.get("asset_class")

        with self._thread_lock:
            for asset_class, strategy in self.strategies.items():
                ac_label = asset_class.value if hasattr(asset_class, "value") else str(asset_class)

                # ── Execution class lock ─────────────────────────────────────────
                # When the operator has locked an execution class, strategies for
                # all other classes are skipped entirely — no signal is generated,
                # stored, or forwarded to the order pipeline.
                if _locked_ac is not None and asset_class != _locked_ac:
                    locked_label = _locked_ac.value if hasattr(_locked_ac, "value") else str(_locked_ac)
                    # Fires for every non-locked class on every tick, so throttle it.
                    _key = (ac_label, locked_label)
                    _last = self._mismatch_log_times.get(_key)
                    _mono = time.monotonic()
                    if _last is None or _mono - _last >= _MISMATCH_LOG_INTERVAL_S:
                        self._mismatch_log_times[_key] = _mono
                        logger.info(
                            "[Signal] Dropped: Class mismatch (Signal=%s, Locked=%s)"
                            " [logged at most once per %ds]",
                            ac_label, locked_label, _MISMATCH_LOG_INTERVAL_S,
                        )
                    logger.debug(
                        "[PIPELINE TRACE] J2 Class Lock: Signal=%s vs Locked=%s -> DROPPED",
                        ac_label, locked_label,
                    )
                    continue

                # Log J2 PASSED only when a class lock is actually active
                if _locked_ac is not None:
                    logger.debug(
                        "[PIPELINE TRACE] J2 Class Lock: Signal=%s vs Locked=%s -> PASSED",
                        ac_label,
                        _locked_ac.value if hasattr(_locked_ac, "value") else str(_locked_ac),
                    )

                # Backward-compat: optional market_state filter (no mismatch logging)
                if _requested_ac_str is not None and _locked_ac is None:
                    if ac_label != _requested_ac_str:
                        continue

                class_indicators = self._get_indicators_for_class(indicators, asset_class)
                try:
                    sig = strategy.evaluate(class_indicators, state)
                    if sig is not None:
                        logger.critical(
                            "[PIPELINE TRACE] J1 Signal Gen: symbol=%s class=%s"
                            " direction=%s tier=%s id=%s",
                            sig.symbol, ac_label,
                            getattr(sig.direction, "value", str(sig.direction)),
                            getattr(sig.confidence_tier, "value", str(sig.confidence_tier)),
                            sig.signal_id[:8],
                        )
                        # ── Pre-signal risk gate ────────────────────────────────
                        _portfolio_state: Dict[str, Any] = {}
                        if "open_positions_count" in state:
                            _portfolio_state["open_positions_count"] = state["open_positions_count"]
                        _allowed, _risk_reason = risk_state_manager.evaluate_signal(
                            sig, _portfolio_state
                        )
                        if not _allowed:
                            sig.state = SignalState.DROPPED_RISK_REJECTION
                            logger.info(
                                "[Risk] Signal Dropped: %s [strategy=%s signal=%s]",
                                _risk_reason, strategy.name, sig.signal_id[:8],
                            )
                            logger.critical(
                                "[PIPELINE TRACE] J3 Risk Gate: symbol=%s -> DROPPED (reason=%s)",
                                sig.symbol, _risk_reason,
                            )
                            self.audit_logger.log_risk_rejection(
                                symbol=sig.symbol,
                                broker="",
                                rule_violated="PRE_SIGNAL_RISK_GATE",
                                reason=_risk_reason or "risk gate rejected signal",
                                signal_id=sig.signal_id,
                            )
                        else:
                            logger.critical(
                                "[PIPELINE TRACE] J3 Risk Gate: symbol=%s -> PASSED",
                                sig.symbol,
                            )
                            results[asset_class].append(sig)
                            self.signal_store.add_signal(sig, top_n=2)
                            self._record_signal_timestamp(asset_class, sig.timestamp)
                except Exception as ex:
                    logger.error(
                        "[StrategyOrchestrator] Error evaluating strategy for %s: %s",
                        asset_class,
                        ex,
                        exc_info=True,
                    )

            self.check_cascade_brake(current_time=now)

        return AwaitableDict(results)

    def get_top_signals(self, asset_class: AssetClass, n: int = 2) -> List[Signal]:
        """Returns top N signals for the given asset class from SignalStore."""
        return self.signal_store.get_top_signals(asset_class, n=n)

    def get_all_actionable_signals(
        self, current_time: Optional[datetime] = None
    ) -> List[Signal]:
        """Returns all actionable non-expired signals across all asset classes."""
        return self.signal_store.get_all_actionable_signals(current_time=current_time)

    def get_status(self) -> Dict[str, Any]:
        """Returns telemetry and runtime status of the strategy orchestrator."""
        with self._thread_lock:
            return {
                "selected_asset_class": (
                    self._selected_asset_class.value
                    if hasattr(self._selected_asset_class, "value")
                    else str(self._selected_asset_class)
                ),
                "cascade_brake_active": self.cascade_brake_active,
                "registered_strategies": [
                    c.value if hasattr(c, "value") else str(c)
                    for c in self.strategies.keys()
                ],
                "signal_store_stats": self.signal_store.get_status(),
                "audit_entries_count": len(self.audit_logger._entries),
            }

    def reset(self) -> None:
        """Resets all strategy internal stores and recent timestamps."""
        with self._thread_lock:
            for strat in self.strategies.values():
                strat.reset()
            self.signal_store.clear()
            self._recent_signal_times.clear()
            self._mismatch_log_times.clear()
            self.cascade_brake_active = False


# Global singleton instance
strategy_orchestrator = StrategyOrchestrator()
