"""
Symbol rename registry.

NSE renames listed entities from time to time. Discovery follows the exchange
masters and will return only the new name, so anything persisted under the old one
— stored signals, historical indicator state, an operator's watchlist, a saved
session — would silently stop matching.

This maps legacy names onto current ones. It is a rename record, not a constituent
list: the tradeable universe still comes entirely from the broker masters.
"""
from typing import Dict, Iterable, List

# legacy name -> current NSE symbol
SYMBOL_ALIASES: Dict[str, str] = {
    # Tata Motors demerged; the passenger-vehicle entity trades as TMPV.
    "TATAMOTORS": "TMPV",
    "TATAMOTORS-PV": "TMPV",
}

# Reverse view, for recognising a current symbol's historical names.
_REVERSE: Dict[str, List[str]] = {}
for _old, _new in SYMBOL_ALIASES.items():
    _REVERSE.setdefault(_new, []).append(_old)


def canonical_symbol(symbol: str) -> str:
    """Returns the current NSE symbol, mapping a legacy name forward if needed."""
    if not symbol:
        return symbol
    key = symbol.strip().upper()
    return SYMBOL_ALIASES.get(key, key)


def canonical_symbols(symbols: Iterable[str]) -> List[str]:
    """Canonicalises a list, dropping duplicates while preserving order."""
    seen, out = set(), []
    for s in symbols or []:
        c = canonical_symbol(s)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def historical_names(symbol: str) -> List[str]:
    """Every name this instrument has traded under, current first."""
    cur = canonical_symbol(symbol)
    return [cur] + _REVERSE.get(cur, [])


def is_alias(symbol: str) -> bool:
    return bool(symbol) and symbol.strip().upper() in SYMBOL_ALIASES
