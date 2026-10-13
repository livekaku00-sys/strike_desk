import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from config.settings import FyersSettings, generate_totp
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
    from fyers_apiv3 import fyersModel
    from fyers_apiv3.FyersWebsocket import data_ws
    _FYERS_AVAILABLE = True
except ImportError:
    _FYERS_AVAILABLE = False
    fyersModel = None
    data_ws = None

logger = logging.getLogger(__name__)

_SDK_MISSING_MSG = (
    "fyers_apiv3 SDK is not installed (ModuleNotFoundError) — refusing to authenticate "
    "or connect. Install it with `pip install fyers-apiv3`."
)
if not _FYERS_AVAILABLE:
    logger.critical("[Fyers] %s", _SDK_MISSING_MSG)

_FYERS_FO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
_IST_TZ = timezone(timedelta(hours=5, minutes=30))

# fyers-apiv3 takes data_type as a plain string; there are no DepthUpdate /
# SymbolUpdate class attributes. Depth is also unsupported on indices, so
# SymbolUpdate is the only valid type for the core index feed.
_FYERS_DATA_TYPE = "SymbolUpdate"

try:
    from services.core_symbols import CORE_SYMBOLS as _CORE_SYMS, FYERS_SYMBOL_TO_NAME as _FYERS_SYM_TO_NAME
    _FYERS_SYMBOL_MAP: Dict[str, str] = {k: v["fyers_symbol"] for k, v in _CORE_SYMS.items()}
except Exception:
    # Fyers rejects index names containing spaces ("Please provide a valid
    # symbol", code -300) — the contracted forms below are the accepted ones.
    _FYERS_SYMBOL_MAP = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX",
    }
    _FYERS_SYM_TO_NAME = {v: k for k, v in _FYERS_SYMBOL_MAP.items()}


class FyersAdapter(BaseAdapter):
    def __init__(self, settings: Optional[FyersSettings] = None, balance_ttl: float = 60.0) -> None:
        super().__init__(broker_name="Fyers", balance_ttl=balance_ttl)
        self.settings = settings
        self._fyers_model = None
        self._data_socket = None
        self._on_tick_callback: Optional[Callable[[Tick], None]] = None
        # Fyers addresses instruments by an exchange-prefixed symbol
        # (NSE:RELIANCE-EQ, NSE:NIFTY26SEP24050CE), which is its own format.
        self._equity_symbols: Dict[str, str] = {}      # canonical -> NSE:XXX-EQ
        self._fno_index: Dict[tuple, Dict[str, Any]] = {}
        self._fno_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._fno_loaded_at: Optional[datetime] = None

        self.ws_handler = WebSocketHandler(
            broker_name="Fyers",
            connect_coro=self._ws_connect,
            disconnect_coro=self._ws_disconnect,
            send_ping_coro=self._ws_send_ping,
            resubscribe_coro=self._ws_resubscribe,
        )

    def _to_fyers_symbols(self, symbols) -> List[str]:
        """Canonical symbols → Fyers symbols, dropping any that do not map."""
        from utils.symbol_aliases import canonical_symbol
        out, unmapped = [], []
        for raw in symbols or []:
            sym = canonical_symbol(str(raw))
            fy = _FYERS_SYMBOL_MAP.get(sym) or self._equity_symbols.get(sym)
            if fy:
                out.append(fy)
            else:
                unmapped.append(sym)
        if unmapped:
            logger.warning("[Fyers] %d symbols have no Fyers mapping and were not "
                           "subscribed: %s", len(unmapped), ", ".join(unmapped[:10]))
        return out

    def register_equity_instruments(self, instruments: list) -> None:
        """Maps canonical symbols to Fyers' exchange-prefixed cash symbols."""
        for inst in instruments:
            sym = getattr(inst, "symbol", None)
            if not sym:
                continue
            sym = str(sym).strip().upper()
            if sym in _FYERS_SYMBOL_MAP:
                continue  # core index already mapped
            raw_exch = getattr(inst, "exchange", None)
            exch = str(getattr(raw_exch, "value", raw_exch) or "NSE").upper()
            broker_sym = str(getattr(inst, "broker_symbol", "") or "").strip().upper()
            # Prefer the master's own spelling; fall back to Fyers' documented form.
            self._equity_symbols[sym] = (
                broker_sym if broker_sym.startswith(("NSE:", "BSE:")) else f"{exch}:{sym}-EQ"
            )
        logger.info("[Fyers] Registered %d equity symbols", len(self._equity_symbols))

    async def load_fno_master(self, underlyings: Optional[List[str]] = None) -> int:
        """
        Loads Fyers' F&O contracts.

        Fyers publishes its own symbol per contract (NSE:NIFTY26SEP24050CE). That
        string is read from the master rather than constructed, so a change in its
        spelling cannot silently produce an address that does not exist.
        """
        import csv as _csv
        import io as _io
        from services.broker_discovery.universe import download_text

        wanted = {u.strip().upper() for u in (underlyings or []) if u} or None
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, download_text, _FYERS_FO_MASTER_URL)
        if not text:
            logger.error("[Fyers] F&O master download failed — option orders will be refused")
            return 0

        index: Dict[tuple, Dict[str, Any]] = {}
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for row in _csv.reader(_io.StringIO(text)):
            try:
                if len(row) < 17:
                    continue
                opt_type = str(row[16]).strip().upper()
                if opt_type not in ("CE", "PE"):
                    continue          # futures rows carry no option type
                name = str(row[13]).strip().upper()
                if wanted is not None and name not in wanted:
                    continue
                fy_symbol = str(row[9]).strip().upper()
                if not fy_symbol:
                    continue
                strike = float(row[15])           # Fyers publishes strike in rupees
                if strike <= 0:
                    continue
                expiry = datetime.fromtimestamp(int(row[8]), _IST_TZ).date()

                entry = {
                    "symbol": fy_symbol,
                    "fytoken": str(row[0]).strip(),
                    "lotsize": int(float(row[3] or 0)),
                    "underlying": name,
                    "expiry": expiry.isoformat(),
                    "strike": strike,
                    "option_type": opt_type,
                }
                index[(name, expiry.isoformat(), round(strike, 2), opt_type)] = entry
                by_symbol[fy_symbol] = entry
            except (ValueError, TypeError, IndexError):
                continue

        self._fno_index = index
        self._fno_by_symbol = by_symbol
        self._fno_loaded_at = datetime.now(timezone.utc)
        logger.info(
            "[Fyers] F&O master loaded: %d option contracts across %d underlyings",
            len(index), len({k[0] for k in index}),
        )
        return len(index)

    def resolve_fno_contract(
        self, underlying: str, expiry: str, strike: float, option_type: str
    ) -> Optional[Dict[str, Any]]:
        if not self._fno_index:
            return None
        key = (str(underlying).strip().upper(), str(expiry), round(float(strike), 2),
               str(option_type).strip().upper())
        hit = self._fno_index.get(key)
        if hit:
            return hit
        try:
            target = date.fromisoformat(str(expiry))
        except (ValueError, TypeError):
            return None
        best, best_gap = None, None
        for (u, e, s_, t), entry in self._fno_index.items():
            if u != key[0] or t != key[3] or abs(s_ - key[2]) > 0.01:
                continue
            gap = abs((date.fromisoformat(e) - target).days)
            if gap <= 4 and (best_gap is None or gap < best_gap):
                best, best_gap = entry, gap
        return best

    def resolve_order_instrument(self, order: Order) -> Dict[str, str]:
        """
        Maps a canonical symbol to Fyers' own symbol.

        Reads only Fyers' maps. Its symbols are exchange-prefixed and unlike any
        other broker's, so passing a bare canonical symbol addresses nothing.
        Unresolvable symbols are refused rather than sent as a raw string.
        """
        from utils.symbol_aliases import canonical_symbol
        sym = canonical_symbol(order.symbol or "")
        meta = order.metadata or {}

        explicit = meta.get("fyers_symbol")
        if explicit:
            return {"symbol": str(explicit).upper()}

        if meta.get("option_type") and meta.get("selected_strike") and meta.get("selected_expiry"):
            hit = self.resolve_fno_contract(
                underlying=meta.get("underlying") or sym,
                expiry=meta["selected_expiry"],
                strike=float(meta["selected_strike"]),
                option_type=meta["option_type"],
            )
            if hit:
                return {"symbol": hit["symbol"]}

        direct = self._fno_by_symbol.get(sym) or self._fno_by_symbol.get(f"NSE:{sym}")
        if direct:
            return {"symbol": direct["symbol"]}

        if sym in _FYERS_SYMBOL_MAP:
            return {"symbol": _FYERS_SYMBOL_MAP[sym]}

        if sym in self._equity_symbols:
            return {"symbol": self._equity_symbols[sym]}

        raise DataError(
            f"Fyers has no symbol mapping for '{sym}'. Refusing to place the order "
            f"rather than sending a string Fyers cannot address.",
            broker="fyers",
        )

    def _load_settings(self) -> FyersSettings:
        if self.settings is None:
            self.settings = FyersSettings.from_env()
        return self.settings

    def generate_totp(self) -> str:
        cfg = self._load_settings()
        return generate_totp(cfg.totp_secret)

    def get_auth_status(self) -> str:
        return auth_manager.get_auth_status("fyers")

    def get_connection_state(self) -> str:
        return self.ws_handler.get_connection_state()

    def set_tick_callback(self, callback: Callable[[Tick], None]) -> None:
        self._on_tick_callback = callback

    def handle_raw_tick(self, raw_data: Dict[str, Any]) -> Optional[Tick]:
        try:
            # Translate Fyers format symbol ("NSE:NIFTY50-INDEX") → short name ("NIFTY")
            fyers_sym = str(raw_data.get("symbol") or "")
            if fyers_sym in _FYERS_SYM_TO_NAME:
                raw_data = dict(raw_data)
                raw_data["symbol"] = _FYERS_SYM_TO_NAME[fyers_sym]
            tick = TickNormalizer.normalize_fyers(raw_data)
            self.ws_handler.health_monitor.record_tick()
            if self._on_tick_callback:
                self._on_tick_callback(tick)
            return tick
        except Exception as exc:
            self.logger.error("Fyers raw tick error: %s", exc)
            return None

    # Per-call HTTP timeout (seconds). Separate from AuthManager's retry timeout so
    # a hung Fyers API call fails fast rather than waiting the full 30s per attempt.
    _HTTP_TIMEOUT = 10.0

    async def _perform_auth(self) -> bool:
        t0 = time.monotonic()
        if not (_FYERS_AVAILABLE and fyersModel):
            raise AuthenticationError(_SDK_MISSING_MSG, broker="fyers")
        await rate_limiter.acquire("fyers")
        cfg = self._load_settings()

        # FYERS_ACCESS_TOKEN (written by the UI re-auth) is the only source. token_store
        # caches our last login, so it must not shadow a newly pasted token, and there
        # is no TOTP login to fall back on — a fabricated token can never authenticate.
        token = (cfg.access_token or "").strip()
        if ":" in token:
            # Some Fyers flows show "APP_ID:TOKEN"; the REST client wants the bare token.
            token = token.split(":", 1)[1].strip()
        if not token:
            raise AuthenticationError(
                "FYERS_ACCESS_TOKEN is not set — paste a token from myapi.fyers.in "
                "on the Broker Setup page.",
                broker="fyers",
            )

        fyers = fyersModel.FyersModel(client_id=cfg.client_id, is_async=False, token=token, log_path="")
        loop = asyncio.get_running_loop()
        logger.info(
            "[Fyers] Starting auth — calling get_profile (%.2fs since attempt start)",
            time.monotonic() - t0,
        )
        try:
            profile = await asyncio.wait_for(
                loop.run_in_executor(None, fyers.get_profile),
                timeout=self._HTTP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.error(
                "[Fyers] get_profile timed out after %.1fs (HTTP_TIMEOUT=%.1fs) — "
                "broker API unreachable or slow; failing fast",
                elapsed, self._HTTP_TIMEOUT,
            )
            raise AuthenticationError(
                f"Fyers get_profile timed out after {self._HTTP_TIMEOUT:.0f}s",
                broker="fyers",
            )
        if isinstance(profile, dict) and profile.get("s") != "ok":
            raise AuthenticationError(f"Fyers auth failed: {profile.get('message')}", broker="fyers")
        self._fyers_model = fyers

        token_store.store_token(
            broker="fyers",
            token=token,
            expires_in_seconds=86400,
            metadata={"client_id": cfg.client_id},
        )
        self.is_authenticated = True
        return True

    async def authenticate(self) -> bool:
        if not _FYERS_AVAILABLE:
            # Not retryable — skip auth_manager's backoff loop.
            logger.critical("[Fyers] %s Broker marked DISCONNECTED.", _SDK_MISSING_MSG)
            self._fyers_model = None
            self._data_socket = None
            self.is_authenticated = False
            self.is_connected = False
            return False
        success = await auth_manager.execute_with_retry(broker="fyers", auth_coro_func=self._perform_auth)
        self.is_authenticated = success
        return success

    async def _ws_connect(self) -> None:
        if not (_FYERS_AVAILABLE and data_ws):
            # WebSocketHandler's own AuthenticationError (not utils.exceptions') is what
            # moves it to DISCONNECTED instead of its reconnect loop — retrying cannot
            # install the SDK.
            logger.critical("[Fyers WS] %s No market feed.", _SDK_MISSING_MSG)
            raise _WSAuthError(_SDK_MISSING_MSG)
        token = token_store.get_token("fyers")
        if not token:
            raise ConnectionError("No access token for Fyers WS.", broker="fyers")
        import threading
        cfg = self._load_settings()
        adapter_ref = self

        def _on_message(msg):
            if not msg or not isinstance(msg, dict):
                return
            # Control frames (auth ack, subscribe ack, mode change) carry a
            # 'type' and no symbol — they are not ticks.
            if msg.get("s") in ("ok", "error") and not msg.get("symbol"):
                if msg.get("s") == "error":
                    logger.error("[Fyers WS] %s", msg)
                else:
                    logger.info("[Fyers WS] %s", msg.get("message", msg))
                return
            adapter_ref.handle_raw_tick(msg)

        def _on_error(msg):
            logger.error("[Fyers WS] error: %s", msg)

        def _on_close(msg):
            logger.warning("[Fyers WS] closed: %s", msg)

        def _on_connect():
            logger.info("[Fyers WS] connected")

        try:
            # Fyers documents the WS token as "APP_ID:ACCESS_TOKEN". The
            # stored token is the bare JWT (the REST client takes client_id
            # separately), so prefix it here rather than in the token store.
            ws_token = token if ":" in token else f"{cfg.client_id}:{token}"
            socket = data_ws.FyersDataSocket(
                access_token=ws_token,
                log_path="",
                litemode=False,
                write_to_file=False,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
                on_connect=_on_connect,
            )
            # socket.connect() is blocking — run in daemon thread
            t = threading.Thread(target=socket.connect, daemon=True, name="fyers-ws")
            t.start()
            self._data_socket = socket
        except Exception as e:
            raise ConnectionError(f"Fyers WS connect error: {e}", broker="fyers") from e

    async def _ws_disconnect(self) -> None:
        if self._data_socket:
            try:
                if hasattr(self._data_socket, "close_connection"):
                    self._data_socket.close_connection()
            except Exception:
                pass
            finally:
                self._data_socket = None

    async def _ws_send_ping(self) -> None:
        if self._data_socket and hasattr(self._data_socket, "ping"):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._data_socket.ping)
            self.ws_handler.health_monitor.record_heartbeat_received()
        else:
            self.ws_handler.health_monitor.record_heartbeat_received()

    async def _ws_resubscribe(self, subscriptions: Dict[str, str]) -> None:
        if not subscriptions or not self._data_socket:
            return
        fyers_syms = self._to_fyers_symbols(subscriptions.keys())
        if hasattr(self._data_socket, "subscribe"):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._data_socket.subscribe, fyers_syms, _FYERS_DATA_TYPE
            )

    async def connect(self) -> None:
        if not self.is_authenticated or self.get_auth_status() != "authenticated":
            if not await self.authenticate():
                raise ConnectionError("Cannot connect to Fyers: Auth failed.", broker="fyers")
        ws_ok = await self.ws_handler.connect()
        self.is_connected = ws_ok

    async def disconnect(self) -> None:
        await self.ws_handler.disconnect()
        self.is_connected = False
        self.is_authenticated = False
        token_store.clear_token("fyers")

    async def subscribe_ticks(self, symbols: List[str], mode: str = "full") -> None:
        if not symbols:
            return
        self.ws_handler.record_subscription(symbols, mode)
        if self._data_socket and _FYERS_AVAILABLE and data_ws:
            fyers_syms = self._to_fyers_symbols(symbols)
            if hasattr(self._data_socket, "subscribe"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, self._data_socket.subscribe, fyers_syms, _FYERS_DATA_TYPE
                )

    async def unsubscribe_ticks(self, symbols: List[str]) -> None:
        if not symbols:
            return
        self.ws_handler.record_unsubscription(symbols)
        if self._data_socket and _FYERS_AVAILABLE and data_ws:
            fyers_syms = self._to_fyers_symbols(symbols)
            if hasattr(self._data_socket, "unsubscribe"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._data_socket.unsubscribe, fyers_syms)

    async def get_balance(self, force_refresh: bool = False) -> Balance:
        now_mono = time.monotonic()
        if not force_refresh and self._cached_balance is not None:
            if (now_mono - self._balance_cached_at) < self.balance_ttl:
                return self._cached_balance

        await rate_limiter.acquire("fyers")
        avail_cash = 0.0
        used_margin = 0.0
        total_equity = 0.0

        if self._fyers_model and hasattr(self._fyers_model, "funds"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._fyers_model.funds)
            if isinstance(res, dict) and res.get("s") != "ok":
                raise DataError(f"Fyers funds failed: {res.get('message')}", broker="fyers")
            limits = res.get("fund_limit", []) if isinstance(res, dict) else []
            for item in limits:
                fid = item.get("id")
                amt = float(item.get("equityAmount", 0.0))
                if fid == 4 or "available" in str(item.get("title", "")).lower():
                    avail_cash = amt
                elif fid == 2 or "utilized" in str(item.get("title", "")).lower():
                    used_margin = amt
                elif fid == 10 or "total" in str(item.get("title", "")).lower():
                    total_equity = amt
            if total_equity == 0.0:
                total_equity = avail_cash + used_margin

        unrealized_pnl = 0.0
        realized_pnl = 0.0
        try:
            positions = await self.get_positions()
            unrealized_pnl = sum(p.unrealized_pnl for p in positions)
            realized_pnl = sum(p.realized_pnl for p in positions)
        except Exception:
            pass

        bal = Balance(
            broker="fyers",
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
        await rate_limiter.acquire("fyers")
        if self._fyers_model and hasattr(self._fyers_model, "positions"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._fyers_model.positions)
            if isinstance(res, dict) and res.get("s") != "ok":
                raise DataError(f"Fyers positions failed: {res.get('message')}", broker="fyers")
            net_positions = res.get("netPositions", []) if isinstance(res, dict) else []
            positions = []
            for item in net_positions:
                qty = float(item.get("netQty") or 0.0)
                buy_qty = float(item.get("buyQty") or 0.0)
                sell_qty = float(item.get("sellQty") or 0.0)
                buy_avg = float(item.get("buyAvg") or 0.0)
                current_price = float(item.get("ltp") or buy_avg)
                realized_pnl = float(item.get("realized_profit") or 0.0)
                unrealized_pnl = float(item.get("unrealized_profit") or item.get("pl") or 0.0)
                pnl = float(item.get("pl") or (realized_pnl + unrealized_pnl))
                cost = abs(qty * buy_avg)
                pnl_pct = round((pnl / cost * 100.0), 2) if cost > 0 else 0.0
                positions.append(
                    Position(
                        broker="fyers",
                        symbol=str(item.get("symbol") or "UNKNOWN"),
                        exchange="NSE" if "NSE:" in str(item.get("symbol")) else "BSE",
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

    async def get_option_chain(
        self, underlying: str, expiry: Optional[str] = None, force_refresh: bool = False
    ) -> Dict[str, Any]:
        await rate_limiter.acquire("fyers")
        expiry_val = expiry or "CURRENT"

        async def _fetch_raw():
            if self._fyers_model and hasattr(self._fyers_model, "optionchain"):
                loop = asyncio.get_running_loop()
                payload = {"symbol": f"NSE:{underlying.upper()}-INDEX", "strikecount": 30}
                return await loop.run_in_executor(None, self._fyers_model.optionchain, payload)
            return {"data": {"optionsChain": []}}

        chain = await option_chain_fetcher.get_chain(
            broker="fyers",
            underlying=underlying,
            expiry=expiry_val,
            raw_fetch_coro=_fetch_raw,
            force_refresh=force_refresh,
        )
        return chain.to_dict()

    async def place_order(self, order: Order) -> OrderUpdate:
        await rate_limiter.acquire("fyers")
        # Without a session the request cannot reach the exchange. Returning an
        # OrderUpdate here would report a phantom fill for an order that never left
        # the process, so fail loudly instead.
        if not self._fyers_model:
            raise ConnectionError(
                "Fyers adapter is not authenticated — order was not sent to the broker",
                broker="fyers",
            )

        instrument = self.resolve_order_instrument(order)
        logger.info(
            "[Fyers] Routing %s %s x%s → symbol=%s",
            order.side.value, order.symbol, order.quantity, instrument["symbol"],
        )

        # place_order must exist on the model. If it doesn't (SDK version mismatch
        # or a stale mock), fail hard rather than silently returning a local UUID
        # which would record a phantom OPEN order that never reached the exchange.
        if not hasattr(self._fyers_model, "place_order"):
            raise ConnectionError(
                "Fyers model has no place_order method — SDK version mismatch or model not initialised",
                broker="fyers",
            )

        loop = asyncio.get_running_loop()
        side_num = 1 if order.side == OrderSide.BUY else -1
        type_num = 1 if order.order_type == OrderType.LIMIT else 2
        # Fyers expects total contracts (lots × lot_size) for FNO orders.
        lot_size = 1
        sym_upper = order.symbol.upper()
        meta = order.metadata or {}
        # Require a digit before CE/PE to avoid matching tickers like RELIANCE (ends in CE).
        _is_opt_sym = bool(re.search(r"\d(?:CE|PE)$", sym_upper))
        is_fno = (
            meta.get("option_type")
            or meta.get("underlying")
            or _is_opt_sym
            or "FUT" in sym_upper
        )
        if is_fno:
            meta_underlying = meta.get("underlying", "")
            if not meta_underlying:
                from services.core_symbols import resolve_fut_underlying, resolve_opt_underlying
                if "FUT" in sym_upper:
                    meta_underlying = resolve_fut_underlying(sym_upper)
                elif _is_opt_sym:
                    meta_underlying = resolve_opt_underlying(sym_upper)
            if meta_underlying:
                underlying = meta_underlying.upper()
                try:
                    from services.instrument_registry import instrument_registry
                    from models.instrument import Exchange as _Exchange
                    from services.core_symbols import CORE_SYMBOLS, STOCK_FUTURES_LOT_SIZES
                    inst = await instrument_registry.get(_Exchange.NSE, underlying + "-FUT")
                    if inst is not None:
                        lot_size = inst.lot_size
                    elif underlying in CORE_SYMBOLS:
                        lot_size = CORE_SYMBOLS[underlying]["lot_size"]
                    elif underlying in STOCK_FUTURES_LOT_SIZES:
                        lot_size = STOCK_FUTURES_LOT_SIZES[underlying]
                except Exception:
                    pass
        logger.info("[Fyers] lot_size=%d for %s", lot_size, order.symbol)
        data = {
            "symbol": instrument["symbol"],
            "qty": int(order.quantity) * lot_size,
            "type": type_num,
            "side": side_num,
            "productType": "INTRADAY" if order.product_type == ProductType.INTRADAY else "CNC",
            "limitPrice": order.price,
            "stopPrice": order.trigger_price,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        res = await loop.run_in_executor(None, self._fyers_model.place_order, data)
        logger.info("[Fyers] place_order raw response: %s", res)

        res_d = res if isinstance(res, dict) else {}
        if isinstance(res, dict) and res_d.get("s") != "ok":
            raise RuntimeError(f"Fyers order failed: {res_d.get('message') or res_d.get('code')}")

        broker_order_id = str(res_d.get("id") or "").strip()
        if not broker_order_id:
            raise RuntimeError(
                f"Fyers place_order returned s='ok' but no id was found. "
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
        )

    async def cancel_order(self, client_order_id: str, broker_order_id: Optional[str] = None) -> OrderUpdate:
        await rate_limiter.acquire("fyers")
        if self._fyers_model and hasattr(self._fyers_model, "cancel_order") and broker_order_id:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._fyers_model.cancel_order, {"id": broker_order_id})
            if isinstance(res, dict) and res.get("s") != "ok":
                raise RuntimeError(f"Fyers cancel failed: {res.get('message')}")

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
        await rate_limiter.acquire("fyers")
        if self._fyers_model and hasattr(self._fyers_model, "modify_order") and broker_order_id:
            loop = asyncio.get_running_loop()
            payload: Dict[str, Any] = {"id": broker_order_id}
            if price is not None:
                payload["limitPrice"] = price
            if quantity is not None:
                payload["qty"] = int(quantity)
            res = await loop.run_in_executor(None, self._fyers_model.modify_order, payload)
            if isinstance(res, dict) and res.get("s") != "ok":
                raise RuntimeError(f"Fyers modify failed: {res.get('message')}")

        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            remaining_qty=quantity or 0.0,
            avg_fill_price=price or 0.0,
            timestamp=datetime.now(timezone.utc),
        )

    def _map_fyers_status(self, raw_status: Any) -> OrderState:
        s = str(raw_status).strip().lower()
        if s in ("2", "traded", "filled"):
            return OrderState.FILLED
        if s in ("1", "cancelled", "canceled"):
            return OrderState.CANCELLED
        if s in ("5", "rejected"):
            return OrderState.REJECTED
        if s in ("6", "pending", "open"):
            return OrderState.OPEN
        if s in ("4", "transit"):
            return OrderState.PENDING
        if "partially" in s:
            return OrderState.PARTIALLY_FILLED
        return OrderState.OPEN

    async def get_order_status(
        self, client_order_id: str, broker_order_id: Optional[str] = None
    ) -> Optional[OrderUpdate]:
        await rate_limiter.acquire("fyers")
        if self._fyers_model and hasattr(self._fyers_model, "orderbook"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._fyers_model.orderbook)
            orders = res.get("orderBook", []) if isinstance(res, dict) else []
            for item in orders:
                if str(item.get("id")) == str(broker_order_id):
                    st = self._map_fyers_status(item.get("status"))
                    filled = float(item.get("filledQty", 0.0))
                    total_qty = float(item.get("qty", 0.0))
                    return OrderUpdate(
                        client_order_id=client_order_id,
                        broker_order_id=broker_order_id,
                        state=st,
                        filled_qty=filled,
                        remaining_qty=max(0.0, total_qty - filled),
                        avg_fill_price=float(item.get("tradedPrice", 0.0)),
                        timestamp=datetime.now(timezone.utc),
                    )

        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            timestamp=datetime.now(timezone.utc),
        )

    async def get_open_orders(self) -> List[OrderUpdate]:
        await rate_limiter.acquire("fyers")
        if self._fyers_model and hasattr(self._fyers_model, "orderbook"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._fyers_model.orderbook)
            orders = res.get("orderBook", []) if isinstance(res, dict) else []
            updates = []
            for item in orders:
                st = self._map_fyers_status(item.get("status"))
                b_id = str(item.get("id"))
                filled = float(item.get("filledQty", 0.0))
                total_qty = float(item.get("qty", 0.0))
                updates.append(
                    OrderUpdate(
                        client_order_id="",
                        broker_order_id=b_id,
                        state=st,
                        filled_qty=filled,
                        remaining_qty=max(0.0, total_qty - filled),
                        avg_fill_price=float(item.get("tradedPrice", 0.0)),
                        metadata=item,
                    )
                )
            return updates
        return []