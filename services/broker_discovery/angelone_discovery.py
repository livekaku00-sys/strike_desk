import asyncio
from datetime import date, datetime
import logging
from typing import Any, Dict, List, Optional

from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from .base_discovery import BaseBrokerDiscovery
from .universe import (
    canonical_index_name,
    download_json,
    in_universe,
    nifty100_membership,
)

logger = logging.getLogger(__name__)

# Public scrip master — no authentication required.
ANGEL_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)


class AngelOneDiscovery(BaseBrokerDiscovery):
    """
    AngelOne (SmartAPI) master contract discovery and normalization adapter.
    AngelOne uses numeric symboltoken + exchange.
    """

    def __init__(self, client: Optional[Any] = None) -> None:
        self.client = client

    def get_broker_name(self) -> str:
        return "angelone"

    async def _fetch_raw_contracts(self) -> List[Dict[str, Any]]:
        """
        Fetches raw contract list from the SmartConnect client if one is
        injected, otherwise downloads the public scrip master JSON.

        The master holds ~153k contracts, so it is narrowed to the tradable
        universe (5 spot indices + NIFTY 100 cash equities). AngelOne publishes
        no ISIN, so equities are matched on ticker.
        """
        if self.client and hasattr(self.client, "searchScrip"):
            res = self.client.searchScrip()
            if isinstance(res, list):
                return res
            if isinstance(res, dict) and "data" in res:
                return res["data"]

        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, download_json, ANGEL_MASTER_URL)
        if not data:
            return []

        tickers, isins = await loop.run_in_executor(None, nifty100_membership)
        return await loop.run_in_executor(None, self._filter_master, data, tickers, isins)

    def _filter_master(
        self, data: List[Dict[str, Any]], tickers: set, isins: set
    ) -> List[Dict[str, Any]]:
        # AngelOne publishes spot indices twice: a short-token row with an empty
        # instrumenttype (NIFTY -> 26000) and an "AMXIDX" row with a long token
        # (NIFTY -> 99926000). The short form is what the tick feed uses, so
        # prefer it and fall back to AMXIDX — SENSEX only exists as AMXIDX.
        index_rows: Dict[str, Dict[str, Any]] = {}
        equities: List[Dict[str, Any]] = []

        for row in data:
            exch = str(row.get("exch_seg") or "").strip().upper()

            if exch in ("NSE", "BSE"):
                symbol = str(row.get("symbol") or "").strip().upper()
                name = str(row.get("name") or "").strip().upper()
                inst_type = str(row.get("instrumenttype") or "").strip().upper()
                # Skip dated derivatives; spot rows carry no expiry.
                if str(row.get("expiry") or "").strip():
                    continue
                if inst_type not in ("", "AMXIDX"):
                    continue

                emit = {
                    "token": row.get("token"),
                    "tradingsymbol": symbol,
                    "name": row.get("name"),
                    "exch_seg": exch,
                    "instrumenttype": inst_type,
                    "lotsize": row.get("lotsize"),
                    "tick_size": row.get("tick_size"),
                }

                canon = canonical_index_name(name) or canonical_index_name(symbol)
                if canon and not symbol.endswith("-EQ"):
                    emit["symbol"] = canon
                    prev = index_rows.get(canon)
                    # Shorter token == the feed-facing form; prefer it.
                    if prev is None or len(str(emit["token"])) < len(str(prev["token"])):
                        index_rows[canon] = emit
                    continue

                if inst_type or exch != "NSE" or not symbol.endswith("-EQ"):
                    continue
                if not in_universe(name or symbol, None, tickers, isins):
                    continue
                emit["symbol"] = name or symbol[:-3]
                equities.append(emit)

            # NFO/BFO rows are intentionally skipped.
            # Architecture (2026-09-30): AngelOne is a data feed only (LTP + ticks).
            # F&O instrument discovery and the option chain are Dhan's sole responsibility.

        out = list(index_rows.values()) + equities
        logger.info(
            "[AngelOne] Scrip master filtered to %d rows (%d indices, %d equities). "
            "F&O excluded — Dhan is the sole F&O discovery source.",
            len(out), len(index_rows), len(equities),
        )
        return out

    async def fetch_master_contracts(self) -> List[Instrument]:
        raw_items = await self._fetch_raw_contracts()
        instruments: List[Instrument] = []
        for raw in raw_items:
            try:
                inst = self.normalize_instrument(raw)
                if inst is not None:
                    instruments.append(inst)
            except Exception as exc:
                logger.debug("Skipping malformed AngelOne instrument payload %s: %s", raw, exc)
        logger.info("[AngelOne] Discovered and normalized %d instruments.", len(instruments))
        return instruments

    def normalize_instrument(self, raw_data: Dict[str, Any]) -> Optional[Instrument]:
        """
        Normalizes AngelOne contract fields to unified Instrument model.
        """
        if not isinstance(raw_data, dict):
            return None

        # Architecture guard: AngelOne is a data feed only. Drop F&O entries
        # even if one slips through (e.g. injected via a client stub).
        _itype_guard = str(raw_data.get("instrumenttype") or "").strip().upper()
        if _itype_guard in ("FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"):
            return None

        # 1. Token (numeric symboltoken)
        raw_token = raw_data.get("token") or raw_data.get("symboltoken")
        if raw_token is None:
            return None
        token = str(raw_token).strip()
        if not token:
            return None

        # 2. Symbol
        raw_sym = raw_data.get("symbol") or raw_data.get("tradingsymbol")
        if not raw_sym or not str(raw_sym).strip():
            return None
        symbol = str(raw_sym).strip().upper()

        # 3. Exchange
        exch_seg = str(raw_data.get("exch_seg") or raw_data.get("exchange") or "NSE").upper()
        if "BSE" in exch_seg or "BFO" in exch_seg:
            exchange = Exchange.BSE
        else:
            exchange = Exchange.NSE

        # 4. Instrument type and asset class
        itype = str(raw_data.get("instrumenttype") or "").upper()
        if "OPTIDX" in itype:
            asset_class = AssetClass.INDEX_OPTIONS
            inst_type = InstrumentType.IDX_OPT
        elif "OPTSTK" in itype:
            asset_class = AssetClass.STOCK_OPTIONS
            inst_type = InstrumentType.OPT
        elif "FUTIDX" in itype:
            asset_class = AssetClass.INDEX_FUTURES
            inst_type = InstrumentType.IDX_FUT
        elif "FUTSTK" in itype:
            asset_class = AssetClass.STOCK_FUTURES
            inst_type = InstrumentType.FUT
        else:
            if symbol.endswith("CE") or symbol.endswith("PE"):
                asset_class = (
                    AssetClass.INDEX_OPTIONS
                    if any(x in symbol for x in ("NIFTY", "BANKNIFTY", "FINNIFTY"))
                    else AssetClass.STOCK_OPTIONS
                )
                inst_type = (
                    InstrumentType.IDX_OPT
                    if asset_class == AssetClass.INDEX_OPTIONS
                    else InstrumentType.OPT
                )
            elif symbol.endswith("FUT"):
                asset_class = (
                    AssetClass.INDEX_FUTURES
                    if any(x in symbol for x in ("NIFTY", "BANKNIFTY", "FINNIFTY"))
                    else AssetClass.STOCK_FUTURES
                )
                inst_type = (
                    InstrumentType.IDX_FUT
                    if asset_class == AssetClass.INDEX_FUTURES
                    else InstrumentType.FUT
                )
            else:
                asset_class = AssetClass.STOCK_CASH
                inst_type = InstrumentType.EQ

        # 5. Lot size
        raw_lot = raw_data.get("lotsize") or raw_data.get("lot_size")
        lot_size = 1
        if raw_lot is not None:
            try:
                lot_size = int(raw_lot)
            except (ValueError, TypeError):
                lot_size = 1
        if lot_size <= 0:
            lot_size = 1

        # 6. Tick size (handles paise format > 1.0)
        raw_tick = raw_data.get("tick_size")
        tick_size = 0.05
        if raw_tick is not None:
            try:
                t_val = float(raw_tick)
                if t_val > 1.0:
                    tick_size = t_val / 100.0
                elif t_val > 0.0:
                    tick_size = t_val
            except (ValueError, TypeError):
                tick_size = 0.05

        # 7. Derivatives details
        strike_price: Optional[float] = None
        raw_strike = raw_data.get("strike") or raw_data.get("strike_price")
        if raw_strike is not None:
            try:
                stk = float(raw_strike)
                if stk > 100000.0 and stk % 100 == 0:
                    stk = stk / 100.0
                if stk > 0.0:
                    strike_price = stk
            except (ValueError, TypeError):
                strike_price = None

        option_type: Optional[str] = None
        if asset_class in (AssetClass.INDEX_OPTIONS, AssetClass.STOCK_OPTIONS):
            if symbol.endswith("CE"):
                option_type = "CE"
            elif symbol.endswith("PE"):
                option_type = "PE"
            else:
                raw_opt = raw_data.get("optiontype") or raw_data.get("option_type")
                if raw_opt and str(raw_opt).strip().upper() in ("CE", "PE"):
                    option_type = str(raw_opt).strip().upper()

        expiry: Optional[date] = None
        raw_exp = raw_data.get("expiry")
        if raw_exp:
            if isinstance(raw_exp, date):
                expiry = raw_exp
            elif isinstance(raw_exp, str):
                exp_str = raw_exp.strip()
                for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y", "%d%B%Y"):
                    try:
                        expiry = datetime.strptime(exp_str, fmt).date()
                        break
                    except ValueError:
                        pass

        raw_name = raw_data.get("name")
        underlying: Optional[str] = None
        if asset_class != AssetClass.STOCK_CASH and raw_name:
            underlying = str(raw_name).strip().upper()
        elif asset_class == AssetClass.STOCK_CASH and symbol.endswith("-EQ"):
            underlying = symbol.replace("-EQ", "")

        isin = raw_data.get("isin")
        if isin:
            isin = str(isin).strip().upper()

        name = str(raw_name).strip() if raw_name else None
        broker_symbol = symbol

        try:
            return Instrument(
                exchange=exchange,
                symbol=symbol,
                broker_symbol=broker_symbol,
                token=token,
                asset_class=asset_class,
                instrument_type=inst_type,
                underlying=underlying,
                expiry=expiry,
                strike_price=strike_price,
                option_type=option_type,
                lot_size=lot_size,
                tick_size=tick_size,
                isin=isin,
                name=name,
            )
        except Exception as exc:
            logger.warning("[AngelOne] Validation error creating Instrument for %s: %s", symbol, exc)
            return None
