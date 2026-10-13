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

# Public master contract file — no authentication required.
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"


class DhanDiscovery(BaseBrokerDiscovery):
    """
    Dhan master contract discovery and normalization adapter.
    Dhan uses numeric security_id as the primary instrument token.
    """

    def __init__(self, client: Optional[Any] = None) -> None:
        self.client = client

    def get_broker_name(self) -> str:
        return "dhan"

    async def _fetch_raw_contracts(self) -> List[Dict[str, Any]]:
        """
        Fetches raw contract list from the Dhan client if one is injected,
        otherwise downloads the public master contract CSV.

        The CSV holds ~208k rows, so it is narrowed to the tradable universe
        (5 spot indices + NIFTY 100 cash equities) before normalization. Keys
        are emitted under the names normalize_instrument() already expects.
        """
        if self.client and hasattr(self.client, "get_instrument_list"):
            res = self.client.get_instrument_list()
            if isinstance(res, list):
                return res
            if isinstance(res, dict) and "data" in res:
                return res["data"]

        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, download_text, DHAN_MASTER_URL)
        if not text:
            return []

        tickers, isins = await loop.run_in_executor(None, nifty100_membership)
        return await loop.run_in_executor(None, self._parse_master_csv, text, tickers, isins)

    def _parse_master_csv(
        self, text: str, tickers: set, isins: set
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen_index: set = set()
        seen_futures: set = set()   # dedup: one lot-size entry per underlying
        reader = csv.DictReader(io.StringIO(text))

        for row in reader:
            exch = (row.get("EXCH_ID") or "").strip().upper()
            if exch not in ("NSE", "BSE"):
                continue

            instrument = (row.get("INSTRUMENT") or "").strip().upper()
            ticker = (row.get("UNDERLYING_SYMBOL") or row.get("SYMBOL_NAME") or "").strip().upper()
            isin = (row.get("ISIN") or "").strip().upper()

            if instrument == "INDEX":
                canon = canonical_index_name(row.get("SYMBOL_NAME")) or canonical_index_name(
                    row.get("DISPLAY_NAME")
                )
                if not canon or canon in seen_index:
                    continue
                seen_index.add(canon)
                symbol = canon
            elif instrument == "EQUITY":
                # NIFTY 100 names are dual-listed; NSE is the trading venue,
                # so keep NSE tokens only and drop the BSE duplicates.
                if exch != "NSE":
                    continue
                # SERIES separates the cash share from bonds/NCDs that share the
                # same underlying (e.g. MOTHERSON has a 'D1' NCD alongside 'EQ').
                if (row.get("SERIES") or "").strip().upper() != "EQ":
                    continue
                if not in_universe(ticker or row.get("SYMBOL_NAME"), isin, tickers, isins):
                    continue
                symbol = ticker or (row.get("SYMBOL_NAME") or "").strip().upper()
            elif instrument in ("FUTIDX", "FUTSTK"):
                # Extract one lot-size record per underlying from futures rows.
                # Monthly contracts repeat; first occurrence wins (same lot_size
                # for every expiry within a contract series).
                # Only NSE; only universe members for stock futures.
                if exch != "NSE":
                    continue
                underlying = ticker
                if not underlying or underlying in seen_futures:
                    continue
                if instrument == "FUTSTK" and not in_universe(underlying, None, tickers, isins):
                    continue
                seen_futures.add(underlying)
                # Canonical registry key: "UNDERLYING-FUT" (no expiry, broker-neutral).
                # Synthetic token avoids collision with the EQ entry for the same underlying.
                out.append(
                    {
                        "security_id": f"FUT-{underlying}",
                        "trading_symbol": f"{underlying}-FUT",
                        "display_name": f"{underlying} Futures",
                        "exchange_segment": exch,
                        "instrument_type": instrument,
                        "lot_size": row.get("LOT_SIZE"),
                        "tick_size": row.get("TICK_SIZE"),
                        "isin": None,
                        "underlying_symbol": underlying,
                    }
                )
                continue
            elif instrument in ("OPTIDX", "OPTSTK"):
                # Options share the same lot_size as their underlying's futures.
                # Individual contracts are NOT stored (too numerous, expire weekly).
                # We emit one supplemental lot_size entry per underlying only when
                # that underlying was NOT already captured by a FUTIDX/FUTSTK row —
                # e.g. an underlying that has options but no front-month futures row.
                # Same canonical "-FUT" key is used so the paper broker lot_size
                # lookup finds it without a separate option-specific path.
                if exch != "NSE":
                    continue
                underlying = ticker
                if not underlying or underlying in seen_futures:
                    continue
                if instrument == "OPTSTK" and not in_universe(underlying, None, tickers, isins):
                    continue
                seen_futures.add(underlying)
                out.append(
                    {
                        "security_id": f"FUT-{underlying}",
                        "trading_symbol": f"{underlying}-FUT",
                        "display_name": f"{underlying} Futures",
                        "exchange_segment": exch,
                        # Promote to the matching futures type so normalize_instrument
                        # maps it to IDX_FUT / FUT rather than IDX_OPT / OPT.
                        "instrument_type": "FUTIDX" if instrument == "OPTIDX" else "FUTSTK",
                        "lot_size": row.get("LOT_SIZE"),
                        "tick_size": row.get("TICK_SIZE"),
                        "isin": None,
                        "underlying_symbol": underlying,
                    }
                )
                continue
            else:
                continue

            out.append(
                {
                    "security_id": row.get("SECURITY_ID"),
                    "trading_symbol": symbol,
                    "display_name": row.get("DISPLAY_NAME") or row.get("SYMBOL_NAME"),
                    "exchange_segment": exch,
                    "instrument_type": instrument,
                    "lot_size": row.get("LOT_SIZE"),
                    "tick_size": row.get("TICK_SIZE"),
                    "isin": isin or None,
                    "underlying_symbol": ticker or None,
                }
            )

        n_eq = len(out) - len(seen_index) - len(seen_futures)
        logger.info(
            "[Dhan] Master CSV filtered to %d rows (%d indices, %d equities, %d futures/options).",
            len(out), len(seen_index), n_eq, len(seen_futures),
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
                logger.debug("Skipping malformed Dhan instrument payload %s: %s", raw, exc)
        logger.info("[Dhan] Discovered and normalized %d instruments.", len(instruments))
        return instruments

    def normalize_instrument(self, raw_data: Dict[str, Any]) -> Optional[Instrument]:
        """
        Normalizes Dhan contract fields to unified Instrument model.
        """
        if not isinstance(raw_data, dict):
            return None

        # 1. Token (numeric security_id)
        raw_token = (
            raw_data.get("security_id")
            or raw_data.get("SECURITY_ID")
            or raw_data.get("securityId")
            or raw_data.get("SEM_SMST_SECURITY_ID")
        )
        if raw_token is None:
            return None
        token = str(raw_token).strip()
        if not token:
            return None

        # 2. Symbol
        symbol = (
            raw_data.get("custom_symbol")
            or raw_data.get("trading_symbol")
            or raw_data.get("SEM_TRADING_SYMBOL")
            or raw_data.get("symbol")
        )
        if not symbol or not str(symbol).strip():
            return None
        symbol = str(symbol).strip().upper()

        # 3. Exchange
        seg = str(
            raw_data.get("exchange_segment")
            or raw_data.get("EXCHANGE_SEGMENT")
            or raw_data.get("segment")
            or "NSE"
        ).upper()
        if "BSE" in seg:
            exchange = Exchange.BSE
        else:
            exchange = Exchange.NSE

        # 4. Instrument type and asset class
        raw_type = str(
            raw_data.get("instrument_type")
            or raw_data.get("SEM_INSTRUMENT_NAME")
            or ""
        ).upper()

        if "OPTIDX" in raw_type or "INDEX_OPT" in raw_type:
            asset_class = AssetClass.INDEX_OPTIONS
            inst_type = InstrumentType.IDX_OPT
        elif "OPTSTK" in raw_type or "STOCK_OPT" in raw_type:
            asset_class = AssetClass.STOCK_OPTIONS
            inst_type = InstrumentType.OPT
        elif "FUTIDX" in raw_type or "INDEX_FUT" in raw_type:
            asset_class = AssetClass.INDEX_FUTURES
            inst_type = InstrumentType.IDX_FUT
        elif "FUTSTK" in raw_type or "STOCK_FUT" in raw_type:
            asset_class = AssetClass.STOCK_FUTURES
            inst_type = InstrumentType.FUT
        else:
            asset_class = AssetClass.STOCK_CASH
            inst_type = InstrumentType.EQ

        # 5. Lot size
        raw_lot = (
            raw_data.get("lot_size")
            or raw_data.get("SEM_LOT_UNITS")
            or raw_data.get("lotSize")
        )
        lot_size = 1
        if raw_lot is not None:
            # Dhan's master writes LOT_SIZE as a float string ("65.0"), and
            # int("65.0") raises — that silently made every lot size 1.
            try:
                lot_size = int(float(raw_lot))
            except (ValueError, TypeError):
                logger.critical(
                    "[DhanDiscovery] Unparseable lot size %r for %s — defaulting to 1",
                    raw_lot, raw_data.get("SYMBOL_NAME") or raw_data.get("symbol"),
                )
                lot_size = 1
        if lot_size <= 0:
            lot_size = 1

        # 6. Tick size
        raw_tick = raw_data.get("tick_size") or raw_data.get("SEM_TICK_SIZE") or 0.05
        try:
            tick_size = float(raw_tick)
            if tick_size <= 0:
                tick_size = 0.05
        except (ValueError, TypeError):
            tick_size = 0.05

        # 7. Derivatives fields
        strike_price: Optional[float] = None
        raw_strike = raw_data.get("strike_price") or raw_data.get("SEM_STRIKE_PRICE")
        if raw_strike is not None:
            try:
                strike_price = float(raw_strike)
            except (ValueError, TypeError):
                strike_price = None

        option_type: Optional[str] = None
        raw_opt = raw_data.get("option_type") or raw_data.get("SEM_OPTION_TYPE")
        if raw_opt:
            opt_str = str(raw_opt).strip().upper()
            if opt_str in ("CE", "PE"):
                option_type = opt_str

        expiry: Optional[date] = None
        raw_exp = raw_data.get("expiry_date") or raw_data.get("SEM_EXPIRY_DATE")
        if raw_exp:
            if isinstance(raw_exp, date):
                expiry = raw_exp
            elif isinstance(raw_exp, str):
                try:
                    expiry = datetime.strptime(raw_exp.strip()[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass

        underlying = (
            raw_data.get("underlying_symbol")
            or raw_data.get("underlying")
            or raw_data.get("SEM_UNDERLYING_SYMBOL")
        )
        if underlying:
            underlying = str(underlying).strip().upper()

        isin = raw_data.get("isin") or raw_data.get("SEM_ISIN") or raw_data.get("isinCode")
        if isin:
            isin = str(isin).strip().upper()

        name = raw_data.get("display_name") or raw_data.get("company_name") or raw_data.get("SEM_CUSTOM_SYMBOL")
        if name:
            name = str(name).strip()

        broker_symbol = f"{exchange.value}:{symbol}"

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
            logger.warning("[Dhan] Validation error creating Instrument for %s: %s", symbol, exc)
            return None
