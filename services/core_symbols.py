"""
Hardcoded fallback token table for the 5 core NSE/BSE index symbols.

Immutable Rule #6: broker API is the primary discovery source.
This module is a FALLBACK for when broker discovery returns 0 instruments
(e.g. outside market hours, API access limitations, or during initial boot).

These tokens are stable and rarely change:
- Dhan: IDX_I segment numeric security IDs
- AngelOne: NSE/BSE numeric tokens
- Fyers: exchange:symbol-INDEX format

STOCK_FUTURES_LOT_SIZES is the static fallback for stock F&O lot sizes.
Primary source is the instrument registry (populated from the Dhan master CSV
during discovery). This table must be refreshed after every quarterly NSE
F&O lot-size revision circular.
"""

import re
from typing import Any, Dict

CORE_SYMBOLS: Dict[str, Dict[str, Any]] = {
    "NIFTY": {
        "symbol":           "NIFTY",
        "display_name":     "NIFTY 50",
        "asset_class":      "INDEX_FUTURES",
        "instrument_type":  "IDX_FUT",
        "exchange":         "NSE",
        "dhan_token":       "13",
        "dhan_segment":     "IDX_I",
        "angel_token":      "26000",
        "angel_exchange":   1,          # exchangeType: 1 = NSE
        "fyers_symbol":     "NSE:NIFTY50-INDEX",
        "lot_size":         65,
    },
    "BANKNIFTY": {
        "symbol":           "BANKNIFTY",
        "display_name":     "BANK NIFTY",
        "asset_class":      "INDEX_FUTURES",
        "instrument_type":  "IDX_FUT",
        "exchange":         "NSE",
        "dhan_token":       "25",
        "dhan_segment":     "IDX_I",
        "angel_token":      "26009",
        "angel_exchange":   1,
        "fyers_symbol":     "NSE:NIFTYBANK-INDEX",
        "lot_size":         30,
    },
    "FINNIFTY": {
        "symbol":           "FINNIFTY",
        "display_name":     "NIFTY FIN SERVICE",
        "asset_class":      "INDEX_FUTURES",
        "instrument_type":  "IDX_FUT",
        "exchange":         "NSE",
        "dhan_token":       "27",
        "dhan_segment":     "IDX_I",
        "angel_token":      "26037",
        "angel_exchange":   1,
        "fyers_symbol":     "NSE:FINNIFTY-INDEX",
        "lot_size":         60,
    },
    "MIDCPNIFTY": {
        "symbol":           "MIDCPNIFTY",
        "display_name":     "NIFTY MIDCAP SELECT",
        "asset_class":      "INDEX_FUTURES",
        "instrument_type":  "IDX_FUT",
        "exchange":         "NSE",
        "dhan_token":       "442",
        "dhan_segment":     "IDX_I",
        "angel_token":      "26074",
        "angel_exchange":   1,
        "fyers_symbol":     "NSE:MIDCPNIFTY-INDEX",
        "lot_size":         120,
    },
    "SENSEX": {
        "symbol":           "SENSEX",
        "display_name":     "BSE SENSEX",
        "asset_class":      "INDEX_FUTURES",
        "instrument_type":  "IDX_FUT",
        "exchange":         "BSE",
        "dhan_token":       "51",
        "dhan_segment":     "IDX_I",
        # AngelOne publishes BSE SENSEX only as an AMXIDX row with token
        # 99919000. Token "1" on BSE is "BSX", an unrelated instrument.
        "angel_token":      "99919000",
        "angel_exchange":   3,          # exchangeType: 3 = BSE
        "fyers_symbol":     "BSE:SENSEX-INDEX",
        "lot_size":         20,
    },
}

def get_symbols_for_broker(broker_name: str) -> list:
    """
    Return the core index symbols in the token format one broker understands.

    Each broker identifies the same index differently (Dhan: numeric
    security_id + segment, AngelOne: numeric token + exchangeType, Fyers:
    exchange:symbol-INDEX string), so callers subscribing or routing for a
    specific broker should go through this rather than reading CORE_SYMBOLS
    directly and picking the wrong field.
    """
    broker = (broker_name or "").strip().lower()
    out = []
    for name, data in CORE_SYMBOLS.items():
        common = {
            "symbol": name,
            "display_name": data["display_name"],
            "exchange": data["exchange"],
            "lot_size": data["lot_size"],
        }
        if broker == "dhan":
            out.append({**common, "security_id": data["dhan_token"],
                        "segment": data["dhan_segment"]})
        elif broker == "angelone":
            out.append({**common, "token": data["angel_token"],
                        "exchange_type": data["angel_exchange"]})
        elif broker == "fyers":
            out.append({**common, "fyers_symbol": data["fyers_symbol"]})
        else:
            raise ValueError(
                f"Unknown broker '{broker_name}'. Expected dhan, angelone or fyers."
            )
    return out


# Reverse lookup: broker token → symbol name
DHAN_TOKEN_TO_SYMBOL: Dict[str, str] = {
    v["dhan_token"]: k for k, v in CORE_SYMBOLS.items()
}
ANGEL_TOKEN_TO_SYMBOL: Dict[str, str] = {
    v["angel_token"]: k for k, v in CORE_SYMBOLS.items()
}
FYERS_SYMBOL_TO_NAME: Dict[str, str] = {
    v["fyers_symbol"]: k for k, v in CORE_SYMBOLS.items()
}


# ---------------------------------------------------------------------------
# F&O lot sizes for NIFTY 100 stock futures (static fallback).
# Synced from Dhan api-scrip-master-detailed.csv (nearest futures expiry) on 2026-10-01.
# Registry values from discovery take precedence; these are used only on a registry miss.
# Primary source: Dhan master CSV parsed during broker discovery.
# Refresh after every quarterly NSE F&O lot-size revision.
# ---------------------------------------------------------------------------
STOCK_FUTURES_LOT_SIZES: Dict[str, int] = {
    # Nifty 50
    "RELIANCE":     500,
    "TCS":          225,
    "HDFCBANK":     650,
    "INFY":         400,
    "HINDUNILVR":   300,
    "ICICIBANK":    700,
    "KOTAKBANK":    2000,
    "SBIN":         750,
    "AXISBANK":     625,
    "BAJFINANCE":   750,
    "BAJAJFINSV":   300,
    "LT":           175,
    "TECHM":        600,
    "M&M":          200,
    "TITAN":        175,
    "NESTLEIND":    500,
    "WIPRO":        3000,
    "TATAMOTORS":   550,
    "TATASTEEL":    2750,
    "ITC":          1725,
    "SUNPHARMA":    350,
    "ADANIPORTS":   475,
    "ONGC":         2250,
    "BPCL":         1975,
    "HCLTECH":      400,
    "ULTRACEMCO":   50,
    "ASIANPAINT":   250,
    "GRASIM":       250,
    "ADANIENT":     309,
    "POWERGRID":    1900,
    "NTPC":         1500,
    "MARUTI":       50,
    "INDUSINDBK":   700,
    "CIPLA":        425,
    "DIVISLAB":     100,
    "EICHERMOT":    100,
    "HEROMOTOCO":   150,
    "JSWSTEEL":     675,
    "COALINDIA":    1350,
    "DRREDDY":      625,
    "BRITANNIA":    125,
    "APOLLOHOSP":   125,
    "HINDALCO":     700,
    "AMBUJACEM":    1200,
    "TATACONSUM":   550,
    "HAVELLS":      500,
    "PIDILITIND":   500,
    "BAJAJ-AUTO":   75,
    "NAUKRI":       550,
    "LTIM":         150,
    "HDFCLIFE":     1100,
    "SBILIFE":      375,
    "MARICO":       1200,
    "SIEMENS":      175,
    "COLPAL":       275,
    "DMART":        150,
    "ZOMATO":       4500,
    "TRENT":        225,
    "SHREECEM":     25,
    "GODREJCP":     500,
    "VEDL":         1150,
    # Remaining Nifty 100 (common futures)
    "BERGEPAINT":   600,
    "BIOCON":       2500,
    "BOSCHLTD":     25,
    "CHOLAFIN":     625,
    "CONCOR":       1250,
    "DABUR":        1250,
    "DLF":          950,
    "FEDERALBNK":   2500,
    "GAIL":         3550,
    "INDHOTEL":     1000,
    "IOC":          4875,
    "LUPIN":        425,
    "MPHASIS":      275,
    "OBEROIRLTY":   350,
    "PERSISTENT":   125,
    "PFC":          1300,
    "RECLTD":       1575,
    "SAIL":         4700,
    "SBICARD":      800,
    "TATAELXSI":    125,
    "VOLTAS":       375,
    "ZEEL":         2500,
    "ADANIGREEN":   600,
}

# ---------------------------------------------------------------------------
# Expiry+suffix strippers — FUT and option (CE/PE) variants
# Handles formats: HINDUNILVR29OCTFUT, NIFTY29SEP26FUT, BANKNIFTY25OCTFUT
#                  NIFTY24OCT24250CE, HINDUNILVR24OCT1900PE, NIFTY2410031750CE
# ---------------------------------------------------------------------------
_FUT_SUFFIX_RE = re.compile(
    r"\d*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d*FUT$",
    re.IGNORECASE,
)

# Strips expiry-code + strike + CE/PE from the end of an option symbol.
# Handles monthly (24OCT24250CE) and weekly / pure-digit (2410031750CE) formats.
_OPT_SUFFIX_RE = re.compile(
    # (?:\.\d+)? — strikes can be fractional ("ITC26OCT257.5CE"); without it only
    # "5CE" was stripped and the underlying resolved to "ITC26OCT257.".
    r"\d+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)?\d*(?:\.\d+)?(?:CE|PE)$",
    re.IGNORECASE,
)


def resolve_fut_underlying(symbol: str) -> str:
    """
    Strip the expiry+FUT suffix to get the underlying ticker.

        "HINDUNILVR29OCTFUT"  -> "HINDUNILVR"
        "NIFTY29SEP26FUT"     -> "NIFTY"
        "BANKNIFTY25OCTFUT"   -> "BANKNIFTY"
        "RELIANCE"            -> "RELIANCE"   (no suffix — returned unchanged)
    """
    return _FUT_SUFFIX_RE.sub("", symbol.upper()).strip()


def resolve_opt_underlying(symbol: str) -> str:
    """
    Strip the expiry+strike+CE/PE suffix to get the underlying ticker.

        "NIFTY24OCT24250CE"       -> "NIFTY"
        "HINDUNILVR24OCT1900PE"   -> "HINDUNILVR"
        "NIFTY2410031750CE"       -> "NIFTY"   (weekly / pure-digit expiry)
        "BANKNIFTY"               -> "BANKNIFTY"  (no suffix — unchanged)
    """
    return _OPT_SUFFIX_RE.sub("", symbol.upper()).strip()
