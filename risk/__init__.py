"""
Strike Desk V4 - Risk Management & Protection Package
Phase 5: Prompts 1/4 (State), 2/4 (Circuit Breakers), 3/4 (EOD Square-Off & Halt), 4/4 (RiskManager)
"""

# Prompt 1/4: PnL & Charges
try:
    from .charges import (
        ChargeCalculator,
        PnLCalculator,
        charge_calculator,
    )
except ImportError:
    try:
        from .pnl_calculator import (
            ChargeCalculator,
            PnLCalculator,
            charge_calculator,
        )
    except ImportError:
        pass

# Prompt 1/4: Risk State & IST Clock
try:
    from .risk_state import (
        RiskStateManager,
        get_ist_date,
        get_ist_now,
        risk_state_manager,
    )
except ImportError:
    pass

# Prompt 2/4: Circuit Breaker & Stagnation Monitor
try:
    from .circuit_breaker import (
        BreakerStatus,
        CircuitBreaker,
        CircuitBreakerConfig,
        circuit_breaker,
    )
except ImportError:
    pass

try:
    from .stagnation_monitor import (
        StagnationAlert,
        StagnationMonitor,
        stagnation_monitor,
    )
except ImportError:
    pass

# Prompt 3/4: EOD Square-Off & Emergency Halt
from .eod_square_off import (
    EODSquareOffConfig,
    EODSquareOffManager,
    SquareOffOrder,
    SquareOffReason,
    SquareOffResult,
    SquareOffStatus,
    eod_square_off_manager,
)

from .emergency_halt import (
    EmergencyHaltManager,
    HaltEvent,
    HaltLevel,
    emergency_halt_manager,
)

# Prompt 4/4: Unified Risk Manager
from .risk_manager import (
    ActivePosition,
    OrderAuthResult,
    RiskLimitsConfig,
    RiskManager,
    risk_manager,
)

__all__ = [
    # Charges & PnL
    "ChargeCalculator",
    "charge_calculator",
    "PnLCalculator",
    # Risk State
    "RiskStateManager",
    "get_ist_date",
    "get_ist_now",
    "risk_state_manager",
    # Circuit Breakers
    "BreakerStatus",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "circuit_breaker",
    # Stagnation Monitor
    "StagnationAlert",
    "StagnationMonitor",
    "stagnation_monitor",
    # EOD Square-Off
    "EODSquareOffConfig",
    "EODSquareOffManager",
    "SquareOffOrder",
    "SquareOffReason",
    "SquareOffResult",
    "SquareOffStatus",
    "eod_square_off_manager",
    # Emergency Halt
    "EmergencyHaltManager",
    "HaltEvent",
    "HaltLevel",
    "emergency_halt_manager",
    # Unified Risk Manager
    "ActivePosition",
    "OrderAuthResult",
    "RiskLimitsConfig",
    "RiskManager",
    "risk_manager",
]
