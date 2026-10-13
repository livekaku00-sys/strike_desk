import asyncio
from datetime import date, datetime, timezone
import functools
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.settings import DhanSettings
from models.account import Balance, Position
from models.tick import Tick
from models.option_chain import OptionChain
from models.order import (
    CommonErrorCode,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    OrderUpdate,
    ProductType,
)
from pipeline.tick_buffer import tick_buffer
from utils.exceptions import AuthenticationError, ConnectionError, DataError, RateLimitError
from utils.rate_limiter import rate_limiter
from utils.tick_normalizer import TickNormalizer
from utils.token_store import token_store
from .auth_manager import auth_manager
from .base_adapter import BaseAdapter
from .option_chain_fetcher import option_chain_fetcher
from .websocket_handler import WebSocketHandler, NetworkError
from .websocket_handler import AuthenticationError as _WSAuthError

try:
    from dhanhq import dhanhq, marketfeed
    from dhanhq import DhanContext as _DhanContext
    # Constants are class-level on MarketFeed, not module-level
    _MF = marketfeed.MarketFeed
    _IDX = _MF.IDX           # 0  — IDX_I segment (indices)
    _NSE_EQ = _MF.NSE        # 1  — NSE equity segment
    _NSE_FNO = _MF.NSE_FNO   # 2  — NSE F&O segment
    _QUOTE = _MF.Quote       # 17 — valid for v2
    _FULL = _MF.Full         # 21 — valid for v2 (Depth=19 is v1 only)
    _DHANHQ_AVAILABLE = True
except ImportError:
    _DHANHQ_AVAILABLE = False
    dhanhq = None
    marketfeed = None
    _DhanContext = None
    _IDX = 0
    _NSE_EQ = 1
    _NSE_FNO = 2
    _QUOTE = 17
    _FULL = 21

logger = logging.getLogger(__name__)

_SDK_MISSING_MSG = (
    "dhanhq SDK is not installed (ModuleNotFoundError) — refusing to authenticate or "
    "connect. Install it with `pip install dhanhq`."
)
if not _DHANHQ_AVAILABLE:
    logger.critical("[Dhan] %s", _SDK_MISSING_MSG)

try:
    from services.core_symbols import CORE_SYMBOLS as _CORE_SYMS, DHAN_TOKEN_TO_SYMBOL as _DHAN_ID_TO_SYM
    _DHAN_SECURITY_IDS: Dict[str, str] = {k: v["dhan_token"] for k, v in _CORE_SYMS.items()}
    _DHAN_SEGMENTS: Dict[str, str] = {k: v.get("dhan_segment", "IDX_I") for k, v in _CORE_SYMS.items()}
except Exception:
    # Dhan security IDs for index instruments. All route via segment IDX_I.
    # NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY are NSE; SENSEX (id "51") is BSE.
    _DHAN_SECURITY_IDS = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27", "MIDCPNIFTY": "442", "SENSEX": "51"}
    _DHAN_ID_TO_SYM = {v: k for k, v in _DHAN_SECURITY_IDS.items()}
    _DHAN_SEGMENTS = {k: "IDX_I" for k in _DHAN_SECURITY_IDS}

# Dhan throttles the option-chain endpoint to 1 request per 3 seconds.
_OC_MIN_INTERVAL = 3.05
_EXPIRY_TTL = 3600.0

# Dhan rejects a correlationId longer than 30 chars or outside [a-zA-Z0-9 _-].
# Our client_order_id is a 36-char UUID.
_CORRELATION_ID_MAX = 30


def _dhan_correlation_id(client_order_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", str(client_order_id))[:_CORRELATION_ID_MAX]


class DhanAdapter(BaseAdapter):
    def __init__(self, settings: Optional[DhanSettings] = None, balance_ttl: float = 60.0) -> None:
        super().__init__(broker_name="Dhan", balance_ttl=balance_ttl)
        self.settings = settings
        self._client = None
        self._feed = None
        self._on_tick_callback: Optional[Callable[[Tick], None]] = None
        # Positive proof of feed flow: ticks/errors per window, logged once a minute.
        self._tick_count = 0
        self._tick_error_count = 0
        self._tick_window_start = time.monotonic()
        self._equity_ids: Dict[str, tuple] = {}      # symbol → (segment, security_id)
        self._equity_id_to_sym: Dict[str, str] = {}  # "seg:sec_id" → symbol
        self._expiry_cache: Dict[str, Tuple[float, List[str]]] = {}
        self._oc_lock = asyncio.Lock()
        self._oc_last_call: float = 0.0
        self._client_ids_by_tag: Dict[str, str] = {}  # Dhan correlationId → client_order_id

        self.ws_handler = WebSocketHandler(
            broker_name="Dhan",
            connect_coro=self._ws_connect,
            disconnect_coro=self._ws_disconnect,
            send_ping_coro=self._ws_send_ping,
            resubscribe_coro=self._ws_resubscribe,
        )

    def _load_settings(self) -> DhanSettings:
        if self.settings is None:
            self.settings = DhanSettings.from_env()
        return self.settings

    def get_auth_status(self) -> str:
        return auth_manager.get_auth_status("dhan")

    def get_connection_state(self) -> str:
        return self.ws_handler.get_connection_state()

    def set_tick_callback(self, callback: Callable[[Tick], None]) -> None:
        self._on_tick_callback = callback

    def register_equity_instruments(self, instruments: list) -> None:
        """Populate equity symbol ↔ Dhan (segment, security_id) maps from discovered instruments."""
        for inst in instruments:
            sym = getattr(inst, "symbol", None)
            tok = getattr(inst, "token", None)
            if not sym or not tok:
                continue
            if sym in _DHAN_SECURITY_IDS:
                continue  # core index already mapped
            # instrument_type is a (str, Enum), whose str() is "InstrumentType.EQ" —
            # comparing that to "EQ" silently filed every equity under F&O.
            raw_type = getattr(inst, "instrument_type", None)
            itype = getattr(raw_type, "value", raw_type) or "EQ"
            seg = _NSE_EQ if str(itype).upper() == "EQ" else _NSE_FNO
            self._equity_ids[sym] = (seg, str(tok))
            self._equity_id_to_sym[f"{seg}:{tok}"] = sym
        logger.info("[Dhan] Registered %d equity instruments for tick subscription", len(self._equity_ids))

    def handle_raw_tick(self, raw_data: Dict[str, Any], symbol_override: Optional[str] = None) -> Optional[Tick]:
        try:
            # Resolve numeric security_id → symbol name when no override given
            if symbol_override is None:
                sec_id = str(raw_data.get("security_id", ""))
                exch_seg = raw_data.get("exchange_segment", 0)
                symbol_override = (
                    _DHAN_ID_TO_SYM.get(sec_id)
                    or self._equity_id_to_sym.get(f"{exch_seg}:{sec_id}")
                )
            tick = TickNormalizer.normalize_dhan(raw_data, symbol_override=symbol_override)
            self.ws_handler.health_monitor.record_tick()
            # _on_tick_callback uses run_coroutine_threadsafe — safe from WS thread
            if self._on_tick_callback:
                self._on_tick_callback(tick)
            self._tick_count += 1
            self._maybe_log_tick_rate()
            return tick
        except Exception as exc:
            self._tick_error_count += 1
            self._maybe_log_tick_rate()
            self.logger.error("Dhan raw tick error: %s", exc)
            return None

    _TICK_LOG_INTERVAL_S = 60.0

    def _maybe_log_tick_rate(self) -> None:
        now = time.monotonic()
        elapsed = now - self._tick_window_start
        if elapsed < self._TICK_LOG_INTERVAL_S:
            return
        logger.info(
            "[Dhan WS] Dhan ticks received: %d in last %.0fs (parse errors: %d)",
            self._tick_count, elapsed, self._tick_error_count,
        )
        self._tick_count = 0
        self._tick_error_count = 0
        self._tick_window_start = now

    # Per-call HTTP timeout (seconds). Separate from AuthManager's retry timeout so
    # a hung Dhan API call fails fast rather than waiting the full 30s per attempt.
    _HTTP_TIMEOUT = 10.0

    async def _perform_auth(self) -> bool:
        t0 = time.monotonic()
        await rate_limiter.acquire("dhan")
        cfg = self._load_settings()
        if not (_DHANHQ_AVAILABLE and dhanhq and _DhanContext):
            raise AuthenticationError(_SDK_MISSING_MSG, broker="dhan")
        ctx = _DhanContext(cfg.client_id, cfg.access_token)
        client = dhanhq(ctx)
        loop = asyncio.get_running_loop()
        logger.info(
            "[Dhan] Starting auth — calling get_fund_limits (%.2fs since attempt start)",
            time.monotonic() - t0,
        )
        try:
            fund_limits = await asyncio.wait_for(
                loop.run_in_executor(None, client.get_fund_limits),
                timeout=self._HTTP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.error(
                "[Dhan] get_fund_limits timed out after %.1fs (HTTP_TIMEOUT=%.1fs) — "
                "broker API unreachable or slow; failing fast",
                elapsed, self._HTTP_TIMEOUT,
            )
            raise AuthenticationError(
                f"Dhan get_fund_limits timed out after {self._HTTP_TIMEOUT:.0f}s",
                broker="dhan",
            )
        if isinstance(fund_limits, dict) and fund_limits.get("status", "").lower() == "failure":
            raise AuthenticationError(f"Dhan auth failed: {fund_limits}", broker="dhan")
        self._client = client
        self._dhan_ctx = ctx  # saved for MarketFeed in _ws_connect

        token_store.store_token(
            broker="dhan",
            token=cfg.access_token,
            expires_in_seconds=86400,
            metadata={"client_id": cfg.client_id},
        )
        self.is_authenticated = True
        return True

    async def authenticate(self) -> bool:
        if not _DHANHQ_AVAILABLE:
            # Not retryable — skip auth_manager's backoff loop.
            logger.critical("[Dhan] %s Broker marked DISCONNECTED.", _SDK_MISSING_MSG)
            self._client = None
            self._feed = None
            self.is_authenticated = False
            self.is_connected = False
            return False
        success = await auth_manager.execute_with_retry(broker="dhan", auth_coro_func=self._perform_auth)
        self.is_authenticated = success
        return success

    async def _ws_connect(self) -> None:
        cfg = self._load_settings()
        if not (_DHANHQ_AVAILABLE and marketfeed and _DhanContext):
            # WebSocketHandler's own AuthenticationError (not utils.exceptions') is what
            # moves it to DISCONNECTED instead of its reconnect loop — retrying cannot
            # install the SDK.
            logger.critical("[Dhan WS] %s No market feed.", _SDK_MISSING_MSG)
            raise _WSAuthError(_SDK_MISSING_MSG)
        ctx = getattr(self, "_dhan_ctx", None) or _DhanContext(cfg.client_id, cfg.access_token)
        # IDX_I (segment 0) for core indices; NSE_EQ (segment 1) for equities
        initial_instruments = [
            (_IDX, sid, _QUOTE)
            for sid in _DHAN_SECURITY_IDS.values()
        ]
        for _seg, _sec_id in self._equity_ids.values():
            initial_instruments.append((_seg, _sec_id, _QUOTE))
        self._log_ws_subscription("initial", initial_instruments)
        adapter_ref = self

        def _on_message(feed_obj, data):
            if data and isinstance(data, dict):
                adapter_ref.handle_raw_tick(data)

        try:
            # MarketFeed is the correct class (not DhanFeed which doesn't exist)
            feed = marketfeed.MarketFeed(
                dhan_context=ctx,
                instruments=initial_instruments,
                version="v2",
                on_message=_on_message,
            )
            self._feed = feed
            feed.start()  # runs in daemon thread; handles connect + recv loop
        except Exception as e:
            raise ConnectionError(f"Dhan feed init error: {e}", broker="dhan") from e

    def _log_ws_subscription(
        self, stage: str, instruments: List[tuple], requested: Optional[int] = None
    ) -> None:
        by_seg: Dict[str, int] = {}
        for seg, _sec_id, _mode in instruments:
            by_seg[str(seg)] = by_seg.get(str(seg), 0) + 1
        idx_ids = {str(sid) for seg, sid, _m in instruments if seg == _IDX}
        idx_syms = sorted(_DHAN_ID_TO_SYM.get(i, i) for i in idx_ids)
        logger.info(
            "[Dhan WS] %s subscription: %d instruments%s | by segment=%s | indices=%s",
            stage, len(instruments),
            f" (of {requested} requested)" if requested is not None else "",
            by_seg, idx_syms,
        )
        logger.debug(
            "[Dhan WS] %s full list (segment, security_id -> symbol): %s", stage,
            [
                (seg, sid, _DHAN_ID_TO_SYM.get(str(sid))
                 or self._equity_id_to_sym.get(f"{seg}:{sid}"))
                for seg, sid, _m in instruments
            ],
        )

    async def _ws_disconnect(self) -> None:
        if self._feed:
            feed = self._feed
            self._feed = None
            try:
                if hasattr(feed, "close_connection"):
                    loop = asyncio.get_running_loop()
                    await asyncio.wait_for(
                        loop.run_in_executor(None, feed.close_connection),
                        timeout=5.0,
                    )
            except Exception:
                pass

    async def _ws_send_ping(self) -> None:
        if self._feed and hasattr(self._feed, "ping"):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._feed.ping)
            self.ws_handler.health_monitor.record_heartbeat_received()
        else:
            self.ws_handler.health_monitor.record_heartbeat_received()

    async def _ws_resubscribe(self, subscriptions: Dict[str, str]) -> None:
        if not subscriptions or not self._feed:
            return
        if _DHANHQ_AVAILABLE and marketfeed:
            instruments = []
            for sym, m in subscriptions.items():
                feed_mode = _FULL if m == "full" else _QUOTE
                if sym in _DHAN_SECURITY_IDS:
                    instruments.append((_IDX, _DHAN_SECURITY_IDS[sym], feed_mode))
                elif sym in self._equity_ids:
                    seg, sec_id = self._equity_ids[sym]
                    instruments.append((seg, sec_id, feed_mode))
            if instruments and hasattr(self._feed, "subscribe_symbols"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._feed.subscribe_symbols, instruments)

    async def connect(self) -> None:
        if not self.is_authenticated or self.get_auth_status() != "authenticated":
            if not await self.authenticate():
                raise ConnectionError("Cannot connect to Dhan: Auth failed.", broker="dhan")
        ws_ok = await self.ws_handler.connect()
        self.is_connected = ws_ok

    async def disconnect(self) -> None:
        await self.ws_handler.disconnect()
        self.is_connected = False
        self.is_authenticated = False
        token_store.clear_token("dhan")

    async def subscribe_ticks(self, symbols: List[str], mode: str = "full") -> None:
        if not symbols:
            return
        self.ws_handler.record_subscription(symbols, mode)
        if self._feed and _DHANHQ_AVAILABLE and marketfeed:
            feed_mode = _FULL if mode == "full" else _QUOTE
            instruments = []
            unresolved = []
            for s in symbols:
                if s in _DHAN_SECURITY_IDS:
                    instruments.append((_IDX, _DHAN_SECURITY_IDS[s], feed_mode))
                elif s in self._equity_ids:
                    seg, sec_id = self._equity_ids[s]
                    instruments.append((seg, sec_id, feed_mode))
                else:
                    unresolved.append(s)
            self._log_ws_subscription("subscribe_ticks", instruments, requested=len(symbols))
            if unresolved:
                logger.warning(
                    "[Dhan WS] %d/%d requested symbols have no Dhan security_id and were "
                    "NOT subscribed (no ticks will arrive for them): %s%s",
                    len(unresolved), len(symbols), sorted(unresolved)[:25],
                    " ..." if len(unresolved) > 25 else "",
                )
            if instruments and hasattr(self._feed, "subscribe_symbols"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._feed.subscribe_symbols, instruments)

    async def unsubscribe_ticks(self, symbols: List[str]) -> None:
        if not symbols:
            return
        self.ws_handler.record_unsubscription(symbols)
        if self._feed and _DHANHQ_AVAILABLE and marketfeed:
            instruments = [
                (_IDX, _DHAN_SECURITY_IDS.get(s, s), _QUOTE)
                for s in symbols
            ]
            if hasattr(self._feed, "unsubscribe_symbols"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._feed.unsubscribe_symbols, instruments)

    async def get_balance(self, force_refresh: bool = False) -> Balance:
        now_mono = time.monotonic()
        if not force_refresh and self._cached_balance is not None:
            if (now_mono - self._balance_cached_at) < self.balance_ttl:
                return self._cached_balance

        await rate_limiter.acquire("dhan")
        avail_cash = 0.0
        used_margin = 0.0
        total_equity = 0.0

        if self._client and hasattr(self._client, "get_fund_limits"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._client.get_fund_limits)
            if isinstance(res, dict) and res.get("status", "").lower() == "failure":
                raise DataError(f"Dhan get_fund_limits failed: {res.get('remarks') or res}", broker="dhan")
            _raw = res.get("data") if isinstance(res, dict) else res
            data = _raw if isinstance(_raw, dict) else {}
            avail_cash = float(data.get("availMargin") or data.get("availableBalance") or 0.0)
            used_margin = float(data.get("utilizedAmount") or data.get("usedMargin") or 0.0)
            total_equity = float(data.get("totalEquity") or (avail_cash + used_margin))

        unrealized_pnl = 0.0
        realized_pnl = 0.0
        try:
            positions = await self.get_positions()
            unrealized_pnl = sum(p.unrealized_pnl for p in positions)
            realized_pnl = sum(p.realized_pnl for p in positions)
        except Exception:
            pass

        bal = Balance(
            broker="dhan",
            available_cash=avail_cash,
            used_margin=used_margin,
            total_equity=total_equity,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            timestamp=datetime.now(timezone.utc),
        )
        self._cached_balance = bal
        self._balance_cached_at = now_mono
        return bal

    async def get_positions(self) -> List[Position]:
        await rate_limiter.acquire("dhan")
        if self._client and hasattr(self._client, "get_positions"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._client.get_positions)
            if isinstance(res, dict) and res.get("status", "").lower() == "failure":
                raise DataError(f"Dhan get_positions failed: {res.get('remarks') or res}", broker="dhan")
            _raw = res.get("data") if isinstance(res, dict) else res
            data_list = _raw if isinstance(_raw, list) else []
            positions = []
            for item in data_list:
                qty = float(item.get("netQty") or 0.0)
                buy_qty = float(item.get("buyQty") or 0.0)
                sell_qty = float(item.get("sellQty") or 0.0)
                buy_avg = float(item.get("buyAvg") or 0.0)
                current_price = float(item.get("costPrice") or item.get("lastPrice") or buy_avg)
                realized_pnl = float(item.get("realizedProfit") or 0.0)
                unrealized_pnl = float(item.get("unrealizedProfit") or 0.0)
                pnl = realized_pnl + unrealized_pnl
                cost = abs(qty * buy_avg)
                pnl_pct = round((pnl / cost * 100.0), 2) if cost > 0 else 0.0
                positions.append(
                    Position(
                        broker="dhan",
                        symbol=str(item.get("securityId") or item.get("tradingSymbol") or "UNKNOWN"),
                        exchange="NSE",
                        product_type=str(item.get("productType") or "INTRADAY"),
                        qty=qty,
                        buy_qty=buy_qty,
                        sell_qty=sell_qty,
                        avg_price=buy_avg,
                        current_price=current_price,
                        pnl=pnl,
                        pnl_percent=pnl_pct,
                        realized_pnl=realized_pnl,
                        unrealized_pnl=unrealized_pnl,
                    )
                )
            return positions
        return []

    def resolve_oc_underlying(self, underlying: str) -> Optional[Tuple[int, str]]:
        """Maps a symbol to the (security_id, exchange_segment) the option-chain API expects."""
        sym = underlying.strip().upper()
        if sym in _DHAN_SECURITY_IDS:
            try:
                return int(_DHAN_SECURITY_IDS[sym]), _DHAN_SEGMENTS.get(sym, "IDX_I")
            except (TypeError, ValueError):
                return None
        if sym in self._equity_ids:
            _seg, sec_id = self._equity_ids[sym]
            try:
                return int(sec_id), "NSE_EQ"
            except (TypeError, ValueError):
                return None
        return None

    async def _oc_throttle(self) -> None:
        elapsed = time.monotonic() - self._oc_last_call
        if elapsed < _OC_MIN_INTERVAL:
            await asyncio.sleep(_OC_MIN_INTERVAL - elapsed)
        self._oc_last_call = time.monotonic()

    async def get_expiry_list(self, sec_id: int, segment: str) -> List[str]:
        cache_key = f"{segment}:{sec_id}"
        cached = self._expiry_cache.get(cache_key)
        now = time.monotonic()
        if cached and (now - cached[0]) < _EXPIRY_TTL:
            return cached[1]

        loop = asyncio.get_running_loop()
        await self._oc_throttle()
        res = await loop.run_in_executor(
            None,
            functools.partial(
                self._client.expiry_list,
                under_security_id=sec_id,
                under_exchange_segment=segment,
            ),
        )
        data = res.get("data", {}) if isinstance(res, dict) else {}
        raw_list = data.get("data", data) if isinstance(data, dict) else data
        expiries = [str(e) for e in raw_list] if isinstance(raw_list, list) else []
        self._expiry_cache[cache_key] = (now, expiries)
        return expiries

    async def get_option_chain(
        self, underlying: str, expiry: Optional[str] = None, force_refresh: bool = False
    ) -> Dict[str, Any]:
        await rate_limiter.acquire("dhan")
        sym = underlying.strip().upper()

        if not self._client:
            raise DataError(f"Dhan client unavailable for option chain: {sym}", broker="dhan")

        resolved = self.resolve_oc_underlying(sym)
        if resolved is None:
            raise DataError(f"No Dhan security_id mapping for underlying '{sym}'", broker="dhan")
        sec_id, segment = resolved

        # Serialize option-chain traffic: Dhan allows 1 request / 3s across both endpoints.
        async with self._oc_lock:
            expiry_val = expiry
            if not expiry_val:
                expiries = await self.get_expiry_list(sec_id, segment)
                if not expiries:
                    raise DataError(f"No expiries returned for '{sym}'", broker="dhan")
                today_iso = date.today().isoformat()
                upcoming = sorted(e for e in expiries if e >= today_iso)
                expiry_val = upcoming[0] if upcoming else sorted(expiries)[-1]

            async def _fetch_raw():
                loop = asyncio.get_running_loop()
                await self._oc_throttle()
                return await loop.run_in_executor(
                    None,
                    functools.partial(
                        self._client.option_chain,
                        under_security_id=sec_id,
                        under_exchange_segment=segment,
                        expiry=expiry_val,
                    ),
                )

            chain = await option_chain_fetcher.get_chain(
                broker="dhan",
                underlying=sym,
                expiry=expiry_val,
                raw_fetch_coro=_fetch_raw,
                force_refresh=force_refresh,
            )
        return chain.to_dict()

    # Dhan feed segment codes → the strings its order API expects.
    _SEGMENT_NAMES = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO"}

    def resolve_order_instrument(self, order: Order) -> Dict[str, str]:
        """
        Maps a canonical symbol to Dhan's own identifiers.

        Reads only Dhan's maps. Dhan needs a numeric security_id, and its numbering
        is its own — NIFTY is 13 here and 26000 at AngelOne — so a token borrowed
        from another broker would silently address a different instrument. If this
        cannot resolve, the order is refused rather than sent with a guess.
        """
        from utils.symbol_aliases import canonical_symbol
        sym = canonical_symbol(order.symbol or "")

        # An explicit id from the signal (option chain contracts carry Dhan's own).
        explicit = (order.metadata or {}).get("dhan_security_id")
        if explicit:
            return {
                "security_id": str(explicit),
                "exchange_segment": str((order.metadata or {}).get("dhan_segment") or "NSE_FNO"),
            }

        if sym in _DHAN_SECURITY_IDS:
            return {
                "security_id": str(_DHAN_SECURITY_IDS[sym]),
                "exchange_segment": _DHAN_SEGMENTS.get(sym, "IDX_I"),
            }

        if sym in self._equity_ids:
            seg_code, sec_id = self._equity_ids[sym]
            return {
                "security_id": str(sec_id),
                "exchange_segment": self._SEGMENT_NAMES.get(int(seg_code), "NSE_EQ"),
            }

        raise DataError(
            f"Dhan has no security_id for '{sym}'. Refusing to place the order rather "
            f"than sending a symbol Dhan cannot address.",
            broker="dhan",
        )

    async def place_order(self, order: Order) -> OrderUpdate:
        await rate_limiter.acquire("dhan")
        # Without a client the request cannot reach the exchange. Returning an OPEN
        # update here would report a phantom fill for an order that never left the
        # process, so fail loudly instead.
        if not self._client:
            raise ConnectionError(
                "Dhan adapter is not authenticated — order was not sent to the broker",
                broker="dhan",
            )
        instrument = self.resolve_order_instrument(order)
        logger.info(
            "[Dhan] Routing %s %s x%s → security_id=%s segment=%s",
            order.side.value, order.symbol, order.quantity,
            instrument["security_id"], instrument["exchange_segment"],
        )

        # placeOrder must exist on the client. If it doesn't (SDK version mismatch
        # or a stale mock), fail hard rather than silently returning a local UUID
        # which would record a phantom OPEN order that never reached the exchange.
        if not hasattr(self._client, "place_order"):
            raise ConnectionError(
                "Dhan client has no place_order method — SDK version mismatch or client not initialised",
                broker="dhan",
            )

        loop = asyncio.get_running_loop()
        # Dhan expects total contracts (lots × lot_size) for FNO orders.
        # Look up lot_size via the registry key populated by DhanDiscovery.
        lot_size = 1
        lot_source = "default(non-FNO)"
        registry_key = None
        segment = instrument["exchange_segment"]
        if segment in ("NSE_FNO", "BSE_FNO"):
            lot_source = "default(underlying unresolved)"
            meta_underlying = (order.metadata or {}).get("underlying", "")
            sym_upper = order.symbol.upper()
            if not meta_underlying:
                from services.core_symbols import resolve_fut_underlying, resolve_opt_underlying
                if "FUT" in sym_upper:
                    meta_underlying = resolve_fut_underlying(sym_upper)
                elif re.search(r"\d(?:CE|PE)$", sym_upper):
                    meta_underlying = resolve_opt_underlying(sym_upper)
            if meta_underlying:
                underlying = meta_underlying.upper()
                try:
                    from services.instrument_registry import instrument_registry
                    from models.instrument import Exchange as _Exchange
                    from services.core_symbols import CORE_SYMBOLS, STOCK_FUTURES_LOT_SIZES
                    registry_key = underlying + "-FUT"
                    inst = await instrument_registry.get(_Exchange.NSE, registry_key)
                    if inst is not None:
                        lot_size = inst.lot_size
                        lot_source = "registry"
                    elif underlying in CORE_SYMBOLS:
                        lot_size = CORE_SYMBOLS[underlying]["lot_size"]
                        lot_source = "CORE_SYMBOLS(static)"
                    elif underlying in STOCK_FUTURES_LOT_SIZES:
                        lot_size = STOCK_FUTURES_LOT_SIZES[underlying]
                        lot_source = "STOCK_FUTURES_LOT_SIZES(static)"
                    else:
                        lot_source = "default(registry miss, no static entry)"
                except Exception as _lot_exc:
                    lot_source = f"default(lookup error: {_lot_exc})"
        final_qty = int(order.quantity) * lot_size
        logger.critical(
            "[LOT SIZE LIVE TRACE] Symbol: %s | Raw Qty: %s | Resolved Lot Size: %s"
            " | FINAL PAYLOAD QTY: %s | registry_key=%s source=%s segment=%s",
            order.symbol, order.quantity, lot_size, final_qty,
            registry_key, lot_source, segment,
        )
        payload = {
            "security_id": instrument["security_id"],
            "exchange_segment": segment,
            "transaction_type": "BUY" if order.side == OrderSide.BUY else "SELL",
            "quantity": final_qty,
            "order_type": "LIMIT" if order.order_type == OrderType.LIMIT else "MARKET",
            "product_type": "INTRADAY" if order.product_type == ProductType.INTRADAY else "CNC",
            "price": order.price,
            "correlation_id": _dhan_correlation_id(order.client_order_id),
        }
        self._client_ids_by_tag[payload["correlation_id"]] = order.client_order_id
        logger.critical("[PIPELINE TRACE] Junction 5 — Dhan API Payload: %s", payload)
        # dhanhq.place_order takes keyword arguments, not one dict (a dict raised
        # TypeError for every live order). The SDK calls the correlation id `tag`.
        sdk_kwargs = {k: v for k, v in payload.items() if k != "correlation_id"}
        sdk_kwargs["tag"] = payload["correlation_id"]
        res = await loop.run_in_executor(
            None, functools.partial(self._client.place_order, **sdk_kwargs)
        )
        logger.info("[Dhan] place_order raw response: %s", res)

        if isinstance(res, dict) and res.get("status", "").lower() == "failure":
            raise RuntimeError(f"Dhan order failed: {res.get('remarks') or res}")
        res_d = res if isinstance(res, dict) else {}
        _raw = res_d.get("data")
        data = _raw if isinstance(_raw, dict) else {}

        broker_order_id = str(data.get("orderId") or res_d.get("orderId") or "").strip()
        if not broker_order_id:
            raise RuntimeError(
                f"Dhan place_order returned success but no orderId was found. "
                f"The order state with the exchange is unknown — do NOT retry. "
                f"Full response: {res_d}"
            )

        self.invalidate_balance_cache()
        return OrderUpdate(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            remaining_qty=order.quantity,
            filled_qty=0.0,
            timestamp=datetime.now(timezone.utc),
            metadata={"lot_size": lot_size},
        )

    async def cancel_order(self, client_order_id: str, broker_order_id: Optional[str] = None) -> OrderUpdate:
        await rate_limiter.acquire("dhan")
        if self._client and hasattr(self._client, "cancel_order") and broker_order_id:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._client.cancel_order, broker_order_id)
            if isinstance(res, dict) and res.get("status", "").lower() == "failure":
                raise RuntimeError(f"Dhan cancel failed: {res.get('remarks') or res}")

        self.invalidate_balance_cache()
        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.CANCELLED,
            timestamp=datetime.now(timezone.utc),
        )

    async def modify_order(
        self,
        client_order_id: str,
        broker_order_id: Optional[str] = None,
        price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> OrderUpdate:
        await rate_limiter.acquire("dhan")
        if self._client and hasattr(self._client, "modify_order") and broker_order_id:
            # Callers count quantity in lots, but Dhan counts units, and this method
            # doesn't know the lot size. Sending lots would resize the order wrongly.
            if quantity is not None:
                raise RuntimeError(
                    "Dhan modify: quantity changes are not supported (lots vs units); "
                    "cancel and re-place the order instead"
                )
            loop = asyncio.get_running_loop()
            # dhanhq.modify_order needs every field as an argument, so carry over
            # the ones we aren't changing from the order as Dhan currently holds it.
            current = await loop.run_in_executor(None, self._client.get_order_by_id, broker_order_id)
            _raw = current.get("data") if isinstance(current, dict) else None
            if isinstance(_raw, list):
                _raw = _raw[0] if _raw else None
            if not isinstance(_raw, dict) or not _raw.get("orderType"):
                raise RuntimeError(f"Dhan modify: could not read order {broker_order_id}: {current}")
            res = await loop.run_in_executor(None, functools.partial(
                self._client.modify_order,
                order_id=broker_order_id,
                order_type=_raw["orderType"],
                leg_name=_raw.get("legName") or "",
                quantity=int(_raw.get("quantity") or 0),
                price=price if price is not None else float(_raw.get("price") or 0.0),
                trigger_price=float(_raw.get("triggerPrice") or 0.0),
                disclosed_quantity=int(_raw.get("disclosedQuantity") or 0),
                validity=_raw.get("validity") or "DAY",
            ))
            if isinstance(res, dict) and res.get("status", "").lower() == "failure":
                raise RuntimeError(f"Dhan modify failed: {res.get('remarks') or res}")

        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            remaining_qty=quantity or 0.0,
            avg_fill_price=price or 0.0,
            timestamp=datetime.now(timezone.utc),
        )

    def _map_dhan_status(self, raw_status: str) -> OrderState:
        s = raw_status.strip().upper()
        if s in ("TRANSIT", "PENDING"):
            return OrderState.PENDING
        if s in ("CONFIRMED", "OPEN"):
            return OrderState.OPEN
        if s in ("PART_TRADED", "PARTIALLY_FILLED"):
            return OrderState.PARTIALLY_FILLED
        if s in ("TRADED", "FILLED"):
            return OrderState.FILLED
        if s in ("REJECTED",):
            return OrderState.REJECTED
        if s in ("CANCELLED", "CANCELED"):
            return OrderState.CANCELLED
        if s in ("EXPIRED",):
            return OrderState.EXPIRED
        return OrderState.OPEN

    async def get_order_status(
        self, client_order_id: str, broker_order_id: Optional[str] = None
    ) -> Optional[OrderUpdate]:
        await rate_limiter.acquire("dhan")
        if self._client and hasattr(self._client, "get_order_by_id") and broker_order_id:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._client.get_order_by_id, broker_order_id)
            _raw = res.get("data") if isinstance(res, dict) else res
            data = _raw if isinstance(_raw, dict) else {}
            raw_status = data.get("orderStatus", "OPEN")
            state = self._map_dhan_status(raw_status)
            filled = float(data.get("filledQty", 0.0))
            total_qty = float(data.get("quantity", 0.0))
            return OrderUpdate(
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                state=state,
                filled_qty=filled,
                remaining_qty=max(0.0, total_qty - filled),
                avg_fill_price=float(data.get("avgTradedPrice", 0.0)),
                timestamp=datetime.now(timezone.utc),
            )

        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            timestamp=datetime.now(timezone.utc),
        )

    async def get_open_orders(self) -> List[OrderUpdate]:
        await rate_limiter.acquire("dhan")
        if self._client and hasattr(self._client, "get_order_list"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._client.get_order_list)
            data_list = res.get("data", []) if isinstance(res, dict) else []
            updates = []
            for item in data_list:
                status = self._map_dhan_status(item.get("orderStatus", "OPEN"))
                b_id = str(item.get("orderId"))
                c_id = str(item.get("correlationId") or "")
                c_id = self._client_ids_by_tag.get(c_id, c_id)
                filled = float(item.get("filledQty", 0.0))
                total_qty = float(item.get("quantity", 0.0))
                updates.append(
                    OrderUpdate(
                        client_order_id=c_id,
                        broker_order_id=b_id,
                        state=status,
                        filled_qty=filled,
                        remaining_qty=max(0.0, total_qty - filled),
                        avg_fill_price=float(item.get("avgTradedPrice", 0.0)),
                        metadata=item,
                    )
                )
            return updates
        return []