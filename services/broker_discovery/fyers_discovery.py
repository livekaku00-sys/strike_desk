import asyncio
import csv
from datetime import date, datetime
import io
import logging
from typing import Any, Dict, List, Optional

from models.instrument import AssetClass, Exchange, Instrument, InstrumentType
from .base_discovery import BaseBrokerDiscovery
from .universe import (
    canonical_index_name,
    download_text,
    in_universe,
    nifty100_membership,
)

logger = logging.getLogger(__name__)

# Public symbol-detail files — no authentication required. Cash segments only;
# the universe is spot indices + NIFTY 100 equities.
FYERS_MASTER_URLS = [
    ("https://public.fyers.in/sym_details/NSE_CM.csv", "NSE"),
    ("https://public.fyers.in/sym_details/BSE_CM.csv", "BSE"),
]

# Futures & Options files — headerless, same column layout as CM files.
FYERS_FO_URLS = [
    ("https://public.fyers.in/sym_details/NSE_FO.csv", "NSE"),
    ("https://public.fyers.in/sym_details/BSE_FO.csv", "BSE"),
]

# The CSVs are headerless; these are the positional columns actually used.
_COL_FYTOKEN = 0
_COL_NAME = 1
_COL_LOT = 3
_COL_TICK = 4
_COL_ISIN = 5
_COL_FYERS_SYMBOL = 9
_COL_TRADING_SYMBOL = 13


class FyersDiscovery(BaseBrokerDiscovery):
    """
    Fyers master contract discovery and normalization adapter.
    Fyers uses full string symbols (e.g. 'NSE:RELIANCE-EQ') and fyToken.
    """

    def __init__(self, client: Optional[Any] = None) -> None:
        self.client = client

    def get_broker_name(self) -> str:
        return "fyers"

    async def _fetch_raw_contracts(self) -> List[Dict[str, Any]]:
        """
        Fetches raw symbol list from the Fyers client if one is injected,
        otherwise downloads the public symbol-detail CSVs.

        The files are headerless, so columns are read positionally. Rows are
        narrowed to the tradable universe (5 spot indices + NIFTY 100 cash
        equities), matched on ISIN where the file provides one.
        """
        if self.client and hasattr(self.client, "get_symbol_master"):
            res = self.client.get_symbol_master()
            if isinstance(res, list):
                return res
            if isinstance(res, dict) and "data" in res:
                return res["data"]

        loop = asyncio.get_running_loop()
        tickers, isins = await loop.run_in_executor(None, nifty100_membership)

        out: List[Dict[str, Any]] = []

        # Cash-market CSVs: spot indices + NIFTY 100 equities.
        for url, exch in FYERS_MASTER_URLS:
            text = await loop.run_in_executor(None, download_text, url)
            if not text:
                continue
            out.extend(
                await loop.run_in_executor(
                    None, self._parse_master_csv, text, exch, tickers, isins
                )
            )

        # F&O CSVs (NSE_FO, BSE_FO) are intentionally not downloaded.
        # Architecture (2026-09-30): Fyers is a data feed only (LTP + ticks).
        # F&O instrument discovery and the option chain are Dhan's sole responsibility.

        n_idx = sum(1 for r in out if r.get("instrument_type") == "INDEX")
        logger.info(
            "[Fyers] Symbol master filtered to %d rows (%d indices, %d equities). "
            "F&O excluded — Dhan is the sole F&O discovery source.",
            len(out), n_idx, len(out) - n_idx,
        )
        return out

    def _parse_master_csv(
        self, text: str, exch: str, tickers: set, isins: set
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_index: set = set()

        for parts in csv.reader(io.StringIO(text)):
            if len(parts) <= _COL_TRADING_SYMBOL:
                continue

            fy_symbol = parts[_COL_FYERS_SYMBOL].strip().upper()
            trading = parts[_COL_TRADING_SYMBOL].strip().upper()
            isin = parts[_COL_ISIN].strip().upper()
            name = parts[_COL_NAME].strip()

            if fy_symbol.endswith("-INDEX"):
                canon = canonical_index_name(trading) or canonical_index_name(
                    fy_symbol.split(":", 1)[-1].replace("-INDEX", "")
                )
                if not canon or canon in seen_index:
                    continue
                seen_index.add(canon)
                symbol, inst_type = canon, "INDEX"
            elif fy_symbol.endswith("-EQ"):
                if not in_universe(trading, isin, tickers, isins):
                    continue
                symbol, inst_type = trading, "EQ"
            else:
                continue

            out.append(
                {
                    "fyers_symbol": fy_symbol,
                    "symbol": symbol,
                    "description": name,
                    "exchange": exch,
                    "instrument_type": inst_type,
                    "lot_size": parts[_COL_LOT],
                    "tick_size": parts[_COL_TICK],
                    "isin": isin or None,
                    "token": parts[_COL_FYTOKEN].strip() or None,
                }
            )
        return out

    def _parse_fo_csv(
        self,
        text: str,
        exch: str,
        tickers: set,
        isins: set,
        seen_futures: set,
    ) -> List[Dict[str, Any]]:
        """
        Parse one Fyers F&O CSV (headerless) and emit one lot-size registry entry
        per underlying.  Futures rows are captured first; option rows supplement
        for underlyings that have no futures row (same lot_size applies across the
        entire series).  Individual option contracts are never stored.

        The seen_futures set is mutated in-place so the caller can share it across
        multiple exchange files (NSE → BSE) to prevent cross-exchange duplicates.
        """
        out: List[Dict[str, Any]] = []

        for parts in csv.reader(io.StringIO(text)):
            if len(parts) <= _COL_TRADING_SYMBOL:
                continue

            fy_sym = parts[_COL_FYERS_SYMBOL].strip()
            underlying = parts[_COL_TRADING_SYMBOL].strip().upper()  # BaseSymbol
            if not underlying or not fy_sym:
                continue

            sym_body = fy_sym.upper().split(":", 1)[-1]
            if sym_body.endswith("FUT"):
                pass  # futures row
            elif sym_body.endswith("CE") or sym_body.endswith("PE"):
                pass  # options row — lot_size supplement only
            else:
                continue

            if underlying in seen_futures:
                continue

            is_index = bool(canonical_index_name(underlying))
            if not is_index and not in_universe(underlying, None, tickers, isins):
                continue

            seen_futures.add(underlying)
            inst_type = "FUTIDX" if is_index else "FUTSTK"

            out.append(
                {
                    "fyers_symbol": f"{exch}:{underlying}-FUT",
                    "symbol": f"{underlying}-FUT",
                    "description": parts[_COL_NAME].strip(),
                    "exchange": exch,
                    "instrument_type": inst_type,
                    "lot_size": parts[_COL_LOT].strip(),
                    "tick_size": parts[_COL_TICK].strip(),
                    "isin": None,
                    "token": f"FUT-{underlying}",
                    "underlying": underlying,
                }
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
                logger.debug("Skipping malformed Fyers instrument payload %s: %s", raw, exc)
        logger.info("[Fyers] Discovered and normalized %d instruments.", len(instruments))
        return instruments

    def normalize_instrument(self, raw_data: Dict[str, Any]) -> Optional[Instrument]:
        """
        Normalizes Fyers contract fields to unified Instrument model.
        """
        if not isinstance(raw_data, dict):
            return None

        # Architecture guard: Fyers is a data feed only. Drop F&O entries
        # even if one slips through (e.g. injected via a client stub).
        _itype_guard = str(
            raw_data.get("instrument_type") or raw_data.get("instrumentType") or ""
        ).upper()
        if _itype_guard in ("FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"):
            return None

        # 1. Broker symbol & Canonical Symbol
        raw_sym = raw_data.get("symbol") or raw_data.get("fyers_symbol")
        if not raw_sym or not str(raw_sym).strip():
            return None
        broker_symbol = str(raw_sym).strip().upper()

        if ":" in broker_symbol:
            exch_prefix, sym_body = broker_symbol.split(":", 1)
            exchange = Exchange.BSE if "BSE" in exch_prefix else Exchange.NSE
            symbol = sym_body
        else:
            raw_exch = raw_data.get("exchange")
            if raw_exch in (11, "11", "BSE"):
                exchange = Exchange.BSE
            else:
                exchange = Exchange.NSE
            symbol = broker_symbol

        # 2. Token
        raw_token = raw_data.get("fyToken") or raw_data.get("token")
        if raw_token is not None:
            token = str(raw_token).strip()
        else:
            token = broker_symbol

        # 3. Instrument type and asset class
        raw_itype = str(raw_data.get("instrument_type") or raw_data.get("instrumentType") or "").upper()

        if "OPTIDX" in raw_itype or "12" in raw_itype:
            asset_class = AssetClass.INDEX_OPTIONS
            inst_type = InstrumentType.IDX_OPT
        elif "OPTSTK" in raw_itype or "14" in raw_itype:
            asset_class = AssetClass.STOCK_OPTIONS
            inst_type = InstrumentType.OPT
        elif "FUTIDX" in raw_itype or "11" in raw_itype:
            asset_class = AssetClass.INDEX_FUTURES
            inst_type = InstrumentType.IDX_FUT
        elif "FUTSTK" in raw_itype or "13" in raw_itype:
            asset_class = AssetClass.STOCK_FUTURES
            inst_type = InstrumentType.FUT
        elif "EQ" in raw_itype or "10" in raw_itype:
            asset_class = AssetClass.STOCK_CASH
            inst_type = InstrumentType.EQ
        else:
            if symbol.endswith("CE") or symbol.endswith("PE"):
                is_index = any(idx in symbol for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"))
                asset_class = AssetClass.INDEX_OPTIONS if is_index else AssetClass.STOCK_OPTIONS
                inst_type = InstrumentType.IDX_OPT if is_index else InstrumentType.OPT
            elif symbol.endswith("FUT"):
                is_index = any(idx in symbol for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"))
                asset_class = AssetClass.INDEX_FUTURES if is_index else AssetClass.STOCK_FUTURES
                inst_type = InstrumentType.IDX_FUT if is_index else InstrumentType.FUT
            else:
                asset_class = AssetClass.STOCK_CASH
                inst_type = InstrumentType.EQ

        # 4. Lot size
        raw_lot = raw_data.get("lotSize") or raw_data.get("lot_size") or raw_data.get("min_lot_size")
        lot_size = 1
        if raw_lot is not None:
            try:
                lot_size = int(raw_lot)
            except (ValueError, TypeError):
                lot_size = 1
        if lot_size <= 0:
            lot_size = 1

        # 5. Tick size
        raw_tick = raw_data.get("tickSize") or raw_data.get("tick_size") or 0.05
        try:
            tick_size = float(raw_tick)
            if tick_size <= 0:
                tick_size = 0.05
        except (ValueError, TypeError):
            tick_size = 0.05

        # 6. Derivatives attributes
        strike_price: Optional[float] = None
        raw_strike = raw_data.get("strike_price") or raw_data.get("strike")
        if raw_strike is not None:
            try:
                strike_price = float(raw_strike)
            except (ValueError, TypeError):
                strike_price = None

        option_type: Optional[str] = None
        if asset_class in (AssetClass.INDEX_OPTIONS, AssetClass.STOCK_OPTIONS):
            if symbol.endswith("CE"):
                option_type = "CE"
            elif symbol.endswith("PE"):
                option_type = "PE"
            else:
                raw_opt = raw_data.get("option_type") or raw_data.get("opt_type")
                if raw_opt and str(raw_opt).strip().upper() in ("CE", "PE"):
                    option_type = str(raw_opt).strip().upper()

        expiry: Optional[date] = None
        raw_exp = raw_data.get("expiry") or raw_data.get("expiry_date")
        if raw_exp:
            if isinstance(raw_exp, date):
                expiry = raw_exp
            elif isinstance(raw_exp, str):
                for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%m-%Y"):
                    try:
                        expiry = datetime.strptime(raw_exp.strip(), fmt).date()
                        break
                    except ValueError:
                        pass

        underlying = raw_data.get("underlying")
        if underlying:
            underlying = str(underlying).strip().upper()

        isin = raw_data.get("isin")
        if isin:
            isin = str(isin).strip().upper()

        name = raw_data.get("description") or raw_data.get("name")
        if name:
            name = str(name).strip()

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
            logger.warning("[Fyers] Validation error creating Instrument for %s: %s", symbol, exc)
            return None
