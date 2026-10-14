"""
Strike Desk V4 - Trading Routes & Blueprint O8 Asset Class Mode Selector
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
import logging
import os
import pytz
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from ..dependencies import get_broker_adapters
from .auth import require_operator

logger = logging.getLogger(__name__)
router = APIRouter()
IST = pytz.timezone("Asia/Kolkata")

ALLOWED_MODES = [
    "AUTO_OPTIONS",         # Index Options (NIFTY, BANKNIFTY, SENSEX)
    "AUTO_FUTURES",         # Index Futures
    "AUTO_STOCK_FUTURES",   # Stock Futures (individual equities)
    "AUTO_STOCK_OPTIONS",   # Stock Options (individual equities)
    "AUTO_CASH_INTRADAY",   # Stock Cash — MIS intraday
    "AUTO_CASH_SWING",      # Stock Cash — 2-10 day swing
    "AUTO_CASH_DELIVERY",   # Stock Cash — CNC delivery
]

_DEFAULT_MODE = "AUTO_OPTIONS"


def _mode_lock_path() -> str:
    # Resolved per call so tests can redirect it (tests/conftest.py) and never
    # overwrite the live lock.
    return os.environ.get("STRIKE_DESK_MODE_FILE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "execution_class_lock.json",
    )


def _paper_broker_db_path() -> str:
    return os.environ.get("STRIKE_DESK_PAPER_LEDGER_FILE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "paper_ledger.json",
    )


def _orders_db_path() -> str:
    return os.environ.get("STRIKE_DESK_ORDERS_FILE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "orders_db.json",
    )


def _save_orders_db() -> None:
    path = _orders_db_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(_orders_db.values()), f, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.critical("[Orders] Failed to persist orders_db to %s: %s", path, exc)


def _load_orders_db() -> None:
    path = _orders_db_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        if not isinstance(records, list):
            return
        for rec in records:
            oid = rec.get("order_id")
            if oid:
                _orders_db[oid] = rec
        logger.info("[Orders] Restored %d order records from %s", len(_orders_db), path)
    except Exception as exc:
        logger.warning("[Orders] Cannot load orders_db from %s: %s", path, exc)


class ModeController:
    """Operator's execution class lock. Persisted so a restart keeps it.
    PAPER/LIVE is deliberately NOT persisted — it always restarts as PAPER."""

    def __init__(self):
        self.allowed_modes = ALLOWED_MODES
        self.active_mode = self._load()

    def _load(self) -> str:
        path = _mode_lock_path()
        try:
            with open(path) as f:
                mode = json.load(f).get("active_mode")
        except FileNotFoundError:
            return _DEFAULT_MODE
        except Exception as exc:
            logger.critical("[ModeController] Unreadable lock file %s (%s) — using %s",
                            path, exc, _DEFAULT_MODE)
            return _DEFAULT_MODE
        if mode not in ALLOWED_MODES:
            logger.critical("[ModeController] Invalid persisted mode %r — using %s", mode, _DEFAULT_MODE)
            return _DEFAULT_MODE
        logger.info("[ModeController] Restored execution class lock: %s (%s)", mode, path)
        return mode

    def _save(self) -> None:
        path = _mode_lock_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"active_mode": self.active_mode,
                           "updated_at": datetime.now(IST).isoformat()}, f)
            os.replace(tmp, path)
        except Exception as exc:
            logger.critical("[ModeController] Failed to persist mode %s to %s: %s — "
                            "it will reset on restart", self.active_mode, path, exc)

    def get_mode(self) -> Dict[str, Any]:
        return {"active_mode": self.active_mode}

    def set_mode(self, mode: str) -> Dict[str, Any]:
        if mode not in self.allowed_modes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid mode '{mode}'. Must be one of {self.allowed_modes}"
            )
        self.active_mode = mode
        self._save()
        return {"active_mode": self.active_mode}

mode_controller = ModeController()
_mode_controller = mode_controller

def get_mode_controller() -> ModeController:
    return mode_controller


# ── Execution mode: where orders actually go ──────────────────────────────
# Server-global rather than per-session, because the auto-execution loop places
# orders with no session attached and must obey the same switch as the operator.
EXEC_PAPER = "PAPER"
EXEC_LIVE = "LIVE"


class ExecutionModeController:
    """
    PAPER routes fills through the virtual ledger; LIVE sends them to the broker.

    Defaults to PAPER: if the mode is ever unset or reset, the safe failure is
    simulating an order, never sending an unintended real one.
    """

    def __init__(self) -> None:
        self.mode = EXEC_PAPER

    def get(self) -> str:
        return self.mode

    def is_paper(self) -> bool:
        return self.mode == EXEC_PAPER

    def set(self, mode: str) -> str:
        m = (mode or "").strip().upper()
        if m == EXEC_LIVE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "LIVE execution mode is permanently disabled in this build. "
                    "Live broker order dispatch is not supported. "
                    "Only PAPER mode is available."
                ),
            )
        if m != EXEC_PAPER:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid execution mode '{mode}'. Only {EXEC_PAPER} is supported.",
            )
        previous, self.mode = self.mode, m
        if previous != m:
            logger.warning("[ExecMode] %s → %s", previous, m)
        return self.mode


execution_mode = ExecutionModeController()


def get_execution_mode() -> ExecutionModeController:
    return execution_mode


def get_paper_broker():
    """The process-wide virtual ledger, priced off the live tick store."""
    global _paper_broker
    if _paper_broker is None:
        from adapters.paper_broker import PaperBrokerAdapter
        from pipeline import pipeline_orchestrator
        from utils.option_contract import mark_price
        _paper_broker = PaperBrokerAdapter(
            # Options aren't on the WebSocket feed; mark_price falls back to the
            # option chain so held contracts show live P&L instead of entry price.
            ltp_provider=mark_price,
            tick_provider=pipeline_orchestrator.get_latest_tick,
            db_path=_paper_broker_db_path(),
        )
    return _paper_broker


_paper_broker = None

_FNO_CATEGORIES = ("AUTO_OPTIONS", "AUTO_FUTURES", "AUTO_STOCK_OPTIONS", "AUTO_STOCK_FUTURES")


def record_fill_in_risk(order, update, order_id: str, fill_price: float, category: str) -> None:
    """
    Book a placed order into BOTH risk stores — risk_manager (Risk Limits panel,
    auto-exec halt/trade-cap checks) and circuit_breaker (order_authorizer gate).
    They are independent on purpose; see the trading-gates notes.

    Quantities are UNITS (lots × the lot size the adapter resolved), so P&L in
    the risk stores matches the ledger/broker. Called by the manual order route
    and the auto-execute loop alike. Never raises: the order is already placed.
    """
    try:
        from risk.risk_manager import risk_manager as _rm
        from risk.circuit_breaker import circuit_breaker as _cb
        from models.risk_state import Trade as _RiskTrade
        from models.order import OrderSide as _Side
        from risk.charge_calculator import ChargeCalculator as _CC

        meta = getattr(update, "metadata", None) or {}
        lot_size = int(meta.get("lot_size") or 1)
        units = int(round(float(order.quantity) * lot_size))
        signed_units = units if order.side == _Side.BUY else -units
        realized = _rm.apply_fill(order_id, order.symbol, signed_units, fill_price)
        # The ledger's own figure wins when it has one (paper); it is computed
        # from the same units, so the two only differ by float rounding.
        if meta.get("realized_pnl") is not None:
            realized = float(meta["realized_pnl"])
        segment = "FNO" if category in _FNO_CATEGORIES else "EQUITY"
        trade_type = "FNO" if segment == "FNO" else "INTRADAY"
        # Charges are computed for exit fills (realized != 0) and for confirmed
        # full-position closures even when realized == 0 (breakeven exits).
        # Entry fills carry realized=0, charges=0, net_pnl=0 — the neutral sentinel
        # used by RiskStateManager.record_trade() to skip consecutive-loss tracking.
        charges = 0.0
        if realized != 0.0 or meta.get("position_closed"):
            turnover = float(units) * fill_price
            charges = _CC.calculate_total_charges(
                turnover=turnover,
                trade_type=trade_type,
                segment=segment,
                is_sell=(order.side == _Side.SELL),
            )
        _cb.on_trade_completed(_RiskTrade(
            trade_id=order_id,
            symbol=order.symbol,
            side=order.side.value,
            quantity=float(units),
            entry_price=fill_price,
            realized_pnl=realized,
            charges=charges,
            # net_pnl: Trade.__post_init__ auto-computes realized - charges when
            # net_pnl is left at its 0.0 default, giving true economic loss.
            trade_type=trade_type,
            segment=segment,
        ))
        summary = _rm.get_risk_summary()
        logger.critical(
            "[RISK LINK] %s %s %s lots x %s = %s units @ %.2f | realized this fill: %.2f | "
            "risk: trades=%s open=%s realized=%.2f total=%.2f",
            order.side.value, order.symbol, order.quantity, lot_size, units, fill_price, realized,
            summary["trade_count"], summary["open_positions_count"],
            summary["realized_pnl"], summary["total_risk_pnl"],
        )
    except Exception as exc:
        logger.critical("[RISK LINK] FAILED to record %s %s in risk stores: %s",
                        getattr(order, "symbol", "?"), order_id, exc, exc_info=True)


def require_active_broker(order_broker: str, active_broker: str) -> None:
    """
    Raise 403 if the order targets a broker other than the operator's selected
    active broker.

    A broker being connected or authenticated confers no execution rights unless
    the operator has designated it as the active broker. This is an authorization
    boundary (403), not a validation error (400): the broker name may be perfectly
    valid — it is simply not the currently permitted one.
    """
    if (order_broker or "").lower() != (active_broker or "").lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Execution blocked: order targets '{order_broker}' but the active execution "
                f"broker is '{active_broker}'. A broker's authentication state does not grant "
                f"it execution rights unless the operator has selected it as the active broker. "
                f"Change the active broker in the broker settings panel first."
            ),
        )


def _select_active_adapter(adapters: Dict[str, Any], active_broker: str) -> Any:
    """
    Return the adapter for the active broker from the DI-injected adapter map.

    Does NOT accept an arbitrary broker name — callers cannot select which adapter
    they get. This means a latent bug that mutates `order.broker` or passes a
    different key cannot reach a non-active broker's adapter through this path.
    Raises 503 rather than returning None, so a missing adapter surfaces
    immediately instead of producing a phantom fill.
    """
    adapter = (adapters or {}).get(active_broker) or (adapters or {}).get(active_broker.lower())
    if adapter is None or not hasattr(adapter, "place_order"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Active broker '{active_broker}' has no connected adapter — order was not placed. "
                f"Available adapters: {', '.join(sorted(adapters or {})) or 'none'}."
            ),
        )
    return adapter


# ── Live feed gate ────────────────────────────────────────────────────────────

_LIVE_FEED_MAX_AGE_SECONDS = 60.0


def _feed_check_symbol(symbol: str, metadata: Optional[dict]) -> str:
    """
    Option contracts are not directly subscribed to the WebSocket feed — only
    their underlyings are. Return the symbol to look up in the tick store:
    the underlying for CE/PE contracts, the symbol itself for everything else.
    """
    import re
    sym = (symbol or "").strip().upper()
    if sym.endswith("CE") or sym.endswith("PE"):
        underlying = ((metadata or {}).get("underlying") or "").strip().upper()
        if underlying:
            return underlying
        m = re.match(r'^([A-Z]+)', sym)
        return m.group(1) if m else sym
    return sym


def require_live_feed(
    symbol: str,
    active_broker: str,
    metadata: Optional[dict] = None,
    max_age_seconds: float = _LIVE_FEED_MAX_AGE_SECONDS,
) -> None:
    """
    Raise 400 if the active broker's live feed has no recent tick for `symbol`.

    Option contracts are checked via their underlying (options are not directly
    subscribed to the WebSocket; their price derives from the underlying + option
    chain cache). All other instruments are checked directly.

    Two failure modes:
    • NEVER_SEEN  — symbol has never appeared in the tick store (not subscribed /
                    unsupported / feed not connected yet).
    • FEED_STALE  — last tick is older than max_age_seconds (feed dropped mid-session).

    The pipeline import is deferred so this function stays importable in test
    environments where the full pipeline has not been initialised. If the pipeline
    module itself raises an import error, orders are allowed through (fail-open on
    infrastructure failure, not on missing market data).
    """
    from datetime import datetime, timezone as _tz

    try:
        from pipeline import pipeline_orchestrator as _po
    except Exception:
        return

    check_sym = _feed_check_symbol(symbol, metadata)
    tick = _po.get_latest_tick(check_sym)

    label = symbol if check_sym == symbol else f"{symbol} (underlying: {check_sym})"

    if tick is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Order rejected: no live tick received for '{label}' from broker "
                f"'{active_broker}'. The symbol may not be in the active broker's "
                f"WebSocket subscription, or the feed has not yet started streaming "
                f"it. Verify the symbol is in the broker's live universe before trading."
            ),
        )

    now = datetime.now(_tz.utc)
    tick_ts = tick.timestamp if tick.timestamp.tzinfo else tick.timestamp.replace(tzinfo=_tz.utc)
    age_secs = (now - tick_ts).total_seconds()

    if age_secs > max_age_seconds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Order rejected: live feed for '{label}' from broker '{active_broker}' "
                f"has gone stale — last tick was {age_secs:.1f}s ago "
                f"(limit: {max_age_seconds:.0f}s). The WebSocket feed appears to have "
                f"dropped this symbol. Trading is suspended until live data is restored."
            ),
        )


class ModeRequest(BaseModel):
    mode: str = Field(..., description="Target active mode from Blueprint O8 categories")

@router.get("/mode")
async def get_trading_mode(ctrl: ModeController = Depends(get_mode_controller)):
    return ctrl.get_mode()

@router.post("/mode")
async def set_trading_mode(
    req: ModeRequest,
    ctrl: ModeController = Depends(get_mode_controller),
    _op: Dict[str, Any] = Depends(require_operator),
):
    return ctrl.set_mode(req.mode)

# Mode → AssetClass name map (for StrategyOrchestrator)
_MODE_TO_ASSET_CLASS = {
    "AUTO_OPTIONS":       "INDEX_OPTIONS",
    "AUTO_FUTURES":       "INDEX_FUTURES",
    "AUTO_STOCK_FUTURES": "STOCK_FUTURES",
    "AUTO_STOCK_OPTIONS": "STOCK_OPTIONS",
    "AUTO_CASH_INTRADAY": "STOCK_CASH",
    "AUTO_CASH_SWING":    "STOCK_CASH",
    "AUTO_CASH_DELIVERY": "STOCK_CASH",
}

# ---------------------------------------------------------------------
# Signals — real signals from StrategyOrchestrator
# ---------------------------------------------------------------------
@router.get("/signals")
async def list_signals(ctrl: ModeController = Depends(get_mode_controller)):
    try:
        from strategies.strategy_orchestrator import strategy_orchestrator as _so
        signals = _so.get_all_actionable_signals()
        curr_mode = ctrl.active_mode
        result = []
        for sig in signals:
            d = sig.to_dict() if hasattr(sig, "to_dict") else {}
            ac = d.get("asset_class", "")
            cat_map = {
                "INDEX_OPTIONS":  "AUTO_OPTIONS",
                "INDEX_FUTURES":  "AUTO_FUTURES",
                "STOCK_FUTURES":  "AUTO_STOCK_FUTURES",
                "STOCK_OPTIONS":  "AUTO_STOCK_OPTIONS",
                "STOCK_CASH":     "AUTO_CASH_INTRADAY",
            }
            category = cat_map.get(ac, ac)
            is_exec = category == curr_mode
            d["category"] = category
            d["instrument_type"] = category
            d["status"] = "APPROVED_FOR_EXECUTION" if is_exec else "ALERT_ONLY"
            d["timestamp"] = d.get("timestamp") or datetime.now(IST).isoformat()
            result.append(d)
        return result
    except Exception:
        return []

@router.get("/watchlist")
async def get_daily_watchlist():
    """
    Returns the top-10 stock options candidates for today (CE or PE),
    ranked by composite score (sentiment conviction + ADX trend + indicator alignment).
    Refreshes from live indicators; call /watchlist/refresh to force a rescan.
    """
    try:
        from pipeline.pipeline_orchestrator import pipeline_orchestrator as _po
        entries = _po.get_watchlist()
        if not entries:
            # Force first scan if watchlist is empty
            entries = _po.refresh_watchlist()
        return {
            "count": len(entries),
            "entries": [e.to_dict() for e in entries],
        }
    except Exception as exc:
        logger.warning("[Watchlist] get failed: %s", exc)
        return {"count": 0, "entries": []}


@router.post("/watchlist/refresh")
async def refresh_daily_watchlist():
    """Force an immediate rescan of all symbols to rebuild the top-10 watchlist."""
    try:
        from pipeline.pipeline_orchestrator import pipeline_orchestrator as _po
        entries = _po.refresh_watchlist()
        return {
            "count": len(entries),
            "entries": [e.to_dict() for e in entries],
        }
    except Exception as exc:
        logger.warning("[Watchlist] refresh failed: %s", exc)
        return {"count": 0, "entries": [], "error": str(exc)}


@router.get("/queue")
async def get_signal_queue():
    """
    Returns signals currently waiting to execute once the open position closes.
    Sorted highest-confidence first. Empty when no position is open (queue drains immediately).
    """
    try:
        from api.main import _signal_queue
        result = []
        for sig in _signal_queue:
            d = sig.to_dict() if hasattr(sig, "to_dict") else {}
            d["confidence_score"] = float(getattr(sig, "confidence_score", 0.0))
            d["confidence_tier"] = str(getattr(sig, "confidence_tier", ""))
            result.append(d)
        return {
            "queue_depth": len(result),
            "signals": result,
        }
    except Exception as exc:
        logger.warning("[Queue] get failed: %s", exc)
        return {"queue_depth": 0, "signals": []}


class OrderRequest(BaseModel):
    symbol: str
    side: str = "BUY"
    quantity: int = 1
    price: Optional[float] = None
    # Manual = the operator clicked BUY/SELL on the dashboard; they may trade
    # any asset class. Auto = the bot acting on a signal, which is restricted
    # to the active execution mode. Risk checks apply to both.
    is_manual: bool = True
    category: Optional[str] = None
    asset_class: Optional[str] = None
    instrument_type: Optional[str] = None
    mode: Optional[str] = None
    # Broker-specific instrument ids, when the caller already knows them — option
    # chain contracts carry Dhan's security_id, which cannot be derived from the
    # contract name. Namespaced per broker so one can never be read as another's.
    dhan_security_id: Optional[str] = None
    dhan_segment: Optional[str] = None
    angel_symboltoken: Optional[str] = None
    angel_tradingsymbol: Optional[str] = None
    angel_exchange: Optional[str] = None
    # Structural contract identity. Brokers spell a contract name differently
    # (NIFTY26SEP23100CE vs NIFTY29SEP2623100CE), so these four fields — not the
    # name — are what lets any broker find the same contract in its own master.
    underlying: Optional[str] = None
    selected_expiry: Optional[str] = None
    selected_strike: Optional[float] = None
    option_type: Optional[str] = None

def resolve_order_category(order_req: OrderRequest) -> str:
    cat = order_req.category or order_req.asset_class or order_req.mode or order_req.instrument_type
    if cat:
        cat = cat.upper()
        if cat in ["INDEX_OPTIONS", "AUTO_OPTIONS"]:
            return "AUTO_OPTIONS"
        if cat in ["INDEX_FUTURES", "AUTO_FUTURES"]:
            return "AUTO_FUTURES"
        if cat in ["STOCK_FUTURES", "AUTO_STOCK_FUTURES"]:
            return "AUTO_STOCK_FUTURES"
        if cat in ["STOCK_OPTIONS", "AUTO_STOCK_OPTIONS"]:
            return "AUTO_STOCK_OPTIONS"
        if cat in ["OPTIONS"]:                          # ambiguous — default to index
            return "AUTO_OPTIONS"
        if cat in ["FUTURES"]:                          # ambiguous — default to index
            return "AUTO_FUTURES"
        if cat in ["CASH_INTRADAY", "STOCK_CASH", "AUTO_CASH_INTRADAY"]:
            return "AUTO_CASH_INTRADAY"
        if cat in ["CASH_SWING", "AUTO_CASH_SWING"]:
            return "AUTO_CASH_SWING"
        if cat in ["CASH_DELIVERY", "CNC", "DELIVERY", "AUTO_CASH_DELIVERY"]:
            return "AUTO_CASH_DELIVERY"
        return cat

    sym = (order_req.symbol or "").upper()
    if sym.endswith("CE") or sym.endswith("PE"):
        return "AUTO_OPTIONS"
    if sym.endswith("FUT") or "FUT" in sym:
        return "AUTO_FUTURES"
    return "AUTO_CASH_INTRADAY"

_orders_db: Dict[str, Dict[str, Any]] = {}
_positions_db: Dict[str, Dict[str, Any]] = {}
_load_orders_db()


def _restore_contract_identity(order, exec_mode: "ExecutionModeController") -> None:
    """
    An exit usually arrives with only the symbol (the Exit button). Our names omit
    the expiry day, so a broker cannot resolve them; fill in the contract the
    position was opened with instead. Orders that already carry any identity are
    left untouched, so a signal's own contract is never overridden.
    """
    from execution.contract_store import IDENTITY_KEYS, contract_store

    meta = order.metadata if order.metadata is not None else {}
    if any(meta.get(k) not in (None, "") for k in IDENTITY_KEYS):
        return
    source, entry = None, None
    if exec_mode.is_paper():
        held = get_paper_broker().positions.get(order.symbol.upper())
        if held is not None and held.quantity != 0 and held.contract:
            source, entry = "paper position", held.contract
    if entry is None:
        entry = contract_store.lookup(order.symbol)
        source = "contract store" if entry else None
    if not entry:
        return
    order.metadata = meta
    for k in IDENTITY_KEYS:
        if entry.get(k) not in (None, ""):
            meta[k] = entry[k]
    logger.info("[Orders] %s: restored contract details from %s: %s", order.symbol, source,
                {k: meta[k] for k in IDENTITY_KEYS if k in meta})

@router.post("/orders")
async def place_order(
    order_req: OrderRequest,
    ctrl: ModeController = Depends(get_mode_controller),
    adapters: Dict[str, Any] = Depends(get_broker_adapters),
    exec_mode: ExecutionModeController = Depends(get_execution_mode),
    _op: Dict[str, Any] = Depends(require_operator),
):
    from brokers.token_manager import BrokerTokenManager
    from models.order import Order, OrderSide, OrderType
    import uuid

    order_cat = resolve_order_category(order_req)

    # Execution class gate — applies to bot-initiated orders only. A manual
    # order is the operator's own decision, so any asset class is permitted;
    # the risk checks below still apply to it exactly as they do to auto.
    if not order_req.is_manual and order_cat != ctrl.active_mode:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Auto-execution is restricted to active mode '{ctrl.active_mode}'; "
                   f"order category '{order_cat}' is alert-only. "
                   f"Place it manually (is_manual=true) to trade this class."
        )

    # Kill-switch gate. risk_manager keeps its own halt_manager and does NOT
    # share the circuit_breaker singleton that order_authorizer reads, so this
    # check is not redundant — dropping it would let orders through while the
    # operator kill-switch is engaged.
    try:
        from risk.risk_manager import risk_manager as _rm
        if _rm.get_risk_summary().get("emergency_halt_active"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Emergency halt active: All order placement frozen by kill-switch.",
            )
    except HTTPException:
        raise
    except Exception:
        pass

    # Position reconciliation gate: if the internal ledger disagrees with the broker's
    # position data, all new order entry is blocked until the operator investigates and
    # clears the mismatch. Fails-open on import error so an infrastructure failure
    # never accidentally becomes a trading lockout.
    try:
        from services.position_reconciler import position_reconciler as _pr
        if _pr.is_mismatch_active():
            last = _pr.get_last_result()
            diff_summary = ""
            if last and last.diffs:
                diff_summary = "; ".join(
                    f"{d.symbol}: internal={d.internal_qty} broker={d.broker_qty}"
                    for d in last.diffs
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Order blocked: position reconciliation mismatch is active. "
                    "Investigate the discrepancy and clear via POST /api/risk/reconcile/clear. "
                    f"Diffs: {diff_summary}"
                ),
            )
    except HTTPException:
        raise
    except Exception:
        pass

    mgr = BrokerTokenManager()
    active_broker = mgr.get_active_broker() if hasattr(mgr, "get_active_broker") else "angelone"

    # Architecture rule: in LIVE mode, F&O orders always route to Dhan.
    # AngelOne and Fyers remain data feeds only; only Dhan's F&O contract
    # master is populated, so both instrument resolution and live execution
    # must go through Dhan. Paper mode is broker-agnostic — no override.
    _is_fno = order_cat in (
        "AUTO_OPTIONS", "AUTO_FUTURES", "AUTO_STOCK_OPTIONS", "AUTO_STOCK_FUTURES"
    )
    if _is_fno and not exec_mode.is_paper():
        active_broker = "dhan"

    order = Order(
        client_order_id=str(uuid.uuid4()),
        symbol=order_req.symbol.upper(),
        exchange=getattr(order_req, "exchange", "NSE"),
        side=OrderSide.BUY if order_req.side.upper() == "BUY" else OrderSide.SELL,
        order_type=OrderType.LIMIT if order_req.price else OrderType.MARKET,
        quantity=float(order_req.quantity),
        price=float(order_req.price or 0.0),
        broker=active_broker,
        # Only ids for the broker actually being routed to are carried forward;
        # each adapter reads its own namespaced keys and ignores the others.
        metadata={
            k: v for k, v in {
                "dhan_security_id": order_req.dhan_security_id,
                "dhan_segment": order_req.dhan_segment,
                "angel_symboltoken": order_req.angel_symboltoken,
                "angel_tradingsymbol": order_req.angel_tradingsymbol,
                "angel_exchange": order_req.angel_exchange,
                "underlying": order_req.underlying,
                "selected_expiry": order_req.selected_expiry,
                "selected_strike": order_req.selected_strike,
                "option_type": order_req.option_type,
            }.items() if v
        },
    )

    # Hard-block: the order's broker field must agree with the active broker.
    # order.broker was set from active_broker above, so this is normally a no-op —
    # but it makes the isolation boundary explicit and catches any code path that
    # corrupts order.broker before this point. Raises 403, not 400.
    require_active_broker(order.broker, active_broker)

    _restore_contract_identity(order, exec_mode)

    # Live feed gate: the active broker's WebSocket must be streaming the symbol
    # (or its underlying, for option contracts). Rejects orders against symbols
    # the feed has never seen or has dropped mid-session, before any resolution
    # or risk check. Evaluated at order-placement time so a symbol dropped by a
    # WebSocket reconnect is blocked from that moment forward.
    require_live_feed(order.symbol, active_broker, metadata=dict(order.metadata))

    # Broker-specific instrument resolution, in both modes. Each adapter reads only
    # its own token map, so the same canonical symbol resolves to that broker's ids
    # and can never carry another broker's. Resolution is a local map lookup and
    # contacts nothing, so paper rehearses live routing exactly: a symbol the
    # selected broker cannot address is refused here rather than at real-money time.
    routing_adapter = (adapters or {}).get(active_broker) or (adapters or {}).get(active_broker.lower())
    if routing_adapter is not None and hasattr(routing_adapter, "resolve_order_instrument"):
        try:
            resolved = routing_adapter.resolve_order_instrument(order)
            order.metadata["resolved_instrument"] = resolved
            order.metadata["resolved_for_broker"] = active_broker
            logger.info("[Orders] %s resolved %s → %s", active_broker, order.symbol, resolved)
        except Exception as exc:
            logger.error("[Orders] %s cannot address '%s': %s", active_broker, order_req.symbol, exc)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Broker '{active_broker}' has no instrument mapping for "
                       f"'{order_req.symbol}' — order refused. ({exc})",
            )

    # Full risk gate for BOTH manual and auto: quantity sanity, per-order value
    # cap, circuit breaker (emergency halt / loss ceiling / trade cap / cooldown)
    # and the 15:10 IST entry cutoff. Raised outside the try below so a
    # rejection is never swallowed into a locally-invented fill.
    from risk.order_authorizer import order_authorizer
    # Exit detection is server-side, from the held position — never a client flag.
    # Only an order on the opposite side that does not exceed the open quantity
    # counts; anything larger would open a new position and stays fully gated.
    # PAPER only for now: LIVE positions are in broker units and the order is in
    # lots, so a LIVE exit is still gated as an entry.
    is_exit = False
    if exec_mode.is_paper():
        _held = get_paper_broker().positions.get(order.symbol.upper())
        if _held is not None and _held.quantity != 0:
            _opposite = (_held.quantity > 0) != (order.side == OrderSide.BUY)
            is_exit = _opposite and float(order.quantity) <= abs(_held.quantity)
    auth = order_authorizer.authorize_order(order, is_exit=is_exit)
    if is_exit:
        logger.critical("[Orders] EXIT detected for %s %s x%s (held %s) -> %s",
                        order.side.value, order.symbol, order.quantity,
                        _held.quantity, "AUTHORIZED" if auth.is_authorized else auth.reason)
    if not auth.is_authorized:
        reason = f"Order rejected by risk gate [{auth.rule_violated}]: {auth.reason}"
        logger.warning("[Orders] %s %s x%s — %s",
                       order_req.side, order_req.symbol, order_req.quantity, reason)
        # Record the attempt so the operator can see *why* it never reached the broker,
        # rather than the order silently vanishing from the orders panel.
        oid = f"ORD-{len(_orders_db) + 1:04d}"
        _orders_db[oid] = {
            "order_id": oid,
            "symbol": order_req.symbol,
            "side": order_req.side,
            "quantity": order_req.quantity,
            "price": order_req.price or 0.0,
            "status": "REJECTED",
            "broker": active_broker,
            "category": order_cat,
            "is_manual": order_req.is_manual,
            "reject_reason": f"{auth.rule_violated}: {auth.reason}",
            "placed_at": datetime.now(IST).isoformat(),
            "filled_at": None,
        }
        _save_orders_db()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)

    # Execution routing. In PAPER the order goes to the virtual ledger and no
    # broker adapter is touched at all; the market data feeding the fill price is
    # the same live stream LIVE mode trades on.
    if exec_mode.is_paper():
        adapter = get_paper_broker()
        if is_exit and not order.price:
            # Option contracts never tick on the WebSocket, so a priceless exit could
            # not fill. Price it the way the square-off endpoint does.
            from utils.option_contract import mark_price
            _exit_px = mark_price(order.symbol)
            if _exit_px:
                order.price = float(_exit_px)
                order.order_type = OrderType.LIMIT
                logger.info("[Orders] PAPER EXIT %s priced at mark %.2f (no price on the request)",
                            order.symbol, order.price)
        logger.info(
            "[Orders] PAPER MODE — %s %s x%s routed to the virtual ledger "
            "(validated against %s's instrument map); no broker order API will be called.",
            order_req.side, order_req.symbol, order_req.quantity, active_broker,
        )
        active_broker = f"paper:{active_broker}"
    else:
        # Live order dispatch is permanently prohibited. This branch can only be
        # reached if ExecutionModeController.set() is bypassed directly — the
        # adapter-level guard in each broker's place_order/cancel_order/modify_order
        # provides a second layer of protection in that case.
        from utils.live_order_guard import prohibit_live_order
        prohibit_live_order("place_order route: LIVE branch")

    try:
        logger.critical(
            "[PIPELINE TRACE] Junction 4 — Routing Order to %s: symbol=%s side=%s"
            " qty=%s price=%s exec_mode=%s is_manual=%s",
            active_broker, order.symbol, order.side.value,
            order.quantity, order.price,
            exec_mode.get(), order_req.is_manual,
        )
        update = await adapter.place_order(order)
    except Exception as exc:
        # A broker rejection must surface, not be recorded as a local fill.
        logger.error("[Orders] Broker '%s' rejected %s %s x%s: %s",
                     active_broker, order_req.side, order_req.symbol,
                     order_req.quantity, exc, exc_info=True)
        oid = f"ORD-{len(_orders_db) + 1:04d}"
        _orders_db[oid] = {
            "order_id": oid,
            "symbol": order_req.symbol,
            "side": order_req.side,
            "quantity": order_req.quantity,
            "price": order_req.price or 0.0,
            "status": "REJECTED",
            "broker": active_broker,
            "category": order_cat,
            "is_manual": order_req.is_manual,
            "reject_reason": str(exc),
            "placed_at": datetime.now(IST).isoformat(),
            "filled_at": None,
        }
        _save_orders_db()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Broker '{active_broker}' rejected the order: {exc}",
        )

    try:
        from execution.contract_store import contract_store
        contract_store.record(order.symbol, order.metadata)
    except Exception as _cs_exc:
        logger.error("[Orders] Could not record contract details for %s: %s", order.symbol, _cs_exc)

    oid = update.broker_order_id or f"ORD-{len(_orders_db) + 1:04d}"
    is_paper = exec_mode.is_paper()
    fill_price = getattr(update, "avg_fill_price", 0.0) or (order_req.price or 0.0)
    filled = getattr(update, "state", None) and update.state.value == "FILLED"

    if is_paper:
        # Mirror the virtual ledger into the positions view the dashboard reads.
        _positions_db.clear()
        for pos in get_paper_broker().get_positions_dict():
            _positions_db[pos["symbol"]] = pos
    # No LIVE position sync — live order dispatch is permanently prohibited.

    # Book the fill into both risk stores, in units (lots × lot_size).
    record_fill_in_risk(order, update, oid, fill_price, order_cat)

    order_data = {
        "order_id": oid,
        "symbol": order_req.symbol,
        "side": order_req.side,
        "quantity": order_req.quantity,
        "price": fill_price if is_paper else (order_req.price or 0.0),
        "status": update.state.value if hasattr(update, "state") else "PLACED",
        "broker": active_broker,
        "execution_mode": EXEC_PAPER if is_paper else EXEC_LIVE,
        "is_simulated": is_paper,
        "category": order_cat,
        "is_manual": order_req.is_manual,
        "auth_token": auth.auth_token,
        "placed_at": datetime.now(IST).isoformat(),
        "filled_at": datetime.now(IST).isoformat() if (is_paper and filled) else None,
    }
    _orders_db[oid] = order_data
    _save_orders_db()
    logger.info("[Orders] %s %s x%s placed on %s → %s (id=%s)",
                order_req.side, order_req.symbol, order_req.quantity,
                active_broker, order_data["status"], oid)
    return order_data

@router.get("/orders")
async def list_orders():
    return list(_orders_db.values())

@router.get("/orders/{order_id}")
async def get_order_detail(order_id: str):
    if order_id not in _orders_db:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found.")
    return _orders_db[order_id]


@router.post("/orders/{order_id}/force-reject",
             dependencies=[Depends(require_operator)])
async def force_reject_order(order_id: str, reason: str = "operator-forced-reject"):
    """
    Transition a locally-stuck OPEN/SUBMITTED order to REJECTED without contacting
    the broker. Use only for phantom orders (those with a locally-generated ID like
    ANGEL_xxx / DHAN_xxx) that were never actually sent to the exchange.

    NOT safe to call on a real live order — it does not cancel the broker-side order.
    """
    if order_id not in _orders_db:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found.")
    entry = _orders_db[order_id]
    current = entry.get("status", "")
    if current in ("REJECTED", "FILLED", "CANCELLED", "EXPIRED"):
        raise HTTPException(
            status_code=400,
            detail=f"Order '{order_id}' is already in terminal state '{current}'."
        )
    entry["status"] = "REJECTED"
    entry["reject_reason"] = reason
    _save_orders_db()
    logger.warning(
        "[Orders] force-reject applied to %s (was %s): %s",
        order_id, current, reason,
    )
    return entry

@router.get("/positions")
async def list_positions(
    exec_mode: ExecutionModeController = Depends(get_execution_mode),
):
    # In paper mode positions are re-derived from the virtual ledger on read so
    # unrealised P&L tracks the live feed rather than the price at fill time.
    if exec_mode.is_paper():
        return get_paper_broker().get_positions_dict()
    return list(_positions_db.values())


class ExecutionModeRequest(BaseModel):
    mode: str = Field(..., description=f"Only {EXEC_PAPER} is supported. {EXEC_LIVE} is permanently disabled.")


@router.get("/execution-mode")
async def get_exec_mode(exec_mode: ExecutionModeController = Depends(get_execution_mode)):
    """Readable by any session so the dashboard can always show which mode is armed."""
    paper = exec_mode.is_paper()
    return {
        "execution_mode": exec_mode.get(),
        "is_paper": paper,
        "description": "Real live market data, virtual money — no broker order API is called.",
    }


@router.post("/execution-mode")
async def set_exec_mode(
    req: ExecutionModeRequest,
    exec_mode: ExecutionModeController = Depends(get_execution_mode),
    _op: Dict[str, Any] = Depends(require_operator),
):
    # exec_mode.set() rejects EXEC_LIVE unconditionally — see ExecutionModeController.
    return {"execution_mode": exec_mode.set(req.mode), "is_paper": exec_mode.is_paper()}


@router.get("/paper/account")
async def get_paper_account(exec_mode: ExecutionModeController = Depends(get_execution_mode)):
    """Virtual cash, realised and unrealised P&L for the paper ledger."""
    summary = get_paper_broker().get_account_summary()
    summary["execution_mode_active"] = exec_mode.get()
    return summary


@router.post("/paper/reset")
async def reset_paper_account(_op: Dict[str, Any] = Depends(require_operator)):
    get_paper_broker().reset()
    _positions_db.clear()
    return {"status": "RESET", **get_paper_broker().get_account_summary()}

@router.post("/positions/{symbol}/square-off")
async def square_off_position(
    symbol: str,
    exec_mode: ExecutionModeController = Depends(get_execution_mode),
    _op: Dict[str, Any] = Depends(require_operator),
):
    """
    Close a position with a real exit fill so realized P&L is booked in units
    in the ledger and in both risk stores. This used to only flip a flag in
    _positions_db and report SUCCESS — nothing was closed and no P&L booked.
    """
    import uuid
    from models.order import Order, OrderSide, OrderType
    from utils.option_contract import mark_price

    sym = symbol.strip().upper()
    if not exec_mode.is_paper():
        # No broker exit path is implemented. Refuse rather than claim a close
        # while a real position stays open at the broker.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"LIVE square-off is not implemented — close {sym} in the broker terminal.",
        )

    ledger = get_paper_broker()
    pos = ledger.positions.get(sym)
    if pos is None or pos.quantity == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No open paper position in {sym}.")
    exit_price = mark_price(sym)
    if not exit_price:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"No live price for {sym}; cannot price the exit fill.")

    order = Order(
        client_order_id=str(uuid.uuid4()),
        symbol=sym,
        exchange="NSE",
        side=OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=float(abs(pos.quantity)),
        price=float(exit_price),
        broker="paper",
    )
    entry_price, lot_size = pos.avg_price, pos.lot_size   # pos is reset by the fill
    update = await ledger.place_order(order)
    oid = update.broker_order_id
    category = mode_controller.active_mode
    record_fill_in_risk(order, update, oid, float(update.avg_fill_price), category)

    _positions_db.clear()
    for p in ledger.get_positions_dict():
        _positions_db[p["symbol"]] = p
    _orders_db[oid] = {
        "order_id": oid, "symbol": sym, "side": order.side.value,
        "quantity": order.quantity, "price": update.avg_fill_price, "status": "FILLED",
        "broker": "paper", "execution_mode": EXEC_PAPER, "is_simulated": True,
        "category": category, "is_manual": True,
        "placed_at": datetime.now(IST).isoformat(), "filled_at": datetime.now(IST).isoformat(),
    }
    _save_orders_db()
    realized = float((update.metadata or {}).get("realized_pnl") or 0.0)
    logger.critical(
        "[SQUARE-OFF] %s %s lots x %s = %s units: entry %.2f -> exit %.2f | realized %.2f",
        sym, order.quantity, lot_size, order.quantity * lot_size,
        entry_price, update.avg_fill_price, realized,
    )
    return {"status": "SUCCESS", "order_id": oid, "exit_price": update.avg_fill_price,
            "realized_pnl": round(realized, 2),
            "message": f"Position {sym} squared off at {update.avg_fill_price:.2f}."}
