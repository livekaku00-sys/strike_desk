"""
Strike Desk V4 - FastAPI Backend Core Application
"""
# Load .env FIRST — before any other imports that read os.environ
from dotenv import load_dotenv
load_dotenv("/root/strike_desk/.env")

import asyncio
import time
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from datetime import datetime
import pytz

from fastapi import FastAPI, Request, HTTPException, Depends, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from .schemas import (
    HealthResponse,
    ReadinessResponse,
    ErrorResponse,
)
from .dependencies import (
    ServiceContainer,
    service_container,
    get_service_container,
    get_risk_manager,
    get_broker_adapters,
    sanitize_text,
)

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("StrikeDeskAPI")
IST = pytz.timezone("Asia/Kolkata")

# Default symbols tracked at startup (fallback if discovery returns nothing).
# Exchange note: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY are NSE indices.
# SENSEX is a BSE index — adapter routing handles the exchange difference.
_CORE_SYMBOLS = [
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX",
]


async def _pcr_fetch_loop(dhan_adapter: Any, symbols: list) -> None:
    """
    Background task: refreshes option chains during market hours and feeds PCR into
    the pipeline for PCR slope calculation.
    Per Immutable Rule #6 — uses live broker API, no hardcoded values.

    Dhan throttles the option-chain endpoint to ~1 request / 3s, which is the binding
    constraint on this loop. The core indices are refreshed first on every cycle so
    they stay inside the strict 30s staleness gate; whatever request budget is left
    over is spent round-robining the equity watchlist. When the index chains are
    still cached the indices cost nothing, and the whole budget goes to equities.
    """
    from config.settings import settings as _settings
    from pipeline import pipeline_orchestrator
    from pipeline.indicators.pcr_staleness_monitor import pcr_staleness_monitor
    from strategies.stock_options_strategy import StockOptionsStrategy
    import pytz as _pytz
    _IST = _pytz.timezone("Asia/Kolkata")

    _TARGET_CYCLE_SECONDS = 25.0    # keeps index refresh inside the 30s gate
    _EQUITY_BUDGET_SECONDS = 22.0   # stop queuing equities past this point in a cycle
    _REQUEST_COST = 3.1             # broker throttle spacing per option-chain call
    _equity_gate = StockOptionsStrategy.STALE_THRESHOLD_SECONDS

    core = [s for s in _CORE_SYMBOLS if s in symbols] or list(_CORE_SYMBOLS)
    candidates = [s for s in symbols if s not in core]

    # A name is only worth polling if the rotation returns to it before its chain
    # ages past the gate. Worst case a cycle affords 2 equities, so that many per
    # cycle over half the gate period is the most the budget can actually sustain.
    max_watchlist = max(1, int(_equity_gate / _TARGET_CYCLE_SECONDS))

    configured = _settings.fno_option_watchlist
    if configured:
        tradable = set(candidates)
        equities = [s for s in configured if s in tradable]
        missing = [s for s in configured if s not in tradable]
        if missing:
            logger.warning("[PCR] %d configured watchlist names are not in the tracked "
                           "universe and will be skipped: %s", len(missing), ", ".join(missing))
    else:
        equities = candidates
        logger.info("[PCR] FNO_OPTION_WATCHLIST unset — using a capacity-sized slice of "
                    "the %d discovered equities", len(candidates))

    if len(equities) > max_watchlist:
        logger.warning(
            "[PCR] %d equities exceeds what the request budget can keep inside the %.0fs "
            "gate; polling the first %d. Set FNO_OPTION_WATCHLIST to choose which.",
            len(equities), _equity_gate, max_watchlist,
        )
        equities = equities[:max_watchlist]

    # Equities cycle far slower than indices, so they get a proportionally relaxed gate.
    for sym in equities:
        pcr_staleness_monitor.set_symbol_threshold(sym, _equity_gate)

    logger.info(
        "[PCR] Polling %d indices every cycle + %d equities round-robin "
        "(equity staleness gate %.0fs, worst-case full pass %.1f min)",
        len(core), len(equities), _equity_gate,
        (len(equities) / 2.0) * _TARGET_CYCLE_SECONDS / 60.0 if equities else 0.0,
    )

    rr = 0

    async def _refresh(sym: str) -> bool:
        try:
            chain_dict = await dhan_adapter.get_option_chain(sym)
            pcr_val = chain_dict.get("pcr") if isinstance(chain_dict, dict) else None
            if pcr_val is not None and float(pcr_val) > 0:
                pipeline_orchestrator.process_pcr_update(sym, float(pcr_val))
                return True
            logger.warning("[PCR] %s returned no usable PCR", sym)
        except Exception as _e:
            logger.warning("[PCR] Fetch failed for %s: %s", sym, _e)
        return False

    await asyncio.sleep(5)  # let startup settle first
    while True:
        cycle_start = time.monotonic()
        try:
            now_ist = datetime.now(_IST)
            h, m = now_ist.hour, now_ist.minute
            # Only during market hours 09:15 – 15:30 IST, weekdays
            is_market = (
                now_ist.weekday() < 5
                and (h > 9 or (h == 9 and m >= 15))
                and (h < 15 or (h == 15 and m <= 30))
            )
            if is_market:
                ok = failed = 0
                for sym in core:
                    if await _refresh(sym):
                        ok += 1
                    else:
                        failed += 1
                index_secs = time.monotonic() - cycle_start

                # Spend the remaining budget on equities, one at a time, while there is
                # provably room for another throttled request before the cycle deadline.
                eq_done = 0
                while equities and (
                    time.monotonic() - cycle_start + _REQUEST_COST <= _EQUITY_BUDGET_SECONDS
                ):
                    sym = equities[rr % len(equities)]
                    rr = (rr + 1) % len(equities)
                    if await _refresh(sym):
                        ok += 1
                    else:
                        failed += 1
                    eq_done += 1
                    if eq_done >= len(equities):
                        break

                logger.info(
                    "[PCR] Cycle %.1fs (indices %.1fs): %d ok, %d failed, "
                    "%d equities this pass, next=%s",
                    time.monotonic() - cycle_start, index_secs, ok, failed, eq_done,
                    equities[rr % len(equities)] if equities else "-",
                )
        except Exception as exc:
            logger.warning("[PCR] Loop error: %s", exc)
        await asyncio.sleep(max(1.0, _TARGET_CYCLE_SECONDS - (time.monotonic() - cycle_start)))


_executed_signal_ids: set = set()   # module-level; survives loop iterations
_signal_queue: list = []            # signals waiting for the open slot to clear


async def _angelone_refresh_loop(angel_adapter: Any) -> None:
    """
    Background task: re-authenticates AngelOne via TOTP every 23 hours.
    AngelOne JWT tokens expire after 24 hours; refreshing at 23h keeps the
    session alive without relying on a stale token.  This runs indefinitely.
    """
    import pytz as _pytz
    _IST = _pytz.timezone("Asia/Kolkata")
    _REFRESH_INTERVAL = 23 * 3600  # 23 hours in seconds

    await asyncio.sleep(60)  # brief startup delay

    while True:
        await asyncio.sleep(_REFRESH_INTERVAL)
        try:
            logger.info("[AngelOne] Auto-refresh: re-authenticating to renew JWT token")
            success = await angel_adapter.authenticate()
            if success:
                logger.info("[AngelOne] Auto-refresh succeeded — JWT token renewed")
            else:
                logger.error("[AngelOne] Auto-refresh FAILED — adapter reported auth failure")
        except Exception as exc:
            logger.error("[AngelOne] Auto-refresh exception: %s", exc)


_LIVE_OPTION_REQUIRED = (
    "underlying", "selected_expiry", "selected_strike", "option_type", "dhan_security_id",
)


def _is_option_symbol(symbol: str) -> bool:
    import re
    return bool(re.search(r"\d(?:CE|PE)$", (symbol or "").upper()))


def _auto_option_contract(symbol: str, signal_meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Contract identity for an automated option order. The signal's own contract comes
    from Dhan's option chain and is authoritative; the ContractStore only fills fields
    the signal lacks, and only when it describes the same contract — names repeat
    across weekly expiries, so a stored id for another expiry must never be borrowed.
    """
    from execution.contract_store import (
        IDENTITY_KEYS, STRUCTURAL_KEYS, contract_store, identity_from_signal_metadata,
    )
    from services.core_symbols import CORE_SYMBOLS

    identity = identity_from_signal_metadata(signal_meta)
    stored = contract_store.lookup(symbol) or {}
    conflict = [k for k in STRUCTURAL_KEYS
                if identity.get(k) not in (None, "") and stored.get(k) not in (None, "")
                and str(identity[k]) != str(stored[k])]
    if conflict:
        logger.warning("[AutoExec] ContractStore entry for %s describes another contract (%s); "
                       "using the signal's contract only", symbol, conflict)
    elif stored:
        for k in IDENTITY_KEYS:
            if identity.get(k) in (None, "") and stored.get(k) not in (None, ""):
                identity[k] = stored[k]
    if identity.get("dhan_security_id") and not identity.get("dhan_segment"):
        # Dhan defaults to NSE_FNO; a SENSEX (BSE) id under NSE_FNO is another instrument.
        exchange = CORE_SYMBOLS.get(str(identity.get("underlying", "")).upper(), {}).get("exchange", "NSE")
        identity["dhan_segment"] = "BSE_FNO" if exchange == "BSE" else "NSE_FNO"
    return identity


async def _auto_execute_loop() -> None:
    """
    Background task: every 5 seconds, check for APPROVED_FOR_EXECUTION signals
    in the active asset class and place orders automatically.
    Blueprint Rule 9: only the selected class executes; others remain alert-only.
    """
    import pytz as _pytz
    from strategies.strategy_orchestrator import strategy_orchestrator as _so
    from execution.execution_router import execution_router as _er
    from models.instrument import AssetClass
    from models.order import Order, OrderSide, OrderType
    from risk.order_authorizer import order_authorizer as _auth
    import uuid

    _IST = _pytz.timezone("Asia/Kolkata")

    await asyncio.sleep(10)   # let market data settle at startup
    while True:
        try:
            await asyncio.sleep(5)

            # Only run during market hours 09:15 – 15:10 IST on weekdays
            now_ist = datetime.now(_IST)
            h, m = now_ist.hour, now_ist.minute
            if not (
                now_ist.weekday() < 5
                and (h > 9 or (h == 9 and m >= 15))
                and (h < 15 or (h == 15 and m <= 10))
            ):
                continue

            # Mark open risk positions to market so the Risk Limits panel's P&L
            # and the tick loss-ceiling breaker see live prices. Options aren't on
            # the WebSocket feed, so mark_price falls back to the option chain.
            try:
                from risk.risk_manager import risk_manager as _rm_mark
                from utils.option_contract import mark_price as _mark_price
                for _sym, _pos in list(_rm_mark.positions.items()):
                    if not _pos.is_open:
                        continue
                    _ltp = _mark_price(_sym)
                    if _ltp:
                        _rm_mark.update_tick(_sym, _ltp)
            except Exception as _mark_exc:
                logger.critical("[AutoExec] Risk mark-to-market failed: %s", _mark_exc, exc_info=True)

            # ── AUTO-EXIT: SL / target / trailing-SL / stagnation / EOD ────────────
            try:
                from risk.risk_manager import risk_manager as _rm_exit
                from execution.contract_store import contract_store as _cs_exit
                now_ist_exit = datetime.now(_IST)
                _is_eod_exit = (
                    now_ist_exit.hour > 15
                    or (now_ist_exit.hour == 15 and now_ist_exit.minute >= 10)
                )

                # Collect exit candidates: SL/target/trail first, then stagnation, then EOD
                _sl_target_exits = {
                    e["symbol"]: e["reason"]
                    for e in _rm_exit.check_sl_target_exits()
                }
                _stagnant_syms = _rm_exit.check_stagnant_positions(now_ist_exit)
                _exit_map: dict = dict(_sl_target_exits)
                for _s in _stagnant_syms:
                    _exit_map.setdefault(_s, "STAGNATION")
                if _is_eod_exit:
                    for _s, _p in _rm_exit.positions.items():
                        if _p.is_open:
                            _exit_map.setdefault(_s, "EOD_1510")

                for _exit_sym in list(_exit_map.keys()):
                    _pos = _rm_exit.positions.get(_exit_sym)
                    if not _pos or not _pos.is_open or _pos.quantity == 0:
                        continue
                    _exit_reason = _exit_map.get(_exit_sym, "UNKNOWN")
                    _held_min = (now_ist_exit - _pos.entry_time).total_seconds() / 60.0
                    logger.critical(
                        "[AutoExit] Triggering %s exit for %s (held=%.1fm qty=%d entry=%.2f)",
                        _exit_reason, _exit_sym, _held_min, _pos.quantity, _pos.entry_price,
                    )
                    _exit_side = OrderSide.SELL if _pos.quantity > 0 else OrderSide.BUY
                    _exit_identity = _cs_exit.lookup(_exit_sym) or {}
                    # Use current_price as limit price so paper broker can fill
                    # options (no WS tick) at the last known mark price.
                    _exit_fill_px = _pos.current_price or _pos.entry_price or 0.0
                    # risk_manager stores quantity in units (lots × lot_size).
                    # Adapters expect quantity in lots, so divide back.
                    _exit_lot_sz = 1
                    try:
                        from services.core_symbols import (
                            CORE_SYMBOLS as _X_cs,
                            STOCK_FUTURES_LOT_SIZES as _X_sfls,
                        )
                        _X_und = (
                            (_exit_identity or {}).get("underlying", "")
                            or _exit_sym.upper().split("2")[0]
                        ).upper()
                        _exit_lot_sz = int(
                            _X_cs.get(_X_und, {}).get("lot_size")
                            or _X_sfls.get(_X_und)
                            or 1
                        )
                    except Exception:
                        _exit_lot_sz = 1
                    _exit_qty_lots = max(1.0, round(float(abs(_pos.quantity)) / _exit_lot_sz))
                    _exit_order = Order(
                        client_order_id=str(uuid.uuid4()),
                        symbol=_exit_sym,
                        exchange="NSE",
                        side=_exit_side,
                        order_type=OrderType.LIMIT if _exit_fill_px > 0 else OrderType.MARKET,
                        quantity=_exit_qty_lots,
                        price=_exit_fill_px,
                        broker="dhan",
                    )
                    if _exit_identity:
                        _exit_order.metadata = dict(_exit_identity)
                    # 8-E: HARD_HALT and FULL_KILL_SWITCH block all order flow.
                    # SOFT_HALT (stop-entry only) allows protective exits to continue.
                    try:
                        from risk.emergency_halt import (
                            emergency_halt_manager as _ehm_ae,
                            HaltLevel as _HL_ae,
                        )
                        if _ehm_ae.is_halted and _ehm_ae.current_level in (
                            _HL_ae.HARD_HALT, _HL_ae.FULL_KILL_SWITCH
                        ):
                            logger.critical(
                                "[AutoExit] BLOCKED — %s active; skip auto-exit for %s (reason=%s)",
                                _ehm_ae.current_level, _exit_sym, _exit_reason,
                            )
                            continue
                    except Exception:
                        pass  # fail-open: allow protective exit if halt check errors
                    try:
                        from api.routes.trading import (
                            execution_mode as _exec_mode_exit,
                            get_paper_broker as _get_paper_exit,
                            _select_active_adapter as _sel_exit,
                        )
                        from api.dependencies import service_container as _sc_exit
                        if _exec_mode_exit.is_paper():
                            _exit_adapter = _get_paper_exit()
                            _exit_broker_name = "paper"
                        else:
                            _all_exit_adapters = _sc_exit.get_broker_adapters() or {}
                            _exit_broker_name = "dhan"
                            _exit_adapter = _sel_exit(_all_exit_adapters, _exit_broker_name)
                        _exit_update = await _exit_adapter.place_order(_exit_order)
                        _exit_price = float(
                            getattr(_exit_update, "avg_fill_price", 0.0)
                            or _pos.current_price
                            or _pos.entry_price
                        )
                        _ae_realized = _rm_exit.close_trade(_exit_sym, exit_price=_exit_price)
                        # Book the exit in circuit_breaker DIRECTLY — not via record_fill_in_risk,
                        # because close_trade already set pos.is_open=False, and apply_fill inside
                        # record_fill_in_risk would re-open a phantom opposite position.
                        try:
                            from risk.circuit_breaker import circuit_breaker as _cb_ae
                            from models.risk_state import Trade as _RiskTrade_ae
                            _ae_units = int(round(float(_exit_qty_lots) * _exit_lot_sz))
                            _cb_ae.on_trade_completed(_RiskTrade_ae(
                                trade_id=getattr(_exit_update, "broker_order_id", None) or _exit_order.client_order_id,
                                symbol=_exit_sym,
                                side=_exit_order.side.value,
                                quantity=float(_ae_units),
                                entry_price=_exit_price,
                                realized_pnl=_ae_realized,
                                net_pnl=_ae_realized,
                                trade_type="FNO",
                                segment="FNO",
                            ))
                        except Exception:
                            pass
                        logger.critical(
                            "[AutoExit] ✅ EXIT %s %s @ %.2f reason=%s",
                            "SIMULATED" if _exec_mode_exit.is_paper() else "LIVE",
                            _exit_sym, _exit_price, _exit_reason,
                        )
                    except Exception as _exit_place_err:
                        logger.critical(
                            "[AutoExit] FAILED to place exit order for %s: %s",
                            _exit_sym, _exit_place_err, exc_info=True,
                        )
            except Exception as _exit_loop_err:
                logger.critical("[AutoExit] Exit check crashed: %s", _exit_loop_err, exc_info=True)

            # Get active class from mode_controller (same singleton the route uses)
            from api.routes.trading import mode_controller as _mc, _MODE_TO_ASSET_CLASS
            active_class_str = _MODE_TO_ASSET_CLASS.get(_mc.active_mode)
            if not active_class_str:
                logger.critical("[PIPELINE TRACE] AutoExec DROP — mode %r maps to no asset class",
                                _mc.active_mode)
                continue
            try:
                active_class = AssetClass(active_class_str)
            except ValueError:
                logger.critical("[PIPELINE TRACE] AutoExec DROP — invalid asset class %r",
                                active_class_str)
                continue

            # Halt / cooldown check — do not execute while risk gates are tripped
            try:
                from risk.risk_manager import risk_manager as _rm
                risk_summary = _rm.get_risk_summary()
                if risk_summary.get("emergency_halt_active") or risk_summary.get("cooldown_active"):
                    logger.critical(
                        "[PIPELINE TRACE] AutoExec HALTED: emergency_halt=%s cooldown=%s",
                        risk_summary.get("emergency_halt_active"),
                        risk_summary.get("cooldown_active"),
                    )
                    continue
                if int(risk_summary.get("trade_count", 0)) >= 5:
                    logger.critical(
                        "[PIPELINE TRACE] AutoExec HALTED: trade_count=%s >= cap of 5",
                        risk_summary.get("trade_count"),
                    )
                    continue
            except Exception as _risk_load_exc:
                logger.critical(
                    "[PIPELINE TRACE] AutoExec SILENT DROP — risk_manager failed to load: %s",
                    _risk_load_exc,
                )
                continue

            # ── One-trade-at-a-time gate ──────────────────────────────────
            # If any position is currently open, collect qualifying signals
            # into the queue for visibility but do NOT execute until the slot clears.
            _open_count = int(risk_summary.get("open_positions_count", 0))
            # Layer 2: also block when a BUY entry order is in-flight but not yet
            # reflected in risk_manager (SUBMITTED/OPEN/UNKNOWN orders produce no
            # ActivePosition until the fill WebSocket callback fires).
            if _open_count == 0:
                try:
                    from adapters.order_manager import order_manager as _om_gate
                    from execution.entry_gate import (
                        is_entry_blocked_by_inflight_orders as _inflight_gate,
                    )
                    _inflight_blocked, _inflight_reason = _inflight_gate(_om_gate)
                    if _inflight_blocked:
                        _open_count = 1
                        logger.critical(
                            "[PIPELINE TRACE] AutoExec WAITING: in-flight entry "
                            "order blocks slot: %s",
                            _inflight_reason,
                        )
                except Exception as _gate_exc:
                    logger.warning("[AutoExec] in-flight gate check failed: %s", _gate_exc)

            # ── Collect and rank all qualifying signals ────────────────────
            # Phase 1: class-lock filter
            _candidates = []
            for signal in _so.get_all_actionable_signals():
                if signal.signal_id in _executed_signal_ids:
                    continue
                _class_passes = _er.should_execute(signal, active_class)
                logger.critical(
                    "[PIPELINE TRACE] Junction 2 — Class Lock: signal=%s class=%s vs active=%s -> %s",
                    signal.symbol,
                    signal.asset_class.value if hasattr(signal.asset_class, "value") else signal.asset_class,
                    active_class_str,
                    "PASSED" if _class_passes else "DROPPED",
                )
                if _class_passes:
                    _candidates.append(signal)

            # Phase 2: watchlist filter — keep only extreme-probability symbols
            try:
                from pipeline.pipeline_orchestrator import pipeline_orchestrator as _po_wl
                _wl_entries = _po_wl.get_watchlist()
                if _wl_entries:
                    _wl_symbols = {e.symbol.upper() for e in _wl_entries}
                    _wl_filtered = [
                        s for s in _candidates
                        if s.symbol.upper() in _wl_symbols
                        or (s.metadata or {}).get("underlying", "").upper() in _wl_symbols
                    ]
                    if _wl_filtered:
                        _candidates = _wl_filtered
                        logger.critical(
                            "[PIPELINE TRACE] AutoExec: watchlist filter kept %d/%d signals",
                            len(_candidates), len(_wl_filtered) + (len(_candidates) - len(_wl_filtered)),
                        )
            except Exception:
                pass

            # Phase 3: sort by confidence score — highest first (extreme probability first)
            _candidates.sort(
                key=lambda s: float(getattr(s, "confidence_score", 0.0)),
                reverse=True,
            )

            if _open_count > 0:
                # Slot occupied — park signals in queue, skip execution
                _signal_queue[:] = _candidates
                if _candidates:
                    logger.critical(
                        "[PIPELINE TRACE] AutoExec WAITING: open_positions=%d, "
                        "queue_depth=%d (top=%s score=%.1f)",
                        _open_count, len(_candidates),
                        _candidates[0].symbol,
                        float(getattr(_candidates[0], "confidence_score", 0.0)),
                    )
                continue

            if not _candidates:
                _signal_queue.clear()
                continue

            # Execute only the single highest-confidence signal; queue the rest
            signal = _candidates[0]
            _signal_queue[:] = _candidates[1:]

            logger.critical(
                "[PIPELINE TRACE] AutoExec SELECTING top signal: %s score=%.1f "
                "(queue_depth=%d)",
                signal.symbol,
                float(getattr(signal, "confidence_score", 0.0)),
                len(_signal_queue),
            )

            if True:  # single-signal block (replaces the original for-loop)

                # Build Order — quantity 1 lot; price = entry_price (limit) or 0 (market)
                # Direction is a (str, Enum): str() gives "Direction.LONG", which
                # never matched and turned every auto order into a SELL. Use .value.
                _dir = getattr(signal.direction, "value", signal.direction)
                side = (
                    OrderSide.BUY
                    if str(_dir).upper() in ("LONG", "BUY")
                    else OrderSide.SELL
                )
                # Options: the contract already encodes the view (LONG → CE,
                # SHORT → PE), so the entry is always a BUY. SHORT→SELL here would
                # write a put — the opposite exposure, and an uncovered short.
                if signal.symbol.upper().endswith(("CE", "PE")):
                    side = OrderSide.BUY
                try:
                    from brokers.token_manager import BrokerTokenManager
                    mgr = BrokerTokenManager()
                    broker = mgr.get_active_broker() if hasattr(mgr, "get_active_broker") else "dhan"
                except Exception:
                    broker = "dhan"

                order = Order(
                    client_order_id=str(uuid.uuid4()),
                    symbol=signal.symbol.upper(),
                    exchange="NSE",
                    side=side,
                    order_type=OrderType.LIMIT if signal.entry_price else OrderType.MARKET,
                    quantity=1.0,
                    price=float(signal.entry_price or 0.0),
                    broker=broker,
                )

                # Risk authorization (same gate as manual orders)
                auth_result = _auth.authorize_order(order)
                logger.critical(
                    "[PIPELINE TRACE] Junction 3 — AutoExec Risk Gate: signal=%s symbol=%s -> %s"
                    " (rule=%s reason=%s)",
                    signal.signal_id[:8], order.symbol,
                    "PASSED" if auth_result.is_authorized else "DROPPED",
                    auth_result.rule_violated, auth_result.reason,
                )
                if not auth_result.is_authorized:
                    logger.critical("[AutoExec] DROPPED — signal %s rejected by risk [%s]: %s",
                                   signal.signal_id, auth_result.rule_violated, auth_result.reason)
                    _executed_signal_ids.add(signal.signal_id)   # don't retry a rejected signal
                    continue

                # Mark executed before placing — prevents double-fire if await yields
                _executed_signal_ids.add(signal.signal_id)

                # The auto loop obeys the same execution switch as the operator:
                # in PAPER it fills against the virtual ledger and never reaches a
                # broker; in LIVE it uses the authenticated adapter registry, since
                # a freshly constructed adapter has no session.
                from api.routes.trading import (
                    execution_mode as _exec_mode,
                    get_paper_broker as _get_paper,
                    require_active_broker as _require_active_broker,
                    require_live_feed as _require_live_feed,
                    _select_active_adapter,
                )

                # Live feed gate — same rule as manual orders: the symbol (or its
                # underlying for options) must have a fresh tick from the active
                # broker's WebSocket feed at the moment of execution.
                _sig_meta = signal.metadata if hasattr(signal, "metadata") and signal.metadata else {}
                try:
                    _require_live_feed(order.symbol, broker, metadata=_sig_meta)
                except Exception as _feed_exc:
                    logger.critical("[AutoExec] DROPPED — live feed gate blocked '%s': %s",
                                   signal.symbol, getattr(_feed_exc, "detail", _feed_exc))
                    # Don't mark as executed — the feed may recover and the signal
                    # could be re-evaluated on the next loop iteration.
                    _executed_signal_ids.discard(signal.signal_id)
                    continue
                from execution.contract_store import (
                    contract_store as _contracts,
                    identity_from_signal_metadata as _identity_of,
                )
                _is_option = _is_option_symbol(order.symbol)
                _identity = (_auto_option_contract(order.symbol, _sig_meta) if _is_option
                             else _identity_of(_sig_meta))
                if _exec_mode.is_paper():
                    adapter = _get_paper()
                    broker = "paper"
                    order.metadata = dict(_identity)
                else:
                    # Hard-block: the order's broker field must still be the active
                    # broker. broker was set from mgr.get_active_broker() above, so
                    # this is normally a no-op, but it catches any race where the
                    # active broker changed between the broker lookup and now.
                    try:
                        _require_active_broker(order.broker, broker)
                    except Exception as _guard_exc:
                        logger.critical("[AutoExec] DROPPED — broker isolation guard fired: %s", _guard_exc)
                        continue
                    from api.dependencies import service_container as _sc
                    _all_adapters = _sc.get_broker_adapters() or {}
                    try:
                        adapter = _select_active_adapter(_all_adapters, broker)
                    except Exception:
                        logger.critical("[AutoExec] DROPPED — broker '%s' is not connected: %s %s",
                                     broker, signal.symbol, side.value)
                        continue
                    if _is_option:
                        # Only Dhan's resolver addresses an option by this contract
                        # identity; handing it to any other broker would let that broker
                        # place a real option order, which this system does not route.
                        if broker != "dhan":
                            logger.critical(
                                "[AutoExec] DROPPED — LIVE option %s not sent: options route only "
                                "via Dhan and the active broker is '%s'", order.symbol, broker)
                            continue
                        _missing = [k for k in _LIVE_OPTION_REQUIRED if not _identity.get(k)]
                        if _missing:
                            logger.critical(
                                "[AutoExec] DROPPED — LIVE option %s has no resolved %s; refusing "
                                "to send it", order.symbol, _missing)
                            continue
                        order.metadata = dict(_identity)
                        logger.critical(
                            "[AutoExec] LIVE option contract attached: %s underlying=%s expiry=%s "
                            "strike=%s type=%s dhan_security_id=%s segment=%s",
                            order.symbol, _identity["underlying"], _identity["selected_expiry"],
                            _identity["selected_strike"], _identity["option_type"],
                            _identity["dhan_security_id"], _identity.get("dhan_segment"))
                try:
                    logger.critical(
                        "[PIPELINE TRACE] Junction 4 — Routing Order to %s: symbol=%s side=%s"
                        " qty=%s price=%.2f exec_mode=%s",
                        broker, order.symbol, order.side.value,
                        order.quantity, order.price,
                        "PAPER" if _exec_mode.is_paper() else "LIVE",
                    )
                    _update = await adapter.place_order(order)
                    try:
                        _contracts.record(order.symbol, _identity)
                    except Exception as _cs_exc:
                        logger.error("[AutoExec] Could not record contract details for %s: %s",
                                     order.symbol, _cs_exc)
                    from api.routes.trading import record_fill_in_risk as _record_risk
                    _record_risk(
                        order, _update,
                        getattr(_update, "broker_order_id", None) or order.client_order_id,
                        float(getattr(_update, "avg_fill_price", 0.0) or order.price),
                        _mc.active_mode,
                    )
                    # Attach SL / target / trailing-SL from signal to the open position
                    try:
                        from risk.risk_manager import risk_manager as _rm_levels
                        _fill_px = float(getattr(_update, "avg_fill_price", 0.0) or order.price)
                        _sl = getattr(signal, "stop_loss_price", None)
                        _tgt = getattr(signal, "target_price", None)
                        # Trailing SL: 5% for options (premium can swing hard)
                        _trail_pct = 0.05 if _is_option_symbol(order.symbol) else 0.02
                        _rm_levels.set_position_levels(
                            order.symbol,
                            stop_loss_price=_sl,
                            target_price=_tgt,
                            trailing_sl_pct=_trail_pct,
                        )
                        logger.critical(
                            "[AutoExec] Risk levels set for %s: SL=%.2f target=%.2f trail_pct=%.0f%%",
                            order.symbol,
                            _sl or 0.0, _tgt or 0.0, _trail_pct * 100,
                        )
                    except Exception as _lvl_err:
                        logger.error("[AutoExec] Could not set risk levels for %s: %s",
                                     order.symbol, _lvl_err)
                    logger.info("[AutoExec] ✅ ORDER %s: %s %s %s @ %.2f (class=%s, broker=%s)",
                                "SIMULATED (paper, no broker call)" if _exec_mode.is_paper() else "PLACED LIVE",
                                signal.symbol, side.value, signal.confidence_tier,
                                signal.entry_price or 0.0, active_class_str, broker)
                except Exception as place_err:
                    logger.critical("[AutoExec] DROPPED — '%s' rejected %s %s: %s",
                                 broker, signal.symbol, side.value, place_err, exc_info=True)

        except Exception as exc:
            logger.critical("[AutoExec] Loop error — this cycle's signals were not executed: %s",
                            exc, exc_info=True)


async def _startup_sequence(app: Optional[FastAPI] = None) -> None:
    """
    Full pipeline startup:
      1. Authenticate broker adapters
      2. Instrument discovery (per Immutable Rule #6 — broker API only)
      3. Initialize pipeline orchestrator with discovered symbols
      4. Wire tick callbacks: adapter → pipeline_orchestrator.ingest_tick()
      5. Connect WebSocket tick feeds
      6. Initialise strategy orchestrator
    Each step is wrapped in try/except so a single failure never stops the API.
    """
    _startup_t0 = time.monotonic()
    logger.info("=" * 60)
    logger.info("STRIKE DESK V4 — STARTUP SEQUENCE BEGINNING")
    logger.info("=" * 60)

    # ── Step 1: Authenticate adapters ─────────────────────────────────
    # STARTUP TIMEOUT CAP: each broker's full authenticate() call (including any
    # internal retries inside AuthManager) is limited to _STARTUP_AUTH_TIMEOUT
    # seconds.  This ensures the lifespan handler — and therefore the entire server
    # start — finishes in at most 3 × _STARTUP_AUTH_TIMEOUT + pipeline overhead,
    # keeping the HTTP server available to the frontend (login, health-check) within
    # ~15 seconds even when all broker APIs are unreachable.
    #
    # The per-adapter _HTTP_TIMEOUT (10 s) is a stricter guard on the individual
    # network call; this is the outer fence on how long startup spends per broker.
    _STARTUP_AUTH_TIMEOUT = 5.0  # seconds per broker during startup
    from adapters.dhan_adapter import DhanAdapter
    from adapters.angelone_adapter import AngelOneAdapter
    from adapters.fyers_adapter import FyersAdapter

    adapters: Dict[str, Any] = {}
    for name, Cls in [("dhan", DhanAdapter), ("angelone", AngelOneAdapter), ("fyers", FyersAdapter)]:
        _broker_t0 = time.monotonic()
        logger.info("[STARTUP] Step 1 — authenticating %s (cap=%.0fs, elapsed %.2fs)",
                    name, _STARTUP_AUTH_TIMEOUT, time.monotonic() - _startup_t0)
        try:
            a = Cls()
            ok = await asyncio.wait_for(a.authenticate(), timeout=_STARTUP_AUTH_TIMEOUT)
            if ok:
                adapters[name] = a
                logger.info("[STARTUP] ✅ %s — authenticated in %.2fs", name, time.monotonic() - _broker_t0)
            else:
                logger.warning("[STARTUP] ❌ %s — authentication failed (%.2fs)", name, time.monotonic() - _broker_t0)
        except asyncio.TimeoutError:
            logger.warning(
                "[STARTUP] ⏱  %s — auth timed out after %.0fs (broker API unreachable); "
                "server will start without this broker. Re-auth via POST /api/v1/brokers/%s/reauth.",
                name, _STARTUP_AUTH_TIMEOUT, name,
            )
        except Exception as exc:
            logger.warning("[STARTUP] ❌ %s — auth error after %.2fs: %s", name, time.monotonic() - _broker_t0, exc)

    if not adapters:
        logger.error("[STARTUP] No brokers authenticated — market data will be unavailable")

    # Expose the live adapter registry so routes (e.g. broker re-auth) can
    # re-authenticate and reconnect a running adapter instead of only
    # mutating token state that the adapters never read back.
    if app is not None:
        app.state.adapters = adapters

    # Hand the *authenticated* adapters to the DI container. Without this the
    # container lazily builds its own unauthenticated instances, and every route
    # that places orders or fetches option chains would talk to an adapter with
    # no broker session.
    if adapters:
        try:
            from api.dependencies import service_container
            service_container.set_broker_adapters(adapters)
            logger.info("[STARTUP] ✅ DI container wired to %d authenticated adapters: %s",
                        len(adapters), ", ".join(sorted(adapters)))
        except Exception as exc:
            logger.error("[STARTUP] ❌ Could not wire adapters into DI container: %s", exc)

    logger.info("[STARTUP] Step 1 complete — %d brokers authenticated in %.2fs", len(adapters), time.monotonic() - _startup_t0)
    _warn_if_options_broker_inactive()

    # Steps 2–11 download multi-MB instrument masters (up to 120–180s per URL) and
    # connect WebSockets. Awaiting them here would keep uvicorn from binding the port
    # until they finish, so they run as a background task once the server is listening.
    global _pipeline_startup_task
    _pipeline_startup_task = asyncio.create_task(
        _startup_pipeline(adapters, _startup_t0), name="startup_pipeline"
    )
    _pipeline_startup_task.add_done_callback(_log_pipeline_startup_failure)
    logger.info("[STARTUP] Server accepting requests; Steps 2–11 continue in background")


_pipeline_startup_task: Optional[asyncio.Task] = None


def _warn_if_options_broker_inactive() -> None:
    """The auto loop refuses LIVE option orders unless Dhan is the active broker."""
    try:
        from brokers.token_manager import BrokerTokenManager
        active_broker = BrokerTokenManager().get_active_broker()
    except Exception as exc:
        logger.warning("[STARTUP] Could not read the active broker: %s", exc)
        return
    if active_broker != "dhan":
        logger.warning("[STARTUP] ⚠️  Active broker is %s. Live bot option orders will be refused.", active_broker)
        logger.warning("[STARTUP] ⚠️  Switch to Dhan in the UI to enable automated option trading.")


def startup_in_progress() -> bool:
    return _pipeline_startup_task is not None and not _pipeline_startup_task.done()


def _make_tick_callback(loop: asyncio.AbstractEventLoop):
    from pipeline import pipeline_orchestrator

    def on_tick(tick):
        # Called from broker WS threads — hand the tick to the event loop.
        try:
            asyncio.run_coroutine_threadsafe(pipeline_orchestrator.ingest_tick(tick), loop)
        except Exception:
            pass
    return on_tick


async def wire_late_adapter(name: str, adapter: Any) -> str:
    """
    Give an adapter that authenticated after startup (e.g. via token re-auth) the
    same wiring startup Steps 2c/2d/4/5/7 give one that authenticated at boot.
    Returns a human-readable summary, including whether the feed actually connected.
    """
    from pipeline import pipeline_orchestrator
    from services.instrument_discovery_service import instrument_discovery_service as _ids
    from config.settings import settings as _settings

    notes = []
    if hasattr(adapter, "register_equity_instruments"):
        _cached = _ids._broker_cache.get(name.lower(), (None, []))
        if _cached and _cached[1]:
            adapter.register_equity_instruments(_cached[1])
            notes.append(f"{len(_cached[1])} equity tokens")
    if hasattr(adapter, "load_fno_master"):
        _count = await adapter.load_fno_master(list(_CORE_SYMBOLS) + _settings.fno_option_watchlist)
        notes.append(f"{_count or 0} option contracts")

    if hasattr(adapter, "set_tick_callback"):
        adapter.set_tick_callback(_make_tick_callback(asyncio.get_running_loop()))

    symbols = pipeline_orchestrator.get_pipeline_status().get("monitored_symbols") or list(_CORE_SYMBOLS)
    await adapter.connect()
    if hasattr(adapter, "subscribe_ticks"):
        await adapter.subscribe_ticks(symbols, mode="full")
    if getattr(adapter, "is_connected", False):
        notes.append(f"feed subscribed to {len(symbols)} symbols")
    else:
        notes.append("feed NOT connected yet (WebSocket reconnecting)")

    if name == "dhan" and not any(
        t.get_name() == "pcr_fetch_loop" and not t.done() for t in asyncio.all_tasks()
    ):
        asyncio.create_task(_pcr_fetch_loop(adapter, symbols), name="pcr_fetch_loop")
        notes.append("PCR fetch loop started")

    logger.info("[LATE-REGISTER] ✅ %s wired into live pipeline: %s", name, ", ".join(notes))
    return ", ".join(notes)


def _log_pipeline_startup_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.critical("[STARTUP] Background pipeline startup crashed: %s", exc,
                        exc_info=(type(exc), exc, exc.__traceback__))


async def _startup_pipeline(adapters: Dict[str, Any], _startup_t0: float) -> None:
    """Steps 2–11: discovery, pipeline, WebSocket feeds and background loops."""
    loop = asyncio.get_running_loop()

    # ── Step 2: Instrument discovery ──────────────────────────────────
    _step2_t0 = time.monotonic()
    logger.info("[STARTUP] Step 2 — instrument discovery starting (elapsed %.2fs)", time.monotonic() - _startup_t0)
    discovered_symbols = list(_CORE_SYMBOLS)
    try:
        from services.instrument_service import instrument_service
        result = await instrument_service.initialize(force_refresh=False)
        total = result.get("total_instruments", 0)
        logger.info("[STARTUP] ✅ Instrument discovery complete — %d instruments registered (%.2fs)", total, time.monotonic() - _step2_t0)
        if total > 0:
            from services.instrument_registry import instrument_registry
            all_instruments = list(instrument_registry._instruments.values())
            discovered_symbols = list({
                i.symbol for i in all_instruments if hasattr(i, "symbol")
            }) or discovered_symbols
            logger.info("[STARTUP]    Tracking %d symbols", len(discovered_symbols))
    except Exception as exc:
        logger.warning("[STARTUP] ❌ Instrument discovery failed: %s — using core symbols", exc)

    # ── Step 2b: Register core index symbols as fallback if registry empty ─
    try:
        from services.instrument_registry import instrument_registry
        from services.core_symbols import CORE_SYMBOLS
        from models.instrument import Instrument, Exchange, AssetClass, InstrumentType

        if instrument_registry.count() == 0:
            core_instruments = []
            for sym, meta in CORE_SYMBOLS.items():
                try:
                    inst = Instrument(
                        exchange=Exchange(meta["exchange"]),
                        symbol=sym,
                        asset_class=AssetClass(meta["asset_class"]),
                        instrument_type=InstrumentType(meta["instrument_type"]),
                        broker_symbol=meta.get("dhan_token", sym),
                        token=meta.get("dhan_token", ""),
                        lot_size=meta.get("lot_size", 1),
                        name=meta.get("display_name", sym),
                    )
                    core_instruments.append(inst)
                except Exception as e:
                    logger.warning("[STARTUP] Skipping core symbol %s: %s", sym, e)
            if core_instruments:
                registered = await instrument_registry.register_many(core_instruments)
                logger.info("[STARTUP] ✅ Core symbols registered: %d", registered)
    except Exception as exc:
        logger.warning("[STARTUP] ❌ Core symbol registration failed: %s", exc)

    # ── Step 2c: Push per-broker equity tokens into adapters ──────────────
    try:
        from services.instrument_discovery_service import instrument_discovery_service as _ids
        for _broker_name, _adapter in adapters.items():
            if hasattr(_adapter, "register_equity_instruments"):
                _cached = _ids._broker_cache.get(_broker_name.lower(), (None, []))
                _broker_insts = _cached[1] if _cached else []
                if _broker_insts:
                    _adapter.register_equity_instruments(_broker_insts)
                    logger.info("[STARTUP] ✅ %s equity token map: %d instruments",
                                _broker_name, len(_broker_insts))
    except Exception as exc:
        logger.warning("[STARTUP] ❌ Equity token registration failed: %s", exc)

    # ── Step 2d: Load F&O contract masters ────────────────────────────────
    # Equity tokens alone cannot address an option, so without this every option
    # order is refused. Scoped to the underlyings actually traded.
    try:
        from config.settings import settings as _settings
        _fno_underlyings = list(_CORE_SYMBOLS) + _settings.fno_option_watchlist
        for _broker_name, _adapter in adapters.items():
            if hasattr(_adapter, "load_fno_master"):
                _count = await _adapter.load_fno_master(_fno_underlyings)
                if _count:
                    logger.info("[STARTUP] ✅ %s F&O master: %d option contracts",
                                _broker_name, _count)
                else:
                    logger.warning("[STARTUP] ⚠️  %s F&O master empty — option orders "
                                   "will be refused for this broker", _broker_name)
    except Exception as exc:
        logger.warning("[STARTUP] ❌ F&O master load failed: %s", exc)

    # ── Step 3: Initialize pipeline orchestrator ───────────────────────
    _step3_t0 = time.monotonic()
    logger.info("[STARTUP] Step 3 — pipeline init (elapsed %.2fs)", time.monotonic() - _startup_t0)
    try:
        from pipeline import pipeline_orchestrator
        await pipeline_orchestrator.initialize(discovered_symbols)
        logger.info("[STARTUP] ✅ Pipeline initialized with %d symbols (%.2fs)", len(discovered_symbols), time.monotonic() - _step3_t0)
    except Exception as exc:
        logger.error("[STARTUP] ❌ Pipeline init failed: %s", exc)

    # ── Step 4: Wire tick callbacks adapter → pipeline ─────────────────
    tick_cb = _make_tick_callback(loop)
    for name, adapter in adapters.items():
        try:
            if hasattr(adapter, "set_tick_callback"):
                adapter.set_tick_callback(tick_cb)
                logger.info("[STARTUP] ✅ Tick callback wired for %s", name)
        except Exception as exc:
            logger.warning("[STARTUP] ❌ Tick callback wire failed for %s: %s", name, exc)

    # ── Step 5: Connect WebSocket tick feeds ───────────────────────────
    _step5_t0 = time.monotonic()
    logger.info("[STARTUP] Step 5 — WebSocket connect (elapsed %.2fs)", time.monotonic() - _startup_t0)
    connected_adapters = []
    for name, adapter in adapters.items():
        _ws_t0 = time.monotonic()
        try:
            await adapter.connect()
            if hasattr(adapter, "subscribe_ticks"):
                await adapter.subscribe_ticks(discovered_symbols, mode="full")
            connected_adapters.append(name)
            logger.info("[STARTUP] ✅ %s WebSocket connected, subscribed to %d symbols (%.2fs)",
                        name, len(discovered_symbols), time.monotonic() - _ws_t0)
        except Exception as exc:
            logger.warning("[STARTUP] ❌ %s WebSocket connect failed after %.2fs: %s",
                           name, time.monotonic() - _ws_t0, exc)

    if not connected_adapters:
        logger.warning("[STARTUP] No WebSocket connections established — no live ticks")

    # ── Step 6: Initialise strategy orchestrator ───────────────────────
    try:
        from strategies.strategy_orchestrator import strategy_orchestrator
        if hasattr(strategy_orchestrator, "initialize"):
            strategy_orchestrator.initialize()
        logger.info("[STARTUP] ✅ Strategy orchestrator ready — %s",
                    strategy_orchestrator.get_status().get("registered_strategies", []))
    except Exception as exc:
        logger.error("[STARTUP] ❌ Strategy orchestrator init failed: %s", exc)

    # ── Step 7: PCR background fetch loop ─────────────────────────────
    dhan_adapter = adapters.get("dhan")
    if dhan_adapter:
        asyncio.create_task(
            _pcr_fetch_loop(dhan_adapter, discovered_symbols),
            name="pcr_fetch_loop",
        )
        logger.info("[STARTUP] ✅ PCR fetch loop started for %d symbols", len(discovered_symbols))
    else:
        logger.warning("[STARTUP] ⚠️  No Dhan adapter — PCR fetch loop not started")

    # ── Step 8: AngelOne 23h auto-refresh loop ────────────────────────
    angel_adapter = adapters.get("angelone")
    if angel_adapter:
        asyncio.create_task(
            _angelone_refresh_loop(angel_adapter),
            name="angelone_refresh_loop",
        )
        logger.info("[STARTUP] ✅ AngelOne auto-refresh loop started (23h interval)")
    else:
        logger.warning("[STARTUP] ⚠️  No AngelOne adapter — auto-refresh loop not started")

    # ── Step 9: Auto-execute background loop ──────────────────────────
    asyncio.create_task(_auto_execute_loop(), name="auto_execute_loop")
    logger.info("[STARTUP] ✅ Auto-execute loop started (5s interval, market hours only)")

    # ── Step 10: Token refresh scheduler ──────────────────────────────
    # AngelOne: TOTP refresh at 06:00 IST via adapter.authenticate().
    # Dhan/Fyers: monitor-and-block — logs CRITICAL if expiring, blocks
    # LIVE MODE arming if expired. No portal scraping (C4).
    try:
        from services.token_scheduler import token_scheduler as _ts
        from brokers.token_manager import token_manager as _tm
        _ts.configure(token_manager=_tm, adapters=adapters)
        asyncio.create_task(_ts.run_loop(), name="token_scheduler_loop")
        logger.info("[STARTUP] ✅ Token scheduler started (daily check at 06:00 IST)")
    except Exception as exc:
        logger.error("[STARTUP] ❌ Token scheduler failed to start: %s", exc)

    # ── Step 11: Pre-flight scheduler (08:45 IST daily) ──────────────
    try:
        from services.preflight import preflight_engine as _pf

        async def _preflight_loop() -> None:
            from datetime import timedelta
            import pytz as _pytz
            _IST = _pytz.timezone("Asia/Kolkata")
            _HOUR, _MIN = 8, 45

            while True:
                now_ist = datetime.now(_IST)
                target = now_ist.replace(hour=_HOUR, minute=_MIN, second=0, microsecond=0)
                if now_ist >= target:
                    target += timedelta(days=1)
                delay = (target - now_ist).total_seconds()
                logger.info("[Preflight] Next scheduled run in %.0fs (08:45 IST)", delay)
                await asyncio.sleep(delay)
                try:
                    report = _pf.run_all()
                    logger.info(
                        "[Preflight] Scheduled run complete — overall=%s blocked=%s",
                        report.overall, report.live_mode_blocked,
                    )
                except Exception as exc:
                    logger.error("[Preflight] Scheduled run error: %s", exc)

        asyncio.create_task(_preflight_loop(), name="preflight_loop")
        logger.info("[STARTUP] ✅ Pre-flight scheduler started (daily 08:45 IST)")
    except Exception as exc:
        logger.error("[STARTUP] ❌ Pre-flight scheduler failed to start: %s", exc)

    # ── Done ───────────────────────────────────────────────────────────
    _total_startup_s = time.monotonic() - _startup_t0
    logger.info("=" * 60)
    pipeline_ok = connected_adapters or True
    logger.info("STRIKE DESK V4 — STARTUP COMPLETE in %.2fs", _total_startup_s)
    logger.info("  Brokers authenticated : %s", list(adapters.keys()) or "none")
    logger.info("  WS feeds connected    : %s", connected_adapters or "none")
    logger.info("  Symbols tracked       : %d", len(discovered_symbols))
    logger.info("=" * 60)


async def _shutdown_sequence() -> None:
    logger.info("[SHUTDOWN] Strike Desk V4 shutting down...")
    if _pipeline_startup_task is not None and not _pipeline_startup_task.done():
        _pipeline_startup_task.cancel()
        logger.info("[SHUTDOWN] Cancelled in-progress background startup")
    try:
        from pipeline import pipeline_orchestrator
        await pipeline_orchestrator.stop()
        logger.info("[SHUTDOWN] Pipeline stopped")
    except Exception as exc:
        logger.warning("[SHUTDOWN] Pipeline stop error: %s", exc)
    try:
        from adapters.dhan_adapter import DhanAdapter
        from adapters.angelone_adapter import AngelOneAdapter
        for Cls in [DhanAdapter, AngelOneAdapter]:
            try:
                a = Cls.__new__(Cls)
                if hasattr(a, "disconnect"):
                    await a.disconnect()
            except Exception:
                pass
    except Exception:
        pass
    logger.info("[SHUTDOWN] Complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup_sequence(app)
    yield
    await _shutdown_sequence()


class SecurityLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            logger.error(f"Unhandled error in {request.method} {request.url.path}: {sanitize_text(str(exc))}")
            raise exc


def create_app() -> FastAPI:
    app = FastAPI(
        title="Strike Desk V4 — Quantitative Trading API",
        version="4.0.0",
        description="Institutional-grade Quantitative Trading Engine for NSE/NFO",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityLoggingMiddleware)

    # -----------------------------------------------------------------
    # Exception Handlers
    # -----------------------------------------------------------------
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        clean_msg = sanitize_text(str(exc.detail))
        if exc.status_code == 404:
            err_name = "Not Found"
        elif exc.status_code == 400:
            err_name = "Bad Request"
        elif exc.status_code == 403:
            err_name = "Forbidden"
        else:
            err_name = "HTTPException"

        error_resp = ErrorResponse(
            error=err_name,
            message=clean_msg,
            status_code=exc.status_code,
            timestamp=datetime.now(IST),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(error_resp.model_dump()),
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: Any):
        if hasattr(exc, "detail") and exc.detail:
            clean_msg = sanitize_text(str(exc.detail))
        else:
            clean_msg = f"Path not found: {request.url.path}"
        error_resp = ErrorResponse(
            error="Not Found",
            message=clean_msg,
            status_code=404,
            timestamp=datetime.now(IST),
        )
        return JSONResponse(
            status_code=404,
            content=jsonable_encoder(error_resp.model_dump()),
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        clean_msg = sanitize_text(str(exc))
        error_resp = ErrorResponse(
            error="Bad Request",
            message=clean_msg,
            status_code=status.HTTP_400_BAD_REQUEST,
            timestamp=datetime.now(IST),
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=jsonable_encoder(error_resp.model_dump()),
        )

    @app.exception_handler(PermissionError)
    async def permission_error_handler(request: Request, exc: PermissionError):
        clean_msg = sanitize_text(str(exc))
        error_resp = ErrorResponse(
            error="Forbidden",
            message=clean_msg,
            status_code=status.HTTP_403_FORBIDDEN,
            timestamp=datetime.now(IST),
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=jsonable_encoder(error_resp.model_dump()),
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        logger.error(f"Internal server error: {sanitize_text(str(exc))}")
        error_resp = ErrorResponse(
            error="Internal Server Error",
            message="An internal server error occurred. Details have been logged securely.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            timestamp=datetime.now(IST),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=jsonable_encoder(error_resp.model_dump()),
        )

    # -----------------------------------------------------------------
    # Telemetry Endpoints
    # -----------------------------------------------------------------
    @app.get("/", tags=["System"])
    async def root():
        return {
            "app": "Strike Desk V4",
            "version": "4.0.0",
            "status": "RUNNING",
            "timestamp": datetime.now(IST).isoformat(),
        }

    @app.get("/health", response_model=HealthResponse, tags=["System"])
    async def health_check(container: ServiceContainer = Depends(get_service_container)):
        from pipeline import pipeline_orchestrator
        ps = pipeline_orchestrator.get_pipeline_status()
        risk_mgr = container.get_risk_manager()
        brokers = container.get_broker_adapters()
        is_running = ps.get("is_running", False)
        services = {
            "api": "healthy",
            "risk_manager": "active" if risk_mgr else "uninitialized",
            "execution_router": "active",
            "brokers": f"{len(brokers)} configured" if brokers else "none",
            "pipeline": "running" if is_running else "idle",
            "feed_alive": str(not bool(ps.get("stale_count", 0))),
            "engine_running": str(is_running),
        }
        return HealthResponse(
            status="healthy",
            timestamp=datetime.now(IST),
            version="4.0.0",
            services=services,
        )

    @app.get("/ready", response_model=ReadinessResponse, tags=["System"])
    async def readiness_check(
        risk_mgr: Any = Depends(get_risk_manager),
        brokers: Dict[str, Any] = Depends(get_broker_adapters),
    ):
        risk_summary = risk_mgr.get_risk_summary() if hasattr(risk_mgr, "get_risk_summary") else {}
        is_halted = risk_summary.get("emergency_halt_active", False)
        checks = {
            "risk_system_operational": not is_halted,
            "brokers_available": len(brokers) > 0,
            "loss_ceiling_intact": not risk_summary.get("loss_ceiling_breached", False),
        }
        all_ready = all(checks.values())
        msg = "System ready for trade execution." if all_ready else "System not ready: gating conditions active."
        return ReadinessResponse(
            ready=all_ready,
            timestamp=datetime.now(IST),
            checks=checks,
            message=msg,
        )

    @app.get("/pipeline/status", tags=["System"])
    async def pipeline_status():
        try:
            from pipeline import pipeline_orchestrator
            return pipeline_orchestrator.get_pipeline_status()
        except Exception as exc:
            return {"error": str(exc)}

    # -----------------------------------------------------------------
    # Mount Subsystem Routers
    # -----------------------------------------------------------------
    try:
        from api.routes.brokers import router as brokers_router
        app.include_router(brokers_router, prefix="/api/v1/brokers", tags=["Brokers"])
    except Exception as e:
        logger.warning(f"Could not load brokers router: {e}")

    try:
        from api.routes.trading import router as trading_router
        app.include_router(trading_router, prefix="/api/v1/trading", tags=["Trading"])
    except Exception as e:
        logger.warning(f"Could not load trading router: {e}")

    try:
        from api.routes.risk import router as risk_router
        app.include_router(risk_router, prefix="/api/v1/risk", tags=["Risk"])
    except Exception as e:
        logger.warning(f"Could not load risk router: {e}")

    try:
        from api.routes.market import router as market_router
        app.include_router(market_router, prefix="/api/v1/market", tags=["Market Data"])
    except Exception as e:
        logger.warning(f"Could not load market router: {e}")

    try:
        from api.routes.auth import router as auth_router
        app.include_router(auth_router, prefix="/api/v1/auth", tags=["Auth"])
    except Exception as e:
        logger.warning(f"Could not load auth router: {e}")

    try:
        from api.routes.preflight import router as preflight_router
        app.include_router(preflight_router, prefix="/api/v1/preflight", tags=["Pre-Flight"])
    except Exception as e:
        logger.warning(f"Could not load preflight router: {e}")

    return app


app = create_app()
