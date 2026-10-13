import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from config.settings import AngelOneSettings
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

try:
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    _SMARTAPI_AVAILABLE = True
except ImportError:
    _SMARTAPI_AVAILABLE = False
    SmartConnect = None
    SmartWebSocketV2 = None

logger = logging.getLogger(__name__)

_ANGEL_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def _parse_angel_expiry(raw: Any) -> Optional[date]:
    """AngelOne writes contract expiry as DDMMMYYYY, e.g. 29DEC2026."""
    txt = str(raw or "").strip().upper()
    if len(txt) < 9:
        return None
    try:
        return date(int(txt[5:9]), _MONTHS[txt[2:5]], int(txt[:2]))
    except (ValueError, KeyError):
        return None

try:
    from services.core_symbols import CORE_SYMBOLS as _CORE_SYMS, ANGEL_TOKEN_TO_SYMBOL as _ANGEL_ID_TO_SYM
    _ANGEL_TOKEN_MAP: Dict[str, tuple] = {k: (v["angel_token"], v["angel_exchange"]) for k, v in _CORE_SYMS.items()}
except Exception:
    _ANGEL_TOKEN_MAP = {
        # Exchange type 1 = NSE, 3 = BSE. SENSEX is a BSE index; all others are NSE.
        "NIFTY": ("26000", 1), "BANKNIFTY": ("26009", 1), "FINNIFTY": ("26037", 1),
        "MIDCPNIFTY": ("26074", 1), "SENSEX": ("1", 3),
    }
    _ANGEL_ID_TO_SYM = {v[0]: k for k, v in _ANGEL_TOKEN_MAP.items()}


def _build_angel_token_list(
    symbols: List[str],
    equity_map: Optional[Dict[str, tuple]] = None,
) -> List[Dict[str, Any]]:
    """Group symbol names into AngelOne {exchangeType, tokens} format."""
    from collections import defaultdict
    groups: Dict[int, List[str]] = defaultdict(list)
    for s in symbols:
        if s in _ANGEL_TOKEN_MAP:
            token, ex_type = _ANGEL_TOKEN_MAP[s]
            groups[ex_type].append(token)
        elif equity_map and s in equity_map:
            token, ex_type = equity_map[s]
            groups[ex_type].append(token)
    return [{"exchangeType": ex, "tokens": toks} for ex, toks in groups.items()]


class AngelOneAdapter(BaseAdapter):
    def __init__(self, settings: Optional[AngelOneSettings] = None, balance_ttl: float = 60.0) -> None:
        super().__init__(broker_name="AngelOne", balance_ttl=balance_ttl)
        self.settings = settings
        self._smart_api = None
        self._ws_client = None
        self._access_token: str = ""
        self._feed_token: Optional[str] = None
        self._on_tick_callback: Optional[Callable[[Tick], None]] = None
        self._equity_token_map: Dict[str, tuple] = {}  # symbol → (token, exchange_type)
        self._equity_id_to_sym: Dict[str, str] = {}    # token → symbol
        # F&O contracts, keyed structurally rather than by name: AngelOne writes a
        # contract as NIFTY29DEC2630000PE (day + YY) while the canonical symbol is
        # NIFTY26DEC30000PE (no day), so the two strings never match.
        self._fno_index: Dict[tuple, Dict[str, Any]] = {}
        self._fno_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._futures_index: Dict[str, Dict[str, Any]] = {}      # underlying → front-month contract
        self._futures_by_symbol: Dict[str, Dict[str, Any]] = {}  # tradingsymbol → contract
        self._fno_loaded_at: Optional[datetime] = None

        self.ws_handler = WebSocketHandler(
            broker_name="AngelOne",
            connect_coro=self._ws_connect,
            disconnect_coro=self._ws_disconnect,
            send_ping_coro=self._ws_send_ping,
            resubscribe_coro=self._ws_resubscribe,
        )

    def _load_settings(self) -> AngelOneSettings:
        if self.settings is None:
            self.settings = AngelOneSettings.from_env()
        return self.settings

    def get_auth_status(self) -> str:
        return auth_manager.get_auth_status("angelone")

    def get_connection_state(self) -> str:
        return self.ws_handler.get_connection_state()

    def set_tick_callback(self, callback: Callable[[Tick], None]) -> None:
        self._on_tick_callback = callback

    async def load_fno_master(self, underlyings: Optional[List[str]] = None) -> int:
        """
        Loads AngelOne's F&O contracts so option orders can be addressed.

        Equity tokens alone are not enough: an option is a distinct instrument with
        its own token, and without this every option order was refused. Filtered to
        the underlyings actually traded, since the full master is ~68k option rows.
        """
        from services.broker_discovery.universe import download_json

        wanted = {u.strip().upper() for u in (underlyings or []) if u} or None
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, download_json, _ANGEL_MASTER_URL)
        if not rows:
            logger.error("[AngelOne] F&O master download failed — option orders will be refused")
            return 0

        index: Dict[tuple, Dict[str, Any]] = {}
        by_symbol: Dict[str, Dict[str, Any]] = {}
        futures_candidates: Dict[str, list] = {}  # underlying → list of expiry rows

        for row in rows:
            try:
                itype = str(row.get("instrumenttype") or "").upper()
                name = str(row.get("name") or "").strip().upper()
                if wanted is not None and name not in wanted:
                    continue
                tsym = str(row.get("symbol") or "").strip().upper()
                token = str(row.get("token") or "").strip()
                if not name or not tsym or not token:
                    continue
                expiry = _parse_angel_expiry(row.get("expiry"))
                if expiry is None:
                    continue
                lot = int(float(row.get("lotsize") or 0))
                exch_seg = str(row.get("exch_seg") or "NFO").upper()

                if itype.startswith("OPT"):
                    # AngelOne publishes strike in paise.
                    strike = float(row.get("strike") or 0.0) / 100.0
                    if strike <= 0:
                        continue
                    opt_type = "CE" if tsym.endswith("CE") else "PE" if tsym.endswith("PE") else None
                    if opt_type is None:
                        continue
                    entry = {
                        "symboltoken": token,
                        "tradingsymbol": tsym,
                        "exchange": exch_seg,
                        "lotsize": lot,
                        "underlying": name,
                        "expiry": expiry.isoformat(),
                        "strike": strike,
                        "option_type": opt_type,
                    }
                    index[(name, expiry.isoformat(), round(strike, 2), opt_type)] = entry
                    by_symbol[tsym] = entry

                elif itype.startswith("FUT"):
                    # Collect all expiry rows; front-month is selected after the loop.
                    futures_candidates.setdefault(name, []).append({
                        "symboltoken": token,
                        "tradingsymbol": tsym,
                        "exchange": exch_seg,
                        "lotsize": lot,
                        "underlying": name,
                        "expiry": expiry.isoformat(),
                        "_expiry_date": expiry,  # temporary sort key
                    })

            except Exception:
                continue

        # Select front-month futures per underlying: nearest expiry >= today.
        today = date.today()
        futures_index: Dict[str, Dict[str, Any]] = {}
        futures_by_symbol: Dict[str, Dict[str, Any]] = {}
        for underlying, candidates in futures_candidates.items():
            valid = sorted(
                [c for c in candidates if c["_expiry_date"] >= today],
                key=lambda c: c["_expiry_date"],
            )
            pool = valid or sorted(candidates, key=lambda c: c["_expiry_date"], reverse=True)
            entry = {k: v for k, v in pool[0].items() if k != "_expiry_date"}
            futures_index[underlying] = entry
            futures_by_symbol[entry["tradingsymbol"]] = entry

        self._fno_index = index
        self._fno_by_symbol = by_symbol
        self._futures_index = futures_index
        self._futures_by_symbol = futures_by_symbol
        self._fno_loaded_at = datetime.now(timezone.utc)
        logger.info(
            "[AngelOne] F&O master loaded: %d option contracts across %d underlyings, %d futures underlyings",
            len(index), len({k[0] for k in index}), len(futures_index),
        )
        return len(index) + len(futures_index)

    def resolve_fno_contract(
        self, underlying: str, expiry: str, strike: float, option_type: str
    ) -> Optional[Dict[str, Any]]:
        """Structural lookup — the only reliable way to match a contract across brokers."""
        if not self._fno_index:
            return None
        key = (str(underlying).strip().upper(), str(expiry), round(float(strike), 2),
               str(option_type).strip().upper())
        hit = self._fno_index.get(key)
        if hit:
            return hit
        # Expiry dates can drift by a day between sources; accept the nearest
        # contract for the same underlying/strike/type within a few days.
        try:
            target = date.fromisoformat(str(expiry))
        except (ValueError, TypeError):
            return None
        best, best_gap = None, None
        for (u, e, s, t), entry in self._fno_index.items():
            if u != key[0] or t != key[3] or abs(s - key[2]) > 0.01:
                continue
            gap = abs((date.fromisoformat(e) - target).days)
            if gap <= 4 and (best_gap is None or gap < best_gap):
                best, best_gap = entry, gap
        return best

    def register_equity_instruments(self, instruments: list) -> None:
        """Populate equity symbol ↔ AngelOne token maps from discovered instruments."""
        _EXCHANGE_TYPE = {"NSE": 1, "BSE": 3}
        for inst in instruments:
            sym = getattr(inst, "symbol", None)
            tok = getattr(inst, "token", None)
            if not sym or not tok:
                continue
            if sym in _ANGEL_TOKEN_MAP:
                continue  # core symbol already mapped
            # exchange is a (str, Enum); str() yields "Exchange.NSE", so reading it
            # raw sent every BSE instrument to the NSE exchange type.
            raw_exch = getattr(inst, "exchange", None)
            exch = str(getattr(raw_exch, "value", raw_exch) or "NSE").upper()
            ex_type = _EXCHANGE_TYPE.get(exch, 1)
            self._equity_token_map[sym] = (str(tok), ex_type)
            self._equity_id_to_sym[str(tok)] = sym
        logger.info("[AngelOne] Registered %d equity instruments for tick subscription", len(self._equity_token_map))

    def handle_raw_tick(self, raw_data: Dict[str, Any], symbol_override: Optional[str] = None) -> Optional[Tick]:
        try:
            if symbol_override is None:
                token = str(raw_data.get("token") or raw_data.get("tk") or "")
                symbol_override = _ANGEL_ID_TO_SYM.get(token) or self._equity_id_to_sym.get(token)
            tick = TickNormalizer.normalize_angelone(raw_data, symbol_override=symbol_override)
            self.ws_handler.health_monitor.record_tick()
            if self._on_tick_callback:
                self._on_tick_callback(tick)
            return tick
        except Exception as exc:
            self.logger.error("AngelOne raw tick error: %s", exc)
            return None

    # Per-call timeout for the broker HTTP request (separate from AuthManager's
    # per-attempt timeout). If the AngelOne API does not respond within this
    # window the executor future is abandoned and a TimeoutError propagates so
    # AuthManager can retry or fail fast instead of hanging indefinitely.
    _HTTP_TIMEOUT = 10.0

    async def _perform_auth(self) -> bool:
        t0 = time.monotonic()
        await rate_limiter.acquire("angelone")
        cfg = self._load_settings()
        if _SMARTAPI_AVAILABLE and SmartConnect:
            totp_code = cfg.current_totp()
            logger.info(
                "[AngelOne] Starting TOTP auth — client_id=%s api_key=%s… (%.2fs since attempt start)",
                cfg.client_id, cfg.api_key[:6] + "***", time.monotonic() - t0,
            )
            smart_api = SmartConnect(api_key=cfg.api_key)
            loop = asyncio.get_running_loop()
            try:
                data = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, smart_api.generateSession, cfg.client_id, cfg.password, totp_code
                    ),
                    timeout=self._HTTP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - t0
                logger.error(
                    "[AngelOne] generateSession timed out after %.1fs (HTTP_TIMEOUT=%.1fs) — "
                    "broker API unreachable or slow; failing fast",
                    elapsed, self._HTTP_TIMEOUT,
                )
                raise AuthenticationError(
                    f"AngelOne generateSession timed out after {self._HTTP_TIMEOUT:.0f}s",
                    broker="angelone",
                )
            logger.debug("[AngelOne] generateSession response: status=%s message=%s",
                         data.get("status") if isinstance(data, dict) else type(data).__name__,
                         data.get("message", "—") if isinstance(data, dict) else str(data)[:120])

            if not isinstance(data, dict) or not data.get("status"):
                msg = data.get("message", "unknown") if isinstance(data, dict) else str(data)
                logger.error("[AngelOne] TOTP auth FAILED — %s | full response: %s",
                             msg, str(data)[:300])
                raise AuthenticationError(f"AngelOne TOTP auth failed: {msg}", broker="angelone")

            jwt_token = data["data"]["jwtToken"]
            smart_api.setAccessToken(jwt_token)
            self._smart_api = smart_api
            self._access_token = jwt_token
            self._feed_token = smart_api.getfeedToken() or jwt_token
            logger.info("[AngelOne] TOTP auth succeeded — fresh JWT and feed token obtained")
        else:
            logger.warning("[AngelOne] SmartAPI not available — skipping live auth")
            self._smart_api = None
            self._access_token = ""
            self._feed_token = ""

        token_store.store_token(
            broker="angelone",
            token=self._access_token,
            expires_in_seconds=86400,
            metadata={"client_id": cfg.client_id, "api_key": cfg.api_key},
        )
        self.is_authenticated = True

        # Keep token_manager in sync so the dashboard status reflects live state.
        try:
            from brokers.token_manager import token_manager as _tm
            import pytz as _pytz
            _IST = _pytz.timezone("Asia/Kolkata")
            _now = datetime.now(_IST)
            _tm._sessions["angelone"].update({
                "is_authenticated": True,
                "status": "ACTIVE",
                "client_id": cfg.client_id,
                "authenticated_at": _now,
                "expires_at": _now + timedelta(hours=24),
                "last_refreshed_at": _now,
                "error_message": None,
            })
        except Exception as _e:
            logger.debug("[AngelOne] token_manager sync skipped: %s", _e)

        return True

    async def authenticate(self) -> bool:
        success = await auth_manager.execute_with_retry(broker="angelone", auth_coro_func=self._perform_auth)
        self.is_authenticated = success
        return success

    async def _ws_connect(self) -> None:
        cfg = self._load_settings()
        if _SMARTAPI_AVAILABLE and SmartWebSocketV2:
            import threading
            try:
                # Use the JWT obtained from generateSession, not a stale env var.
                auth_token = self._access_token
                feed_token = self._feed_token or auth_token
                if not auth_token:
                    raise ConnectionError("AngelOne WS: no auth token — authenticate() must run first", broker="angelone")
                sws = SmartWebSocketV2(
                    auth_token=auth_token,
                    api_key=cfg.api_key,
                    client_code=cfg.client_id,
                    feed_token=feed_token,
                )
                adapter_ref = self

                def _on_data(wsapp, data):
                    if data and isinstance(data, dict):
                        adapter_ref.handle_raw_tick(data)

                def _on_error(wsapp, err, *_args):
                    logger.error("[AngelOne WS] error: %s", err)

                def _on_close(wsapp, code, msg, *_args):
                    logger.warning("[AngelOne WS] closed code=%s msg=%s", code, msg)

                # Wire callbacks before starting connection thread.
                # on_error and _on_close are overridden as instance attrs so
                # SmartWebSocketV2.connect() picks up the correct arity for the
                # newer websocket-client library (on_error(wsapp,err) vs old on_error(self)).
                sws.on_data = _on_data
                sws.on_error = _on_error
                sws._on_close = _on_close
                # sws.connect() is blocking (WebSocketApp.run_forever) — run in daemon thread
                t = threading.Thread(target=sws.connect, daemon=True, name="angel-ws")
                t.start()
                self._ws_client = sws
                # Yield to let the WS handshake complete before subscribe_ticks is called.
                await asyncio.sleep(0.8)
            except Exception as e:
                raise ConnectionError(f"AngelOne WS connect error: {e}", broker="angelone") from e
        else:
            self._ws_client = None

    async def _ws_disconnect(self) -> None:
        if self._ws_client:
            try:
                if hasattr(self._ws_client, "close_connection"):
                    self._ws_client.close_connection()
            except Exception:
                pass
            finally:
                self._ws_client = None

    async def _ws_send_ping(self) -> None:
        if self._ws_client and hasattr(self._ws_client, "ping"):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._ws_client.ping)
            self.ws_handler.health_monitor.record_heartbeat_received()
        else:
            self.ws_handler.health_monitor.record_heartbeat_received()

    async def _ws_resubscribe(self, subscriptions: Dict[str, str]) -> None:
        if not subscriptions or not self._ws_client:
            return
        token_list = _build_angel_token_list(list(subscriptions.keys()), self._equity_token_map)
        if token_list and hasattr(self._ws_client, "subscribe"):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._ws_client.subscribe, "resub_corr", 3, token_list)

    async def connect(self) -> None:
        if not self.is_authenticated or self.get_auth_status() != "authenticated":
            if not await self.authenticate():
                raise ConnectionError("Cannot connect to AngelOne: Auth failed.", broker="angelone")
        ws_ok = await self.ws_handler.connect()
        self.is_connected = ws_ok

    async def disconnect(self) -> None:
        await self.ws_handler.disconnect()
        self.is_connected = False
        self.is_authenticated = False
        token_store.clear_token("angelone")

    async def subscribe_ticks(self, symbols: List[str], mode: str = "full") -> None:
        if not symbols:
            return
        self.ws_handler.record_subscription(symbols, mode)
        if self._ws_client and _SMARTAPI_AVAILABLE and SmartWebSocketV2:
            ws_mode = 3 if mode == "full" else 1
            token_list = _build_angel_token_list(symbols, self._equity_token_map)
            if token_list and hasattr(self._ws_client, "subscribe"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._ws_client.subscribe, "sub_corr", ws_mode, token_list)

    async def unsubscribe_ticks(self, symbols: List[str]) -> None:
        if not symbols:
            return
        self.ws_handler.record_unsubscription(symbols)
        if self._ws_client and _SMARTAPI_AVAILABLE and SmartWebSocketV2:
            token_list = _build_angel_token_list(symbols)
            if token_list and hasattr(self._ws_client, "unsubscribe"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._ws_client.unsubscribe, "unsub_corr", 1, token_list)

    async def get_balance(self, force_refresh: bool = False) -> Balance:
        now_mono = time.monotonic()
        if not force_refresh and self._cached_balance is not None:
            if (now_mono - self._balance_cached_at) < self.balance_ttl:
                return self._cached_balance

        await rate_limiter.acquire("angelone")
        avail_cash = 0.0
        used_margin = 0.0
        total_equity = 0.0

        if self._smart_api and hasattr(self._smart_api, "rmsLimit"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._smart_api.rmsLimit)
            res_d = res if isinstance(res, dict) else {}
            if res_d.get("status") is False or res_d.get("success") is False:
                raise DataError(f"AngelOne rmsLimit failed: {res_d.get('message')}", broker="angelone")
            raw = res_d.get("data")
            data = raw if isinstance(raw, dict) else {}
            avail_cash = float(data.get("net") or data.get("availablecash") or 0.0)
            used_margin = float(data.get("utilisedAmount") or data.get("collateral") or 0.0)
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
            broker="angelone",
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
        await rate_limiter.acquire("angelone")
        if self._smart_api and hasattr(self._smart_api, "position"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._smart_api.position)
            res_d = res if isinstance(res, dict) else {}
            if res_d.get("status") is False or res_d.get("success") is False:
                raise DataError(f"AngelOne position failed: {res_d.get('message')}", broker="angelone")
            raw = res_d.get("data")
            data_list = raw if isinstance(raw, list) else []
            positions = []
            for item in data_list:
                qty = float(item.get("netqty") or 0.0)
                buy_qty = float(item.get("buyqty") or 0.0)
                sell_qty = float(item.get("sellqty") or 0.0)
                avg_price = float(item.get("netprice") or item.get("buyavgprice") or 0.0)
                current_price = float(item.get("ltp") or avg_price)
                realized_pnl = float(item.get("realised") or 0.0)
                unrealized_pnl = float(item.get("unrealised") or 0.0)
                pnl = float(item.get("pnl") or (realized_pnl + unrealized_pnl))
                cost = abs(qty * avg_price)
                pnl_pct = round((pnl / cost * 100.0), 2) if cost > 0 else 0.0
                positions.append(
                    Position(
                        broker="angelone",
                        symbol=str(item.get("tradingsymbol") or item.get("symboltoken") or "UNKNOWN"),
                        exchange=str(item.get("exchange") or "NSE"),
                        product_type=str(item.get("producttype") or "INTRADAY"),
                        qty=qty,
                        buy_qty=buy_qty,
                        sell_qty=sell_qty,
                        avg_price=avg_price,
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
        await rate_limiter.acquire("angelone")
        expiry_val = expiry or "CURRENT"

        async def _fetch_raw():
            if self._smart_api and hasattr(self._smart_api, "getOptionChain"):
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, self._smart_api.getOptionChain, underlying, expiry_val)
            return {"data": []}

        chain = await option_chain_fetcher.get_chain(
            broker="angelone",
            underlying=underlying,
            expiry=expiry_val,
            raw_fetch_coro=_fetch_raw,
            force_refresh=force_refresh,
        )
        return chain.to_dict()

    _EXCHANGE_NAMES = {1: "NSE", 3: "BSE"}

    def resolve_order_instrument(self, order: Order) -> Dict[str, str]:
        """
        Maps a canonical symbol to AngelOne's own identifiers.

        Reads only AngelOne's maps. AngelOne addresses instruments by its own
        numeric symboltoken — NIFTY is 26000 here and 13 at Dhan — so a token taken
        from another broker's map would address a different instrument. Unresolvable
        symbols are refused rather than sent with a guess.
        """
        from utils.symbol_aliases import canonical_symbol
        sym = canonical_symbol(order.symbol or "")

        meta = order.metadata or {}

        explicit = meta.get("angel_symboltoken")
        if explicit:
            return {
                "symboltoken": str(explicit),
                "tradingsymbol": str(meta.get("angel_tradingsymbol") or sym),
                "exchange": str(meta.get("angel_exchange") or "NFO"),
            }

        # Option contracts are matched structurally. The signal already carries the
        # underlying, expiry, strike and type, which identify the contract far more
        # reliably than reconciling two brokers' contract-name spellings.
        if meta.get("option_type") and meta.get("selected_strike") and meta.get("selected_expiry"):
            hit = self.resolve_fno_contract(
                underlying=meta.get("underlying") or sym,
                expiry=meta["selected_expiry"],
                strike=float(meta["selected_strike"]),
                option_type=meta["option_type"],
            )
            if hit:
                return {
                    "symboltoken": hit["symboltoken"],
                    "tradingsymbol": hit["tradingsymbol"],
                    "exchange": hit["exchange"],
                }

        # A contract name that happens to match AngelOne's own spelling.
        direct = self._fno_by_symbol.get(sym)
        if direct:
            return {
                "symboltoken": direct["symboltoken"],
                "tradingsymbol": direct["tradingsymbol"],
                "exchange": direct["exchange"],
            }

        # Futures canonical symbol: "UNDERLYING-FUT" → front-month contract.
        if sym.endswith("-FUT"):
            underlying = sym[:-4]
            hit = self._futures_index.get(underlying)
            if hit:
                return {
                    "symboltoken": hit["symboltoken"],
                    "tradingsymbol": hit["tradingsymbol"],
                    "exchange": hit["exchange"],
                }

        if sym in _ANGEL_TOKEN_MAP:
            token, ex_type = _ANGEL_TOKEN_MAP[sym]
            return {
                "symboltoken": str(token),
                "tradingsymbol": sym,
                "exchange": self._EXCHANGE_NAMES.get(int(ex_type), "NSE"),
            }

        if sym in self._equity_token_map:
            token, ex_type = self._equity_token_map[sym]
            exch = self._EXCHANGE_NAMES.get(int(ex_type), "NSE")
            return {
                "symboltoken": str(token),
                # AngelOne cash scrips carry the -EQ suffix in tradingsymbol.
                "tradingsymbol": f"{sym}-EQ" if exch in ("NSE", "BSE") else sym,
                "exchange": exch,
            }

        raise DataError(
            f"AngelOne has no symboltoken for '{sym}'. Refusing to place the order "
            f"rather than sending a token that would address a different instrument.",
            broker="angelone",
        )

    async def place_order(self, order: Order) -> OrderUpdate:
        from utils.live_order_guard import prohibit_live_order
        prohibit_live_order("angelone.place_order")
        await rate_limiter.acquire("angelone")
        # Without a session the request cannot reach the exchange. Returning an OPEN
        # update here would report a phantom fill for an order that never left the
        # process, so fail loudly instead.
        if not self._smart_api:
            raise ConnectionError(
                "AngelOne adapter is not authenticated — order was not sent to the broker",
                broker="angelone",
            )
        # placeOrder must exist on the session object. If it doesn't (SDK version
        # mismatch or a stale mock), fail hard rather than silently using a local UUID
        # which would record a phantom OPEN order that never reached the exchange.
        if not hasattr(self._smart_api, "placeOrder"):
            raise ConnectionError(
                "AngelOne SmartConnect session has no placeOrder method — "
                "SDK version mismatch or session was not fully initialised",
                broker="angelone",
            )

        instrument = self.resolve_order_instrument(order)
        logger.info(
            "[AngelOne] Routing %s %s x%s → symboltoken=%s tradingsymbol=%s exchange=%s",
            order.side.value, order.symbol, order.quantity,
            instrument["symboltoken"], instrument["tradingsymbol"], instrument["exchange"],
        )

        loop = asyncio.get_running_loop()
        exch = instrument["exchange"]

        # producttype: positional F&O uses CARRYFORWARD on AngelOne — DELIVERY is
        # only valid for CNC cash equities and is rejected outright on NFO/BFO.
        if order.product_type == ProductType.INTRADAY:
            product_type_str = "INTRADAY"
        elif order.product_type == ProductType.CARRYFORWARD or exch in ("NFO", "BFO"):
            product_type_str = "CARRYFORWARD"
        else:
            product_type_str = "DELIVERY"

        # quantity: the Order carries lots; the exchange requires total shares.
        lot_size = 1
        if exch in ("NFO", "BFO"):
            tsym = instrument["tradingsymbol"]
            opt_entry = self._fno_by_symbol.get(tsym)
            fut_entry = self._futures_by_symbol.get(tsym)
            if opt_entry:
                lot_size = max(1, int(opt_entry.get("lotsize") or 1))
            elif fut_entry:
                lot_size = max(1, int(fut_entry.get("lotsize") or 1))

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": instrument["tradingsymbol"],
            "symboltoken": instrument["symboltoken"],
            "transactiontype": "BUY" if order.side == OrderSide.BUY else "SELL",
            "exchange": exch,
            "ordertype": "LIMIT" if order.order_type == OrderType.LIMIT else "MARKET",
            "producttype": product_type_str,
            "duration": "DAY",
            "price": str(order.price),
            "quantity": str(int(order.quantity) * lot_size),
        }
        logger.info("[AngelOne] Placing order — payload: %s", order_params)
        res = await loop.run_in_executor(None, self._smart_api.placeOrder, order_params)

        # Always log the raw response so post-mortems are possible.
        logger.info("[AngelOne] placeOrder raw response: %s", res)

        if res is None:
            raise RuntimeError(
                "AngelOne placeOrder returned None — possible session expiry or network failure"
            )

        res_d = res if isinstance(res, dict) else {}

        # AngelOne uses "status" in some API versions and "success" in others.
        # Treat the response as OK only when one of them is explicitly True (bool).
        # A missing key is also a failure — never treat absence as success.
        status_ok = (res_d.get("status") is True) or (res_d.get("success") is True)
        errorcode = str(res_d.get("errorcode") or res_d.get("errorCode") or "").strip()
        message = str(res_d.get("message") or "").strip()

        if not status_ok or errorcode:
            raise RuntimeError(
                f"AngelOne order error: message='{message}' errorcode='{errorcode}' "
                f"| full response: {res_d}"
            )

        # Extract the broker order ID. AngelOne's SmartAPI returns it as "orderid"
        # inside the "data" dict, but some library versions use "uniqueorderid" at
        # the top level — check both locations.
        # "data" can be an empty string "" on some error paths, so guard the isinstance.
        raw_data = res_d.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        broker_order_id = (
            data.get("orderid")
            or data.get("uniqueorderid")
            or res_d.get("orderid")
            or res_d.get("uniqueorderid")
        )

        if not broker_order_id:
            raise RuntimeError(
                f"AngelOne placeOrder returned status=True but no orderid was found. "
                f"The order state with the exchange is unknown — do NOT retry. "
                f"Full response: {res_d}"
            )

        self.invalidate_balance_cache()
        return OrderUpdate(
            client_order_id=order.client_order_id,
            broker_order_id=str(broker_order_id),
            state=OrderState.OPEN,
            remaining_qty=order.quantity,
            filled_qty=0.0,
            timestamp=datetime.now(timezone.utc),
        )

    async def cancel_order(self, client_order_id: str, broker_order_id: Optional[str] = None) -> OrderUpdate:
        from utils.live_order_guard import prohibit_live_order
        prohibit_live_order("angelone.cancel_order")
        await rate_limiter.acquire("angelone")
        if self._smart_api and hasattr(self._smart_api, "cancelOrder") and broker_order_id:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._smart_api.cancelOrder, broker_order_id, "NORMAL")
            res_d = res if isinstance(res, dict) else {}
            if res_d.get("status") is False or res_d.get("success") is False:
                raise RuntimeError(f"AngelOne cancel failed: {res_d.get('message')}")

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
        from utils.live_order_guard import prohibit_live_order
        prohibit_live_order("angelone.modify_order")
        await rate_limiter.acquire("angelone")
        if self._smart_api and hasattr(self._smart_api, "modifyOrder") and broker_order_id:
            loop = asyncio.get_running_loop()
            params = {
                "orderid": broker_order_id,
                "variety": "NORMAL",
            }
            if price is not None:
                params["price"] = str(price)
            if quantity is not None:
                params["quantity"] = str(int(quantity))
            res = await loop.run_in_executor(None, self._smart_api.modifyOrder, params)
            res_d = res if isinstance(res, dict) else {}
            if res_d.get("status") is False or res_d.get("success") is False:
                raise RuntimeError(f"AngelOne modify failed: {res_d.get('message')}")

        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            remaining_qty=quantity or 0.0,
            avg_fill_price=price or 0.0,
            timestamp=datetime.now(timezone.utc),
        )

    def _map_angel_status(self, raw_status: str) -> OrderState:
        s = raw_status.strip().lower()
        if "complete" in s or "traded" in s:
            return OrderState.FILLED
        if "partially" in s:
            return OrderState.PARTIALLY_FILLED
        if "rejected" in s:
            return OrderState.REJECTED
        if "cancelled" in s or "canceled" in s:
            return OrderState.CANCELLED
        if "open" in s or "put order req received" in s or "validation pending" in s:
            return OrderState.OPEN
        return OrderState.OPEN

    async def get_order_status(
        self, client_order_id: str, broker_order_id: Optional[str] = None
    ) -> Optional[OrderUpdate]:
        await rate_limiter.acquire("angelone")
        if self._smart_api and hasattr(self._smart_api, "orderBook"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._smart_api.orderBook)
            res_d = res if isinstance(res, dict) else {}
            if res_d.get("status") is False or res_d.get("success") is False:
                return None
            raw = res_d.get("data")
            orders = raw if isinstance(raw, list) else []
            for item in orders:
                if str(item.get("orderid")) == str(broker_order_id):
                    st = self._map_angel_status(str(item.get("status", "open")))
                    filled = float(item.get("filledshares", 0.0))
                    total_qty = float(item.get("quantity", 0.0))
                    return OrderUpdate(
                        client_order_id=client_order_id,
                        broker_order_id=broker_order_id,
                        state=st,
                        filled_qty=filled,
                        remaining_qty=max(0.0, total_qty - filled),
                        avg_fill_price=float(item.get("averageprice", 0.0)),
                        timestamp=datetime.now(timezone.utc),
                    )

        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            timestamp=datetime.now(timezone.utc),
        )

    async def get_open_orders(self) -> List[OrderUpdate]:
        await rate_limiter.acquire("angelone")
        if self._smart_api and hasattr(self._smart_api, "orderBook"):
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self._smart_api.orderBook)
            res_d = res if isinstance(res, dict) else {}
            if res_d.get("status") is False or res_d.get("success") is False:
                return []
            raw = res_d.get("data")
            orders = raw if isinstance(raw, list) else []
            updates = []
            for item in orders:
                st = self._map_angel_status(str(item.get("status", "open")))
                b_id = str(item.get("orderid"))
                filled = float(item.get("filledshares", 0.0))
                total_qty = float(item.get("quantity", 0.0))
                updates.append(
                    OrderUpdate(
                        client_order_id="",
                        broker_order_id=b_id,
                        state=st,
                        filled_qty=filled,
                        remaining_qty=max(0.0, total_qty - filled),
                        avg_fill_price=float(item.get("averageprice", 0.0)),
                        metadata=item,
                    )
                )
            return updates
        return []