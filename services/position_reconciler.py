"""
P0-04 — Position Reconciliation Engine

Compares the internal position ledger against broker-reported positions.
On mismatch: sets an active flag that blocks all new order placement (HTTP 409),
logs the full diff, and calls audit_logger.log_reconciliation.

Constraints:
- Detection only. Never auto-corrects positions.
- Correction must be performed manually by the operator.
- Mismatch flag is cleared when a subsequent reconcile returns clean, OR when
  the operator explicitly clears it via clear_mismatch().
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_MATCH = "MATCH"
_MISMATCH = "MISMATCH"
_QTY_TOLERANCE = 0.001  # treat qty differences < this as floating-point noise


@dataclass
class ReconciliationDiff:
    symbol: str
    internal_qty: float
    broker_qty: float
    delta: float  # broker_qty - internal_qty

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "internal_qty": self.internal_qty,
            "broker_qty": self.broker_qty,
            "delta": self.delta,
        }


@dataclass
class ReconciliationResult:
    status: str
    diffs: List[ReconciliationDiff]
    checked_at: datetime
    broker: str
    symbols_checked: int

    @property
    def is_match(self) -> bool:
        return self.status == _MATCH

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "is_match": self.is_match,
            "symbols_checked": self.symbols_checked,
            "broker": self.broker,
            "checked_at": self.checked_at.isoformat(),
            "diffs": [d.to_dict() for d in self.diffs],
            "diff_count": len(self.diffs),
        }


class PositionReconciler:
    """
    Stateful reconciler singleton.  Call reconcile() from an operator endpoint
    or a background task to compare positions.  The mismatch flag persists until
    either a clean reconcile clears it automatically or the operator calls
    clear_mismatch() after manually resolving the discrepancy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_result: Optional[ReconciliationResult] = None
        self._mismatch_active = False
        self._mismatch_since: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Core comparison
    # ------------------------------------------------------------------

    def reconcile(
        self,
        internal: Dict[str, float],
        broker: Dict[str, float],
        broker_name: str = "unknown",
    ) -> ReconciliationResult:
        """
        Compare internal position qtys against broker qtys.

        Both dicts map symbol (str) to net signed quantity (float, positive=long).
        Symbols present in one dict but absent in the other are treated as qty=0
        in the missing side.  Differences below _QTY_TOLERANCE are ignored.
        """
        all_symbols = set(internal.keys()) | set(broker.keys())
        diffs: List[ReconciliationDiff] = []
        for sym in sorted(all_symbols):
            iq = internal.get(sym, 0.0)
            bq = broker.get(sym, 0.0)
            if abs(bq - iq) > _QTY_TOLERANCE:
                diffs.append(
                    ReconciliationDiff(symbol=sym, internal_qty=iq, broker_qty=bq, delta=bq - iq)
                )

        status = _MATCH if not diffs else _MISMATCH
        result = ReconciliationResult(
            status=status,
            diffs=diffs,
            checked_at=datetime.now(timezone.utc),
            broker=broker_name,
            symbols_checked=len(all_symbols),
        )

        with self._lock:
            self._last_result = result
            if diffs:
                if not self._mismatch_active:
                    self._mismatch_since = result.checked_at
                self._mismatch_active = True
                logger.error(
                    "[Reconciler] MISMATCH detected vs '%s': %d diff(s): %s",
                    broker_name,
                    len(diffs),
                    [(d.symbol, d.internal_qty, d.broker_qty) for d in diffs],
                )
                try:
                    from execution.audit_logger import audit_logger as _al
                    _al.log_reconciliation(
                        broker=broker_name,
                        status=status,
                        diff_summary="; ".join(
                            f"{d.symbol}(int={d.internal_qty},brk={d.broker_qty})" for d in diffs
                        ),
                        symbols_checked=len(all_symbols),
                    )
                except Exception:
                    pass
            else:
                if self._mismatch_active:
                    logger.info(
                        "[Reconciler] Mismatch cleared — reconcile vs '%s' returned MATCH.",
                        broker_name,
                    )
                self._mismatch_active = False
                self._mismatch_since = None

        return result

    # ------------------------------------------------------------------
    # Gate query (called by place_order)
    # ------------------------------------------------------------------

    def is_mismatch_active(self) -> bool:
        with self._lock:
            return self._mismatch_active

    # ------------------------------------------------------------------
    # Operator actions
    # ------------------------------------------------------------------

    def clear_mismatch(self, operator_id: str) -> None:
        """
        Operator-explicit clear.  Use after manually resolving a ledger discrepancy
        when you cannot immediately re-run a clean reconcile.
        """
        with self._lock:
            was_active = self._mismatch_active
            self._mismatch_active = False
            self._mismatch_since = None
        if was_active:
            logger.warning(
                "[Reconciler] Mismatch flag cleared manually by operator '%s'.", operator_id
            )
        try:
            from execution.audit_logger import audit_logger as _al
            _al.log_reconciliation(
                broker="manual-clear",
                status="CLEARED",
                diff_summary=f"Cleared by operator '{operator_id}'",
                symbols_checked=0,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Status / read
    # ------------------------------------------------------------------

    def get_last_result(self) -> Optional[ReconciliationResult]:
        with self._lock:
            return self._last_result

    def get_status(self) -> Dict:
        with self._lock:
            lr = self._last_result
            return {
                "mismatch_active": self._mismatch_active,
                "mismatch_since": self._mismatch_since.isoformat() if self._mismatch_since else None,
                "last_reconcile": lr.to_dict() if lr else None,
            }


# Process-wide singleton.  Tests create their own instances to avoid shared state.
position_reconciler = PositionReconciler()
