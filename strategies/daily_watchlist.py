"""
Strike Desk V4 – Daily Watchlist Scanner
Scans the full 100-stock universe each scan cycle and returns the top 10 option
candidates (CE or PE) ranked by composite setup quality.

Scoring breakdown (100 pts):
  Sentiment Conviction  35 pts  |sentiment_score| strength
  Trend Strength        25 pts  ADX-based; rewards established trends
  Indicator Alignment   25 pts  MACD + Supertrend + EMA9 + VWAP + RSI (5 × 5 pts)
  Option Availability   15 pts  symbol has a cached option chain

Direction:
  CE (LONG)  – sentiment_score > 0 or price > EMA9 when sentiment neutral
  PE (SHORT) – sentiment_score < 0 or price < EMA9 when sentiment neutral

Minimum bar to appear in the list:
  - Option chain available
  - ADX > 15  (any trend)
  - Sufficient EMA9 data
  - Total score >= 30
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCAN_TOP_N = 10
_MIN_ADX = 15.0
_MIN_SCORE = 30.0


@dataclass
class WatchlistEntry:
    rank: int
    symbol: str
    direction: str          # "CE" or "PE"
    score: float            # 0–100
    sentiment_score: float
    adx: Optional[float]
    ltp: Optional[float]
    reasons: List[str]
    scanned_at: str = ""    # ISO timestamp

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "direction": self.direction,
            "score": round(self.score, 1),
            "sentiment_score": round(self.sentiment_score, 2),
            "adx": round(self.adx, 1) if self.adx is not None else None,
            "ltp": round(self.ltp, 2) if self.ltp is not None else None,
            "reasons": self.reasons,
            "scanned_at": self.scanned_at,
        }


def _score_symbol(
    symbol: str,
    ind: Dict[str, Any],
    has_option_chain: bool,
) -> Optional[Dict[str, Any]]:
    """
    Scores a single symbol. Returns None if the minimum bar is not met.
    Returns a dict with score, direction, reasons.
    """
    reasons: List[str] = []

    # ── Require option chain ──────────────────────────────────────────────
    if not has_option_chain:
        return None

    ema9 = ind.get("ema9") or ind.get("ema")
    ltp = ind.get("ltp") or ind.get("price")
    if ema9 is None or ltp is None or float(ema9) <= 0:
        return None

    ema9 = float(ema9)
    ltp = float(ltp)

    # ── Determine direction ───────────────────────────────────────────────
    sentiment_score = float(ind.get("sentiment_score") or ind.get("sentiment") or 0.0)
    if sentiment_score > 5.0:
        direction = "CE"
    elif sentiment_score < -5.0:
        direction = "PE"
    else:
        direction = "CE" if ltp >= ema9 else "PE"

    is_long = direction == "CE"

    # ── ADX gate ──────────────────────────────────────────────────────────
    adx = ind.get("adx")
    adx_val = float(adx) if adx is not None else 0.0
    if adx_val < _MIN_ADX:
        return None

    # ── 1. Sentiment Conviction (0–35 pts) ───────────────────────────────
    conviction_pts = min(35.0, abs(sentiment_score) / 100.0 * 35.0)
    if abs(sentiment_score) >= 30:
        reasons.append(
            f"sentiment {'bullish' if sentiment_score > 0 else 'bearish'} "
            f"({sentiment_score:+.1f})"
        )

    # ── 2. Trend Strength (0–25 pts) ──────────────────────────────────────
    if adx_val >= 20.0:
        trend_pts = min(25.0, (adx_val - 20.0) / 30.0 * 25.0)
        reasons.append(f"ADX={adx_val:.1f} trending")
    else:
        trend_pts = 0.0

    # ── 3. Indicator Alignment (0–25 pts, 5 × 5 pts) ─────────────────────
    alignment_pts = 0.0

    # a) EMA9
    ema_aligned = ltp > ema9 if is_long else ltp < ema9
    if ema_aligned:
        alignment_pts += 5.0
        reasons.append(f"EMA9 {'above' if is_long else 'below'}")

    # b) VWAP
    vwap = ind.get("vwap")
    if vwap is not None and float(vwap) > 0:
        vwap_aligned = ltp > float(vwap) if is_long else ltp < float(vwap)
        if vwap_aligned:
            alignment_pts += 5.0
            reasons.append(f"VWAP {'above' if is_long else 'below'}")

    # c) MACD
    macd_line = ind.get("macd_line")
    signal_line = ind.get("signal_line")
    if macd_line is not None and signal_line is not None:
        macd_aligned = float(macd_line) > float(signal_line) if is_long else float(macd_line) < float(signal_line)
        if macd_aligned:
            alignment_pts += 5.0
            reasons.append(f"MACD {'bullish' if is_long else 'bearish'}")
        # Extra nudge for fresh crossover
        crossover = ind.get("macd_crossover")
        if crossover == ("BULLISH_CROSS" if is_long else "BEARISH_CROSS"):
            alignment_pts = min(25.0, alignment_pts + 2.0)
            reasons.append("MACD fresh cross")

    # d) Supertrend
    st_dir = ind.get("supertrend_direction")
    if st_dir is not None:
        st_aligned = (st_dir == "UP") if is_long else (st_dir == "DOWN")
        if st_aligned:
            alignment_pts += 5.0
            reasons.append(f"Supertrend {'UP' if is_long else 'DOWN'}")

    # e) RSI sweet zone (not overbought for CE, not oversold for PE)
    rsi = ind.get("rsi")
    if rsi is not None:
        rsi_val = float(rsi)
        if is_long and 40.0 <= rsi_val <= 65.0:
            alignment_pts += 5.0
            reasons.append(f"RSI={rsi_val:.0f} healthy")
        elif not is_long and 35.0 <= rsi_val <= 60.0:
            alignment_pts += 5.0
            reasons.append(f"RSI={rsi_val:.0f} healthy")
        elif is_long and rsi_val < 35.0:
            alignment_pts += 3.0
            reasons.append(f"RSI={rsi_val:.0f} oversold bounce")
        elif not is_long and rsi_val > 65.0:
            alignment_pts += 3.0
            reasons.append(f"RSI={rsi_val:.0f} overbought reversal")

    # ── 4. Option Chain Available (15 pts) ───────────────────────────────
    option_pts = 15.0  # already gated above

    total = conviction_pts + trend_pts + alignment_pts + option_pts

    if total < _MIN_SCORE:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "score": round(total, 2),
        "sentiment_score": sentiment_score,
        "adx": adx_val,
        "ltp": ltp,
        "reasons": reasons,
    }


class DailyWatchlistScanner:
    """
    Thread-safe scanner. Call `refresh(bundles, option_chain_cache)` to rebuild
    the watchlist; call `get_watchlist()` to read the last result.
    """

    def __init__(self, top_n: int = _SCAN_TOP_N) -> None:
        self.top_n = top_n
        self._lock = threading.Lock()
        self._watchlist: List[WatchlistEntry] = []
        self._last_scanned_at: Optional[str] = None

    def _symbols_with_chains(self, option_chain_cache: Any) -> set:
        """Returns set of underlying symbols that have at least one cached chain."""
        try:
            return {k[0].upper() for k in option_chain_cache._cache.keys()}
        except Exception:
            return set()

    def refresh(
        self,
        bundles: Dict[str, Any],
        option_chain_cache: Any,
    ) -> List[WatchlistEntry]:
        """
        Scores all symbols in bundles, picks top N, updates internal state.
        Safe to call from any thread.
        """
        chainable = self._symbols_with_chains(option_chain_cache)
        candidates = []
        now_str = datetime.now(timezone.utc).isoformat()

        for symbol, bundle in bundles.items():
            ind = dict(getattr(bundle, "indicator_values", {}))
            if not ind:
                continue

            # Add LTP from bundle's latest tick if not in indicators
            latest_tick = getattr(bundle, "_latest_tick", None)
            if ind.get("ltp") is None and latest_tick is not None:
                ind["ltp"] = getattr(latest_tick, "ltp", None)

            has_chain = symbol.upper() in chainable
            result = _score_symbol(symbol.upper(), ind, has_chain)
            if result is not None:
                result["scanned_at"] = now_str
                candidates.append(result)

        # Sort by score descending
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[: self.top_n]

        entries = [
            WatchlistEntry(
                rank=i + 1,
                symbol=c["symbol"],
                direction=c["direction"],
                score=c["score"],
                sentiment_score=c["sentiment_score"],
                adx=c["adx"],
                ltp=c["ltp"],
                reasons=c["reasons"],
                scanned_at=c["scanned_at"],
            )
            for i, c in enumerate(top)
        ]

        with self._lock:
            self._watchlist = entries
            self._last_scanned_at = now_str

        ce_count = sum(1 for e in entries if e.direction == "CE")
        pe_count = len(entries) - ce_count
        logger.info(
            "[DailyWatchlist] Scanned %d symbols → %d candidates → top %d (CE=%d PE=%d)",
            len(bundles), len(candidates), len(entries), ce_count, pe_count,
        )
        return entries

    def get_watchlist(self) -> List[WatchlistEntry]:
        with self._lock:
            return list(self._watchlist)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "last_scanned_at": self._last_scanned_at,
                "entry_count": len(self._watchlist),
                "entries": [e.to_dict() for e in self._watchlist],
            }


# Module-level singleton
daily_watchlist_scanner = DailyWatchlistScanner(top_n=_SCAN_TOP_N)
