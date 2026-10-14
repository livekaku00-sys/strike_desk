"""
Strike Desk V4 - Strategies Package
Provides base strategy architecture, confidence scoring framework, signal store,
concrete asset-class strategies, the unified strategy orchestrator, and the
S13 signal fusion component.
"""
from .base_strategy import BaseStrategy
from .confidence import ConfidenceScorer, confidence_scorer
from .index_futures_strategy import IndexFuturesStrategy
from .index_options_strategy import IndexOptionsStrategy
from .signal_fuser import FusedDirection, FusedSignal, SignalFuser
from .signal_store import SignalStore, signal_store
from .stock_cash_strategy import StockCashStrategy
from .stock_futures_strategy import StockFuturesStrategy
from .stock_options_strategy import StockOptionsStrategy
from .strategy_orchestrator import StrategyOrchestrator, strategy_orchestrator

__all__ = [
    "BaseStrategy",
    "ConfidenceScorer",
    "confidence_scorer",
    "FusedDirection",
    "FusedSignal",
    "SignalFuser",
    "SignalStore",
    "signal_store",
    "StockCashStrategy",
    "StockFuturesStrategy",
    "IndexFuturesStrategy",
    "IndexOptionsStrategy",
    "StockOptionsStrategy",
    "StrategyOrchestrator",
    "strategy_orchestrator",
]