from datetime import datetime, timezone
import logging
import math
import re
from typing import Any, Dict, Optional
from models.tick import Tick


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    """Convert value to float, returning fallback for None, NaN, and Inf."""
    if value is None:
        return fallback
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback

logger = logging.getLogger(__name__)

OPTION_PATTERN = re.compile(r"(\d+(CE|PE)$|[\s_-](CE|PE)$)")
FUTURES_PATTERN = re.compile(r"(FUT$|[\s_-]FUT)")
# Pure index names: no CE/PE, no FUT, no expiry digits — pure alpha or alpha+digits-free
# Examples: NIFTY, BANKNIFTY, SENSEX, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, BANKEX
INDEX_BASE_PATTERN = re.compile(r"^[A-Z]+(?:50|NXT50|NIFTY|NEXT50)?$")
# Fyers-style fully-qualified index symbols end with "-INDEX" (e.g. "NSE:NIFTY50-INDEX")
_FYERS_INDEX_SUFFIX = re.compile(r"-INDEX$", re.IGNORECASE)
_KNOWN_INDICES = frozenset({
    "NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "BANKEX", "NIFTYIT", "NIFTYMIDCAP",
})


def _parse_timestamp(raw_time: Any) -> datetime:
    if raw_time is None:
        return datetime.now(timezone.utc)
    if isinstance(raw_time, (int, float)):
        if raw_time > 1e11:
            raw_time = raw_time / 1000.0
        return datetime.fromtimestamp(raw_time, tz=timezone.utc)
    if isinstance(raw_time, str):
        try:
            dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def detect_instrument_type(symbol: str) -> str:
    upper = symbol.strip().upper()
    # Strip exchange prefix (e.g. "NSE:", "BSE:") before pattern matching
    bare = upper.split(":", 1)[-1] if ":" in upper else upper
    if OPTION_PATTERN.search(bare):
        return "OPT"
    if FUTURES_PATTERN.search(bare):
        return "FUT"
    # Fyers fully-qualified index symbols end with "-INDEX" (e.g. "NSE:NIFTY50-INDEX")
    if _FYERS_INDEX_SUFFIX.search(bare):
        return "IDX_FUT"
    # Short canonical index names (no date/strike suffix)
    if upper in _KNOWN_INDICES or bare in _KNOWN_INDICES:
        return "IDX_FUT"
    return "EQ"


def detect_exchange(symbol: str, raw_segment: Optional[str] = None) -> str:
    combined = f"{symbol} {raw_segment or ''}".upper()
    if "BSE" in combined:
        return "BSE"
    return "NSE"


class TickNormalizer:
    @staticmethod
    def normalize_dhan(raw: Dict[str, Any], symbol_override: Optional[str] = None) -> Tick:
        symbol = symbol_override or str(raw.get("security_id") or raw.get("symbol") or "UNKNOWN")
        exchange = detect_exchange(symbol, raw.get("exchange_segment"))
        inst_type = detect_instrument_type(symbol)

        depth = raw.get("depth", {}) or {}
        if isinstance(depth, list):
            # dhanhq Full packets send depth as a list of 5 levels, each holding
            # both sides: {bid_price, bid_quantity, ask_price, ask_quantity, ...}.
            levels = [lvl for lvl in depth if isinstance(lvl, dict)]
            bids = [{"price": l.get("bid_price", 0.0), "quantity": l.get("bid_quantity", 0.0)} for l in levels]
            asks = [{"price": l.get("ask_price", 0.0), "quantity": l.get("ask_quantity", 0.0)} for l in levels]
        else:
            bids = depth.get("buy") or depth.get("bids") or []
            asks = depth.get("sell") or depth.get("asks") or []

        bid_price = float(bids[0].get("price", 0.0)) if bids and isinstance(bids[0], dict) else float(raw.get("bid", 0.0) or 0.0)
        bid_qty = float(bids[0].get("quantity", 0.0)) if bids and isinstance(bids[0], dict) else float(raw.get("bid_qty", 0.0) or 0.0)
        ask_price = float(asks[0].get("price", 0.0)) if asks and isinstance(asks[0], dict) else float(raw.get("ask", 0.0) or 0.0)
        ask_qty = float(asks[0].get("quantity", 0.0)) if asks and isinstance(asks[0], dict) else float(raw.get("ask_qty", 0.0) or 0.0)

        ts = _parse_timestamp(raw.get("LTT") or raw.get("exchange_timestamp") or raw.get("timestamp"))

        return Tick(
            symbol=symbol,
            exchange=exchange,
            instrument_type=inst_type,
            timestamp=ts,
            ltp=_safe_float(raw.get("LTP") if raw.get("LTP") is not None else raw.get("ltp")),
            bid_price=bid_price,
            ask_price=ask_price,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            volume=_safe_float(raw.get("volume") if raw.get("volume") is not None else raw.get("vol")),
            total_buy_qty=_safe_float(raw.get("total_buy_quantity") if raw.get("total_buy_quantity") is not None else raw.get("total_buy_qty")),
            total_sell_qty=_safe_float(raw.get("total_sell_quantity") if raw.get("total_sell_quantity") is not None else raw.get("total_sell_qty")),
            open=_safe_float(raw.get("open")),
            high=_safe_float(raw.get("high")),
            low=_safe_float(raw.get("low")),
            close=_safe_float(raw.get("close") if raw.get("close") is not None else raw.get("prev_close")),
            vwap=_safe_float(raw.get("avg_price") if raw.get("avg_price") is not None else (raw.get("atp") if raw.get("atp") is not None else raw.get("vwap"))),
            oi=_safe_float(raw.get("OI") if raw.get("OI") is not None else raw.get("oi")),
            oi_change=_safe_float(raw.get("oi_change")),
        )

    @staticmethod
    def normalize_angelone(raw: Dict[str, Any], symbol_override: Optional[str] = None) -> Tick:
        symbol = symbol_override or str(raw.get("token") or raw.get("symbol") or "UNKNOWN")
        exch_type = str(raw.get("exchange_type", ""))
        exchange = "BSE" if "BSE" in exch_type or "3" in exch_type else "NSE"
        inst_type = detect_instrument_type(symbol)

        bids = raw.get("best_5_buy_data") or []
        asks = raw.get("best_5_sell_data") or []

        # AngelOne SmartWebSocket v2 sends all prices in paise; divide by 100 to get rupees.
        _P = 100.0
        bid_price = float(bids[0].get("price", 0.0)) / _P if bids and isinstance(bids[0], dict) else 0.0
        bid_qty = float(bids[0].get("quantity", 0.0)) if bids and isinstance(bids[0], dict) else 0.0
        ask_price = float(asks[0].get("price", 0.0)) / _P if asks and isinstance(asks[0], dict) else 0.0
        ask_qty = float(asks[0].get("quantity", 0.0)) if asks and isinstance(asks[0], dict) else 0.0

        ts = _parse_timestamp(raw.get("exchange_timestamp") or raw.get("timestamp"))
        # AngelOne sends all prices in paise; _safe_float then divide by 100.
        _raw_ltp = raw.get("last_traded_price") if raw.get("last_traded_price") is not None else raw.get("ltp")
        ltp = _safe_float(_raw_ltp) / _P

        return Tick(
            symbol=symbol,
            exchange=exchange,
            instrument_type=inst_type,
            timestamp=ts,
            ltp=ltp,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            volume=_safe_float(raw.get("volume_trade_for_the_day") if raw.get("volume_trade_for_the_day") is not None else raw.get("volume")),
            total_buy_qty=_safe_float(raw.get("total_buy_quantity") if raw.get("total_buy_quantity") is not None else raw.get("total_buy_qty")),
            total_sell_qty=_safe_float(raw.get("total_sell_quantity") if raw.get("total_sell_quantity") is not None else raw.get("total_sell_qty")),
            open=_safe_float(raw.get("open_price_of_the_day") if raw.get("open_price_of_the_day") is not None else raw.get("open")) / _P,
            high=_safe_float(raw.get("high_price_of_the_day") if raw.get("high_price_of_the_day") is not None else raw.get("high")) / _P,
            low=_safe_float(raw.get("low_price_of_the_day") if raw.get("low_price_of_the_day") is not None else raw.get("low")) / _P,
            close=_safe_float(raw.get("closed_price") if raw.get("closed_price") is not None else raw.get("close")) / _P,
            vwap=_safe_float(raw.get("avg_price") if raw.get("avg_price") is not None else raw.get("vwap")) / _P,
            oi=_safe_float(raw.get("open_interest") if raw.get("open_interest") is not None else raw.get("oi")),
            oi_change=_safe_float(raw.get("oi_change")),
        )

    @staticmethod
    def normalize_fyers(raw: Dict[str, Any]) -> Tick:
        symbol = str(raw.get("symbol") or "UNKNOWN")
        exchange = "BSE" if symbol.startswith("BSE:") else "NSE"
        inst_type = detect_instrument_type(symbol)

        ts = _parse_timestamp(raw.get("exch_feed_time") or raw.get("timestamp"))
        oi = _safe_float(raw.get("oi"))
        pdoi = _safe_float(raw.get("pdoi"))
        _raw_oi_change = raw.get("oi_change")
        oi_change = _safe_float(_raw_oi_change) if _raw_oi_change is not None else (oi - pdoi if pdoi > 0 else 0.0)

        # Use None-aware key selection so an explicitly provided 0.0 is not silently
        # skipped in favour of an alternate key (the old `or` chain had this bug).
        raw_bid = raw.get("bid") if "bid" in raw else raw.get("bid_price")
        raw_ask = raw.get("ask") if "ask" in raw else raw.get("ask_price")
        raw_bqty = raw.get("bqty") if "bqty" in raw else raw.get("bid_qty")
        raw_aqty = raw.get("aqty") if "aqty" in raw else raw.get("ask_qty")

        bid_price = float(raw_bid) if raw_bid is not None else 0.0
        ask_price = float(raw_ask) if raw_ask is not None else 0.0
        bid_qty = float(raw_bqty) if raw_bqty is not None else 0.0
        ask_qty = float(raw_aqty) if raw_aqty is not None else 0.0

        # bid_ask_valid: True only when both sides are finite, strictly positive, and
        # the instrument type has an order book.  Index instruments (IDX_FUT) never
        # carry a real order book on Fyers — the fields are absent or zero.
        bid_ask_valid = (
            inst_type != "IDX_FUT"
            and math.isfinite(bid_price) and bid_price > 0.0
            and math.isfinite(ask_price) and ask_price > 0.0
        )

        return Tick(
            symbol=symbol,
            exchange=exchange,
            instrument_type=inst_type,
            timestamp=ts,
            ltp=_safe_float(raw.get("ltp")),
            bid_price=bid_price,
            ask_price=ask_price,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            bid_ask_valid=bid_ask_valid,
            volume=_safe_float(raw.get("vol_traded_today") if raw.get("vol_traded_today") is not None else raw.get("volume")),
            total_buy_qty=_safe_float(raw.get("tot_buy_gbd") if raw.get("tot_buy_gbd") is not None else raw.get("total_buy_qty")),
            total_sell_qty=_safe_float(raw.get("tot_sell_gbd") if raw.get("tot_sell_gbd") is not None else raw.get("total_sell_qty")),
            open=_safe_float(raw.get("open_price") if raw.get("open_price") is not None else raw.get("open")),
            high=_safe_float(raw.get("high_price") if raw.get("high_price") is not None else raw.get("high")),
            low=_safe_float(raw.get("low_price") if raw.get("low_price") is not None else raw.get("low")),
            close=_safe_float(raw.get("prev_close_price") if raw.get("prev_close_price") is not None else raw.get("close")),
            vwap=_safe_float(raw.get("atp") if raw.get("atp") is not None else raw.get("vwap")),
            oi=oi,
            oi_change=oi_change,
        )

    @classmethod
    def normalize(cls, broker: str, raw_tick: Dict[str, Any], **kwargs) -> Tick:
        b = broker.lower()
        if b == "dhan":
            return cls.normalize_dhan(raw_tick, **kwargs)
        if b in ("angelone", "angel"):
            return cls.normalize_angelone(raw_tick, **kwargs)
        if b == "fyers":
            return cls.normalize_fyers(raw_tick)
        raise ValueError(f"Unsupported broker for tick normalization: {broker}")
