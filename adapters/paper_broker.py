"""
Paper broker: real market data, virtual money.

Prices come from the same live WebSocket feed the live adapters populate — this
class never fabricates a price. What it does not do is call any broker order API:
`place_order` fills against the current LTP and books the result in an in-process
virtual ledger, so no capital is ever at risk.

The invariant this file exists to hold: no import of, or reference to, a broker
SDK or a live adapter's order methods. If a fill cannot be priced from the live
feed, the order is rejected rather than filled at a guessed price.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from models.order import Order, OrderSide, OrderState, OrderUpdate

logger = logging.getLogger(__name__)

PAPER_BROKER_NAME = "paper"


@dataclass
class PaperPosition:
    symbol: str
    quantity: float = 0.0          # signed: positive long, negative short
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    lot_size: int = 1              # contract multiplier: 1 for EQ, >1 for FUT/OPT
    instrument_type: str = "EQ"   # for display; drives nothing beyond lot_size
    # Underlying/expiry/strike/CE-PE and broker ids from the opening order; the
    # symbol alone cannot address the contract at a broker (see execution/contract_store).
    contract: Dict[str, Any] = field(default_factory=dict)

    def market_value(self, ltp: float) -> float:
        return self.quantity * ltp * self.lot_size

    def unrealized_pnl(self, ltp: float) -> float:
        return (ltp - self.avg_price) * self.quantity * self.lot_size

    def to_dict(self, ltp: Optional[float]) -> Dict[str, Any]:
        price = ltp if ltp is not None else self.avg_price
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "direction": "LONG" if self.quantity >= 0 else "SHORT",
            "entry_price": round(self.avg_price, 2),
            "current_price": round(price, 2),
            "pnl": round(self.unrealized_pnl(price), 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "lot_size": self.lot_size,
            "instrument_type": self.instrument_type,
            "is_open": self.quantity != 0,
            "broker": PAPER_BROKER_NAME,
            "contract": dict(self.contract),
        }


class PaperBrokerAdapter:
    """
    Order-side stand-in for a broker. Deliberately not a BaseAdapter subclass: it
    owns no connection, no credentials and no market data of its own, and exposing
    connect/authenticate would imply otherwise. It implements only what the
    execution path calls on an adapter.
    """

    def __init__(self, ltp_provider, starting_cash: float = 1_000_000.0, tick_provider=None, db_path: Optional[str] = None) -> None:
        # `ltp_provider(symbol) -> Optional[float]` reads the live tick store.
        # `tick_provider(symbol) -> Optional[Tick]` used when source validation is required (P0-09).
        self._ltp_provider = ltp_provider
        self._tick_provider = tick_provider
        self.broker_name = PAPER_BROKER_NAME
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: Dict[str, PaperPosition] = {}
        self.fills: List[Dict[str, Any]] = []
        self._db_path = db_path
        if self._db_path:
            self._load_ledger()

    # ----- disk persistence ----------------------------------------------

    def _save_ledger(self) -> None:
        """Atomically persist the virtual ledger to disk using tmp-then-rename."""
        if not self._db_path:
            return
        state = {
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "positions": {
                sym: {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "realized_pnl": p.realized_pnl,
                    "lot_size": p.lot_size,
                    "instrument_type": p.instrument_type,
                    "contract": p.contract,
                }
                for sym, p in self.positions.items()
            },
            "fills": self.fills,
        }
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        tmp = self._db_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, self._db_path)

    def _load_ledger(self) -> bool:
        """
        Restore the virtual ledger from disk. Safe against missing or corrupt files.
        Returns True when a prior session's state was successfully loaded.
        """
        if not self._db_path or not os.path.exists(self._db_path):
            return False
        try:
            with open(self._db_path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception as exc:
            logger.warning("[PAPER] Cannot load ledger from %s: %s", self._db_path, exc)
            return False
        self.cash = float(state.get("cash", self.starting_cash))
        self.fills = list(state.get("fills") or [])
        self.positions = {}
        for sym, d in (state.get("positions") or {}).items():
            try:
                self.positions[sym] = PaperPosition(
                    symbol=d["symbol"],
                    quantity=float(d.get("quantity", 0.0)),
                    avg_price=float(d.get("avg_price", 0.0)),
                    realized_pnl=float(d.get("realized_pnl", 0.0)),
                    lot_size=int(d.get("lot_size", 1)),
                    instrument_type=str(d.get("instrument_type", "EQ")),
                    contract=dict(d.get("contract") or {}),
                )
            except Exception as exc:
                logger.warning("[PAPER] Skipping corrupt position %s in ledger: %s", sym, exc)
        logger.info(
            "[PAPER] Restored ledger from %s: cash=%.2f positions=%d fills=%d",
            self._db_path, self.cash, len(self.positions), len(self.fills),
        )
        return True

    # ----- pricing -------------------------------------------------------

    def get_fill_price(self, order: Order) -> Optional[float]:
        """
        A paper fill is only meaningful at a real, current price. A limit order
        fills at its limit; a market order needs a live LTP and is rejected
        without one rather than being filled at an invented price.

        When a tick_provider is set (C1/C3 enforcement):
        - Validates the tick source is "live" before accepting any fill.
        - Returns None if the live feed is unavailable (Paper Mode goes idle).
        - Raises ValueError if the tick source is not "live" (historical/cached forbidden).
        """
        if self._tick_provider is not None:
            tick = self._tick_provider(order.symbol)
            if tick is None:
                # Option strikes are not subscribed to the WebSocket feed — only
                # their underlying is. A limit order can fill at its stated price
                # without requiring a live tick for the individual strike.
                if order.price and order.price > 0:
                    return float(order.price)
                return None  # market order with no live price — Paper Mode idles
            tick_source = getattr(tick, "source", "live")
            if tick_source != "live":
                raise ValueError(
                    f"Paper Mode requires a live tick for {order.symbol}; "
                    f"received source='{tick_source}'. "
                    f"Historical or cached data must never be used for Paper fills (C1)."
                )
            if order.price and order.price > 0:
                return float(order.price)
            return tick.ltp if tick.ltp and tick.ltp > 0 else None

        # Backward-compat path: no tick_provider (unit tests, legacy callers)
        ltp = self._ltp_provider(order.symbol)
        if order.price and order.price > 0:
            return float(order.price)
        return ltp

    # ----- order path ----------------------------------------------------

    async def place_order(self, order: Order) -> OrderUpdate:
        fill_price = self.get_fill_price(order)
        if fill_price is None or fill_price <= 0:
            logger.warning(
                "[PAPER] REJECTED %s %s x%s — no live price available for %s. "
                "No broker API was called.",
                order.side.value, order.symbol, order.quantity, order.symbol,
            )
            raise ValueError(
                f"Paper fill needs a live price for {order.symbol}; none has ticked yet."
            )

        # Resolve lot_size for this order.
        #
        # For futures (symbol contains "FUT"):
        #   1. Extract the underlying ticker (strip expiry+FUT suffix).
        #   2. Look up "<UNDERLYING>-FUT" in the registry (populated from the
        #      Dhan master CSV by DhanDiscovery._parse_master_csv).
        #   3. Fall back to CORE_SYMBOLS (index futures: NIFTY, BANKNIFTY …).
        #   4. Fall back to STOCK_FUTURES_LOT_SIZES (static table, updated
        #      quarterly after NSE F&O lot-size revision circulars).
        #
        # For cash equities (symbol has no "FUT"): registry returns the EQ
        # instrument with lot_size=1, which is correct.
        lot_size = 1
        instrument_type_str = "EQ"
        sym_upper = order.symbol.upper()
        try:
            from services.instrument_registry import instrument_registry
            from models.instrument import Exchange as _Exchange

            if "FUT" in sym_upper:
                from services.core_symbols import (
                    CORE_SYMBOLS,
                    STOCK_FUTURES_LOT_SIZES,
                    resolve_fut_underlying,
                )
                underlying = resolve_fut_underlying(sym_upper)  # "HINDUNILVR29OCTFUT" → "HINDUNILVR"
                registry_key = underlying + "-FUT"              # canonical key used by DhanDiscovery

                inst = await instrument_registry.get(_Exchange.NSE, registry_key)
                if inst is not None:
                    lot_size = inst.lot_size
                    instrument_type_str = inst.instrument_type.value
                elif underlying in CORE_SYMBOLS:
                    lot_size = CORE_SYMBOLS[underlying]["lot_size"]
                    instrument_type_str = CORE_SYMBOLS[underlying].get("instrument_type", "IDX_FUT")
                elif underlying in STOCK_FUTURES_LOT_SIZES:
                    lot_size = STOCK_FUTURES_LOT_SIZES[underlying]
                    instrument_type_str = "FUT"
            elif re.search(r"\d(?:CE|PE)$", sym_upper):
                # Options share lot_size with their underlying's futures contract.
                # The registry stores lot_size under the "UNDERLYING-FUT" key for both
                # FUT and OPT instruments (DhanDiscovery._parse_master_csv populates both
                # from the same LOT_SIZE column). Prefer the explicit "underlying" field
                # from order metadata (set by the option-chain UI) over regex parsing.
                from services.core_symbols import (
                    CORE_SYMBOLS,
                    STOCK_FUTURES_LOT_SIZES,
                    resolve_opt_underlying,
                )
                meta_underlying = (order.metadata or {}).get("underlying", "")
                underlying = (meta_underlying or resolve_opt_underlying(sym_upper)).upper()
                registry_key = underlying + "-FUT"

                inst = await instrument_registry.get(_Exchange.NSE, registry_key)
                if inst is not None:
                    lot_size = inst.lot_size
                    instrument_type_str = (
                        "IDX_OPT" if inst.instrument_type.value in ("IDX_FUT", "IDX_OPT") else "OPT"
                    )
                elif underlying in CORE_SYMBOLS:
                    lot_size = CORE_SYMBOLS[underlying]["lot_size"]
                    instrument_type_str = "IDX_OPT"
                elif underlying in STOCK_FUTURES_LOT_SIZES:
                    lot_size = STOCK_FUTURES_LOT_SIZES[underlying]
                    instrument_type_str = "OPT"
            else:
                inst = await instrument_registry.get(_Exchange.NSE, sym_upper)
                if inst is not None:
                    lot_size = inst.lot_size
                    instrument_type_str = inst.instrument_type.value
        except Exception:
            pass

        qty = float(order.quantity)
        signed = qty if order.side == OrderSide.BUY else -qty
        logger.critical(
            "[LOT SIZE DEBUG] Symbol: %s | Requested Lots: %s | Resolved Lot Size: %s"
            " | FINAL QTY (units): %s | cash impact: %.2f | instrument_type=%s",
            order.symbol, order.quantity, lot_size, qty * lot_size,
            qty * lot_size * fill_price, instrument_type_str,
        )
        from execution.contract_store import IDENTITY_KEYS
        contract = {k: v for k, v in (order.metadata or {}).items()
                    if k in IDENTITY_KEYS and v not in (None, "")}
        sym_upper = order.symbol.strip().upper()
        pre_fill_qty = self.positions[sym_upper].quantity if sym_upper in self.positions else 0.0
        realized_delta = self._apply_fill(
            order.symbol, signed, fill_price, lot_size=lot_size,
            instrument_type=instrument_type_str, contract=contract,
        )
        post_pos = self.positions.get(sym_upper)
        post_qty = post_pos.quantity if post_pos is not None else 0.0
        position_closed = (pre_fill_qty != 0.0) and (post_qty == 0.0)

        broker_order_id = f"PAPER_{order.client_order_id}"
        self.fills.append({
            "broker_order_id": broker_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": qty,
            "price": fill_price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._save_ledger()

        # This log line is the audit trail proving the live order API was bypassed.
        logger.info(
            "[PAPER] SIMULATED FILL (no broker API call): %s %s x%s @ %.2f | "
            "virtual cash=%.2f pos=%s",
            order.side.value, order.symbol, qty, fill_price,
            self.cash, self.positions.get(order.symbol.upper(), PaperPosition(order.symbol)).quantity,
        )

        return OrderUpdate(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.FILLED,
            filled_qty=qty,
            remaining_qty=0.0,
            avg_fill_price=fill_price,
            last_fill_qty=qty,
            last_fill_price=fill_price,
            timestamp=datetime.now(timezone.utc),
            # lot_size / realized_pnl let the risk managers book this fill in units
            # (lots × lot_size) with the same P&L the ledger computed.
            metadata={"simulated": True, "broker": PAPER_BROKER_NAME,
                      "lot_size": lot_size, "realized_pnl": realized_delta,
                      "position_closed": position_closed},
        )

    def _apply_fill(
        self, symbol: str, signed_qty: float, price: float,
        lot_size: int = 1, instrument_type: str = "EQ",
        contract: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Books a fill; returns the P&L it realised (0.0 for an opening fill)."""
        sym = symbol.upper()
        realized_delta = 0.0
        if sym not in self.positions:
            self.positions[sym] = PaperPosition(
                symbol=sym, lot_size=lot_size, instrument_type=instrument_type
            )
        pos = self.positions[sym]
        if contract:
            # Keep identity already known; fill in whatever this fill adds.
            pos.contract = {**contract, **pos.contract} if pos.quantity != 0 else dict(contract)
        # signed_qty is in lots; cash moves by the full contract value (lots × lot_size).
        self.cash -= signed_qty * pos.lot_size * price

        if pos.quantity == 0 or (pos.quantity > 0) == (signed_qty > 0):
            # Opening or adding: weighted-average the entry.
            total = pos.quantity + signed_qty
            if total != 0:
                pos.avg_price = ((pos.avg_price * pos.quantity) + (price * signed_qty)) / total
            pos.quantity = total
        else:
            # Reducing or flipping: realise P&L on the closed portion.
            closing = min(abs(signed_qty), abs(pos.quantity))
            direction = 1 if pos.quantity > 0 else -1
            realized_delta = (price - pos.avg_price) * closing * direction * pos.lot_size
            pos.realized_pnl += realized_delta
            pos.quantity += signed_qty
            if pos.quantity == 0:
                pos.avg_price = 0.0
            elif (pos.quantity > 0) != (direction > 0):
                pos.avg_price = price   # flipped side; the remainder opens at this price

        if pos.quantity == 0 and pos.realized_pnl == 0.0:
            self.positions.pop(sym, None)
        return realized_delta

    # ----- reporting -----------------------------------------------------

    def get_positions_dict(self) -> List[Dict[str, Any]]:
        # Only surface positions that are still open. Flat positions (quantity=0)
        # are kept in self.positions to preserve realized P&L in the account
        # summary, but they must not appear in the Open Positions panel.
        return [
            p.to_dict(self._ltp_provider(p.symbol))
            for p in self.positions.values()
            if p.quantity != 0
        ]

    def get_account_summary(self) -> Dict[str, Any]:
        unrealized = 0.0
        for p in self.positions.values():
            ltp = self._ltp_provider(p.symbol)
            if ltp is not None:
                unrealized += p.unrealized_pnl(ltp)
        realized = sum(p.realized_pnl for p in self.positions.values())
        return {
            "broker": PAPER_BROKER_NAME,
            "starting_cash": round(self.starting_cash, 2),
            "cash": round(self.cash, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "open_positions": len([p for p in self.positions.values() if p.quantity != 0]),
            "fill_count": len(self.fills),
        }

    def reset(self) -> None:
        self.cash = self.starting_cash
        self.positions.clear()
        self.fills.clear()
        logger.info("[PAPER] Virtual ledger reset to %.2f", self.starting_cash)
        self._save_ledger()
