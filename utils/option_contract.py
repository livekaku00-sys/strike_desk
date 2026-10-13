"""
Resolves the tradeable option contract (premium, symbol, security_id) for a
signal from the cached option chain.
"""
from dataclasses import dataclass
from datetime import date
import logging
import time
from typing import Any, Dict, Optional

from utils.cache_manager import option_chain_cache

logger = logging.getLogger(__name__)

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Suppress repeated "no cached chain" warnings to at most once per 5 min per symbol.
_no_chain_warned: Dict[str, float] = {}
_NO_CHAIN_WARN_INTERVAL = 300.0


@dataclass
class ResolvedContract:
    premium: float = 0.0
    symbol: str = ""
    strike: float = 0.0
    security_id: str = ""
    expiry: str = ""
    resolved: bool = False


def _format_symbol(underlying: str, expiry_iso: str, strike: float, opt_type: str) -> str:
    """NSE contract convention: UNDERLYING + YY + MMM + STRIKE + CE/PE."""
    try:
        d = date.fromisoformat(expiry_iso)
        code = f"{d.year % 100:02d}{_MONTHS[d.month - 1]}"
    except (ValueError, TypeError):
        code = "EXP"
    strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
    return f"{underlying}{code}{strike_str}{opt_type}"


def resolve_option_contract(
    underlying: str,
    spot_price: float,
    is_long: bool,
    indicators: Dict[str, Any],
    market_state: Dict[str, Any],
) -> ResolvedContract:
    """
    Finds the ATM contract for `underlying` in the cached option chain and returns
    its live premium, NSE symbol and Dhan security_id.

    Falls back to an unresolved result (caller uses spot) when no chain is cached.
    """
    und = underlying.strip().upper()
    opt_type = "CE" if is_long else "PE"

    # Explicit premium from the indicator payload wins (used by tests / replay).
    raw_premium = indicators.get("option_price") or indicators.get("premium")
    if raw_premium is not None and float(raw_premium) > 0.0:
        sym = str(indicators.get("symbol") or market_state.get("symbol") or und).strip().upper()
        return ResolvedContract(
            premium=float(raw_premium), symbol=sym, strike=0.0, resolved=True
        )

    candidates = [
        (key, entry)
        for key, entry in option_chain_cache._cache.items()
        if key[0] == und and entry.data is not None and getattr(entry.data, "strikes", None)
    ]
    if not candidates:
        _now = time.monotonic()
        if _now - _no_chain_warned.get(und, 0.0) >= _NO_CHAIN_WARN_INTERVAL:
            present = sorted({k[0] for k in option_chain_cache._cache})
            logger.warning(
                "[OptionContract] No cached chain with strikes for '%s'. "
                "Cache holds %d entries for: %s",
                und, len(option_chain_cache._cache), present[:20] or "<empty>",
            )
            _no_chain_warned[und] = _now
        return ResolvedContract(symbol=und, resolved=False)

    # Freshest chain wins if several expiries are cached.
    _key, entry = max(candidates, key=lambda kv: kv[1].last_update_time)
    chain = entry.data

    # The chain carries the broker's own spot, which is authoritative for ATM choice.
    reference = float(getattr(chain, "underlying_ltp", 0.0) or 0.0) or float(spot_price)
    atm = min(chain.strikes, key=lambda s: abs(s.strike_price - reference))
    contract = atm.call if is_long else atm.put

    if contract.ltp <= 0.0:
        logger.warning(
            "[OptionContract] %s %s strike %.2f has zero premium (illiquid) — skipping resolution",
            und, opt_type, atm.strike_price,
        )
        return ResolvedContract(symbol=und, resolved=False)

    symbol = _format_symbol(und, chain.expiry_date, atm.strike_price, opt_type)
    logger.info(
        "[OptionContract] %s -> %s premium=%.2f strike=%.2f spot=%.2f sec_id=%s",
        und, symbol, contract.ltp, atm.strike_price, reference, contract.security_id,
    )
    return ResolvedContract(
        premium=float(contract.ltp),
        symbol=symbol,
        strike=float(atm.strike_price),
        security_id=contract.security_id,
        expiry=chain.expiry_date,
        resolved=True,
    )


def option_ltp(symbol: str) -> Optional[float]:
    """
    Live premium for an option contract symbol from the cached option chain.

    Option strikes are not on the WebSocket feed (only underlyings are), so this
    is the only live price a held option has. Matching rebuilds each cached
    contract's symbol with the same _format_symbol that named it, rather than
    parsing the symbol. Returns None if no cached chain carries the contract.
    """
    sym = (symbol or "").strip().upper()
    if not sym.endswith(("CE", "PE")):
        return None
    opt_type = sym[-2:]
    for (und, _exp), entry in list(option_chain_cache._cache.items()):
        if not sym.startswith(str(und).upper()):
            continue
        chain = entry.data
        if chain is None:
            continue
        for row in getattr(chain, "strikes", None) or []:
            if _format_symbol(str(und).upper(), chain.expiry_date, row.strike_price, opt_type) == sym:
                contract = row.call if opt_type == "CE" else row.put
                ltp = float(getattr(contract, "ltp", 0.0) or 0.0)
                return ltp if ltp > 0.0 else None
    return None


def mark_price(symbol: str) -> Optional[float]:
    """Best live price for marking a position: WebSocket tick, else option chain."""
    from pipeline.pipeline_orchestrator import pipeline_orchestrator
    ltp = pipeline_orchestrator.get_ltp(symbol)
    if ltp:
        return ltp
    return option_ltp(symbol)
