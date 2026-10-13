from datetime import datetime, timezone
import logging
from typing import Any, Callable, Dict, List, Optional

from models.option_chain import OptionChain, OptionContract, StrikeData
from utils.cache_manager import option_chain_cache
from utils.pcr_calculator import pcr_calculator

logger = logging.getLogger(__name__)


class OptionChainFetcher:
    def __init__(self, cache_ttl_seconds: float = 60.0) -> None:
        self.cache = option_chain_cache

    @staticmethod
    def normalize_dhan_chain(raw_data: Dict[str, Any], underlying: str, expiry: str) -> OptionChain:
        strikes: List[StrikeData] = []
        raw_data_block = raw_data.get("data", {}) if isinstance(raw_data, dict) else {}
        # Dhan API may return {"data": "error message"} on a per-symbol failure
        # (HTTP 200 with a non-dict "data" field). Guard before calling .get() on it.
        data_block = raw_data_block if isinstance(raw_data_block, dict) else {}
        # Dhan v2 nests the payload one level deeper: data -> data -> oc
        inner = data_block.get("data") if isinstance(data_block.get("data"), dict) else data_block
        inner = inner if isinstance(inner, dict) else {}
        oc_dict = inner.get("oc", {}) or {}
        underlying_ltp = float(inner.get("last_price") or 0.0)

        for strike_str, contracts in oc_dict.items():
            try:
                strike_price = float(strike_str)
            except (ValueError, TypeError):
                continue
            if not isinstance(contracts, dict):
                continue

            ce_raw = contracts.get("ce", {}) or {}
            pe_raw = contracts.get("pe", {}) or {}

            call = OptionContract(
                ltp=float(ce_raw.get("last_price") or ce_raw.get("ltp") or 0.0),
                bid_price=float(ce_raw.get("top_bid_price") or ce_raw.get("bid") or 0.0),
                ask_price=float(ce_raw.get("top_ask_price") or ce_raw.get("ask") or 0.0),
                volume=float(ce_raw.get("volume") or 0.0),
                oi=float(ce_raw.get("oi") or 0.0),
                iv=float(ce_raw.get("implied_volatility") or ce_raw.get("iv") or 0.0),
                security_id=str(ce_raw.get("security_id") or ""),
            )

            put = OptionContract(
                ltp=float(pe_raw.get("last_price") or pe_raw.get("ltp") or 0.0),
                bid_price=float(pe_raw.get("top_bid_price") or pe_raw.get("bid") or 0.0),
                ask_price=float(pe_raw.get("top_ask_price") or pe_raw.get("ask") or 0.0),
                volume=float(pe_raw.get("volume") or 0.0),
                oi=float(pe_raw.get("oi") or 0.0),
                iv=float(pe_raw.get("implied_volatility") or pe_raw.get("iv") or 0.0),
                security_id=str(pe_raw.get("security_id") or ""),
            )

            row = StrikeData(strike_price=strike_price, call=call, put=put)
            row.calculate_pcr()
            strikes.append(row)

        strikes.sort(key=lambda s: s.strike_price)
        chain = OptionChain(
            underlying=underlying.upper(),
            expiry_date=expiry,
            strikes=strikes,
            underlying_ltp=underlying_ltp,
            timestamp=datetime.now(timezone.utc),
        )
        pcr_calculator.calculate(chain)
        logger.info(
            "[OptionChain] Normalized %s expiry=%s: %d strikes, spot=%.2f, pcr=%.4f",
            underlying.upper(), expiry, len(strikes), underlying_ltp, chain.pcr,
        )
        return chain

    @staticmethod
    def normalize_angelone_chain(raw_data: Dict[str, Any], underlying: str, expiry: str) -> OptionChain:
        strikes_map: Dict[float, StrikeData] = {}
        data_list = raw_data.get("data", [])
        if isinstance(data_list, dict):
            data_list = data_list.get("optionsChain", []) or []

        for item in data_list:
            strike_price = float(item.get("strike_price") or item.get("strikePrice") or 0.0)
            if strike_price <= 0:
                continue

            if strike_price not in strikes_map:
                strikes_map[strike_price] = StrikeData(strike_price=strike_price)

            row = strikes_map[strike_price]

            if "ce" in item or "call" in item:
                ce = item.get("ce") or item.get("call") or {}
                row.call = OptionContract(
                    ltp=float(ce.get("ltp") or 0.0),
                    bid_price=float(ce.get("bid") or 0.0),
                    ask_price=float(ce.get("ask") or 0.0),
                    volume=float(ce.get("volume") or 0.0),
                    oi=float(ce.get("oi") or ce.get("open_interest") or 0.0),
                    iv=float(ce.get("iv") or 0.0),
                )
            if "pe" in item or "put" in item:
                pe = item.get("pe") or item.get("put") or {}
                row.put = OptionContract(
                    ltp=float(pe.get("ltp") or 0.0),
                    bid_price=float(pe.get("bid") or 0.0),
                    ask_price=float(pe.get("ask") or 0.0),
                    volume=float(pe.get("volume") or 0.0),
                    oi=float(pe.get("oi") or pe.get("open_interest") or 0.0),
                    iv=float(pe.get("iv") or 0.0),
                )

        strikes = list(strikes_map.values())
        for s in strikes:
            s.calculate_pcr()
        strikes.sort(key=lambda s: s.strike_price)

        chain = OptionChain(
            underlying=underlying.upper(),
            expiry_date=expiry,
            strikes=strikes,
            timestamp=datetime.now(timezone.utc),
        )
        pcr_calculator.calculate(chain)
        return chain

    @staticmethod
    def normalize_fyers_chain(raw_data: Dict[str, Any], underlying: str, expiry: str) -> OptionChain:
        strikes: List[StrikeData] = []
        data_block = raw_data.get("data", {})
        options_chain = data_block.get("optionsChain", []) if isinstance(data_block, dict) else []

        for item in options_chain:
            strike_price = float(item.get("strike_price") or 0.0)
            if strike_price <= 0:
                continue

            call = OptionContract(
                ltp=float(item.get("call_ltp") or item.get("ltp_call") or 0.0),
                bid_price=float(item.get("call_bid") or 0.0),
                ask_price=float(item.get("call_ask") or 0.0),
                volume=float(item.get("call_volume") or 0.0),
                oi=float(item.get("call_oi") or 0.0),
                iv=float(item.get("call_iv") or 0.0),
            )

            put = OptionContract(
                ltp=float(item.get("put_ltp") or item.get("ltp_put") or 0.0),
                bid_price=float(item.get("put_bid") or 0.0),
                ask_price=float(item.get("put_ask") or 0.0),
                volume=float(item.get("put_volume") or 0.0),
                oi=float(item.get("put_oi") or 0.0),
                iv=float(item.get("put_iv") or 0.0),
            )

            row = StrikeData(strike_price=strike_price, call=call, put=put)
            row.calculate_pcr()
            strikes.append(row)

        strikes.sort(key=lambda s: s.strike_price)
        chain = OptionChain(
            underlying=underlying.upper(),
            expiry_date=expiry,
            strikes=strikes,
            timestamp=datetime.now(timezone.utc),
        )
        pcr_calculator.calculate(chain)
        return chain

    async def get_chain(
        self,
        broker: str,
        underlying: str,
        expiry: str,
        raw_fetch_coro: Callable[[], Any],
        force_refresh: bool = False,
    ) -> OptionChain:
        u = underlying.strip().upper()
        cached = self.cache.get(u, expiry, force_refresh=force_refresh)
        if cached is not None:
            return cached

        raw_data = await raw_fetch_coro()

        b = broker.lower()
        if b == "dhan":
            chain = self.normalize_dhan_chain(raw_data, u, expiry)
        elif b in ("angelone", "angel"):
            chain = self.normalize_angelone_chain(raw_data, u, expiry)
        elif b == "fyers":
            chain = self.normalize_fyers_chain(raw_data, u, expiry)
        else:
            raise ValueError(f"Unknown broker: {broker}")

        self.cache.put(u, expiry, chain)
        return chain


option_chain_fetcher = OptionChainFetcher()
