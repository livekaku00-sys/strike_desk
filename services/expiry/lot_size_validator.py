from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

from models.instrument import Instrument

logger = logging.getLogger(__name__)


class LotSizeValidator:
    """
    Validates instrument lot sizes (lot_size > 0) and detects quarterly
    exchange lot-size revisions across discoveries.
    Maintains 90-day in-memory audit history.
    """

    MAX_HISTORY_DAYS = 90

    def __init__(self) -> None:
        self._history: Dict[str, List[Dict[str, Any]]] = {}

    def validate_lot_size(self, instrument: Instrument) -> bool:
        """
        Validates that an instrument has a strictly positive integer lot size.
        """
        if not isinstance(instrument.lot_size, int):
            return False
        return instrument.lot_size > 0

    def detect_lot_size_changes(
        self, current: List[Instrument], previous: List[Instrument]
    ) -> List[Dict[str, Any]]:
        """
        Compares lot sizes for matching instruments between two discovery runs.
        Logs WARNING and returns records for all detected changes.
        """
        prev_map = {
            (inst.exchange, inst.symbol): inst.lot_size
            for inst in previous
        }
        changes: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for curr in current:
            key = (curr.exchange, curr.symbol)
            if key in prev_map:
                prev_lot = prev_map[key]
                if curr.lot_size != prev_lot:
                    logger.warning(
                        "[LotSizeValidator] Exchange Lot Size Revision detected for %s:%s! "
                        "Previous: %d, Current: %d",
                        curr.exchange.value,
                        curr.symbol,
                        prev_lot,
                        curr.lot_size,
                    )
                    change_record = {
                        "symbol": curr.symbol,
                        "exchange": curr.exchange.value,
                        "old_lot_size": prev_lot,
                        "new_lot_size": curr.lot_size,
                        "timestamp": now.isoformat(),
                    }
                    changes.append(change_record)

                    sym_key = curr.symbol.upper()
                    if sym_key not in self._history:
                        self._history[sym_key] = []
                    self._history[sym_key].append(change_record)
                    self._prune_history(sym_key)

        return changes

    def get_lot_size_history(self, symbol: str) -> List[Dict[str, Any]]:
        """Returns lot size revision history for a symbol."""
        sym_key = symbol.strip().upper()
        self._prune_history(sym_key)
        return list(self._history.get(sym_key, []))

    def _prune_history(self, symbol: str) -> None:
        if symbol not in self._history:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.MAX_HISTORY_DAYS)
        self._history[symbol] = [
            rec for rec in self._history[symbol]
            if datetime.fromisoformat(rec["timestamp"]) >= cutoff
        ]


lot_size_validator = LotSizeValidator()
