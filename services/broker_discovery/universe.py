"""
Shared instrument-universe definition for broker discovery.

Broker master files list 150k-200k contracts, far beyond any broker's
WebSocket subscription cap (Fyers rejects past ~5000). Discovery therefore
narrows every broker's master file down to a common tradable universe:

  * the 5 core spot indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX)
  * the NIFTY 100 cash equities

Membership cannot be derived from a broker master file — those files carry no
index-membership field — so the NIFTY 100 roster comes from NSE and is cached
to disk. Matching is done on ISIN where the broker publishes it (Dhan, Fyers)
and on ticker otherwise (AngelOne publishes no ISIN).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import time
from typing import Dict, List, Optional, Set, Tuple

import requests

logger = logging.getLogger(__name__)

NIFTY100_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_FILE = os.path.join(_BASE_DIR, "data", "nifty100_constituents.csv")
_CACHE_TTL_SECONDS = 86400

# NSE blocks requests without a browser-like UA.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}

# Spot index tickers as they appear in broker master files, mapped to the
# canonical internal name. Brokers spell the same index several ways.
INDEX_ALIASES: Dict[str, str] = {
    "NIFTY": "NIFTY",
    "NIFTY 50": "NIFTY",
    "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
    "BSE SENSEX": "SENSEX",
}


def canonical_index_name(raw: Optional[str]) -> Optional[str]:
    """Map a broker's spelling of a spot index to the canonical internal name."""
    if not raw:
        return None
    key = " ".join(str(raw).strip().upper().split())
    return INDEX_ALIASES.get(key)


def _read_cache() -> Optional[str]:
    if not os.path.exists(_CACHE_FILE):
        return None
    if time.time() - os.path.getmtime(_CACHE_FILE) > _CACHE_TTL_SECONDS:
        return None
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        logger.warning("Could not read NIFTY 100 cache: %s", exc)
        return None


def _write_cache(text: str) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = f"{_CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, _CACHE_FILE)
    except Exception as exc:
        logger.warning("Could not cache NIFTY 100 list: %s", exc)


def _fetch_nifty100_csv(force_refresh: bool = False) -> Optional[str]:
    if not force_refresh:
        cached = _read_cache()
        if cached:
            return cached
    try:
        resp = requests.get(NIFTY100_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        text = resp.text
        if "Symbol" not in text.splitlines()[0]:
            raise ValueError("unexpected NIFTY 100 CSV header")
        _write_cache(text)
        return text
    except Exception as exc:
        logger.error("Failed to download NIFTY 100 list: %s", exc)
        # Fall back to a stale cache rather than an empty universe.
        if os.path.exists(_CACHE_FILE):
            try:
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    logger.warning("Using stale NIFTY 100 cache.")
                    return f.read()
            except Exception:
                pass
        return None


def nifty100_membership(force_refresh: bool = False) -> Tuple[Set[str], Set[str]]:
    """
    Return (tickers, isins) for the NIFTY 100 constituents.
    Both sets are empty if the roster could not be obtained.
    """
    text = _fetch_nifty100_csv(force_refresh=force_refresh)
    if not text:
        return set(), set()

    tickers: Set[str] = set()
    isins: Set[str] = set()
    try:
        for row in csv.DictReader(io.StringIO(text)):
            sym = (row.get("Symbol") or "").strip().upper()
            isin = (row.get("ISIN Code") or "").strip().upper()
            if sym:
                tickers.add(sym)
            if isin:
                isins.add(isin)
    except Exception as exc:
        logger.error("Failed to parse NIFTY 100 CSV: %s", exc)
        return set(), set()

    logger.info("NIFTY 100 universe: %d tickers, %d ISINs", len(tickers), len(isins))
    return tickers, isins


def in_universe(
    ticker: Optional[str],
    isin: Optional[str],
    tickers: Set[str],
    isins: Set[str],
) -> bool:
    """True if a cash-equity row belongs to the NIFTY 100 universe."""
    if isin and isins:
        if str(isin).strip().upper() in isins:
            return True
    if ticker and tickers:
        # Strip broker suffixes such as "-EQ" / "-BE".
        t = str(ticker).strip().upper()
        for suffix in ("-EQ", "-BE", "-BZ", "-SM"):
            if t.endswith(suffix):
                t = t[: -len(suffix)]
                break
        return t in tickers
    return False


def download_text(url: str, timeout: int = 120) -> Optional[str]:
    """Download a master-contract file as text, or None on failure."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.error("Failed to download %s: %s", url, exc)
        return None


def download_json(url: str, timeout: int = 180) -> Optional[List[dict]]:
    """Download a JSON master-contract file, or None on failure."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as exc:
        logger.error("Failed to download %s: %s", url, exc)
        return None
