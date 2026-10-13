import hashlib
import time
import uuid
from typing import Optional


def compute_order_checksum(symbol: str, side: str, quantity: float, price: float, timestamp_ms: int) -> str:
    raw = f"{symbol.strip().upper()}:{side.strip().upper()}:{float(quantity)}:{float(price)}:{int(timestamp_ms)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def generate_client_order_id(
    symbol: str,
    side: str,
    quantity: float,
    price: float = 0.0,
    prefix: str = "SD",
    custom_timestamp_ms: Optional[int] = None,
) -> str:
    ts = custom_timestamp_ms if custom_timestamp_ms is not None else int(time.time() * 1000)
    uid = uuid.uuid4().hex[:8]
    checksum = compute_order_checksum(symbol, side, quantity, price, ts)
    return f"{prefix}_{ts}_{uid}_{checksum}"


def parse_client_order_id(client_order_id: str) -> Optional[dict]:
    parts = client_order_id.split("_")
    if len(parts) != 4:
        return None
    prefix, ts_str, uid, checksum = parts
    try:
        ts = int(ts_str)
        return {
            "prefix": prefix,
            "timestamp_ms": ts,
            "uuid": uid,
            "checksum": checksum,
        }
    except ValueError:
        return None


def verify_client_order_id(
    client_order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float = 0.0,
) -> bool:
    parsed = parse_client_order_id(client_order_id)
    if not parsed:
        return False
    expected_checksum = compute_order_checksum(symbol, side, quantity, price, parsed["timestamp_ms"])
    return parsed["checksum"] == expected_checksum


def compute_order_signature(
    broker: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float = 0.0,
    order_type: str = "LIMIT",
) -> str:
    return f"{broker.strip().lower()}:{symbol.strip().upper()}:{side.strip().upper()}:{float(quantity)}:{round(float(price), 2)}:{order_type.strip().upper()}"