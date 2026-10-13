from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, List, Optional, Set

from models.instrument import Instrument

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MembershipChange:
    timestamp: datetime
    index_name: str
    added: List[str]
    removed: List[str]


class MembershipTracker:
    """
    Tracks index constituent membership changes across snapshots.
    Logs WARNING whenever symbols enter or exit an index.
    Maintains 30-day in-memory audit history.
    """

    MAX_HISTORY_DAYS = 30

    def __init__(self) -> None:
        self._snapshots: Dict[str, Set[str]] = {}
        self._history: Dict[str, List[MembershipChange]] = {}

    def record_snapshot(self, index_name: str, constituents: List[Instrument]) -> None:
        norm_name = index_name.upper()
        self._snapshots[norm_name] = {inst.symbol.upper() for inst in constituents}

    def detect_changes(
        self, index_name: str, current: List[Instrument]
    ) -> Dict[str, List[str]]:
        norm_name = index_name.upper()
        curr_symbols = {inst.symbol.upper() for inst in current}
        prev_symbols = self._snapshots.get(norm_name, set())

        added = sorted(list(curr_symbols - prev_symbols))
        removed = sorted(list(prev_symbols - curr_symbols))

        if norm_name in self._snapshots:
            if added:
                logger.warning(
                    "[MembershipTracker] Index '%s' constituents ADDED: %s",
                    norm_name,
                    added,
                )
            if removed:
                logger.warning(
                    "[MembershipTracker] Index '%s' constituents REMOVED: %s",
                    norm_name,
                    removed,
                )

            if added or removed:
                change = MembershipChange(
                    timestamp=datetime.now(timezone.utc),
                    index_name=norm_name,
                    added=added,
                    removed=removed,
                )
                if norm_name not in self._history:
                    self._history[norm_name] = []
                self._history[norm_name].append(change)
                self._prune_history(norm_name)

        self._snapshots[norm_name] = curr_symbols
        return {"added": added, "removed": removed}

    def get_history(self, index_name: str) -> List[MembershipChange]:
        norm_name = index_name.upper()
        self._prune_history(norm_name)
        return list(self._history.get(norm_name, []))

    def _prune_history(self, index_name: str) -> None:
        if index_name not in self._history:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.MAX_HISTORY_DAYS)
        self._history[index_name] = [
            c for c in self._history[index_name] if c.timestamp >= cutoff
        ]


membership_tracker = MembershipTracker()
