"""
Strike Desk V4 - Pipeline State Persistence
Handles serialization and atomic restoration of real-time pipeline state to/from JSON.
Provides checkpointing for crash recovery and session resumption.
"""
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = "/root/strike_desk/data/pipeline_state.json"


class StatePersistence:
    """
    Manages persistent JSON snapshots of orchestrator state across restarts.
    """

    def __init__(self, default_filepath: str = DEFAULT_STATE_FILE) -> None:
        self.default_filepath = default_filepath

    @staticmethod
    def _resolve_filepath(filepath: Optional[str] = None) -> str:
        if filepath:
            if filepath.startswith("/root/strike_desk"):
                alt = filepath.replace("/root/strike_desk", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
                parent_dir = os.path.dirname(filepath)
                if not os.path.exists(parent_dir):
                    try:
                        os.makedirs(parent_dir, exist_ok=True)
                        return filepath
                    except (PermissionError, OSError):
                        return alt
                return filepath
            return filepath

        candidates = [
            DEFAULT_STATE_FILE,
            os.path.join(os.path.dirname(__file__), "..", "data", "pipeline_state.json"),
            os.path.abspath("data/pipeline_state.json"),
        ]
        for c in candidates:
            dirname = os.path.dirname(os.path.abspath(c))
            if os.path.exists(dirname):
                return c
        return DEFAULT_STATE_FILE

    def save_state(self, state_data: Dict[str, Any], filepath: Optional[str] = None) -> bool:
        """
        Atomically saves state data to JSON.
        """
        path = self._resolve_filepath(filepath)
        dir_name = os.path.dirname(os.path.abspath(path))
        try:
            os.makedirs(dir_name, exist_ok=True)
            temp_file = os.path.join(dir_name, f".state_tmp_{os.getpid()}_{int(datetime.now().timestamp()*1000)}.json")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2)
            os.replace(temp_file, path)
            logger.info("[StatePersistence] Successfully saved pipeline state to %s", path)
            return True
        except Exception as exc:
            logger.error("[StatePersistence] Failed to save state to %s: %s", path, exc, exc_info=True)
            return False

    def load_state(self, filepath: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Loads and parses state data from JSON.
        """
        path = self._resolve_filepath(filepath)
        if not os.path.exists(path):
            alt_path = path.replace("/root/strike_desk", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
            if os.path.exists(alt_path):
                path = alt_path
            else:
                logger.warning("[StatePersistence] State file not found at %s", path)
                return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if self.validate_state(data):
                logger.info("[StatePersistence] Successfully loaded and verified state from %s", path)
                return data
            else:
                logger.error("[StatePersistence] State file at %s failed schema validation", path)
                return None
        except Exception as exc:
            logger.error("[StatePersistence] Error loading state file from %s: %s", path, exc, exc_info=True)
            return None

    @staticmethod
    def validate_state(state_data: Dict[str, Any]) -> bool:
        """
        Validates structure of state payload.
        Must contain 'metadata' and 'symbols' blocks with valid subfields.
        """
        if not isinstance(state_data, dict):
            return False
        if "metadata" not in state_data or "symbols" not in state_data:
            return False
        metadata = state_data.get("metadata", {})
        if not isinstance(metadata, dict) or "save_time" not in metadata:
            return False
        symbols = state_data.get("symbols", {})
        if not isinstance(symbols, dict):
            return False

        for sym, sym_state in symbols.items():
            if not isinstance(sym_state, dict):
                return False
            for req in ["vwap", "ema9", "tick_count"]:
                if req not in sym_state:
                    return False
        return True


state_persistence = StatePersistence()