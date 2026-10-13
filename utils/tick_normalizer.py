from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, Optional
from models.tick import Tick

logger = logging.getLogger(__name__)

OPTION_PATTERN = re.compile(r"(\d+(CE|PE)$|[\s_-](CE|PE)$)")
FUTURES_PATTERN = re.compile(r"(FUT$|[\s_-]FUT)")
# Pure index names: no CE/PE, no FUT, no expiry digits — pure alpha or alpha+digits-free
# Examples: NIFTY, BANKNIFTY, SENSEX, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, BANKEX
INDEX_BASE_PATTERN = re.compile(r"^[A-Z]+(?:50|NXT50|NIFTY|NEXT50)?$")
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
    if OPTION_PATTERN.search(upper):
        return "OPT"
    if FUTURES_PATTERN.search(upper):
        return "FUT"
    # Pure index names (no date/strike suffix) — classify before falling to EQ
    if upper in _KNOWN_INDICES:
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
            ltp=float(raw.get("LTP") or raw.get("ltp") or 0.0),
            bid_price=bid_price,
            ask_price=ask_price,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            volume=float(raw.get("volume") or raw.get("vol") or 0.0),
            total_buy_qty=float(raw.get("total_buy_quantity") or raw.get("total_buy_qty") or 0.0),
            total_sell_qty=float(raw.get("total_sell_quantity") or raw.get("total_sell_qty") or 0.0),
            open=float(raw.get("open") or 0.0),
            high=float(raw.get("high") or 0.0),
            low=float(raw.get("low") or 0.0),
            close=float(raw.get("close") or raw.get("prev_close") or 0.0),
            vwap=float(raw.get("avg_price") or raw.get("atp") or raw.get("vwap") or 0.0),
            oi=float(raw.get("OI") or raw.get("oi") or 0.0),
            oi_change=float(raw.get("oi_change") or 0.0),
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
        ltp = float(raw.get("last_traded_price") or raw.get("ltp") or 0.0) / _P

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
            volume=float(raw.get("volume_trade_for_the_day") or raw.get("volume") or 0.0),
            total_buy_qty=float(raw.get("total_buy_quantity") or raw.get("total_buy_qty") or 0.0),
            total_sell_qty=float(raw.get("total_sell_quantity") or raw.get("total_sell_qty") or 0.0),
            open=float(raw.get("open_price_of_the_day") or raw.get("open") or 0.0) / _P,
            high=float(raw.get("high_price_of_the_day") or raw.get("high") or 0.0) / _P,
            low=float(raw.get("low_price_of_the_day") or raw.get("low") or 0.0) / _P,
            close=float(raw.get("closed_price") or raw.get("close") or 0.0) / _P,
            vwap=float(raw.get("avg_price") or raw.get("vwap") or 0.0) / _P,
            oi=float(raw.get("open_interest") or raw.get("oi") or 0.0),
            oi_change=float(raw.get("oi_change") or 0.0),
        )

    @staticmethod
    def normalize_fyers(raw: Dict[str, Any]) -> Tick:
        symbol = str(raw.get("symbol") or "UNKNOWN")
        exchange = "BSE" if symbol.startswith("BSE:") else "NSE"
        inst_type = detect_instrument_type(symbol)

        ts = _parse_timestamp(raw.get("exch_feed_time") or raw.get("timestamp"))
        oi = float(raw.get("oi") or 0.0)
        pdoi = float(raw.get("pdoi") or 0.0)
        oi_change = float(raw.get("oi_change") or (oi - pdoi if pdoi > 0 else 0.0))

        return Tick(
            symbol=symbol,
            exchange=exchange,
            instrument_type=inst_type,
            timestamp=ts,
            ltp=float(raw.get("ltp") or 0.0),
            bid_price=float(raw.get("bid") or raw.get("bid_price") or 0.0),
            ask_price=float(raw.get("ask") or raw.get("ask_price") or 0.0),
            bid_qty=float(raw.get("bqty") or raw.get("bid_qty") or 0.0),
            ask_qty=float(raw.get("aqty") or raw.get("ask_qty") or 0.0),
            volume=float(raw.get("vol_traded_today") or raw.get("volume") or 0.0),
            total_buy_qty=float(raw.get("tot_buy_gbd") or raw.get("total_buy_qty") or 0.0),
            total_sell_qty=float(raw.get("tot_sell_gbd") or raw.get("total_sell_qty") or 0.0),
            open=float(raw.get("open_price") or raw.get("open") or 0.0),
            high=float(raw.get("high_price") or raw.get("high") or 0.0),
            low=float(raw.get("low_price") or raw.get("low") or 0.0),
            close=float(raw.get("prev_close_price") or raw.get("close") or 0.0),
            vwap=float(raw.get("atp") or raw.get("vwap") or 0.0),
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
