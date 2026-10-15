"""
Strike Desk V4 - Net Edge Shadow Evaluator (S14)

PDF status: Strike_Desk_Upgrade_Prompt_Series.pdf unavailable.
ALL thresholds, cost-model mappings, and unit conventions below are
PROVISIONAL — derived from the existing codebase architecture.
They are marked [PROVISIONAL] in inline comments and must be reviewed
against the verified specification before use beyond testing.

Responsibility
--------------
Evaluates whether the estimated net edge of a trade — after subtracting
all applicable cost components — meets the configured minimum threshold.

SHADOW MODE — ADVISORY ONLY
----------------------------
This component is a shadow evaluator.  Its output MUST NOT be connected
to order placement, modification, cancellation, or trade authorization.
`NetEdgeResult.is_shadow` is always True and serves as a runtime marker
to distinguish shadow decisions from actual trade decisions.

Do not claim that a positive net-edge estimate establishes profitability
or execution quality.  Net edge is an accounting convenience, not a
forecast of future returns.

Units
-----
All monetary values are in Indian Rupees (₹) per lot.

- gross_edge              : ₹ expected price move × lot_size (before costs)
- deduction_*             : ₹ cost component for round-trip (entry + exit legs)
- total_statutory_charges : ₹ sum of all statutory deduction components
- total_deductions        : ₹ statutory charges + any provided optional deductions
- net_edge                : ₹ gross_edge − total_deductions
- min_net_edge            : ₹ minimum acceptable net edge (from NetEdgeConfig)

When lot_size=1 (the default), all values are expressed per unit.

Gross edge formula [PROVISIONAL]
---------------------------------
- LONG  : gross_edge = (target_price − entry_price) × lot_size
- SHORT : gross_edge = (entry_price − target_price) × lot_size

A negative gross edge (e.g. LONG with target < entry) is computed and
reported as-is.  The net_edge will typically be very negative and
produce a FAIL decision.

Cost components
---------------
Statutory charges are calculated round-trip (entry leg + exit leg) using
the existing ChargeCalculator.  Optional components (spread, slippage,
funding) are accepted as caller-supplied ₹ values.  They are NOT applied
when None — absent costs are not silently treated as zero.  Omitted
components are recorded in omitted_deductions.

Asset-class → ChargeCalculator segment mapping [PROVISIONAL]
--------------------------------------------------------------
STOCK_CASH     → segment="EQUITY",  trade_type="INTRADAY"
STOCK_FUTURES  → segment="FNO",     trade_type="FUTURES"
INDEX_FUTURES  → segment="FNO",     trade_type="FUTURES"
STOCK_OPTIONS  → segment="FNO",     trade_type="OPTIONS"
INDEX_OPTIONS  → segment="FNO",     trade_type="OPTIONS"

Configuration
-------------
MIN_NET_EDGE env var (float ≥ 0, default "0.0"): minimum net edge in ₹
per lot required for a PASS decision.  [PROVISIONAL: default=0.0]

Input contract
--------------
- signal            : Signal — must have a finite entry_price > 0 and a
                      finite target_price > 0; if either is absent or
                      invalid, decision=INSUFFICIENT_DATA is returned.
- lot_size          : int ≥ 1 — defaults to 1. [PROVISIONAL]
- spread_per_unit   : Optional[float] ≥ 0 — half-spread cost in ₹ per
                      unit.  Must be finite if provided.
- slippage_per_unit : Optional[float] ≥ 0 — estimated slippage in ₹ per
                      unit.  Must be finite if provided.
- funding_per_lot   : Optional[float] ≥ 0 — overnight/carry cost in ₹
                      per lot.  Must be finite if provided.
- config            : Optional[NetEdgeConfig] — defaults to
                      NetEdgeConfig.default().

Output contract
---------------
- decision    : PASS | FAIL | INSUFFICIENT_DATA
- is_shadow   : always True
- reason      : human-readable accounting summary
- evaluated_at: UTC datetime of evaluation call
"""
from __future__ import annotations

import math
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from models.instrument import AssetClass, Exchange
from models.signal import Direction, Signal
from risk.charge_calculator import ChargeCalculator

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NetEdgeConfig:
    """
    Configuration for the net-edge shadow evaluator.

    min_net_edge : Minimum net edge in ₹ per lot required for PASS.
                   Must be finite and >= 0.  [PROVISIONAL: default=0.0]
    """
    min_net_edge: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.min_net_edge):
            raise ValueError(
                f"min_net_edge must be a finite number, got {self.min_net_edge!r}"
            )
        if self.min_net_edge < 0.0:
            raise ValueError(
                f"min_net_edge must be >= 0.0, got {self.min_net_edge!r}"
            )

    @classmethod
    def default(cls) -> "NetEdgeConfig":
        """Returns the default configuration (min_net_edge=0.0). [PROVISIONAL]"""
        return cls(min_net_edge=0.0)

    @classmethod
    def from_env(cls) -> "NetEdgeConfig":
        """
        Constructs NetEdgeConfig from the MIN_NET_EDGE environment variable.

        MIN_NET_EDGE : float ≥ 0, in ₹ per lot.  Default: "0.0". [PROVISIONAL]

        Raises ValueError if the value is non-numeric, non-finite, or < 0.
        """
        raw = os.environ.get("MIN_NET_EDGE", "0.0").strip()
        try:
            value = float(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"MIN_NET_EDGE={raw!r} is not a valid float: {exc}"
            ) from exc
        return cls(min_net_edge=value)


# ─────────────────────────────────────────────────────────────────────────────
# Output enum
# ─────────────────────────────────────────────────────────────────────────────

class NetEdgeDecision(str, Enum):
    PASS              = "PASS"               # net_edge >= min_net_edge
    FAIL              = "FAIL"               # net_edge < min_net_edge
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # missing or invalid inputs


# ─────────────────────────────────────────────────────────────────────────────
# Output dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class NetEdgeResult:
    """
    Result of a single NetEdgeEvaluator.evaluate() call.

    SHADOW MODE — is_shadow is always True.  This result is advisory only
    and must not be used to authorize, block, submit, modify, or cancel
    any order.

    All monetary fields are in ₹ per lot (or per unit when lot_size=1).
    """
    # Shadow-mode marker — always True
    is_shadow: bool

    # Signal identity
    signal_id: str
    symbol: str
    exchange: Exchange
    asset_class: AssetClass
    direction: Direction
    entry_price: float
    target_price: Optional[float]
    lot_size: int

    # Accounting (₹ per lot, round-trip for statutory charges)
    gross_edge: float
    deduction_brokerage: float
    deduction_stt: float
    deduction_exchange_charges: float
    deduction_gst: float
    deduction_sebi_charges: float
    deduction_spread: Optional[float]       # None = not applied (not provided)
    deduction_slippage: Optional[float]     # None = not applied (not provided)
    deduction_funding: Optional[float]      # None = not applied (not provided)
    total_statutory_charges: float          # brokerage+stt+exchange_charges+gst+sebi
    total_deductions: float                 # statutory + all provided optional costs
    net_edge: float                         # gross_edge - total_deductions

    # Decision
    min_net_edge: float
    decision: NetEdgeDecision
    reason: str

    # Audit metadata
    omitted_deductions: List[str]           # optional costs not applied
    entry_turnover: float                   # entry_price × lot_size (₹)
    exit_turnover: float                    # target_price × lot_size (₹); 0.0 if no target
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None:
            self.evaluated_at = self.evaluated_at.replace(tzinfo=timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_shadow": self.is_shadow,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "exchange": self.exchange.value if hasattr(self.exchange, "value") else str(self.exchange),
            "asset_class": self.asset_class.value if hasattr(self.asset_class, "value") else str(self.asset_class),
            "direction": self.direction.value if hasattr(self.direction, "value") else str(self.direction),
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "lot_size": self.lot_size,
            "gross_edge": self.gross_edge,
            "deduction_brokerage": self.deduction_brokerage,
            "deduction_stt": self.deduction_stt,
            "deduction_exchange_charges": self.deduction_exchange_charges,
            "deduction_gst": self.deduction_gst,
            "deduction_sebi_charges": self.deduction_sebi_charges,
            "deduction_spread": self.deduction_spread,
            "deduction_slippage": self.deduction_slippage,
            "deduction_funding": self.deduction_funding,
            "total_statutory_charges": self.total_statutory_charges,
            "total_deductions": self.total_deductions,
            "net_edge": self.net_edge,
            "min_net_edge": self.min_net_edge,
            "decision": self.decision.value,
            "reason": self.reason,
            "omitted_deductions": list(self.omitted_deductions),
            "entry_turnover": self.entry_turnover,
            "exit_turnover": self.exit_turnover,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Asset-class → ChargeCalculator segment mapping  [PROVISIONAL]
# ─────────────────────────────────────────────────────────────────────────────

_AC_TO_SEGMENT: Dict[AssetClass, tuple] = {
    AssetClass.STOCK_CASH:    ("EQUITY", "INTRADAY"),
    AssetClass.STOCK_FUTURES: ("FNO",    "FUTURES"),
    AssetClass.INDEX_FUTURES: ("FNO",    "FUTURES"),
    AssetClass.STOCK_OPTIONS: ("FNO",    "OPTIONS"),
    AssetClass.INDEX_OPTIONS: ("FNO",    "OPTIONS"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Evaluator
# ─────────────────────────────────────────────────────────────────────────────

class NetEdgeEvaluator:
    """
    Stateless, deterministic net-edge shadow evaluator.

    All thresholds and mappings are PROVISIONAL (PDF unavailable).

    SHADOW MODE — ADVISORY ONLY.  This evaluator does not authorize,
    block, submit, modify, or cancel orders.
    """

    @staticmethod
    def _validate_optional_cost(name: str, value: Optional[float]) -> None:
        """Raises ValueError if a provided optional cost is non-finite or negative."""
        if value is None:
            return
        if not math.isfinite(value):
            raise ValueError(f"{name}={value!r} must be a finite number")
        if value < 0.0:
            raise ValueError(f"{name}={value!r} must be >= 0.0")

    @classmethod
    def _round_trip_charges(
        cls,
        entry_price: float,
        exit_price: float,
        lot_size: int,
        asset_class: AssetClass,
        direction: Direction,
    ) -> Dict[str, float]:
        """
        Computes statutory charges for both legs using ChargeCalculator.

        Returns aggregated breakdown (both legs summed) plus turnover values.

        Entry is_sell:
          LONG  → buy  (is_sell=False) at entry; sell (is_sell=True)  at exit
          SHORT → sell (is_sell=True)  at entry; buy  (is_sell=False) at exit
        """
        segment, trade_type = _AC_TO_SEGMENT.get(
            asset_class, ("EQUITY", "INTRADAY")  # [PROVISIONAL] fallback
        )
        entry_turnover = round(entry_price * lot_size, 4)
        exit_turnover  = round(exit_price  * lot_size, 4)

        entry_is_sell = (direction == Direction.SHORT)
        exit_is_sell  = (direction == Direction.LONG)

        e = ChargeCalculator.get_charges_breakdown(
            entry_turnover, trade_type, segment, is_sell=entry_is_sell
        )
        x = ChargeCalculator.get_charges_breakdown(
            exit_turnover,  trade_type, segment, is_sell=exit_is_sell
        )

        return {
            "brokerage":        round(e["brokerage"]        + x["brokerage"],        4),
            "stt":              round(e["stt"]               + x["stt"],               4),
            "exchange_charges": round(e["exchange_charges"]  + x["exchange_charges"],  4),
            "gst":              round(e["gst"]               + x["gst"],               4),
            "sebi_charges":     round(e["sebi_charges"]      + x["sebi_charges"],      4),
            "entry_turnover":   entry_turnover,
            "exit_turnover":    exit_turnover,
        }

    @classmethod
    def evaluate(
        cls,
        signal: Signal,
        *,
        lot_size: int = 1,
        spread_per_unit: Optional[float] = None,
        slippage_per_unit: Optional[float] = None,
        funding_per_lot: Optional[float] = None,
        config: Optional[NetEdgeConfig] = None,
        _now: Optional[datetime] = None,
    ) -> NetEdgeResult:
        """
        Evaluates the net edge of a trade in shadow mode.

        Parameters
        ----------
        signal            : Signal to evaluate.
        lot_size          : Units per lot (>= 1, default 1). [PROVISIONAL]
        spread_per_unit   : Half-spread cost in ₹ per unit; None = not applied.
        slippage_per_unit : Estimated slippage in ₹ per unit; None = not applied.
        funding_per_lot   : Overnight/carry cost in ₹ per lot; None = not applied.
        config            : NetEdgeConfig; defaults to NetEdgeConfig.default().
        _now              : Override evaluation timestamp (deterministic tests only).

        Returns
        -------
        NetEdgeResult (always is_shadow=True).
        """
        now = _now if _now is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        cfg = config if config is not None else NetEdgeConfig.default()

        # Validate optional cost inputs before doing any arithmetic.
        cls._validate_optional_cost("spread_per_unit",    spread_per_unit)
        cls._validate_optional_cost("slippage_per_unit",  slippage_per_unit)
        cls._validate_optional_cost("funding_per_lot",    funding_per_lot)

        # ── Shared INSUFFICIENT_DATA builder ─────────────────────────────
        def _insuf(reason: str) -> NetEdgeResult:
            return NetEdgeResult(
                is_shadow=True,
                signal_id=signal.signal_id,
                symbol=signal.symbol.strip().upper() if signal.symbol else "",
                exchange=signal.exchange,
                asset_class=signal.asset_class,
                direction=signal.direction,
                entry_price=float(signal.entry_price),
                target_price=signal.target_price,
                lot_size=lot_size,
                gross_edge=0.0,
                deduction_brokerage=0.0,
                deduction_stt=0.0,
                deduction_exchange_charges=0.0,
                deduction_gst=0.0,
                deduction_sebi_charges=0.0,
                deduction_spread=None,
                deduction_slippage=None,
                deduction_funding=None,
                total_statutory_charges=0.0,
                total_deductions=0.0,
                net_edge=0.0,
                min_net_edge=cfg.min_net_edge,
                decision=NetEdgeDecision.INSUFFICIENT_DATA,
                reason=reason,
                omitted_deductions=[],
                entry_turnover=0.0,
                exit_turnover=0.0,
                evaluated_at=now,
            )

        # ── Input validation ──────────────────────────────────────────────
        if lot_size < 1:
            return _insuf(f"lot_size={lot_size} must be >= 1")

        entry_price = float(signal.entry_price)
        if not math.isfinite(entry_price) or entry_price <= 0.0:
            return _insuf(f"entry_price={entry_price!r} must be finite and > 0")

        if signal.target_price is None:
            return _insuf("target_price is None; cannot compute gross edge")
        target_price = float(signal.target_price)
        if not math.isfinite(target_price) or target_price <= 0.0:
            return _insuf(f"target_price={target_price!r} must be finite and > 0")

        # ── Gross edge ────────────────────────────────────────────────────
        if signal.direction == Direction.LONG:
            gross_edge = round((target_price - entry_price) * lot_size, 4)
        else:
            gross_edge = round((entry_price - target_price) * lot_size, 4)

        # ── Statutory charges (round-trip) ────────────────────────────────
        ch = cls._round_trip_charges(
            entry_price=entry_price,
            exit_price=target_price,
            lot_size=lot_size,
            asset_class=signal.asset_class,
            direction=signal.direction,
        )

        d_brokerage  = ch["brokerage"]
        d_stt        = ch["stt"]
        d_exchange   = ch["exchange_charges"]
        d_gst        = ch["gst"]
        d_sebi       = ch["sebi_charges"]
        entry_tv     = ch["entry_turnover"]
        exit_tv      = ch["exit_turnover"]
        total_stat   = round(d_brokerage + d_stt + d_exchange + d_gst + d_sebi, 4)

        # ── Optional deductions ───────────────────────────────────────────
        omitted: List[str] = []

        d_spread   = round(spread_per_unit   * lot_size, 4) if spread_per_unit   is not None else None
        d_slippage = round(slippage_per_unit * lot_size, 4) if slippage_per_unit is not None else None
        d_funding  = funding_per_lot  # already expressed per lot

        if d_spread   is None: omitted.append("spread")
        if d_slippage is None: omitted.append("slippage")
        if d_funding  is None: omitted.append("funding")

        total_opt = round(
            (d_spread   or 0.0)
            + (d_slippage or 0.0)
            + (d_funding  or 0.0),
            4,
        )
        total_deductions = round(total_stat + total_opt, 4)
        net_edge         = round(gross_edge - total_deductions, 4)

        # ── Decision ──────────────────────────────────────────────────────
        decision = NetEdgeDecision.PASS if net_edge >= cfg.min_net_edge else NetEdgeDecision.FAIL

        # ── Reason string ─────────────────────────────────────────────────
        omitted_str = ",".join(omitted) if omitted else "none"
        reason = (
            f"decision={decision.value}; "
            f"gross_edge={gross_edge:.4f}; "
            f"total_deductions={total_deductions:.4f}; "
            f"net_edge={net_edge:.4f}; "
            f"min_net_edge={cfg.min_net_edge:.4f}; "
            f"omitted=[{omitted_str}]"
        )

        logger.debug(
            "[NetEdgeEvaluator] SHADOW %s %s/%s → %s | "
            "gross=%.4f deductions=%.4f net=%.4f min=%.4f omitted=[%s]",
            decision.value,
            signal.symbol,
            signal.asset_class.value if hasattr(signal.asset_class, "value") else str(signal.asset_class),
            signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
            gross_edge,
            total_deductions,
            net_edge,
            cfg.min_net_edge,
            omitted_str,
        )

        return NetEdgeResult(
            is_shadow=True,
            signal_id=signal.signal_id,
            symbol=signal.symbol.strip().upper() if signal.symbol else "",
            exchange=signal.exchange,
            asset_class=signal.asset_class,
            direction=signal.direction,
            entry_price=entry_price,
            target_price=target_price,
            lot_size=lot_size,
            gross_edge=gross_edge,
            deduction_brokerage=d_brokerage,
            deduction_stt=d_stt,
            deduction_exchange_charges=d_exchange,
            deduction_gst=d_gst,
            deduction_sebi_charges=d_sebi,
            deduction_spread=d_spread,
            deduction_slippage=d_slippage,
            deduction_funding=d_funding,
            total_statutory_charges=total_stat,
            total_deductions=total_deductions,
            net_edge=net_edge,
            min_net_edge=cfg.min_net_edge,
            decision=decision,
            reason=reason,
            omitted_deductions=omitted,
            entry_turnover=entry_tv,
            exit_turnover=exit_tv,
            evaluated_at=now,
        )
