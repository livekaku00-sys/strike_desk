"""
Persistent record of the exact contract behind each traded derivative symbol.

Our contract names (MIDCPNIFTY26OCT13700PE) omit the expiry day, so they cannot be
matched against a broker's own spelling (AngelOne: MIDCPNIFTY27OCT2613700PE), and
for indices with weekly expiries the name alone is ambiguous. The opening order
carries the structural identity (underlying, expiry, strike, CE/PE) and broker ids;
this store keeps them so a later exit that arrives with only the symbol addresses
the same contract that was opened.
"""
from datetime import date, datetime, timezone
import json
import logging
import os
import re
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Same namespaced keys the order route accepts and each adapter's resolver reads.
IDENTITY_KEYS = (
    "underlying", "selected_expiry", "selected_strike", "option_type",
    "dhan_security_id", "dhan_segment",
    "angel_symboltoken", "angel_tradingsymbol", "angel_exchange",
)
STRUCTURAL_KEYS = ("selected_expiry", "selected_strike", "option_type")

# How each adapter's resolve_order_instrument() result maps back onto those keys.
_RESOLVED_TO_KEYS = {
    "angelone": {"symboltoken": "angel_symboltoken", "tradingsymbol": "angel_tradingsymbol",
                 "exchange": "angel_exchange"},
    "dhan": {"security_id": "dhan_security_id", "exchange_segment": "dhan_segment"},
}

_DERIVATIVE_RE = re.compile(r"(?:\d(?:CE|PE)|FUT)$")


def _store_path() -> str:
    # Resolved per call so tests can redirect it (tests/conftest.py).
    return os.environ.get("STRIKE_DESK_CONTRACT_FILE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "contract_registry.json",
    )


def is_derivative(symbol: str) -> bool:
    return bool(_DERIVATIVE_RE.search((symbol or "").strip().upper()))


def identity_from_signal_metadata(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Strategy signals name Dhan's id `security_id` (it comes from Dhan's option chain)."""
    meta = meta or {}
    out = {k: meta[k] for k in IDENTITY_KEYS if meta.get(k) not in (None, "")}
    if meta.get("security_id") and "dhan_security_id" not in out:
        out["dhan_security_id"] = str(meta["security_id"])
    return out


def _expired(entry: Dict[str, Any]) -> bool:
    exp = str(entry.get("selected_expiry") or "")[:10]
    try:
        return date.fromisoformat(exp) < date.today()
    except ValueError:
        return False


class ContractStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Optional[Dict[str, Dict[str, Any]]] = None
        self._loaded_from: Optional[str] = None

    def _load(self) -> Dict[str, Dict[str, Any]]:
        path = _store_path()
        if self._entries is not None and self._loaded_from == path:
            return self._entries
        entries: Dict[str, Dict[str, Any]] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                entries = {k: v for k, v in raw.items() if isinstance(v, dict) and not _expired(v)}
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.error("[ContractStore] Could not read %s: %s — starting empty", path, exc)
        self._entries, self._loaded_from = entries, path
        return entries

    def _save(self, entries: Dict[str, Dict[str, Any]]) -> None:
        path = _store_path()
        tmp = f"{path}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception as exc:
            logger.error("[ContractStore] Could not persist %s: %s", path, exc)

    def record(self, symbol: str, metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Merge the identity carried by a placed/filled order into the stored entry.
        `metadata` is order.metadata: identity keys plus, when the route resolved it,
        `resolved_instrument` / `resolved_for_broker`.
        """
        sym = (symbol or "").strip().upper()
        if not is_derivative(sym):
            return None
        meta = metadata or {}
        found = {k: meta[k] for k in IDENTITY_KEYS if meta.get(k) not in (None, "")}
        broker = str(meta.get("resolved_for_broker") or "").lower()
        resolved = meta.get("resolved_instrument")
        if isinstance(resolved, dict):
            for src, dst in _RESOLVED_TO_KEYS.get(broker, {}).items():
                if resolved.get(src):
                    found[dst] = str(resolved[src])
        if not found:
            return None
        with self._lock:
            entries = self._load()
            entry = dict(entries.get(sym) or {})
            entry.update(found)
            entry["symbol"] = sym
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            entries[sym] = entry
            self._save(entries)
        logger.info("[ContractStore] Recorded contract for %s: %s", sym,
                    {k: entry[k] for k in IDENTITY_KEYS if k in entry})
        return dict(entry)

    def lookup(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._load().get((symbol or "").strip().upper())
            return dict(entry) if entry else None


contract_store = ContractStore()
