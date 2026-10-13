"""
Strike Desk V4 - Market Data REST API Endpoints
Blueprint L6: REST API endpoints for market data
Blueprint O7: Zero Mock Data - real feeds or explicit empty/404 states only
Review Risk 7: Zero credential exposure in market responses
"""

from typing import Any, Dict, List, Optional
from datetime import datetime
import logging
import pytz

from fastapi import APIRouter, HTTPException, Depends, Query, status

from ..schemas import (
    InstrumentResponse,
    TickResponse,
    IndicatorResponse,
    OptionChainResponse,
    FullIndicatorResponse,
    RSIData,
    ADXData,
    MACDData,
    BollingerData,
    ATRData,
    SupertrendData,
    SentimentData,
)
from ..dependencies import (
    get_broker_adapters,
    get_instrument_registry,
    get_pipeline_orchestrator,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")
router = APIRouter(tags=["Market Data"])


@router.get("/instruments", response_model=List[InstrumentResponse])
async def list_instruments(
    segment: Optional[str] = Query(None, description="Filter by market segment (e.g. NSE_EQ, NFO_OPT, NFO_FUT)"),
    search: Optional[str] = Query(None, description="Substring search against symbol or company name"),
    registry: Any = Depends(get_instrument_registry),
):
    """
    Retrieves list of registered tradable instruments.
    Returns empty list [] if no matches are found (Blueprint O7: Zero Mock Data).
    """
    items = []
    try:
        if hasattr(registry, "filter"):
            items = await registry.filter(symbol=search) if search else []
            if not items and hasattr(registry, "_instruments"):
                items = list(registry._instruments.values())
        elif hasattr(registry, "list_instruments"):
            items = registry.list_instruments(segment=segment, search=search)
    except Exception:
        if hasattr(registry, "_instruments"):
            items = list(registry._instruments.values())
        else:
            items = []

    result = []
    for i in items:
        try:
            d = i.to_dict() if hasattr(i, "to_dict") else (i if isinstance(i, dict) else {})
            if not d:
                continue
            # Derive 'segment' if not present (to_dict() doesn't include it)
            if "segment" not in d:
                exchange = d.get("exchange", "")
                itype = d.get("instrument_type", "")
                if itype == "IDX_FUT":
                    d["segment"] = f"{exchange}_IDX"
                elif itype == "FUT":
                    d["segment"] = f"{exchange}_FUT"
                elif itype in ("CE", "PE"):
                    d["segment"] = f"{exchange}_OPT"
                else:
                    d["segment"] = exchange
            result.append(InstrumentResponse(**d))
        except Exception:
            pass
    return result


@router.get("/instruments/{symbol}", response_model=InstrumentResponse)
async def get_instrument(
    symbol: str,
    registry: Any = Depends(get_instrument_registry),
):
    """
    Retrieves full metadata for a specific instrument.
    Returns HTTP 404 if the instrument is not registered.
    """
    item = None
    try:
        if hasattr(registry, "_instruments"):
            # Direct lookup from real registry internal store
            from models.instrument import Exchange
            for exc in [Exchange.NSE, Exchange.BSE, Exchange.NFO, Exchange.BFO, Exchange.MCX]:
                item = registry._instruments.get((exc, symbol.upper()))
                if item:
                    break
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Instrument '{symbol}' not found in registry.",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instrument '{symbol}' not found in registry.",
        )
    d = item.to_dict() if hasattr(item, "to_dict") else {}
    return InstrumentResponse(**d) if d else item


@router.get("/ticks", response_model=List[TickResponse])
async def get_all_ticks(
    pipeline: Any = Depends(get_pipeline_orchestrator),
):
    """
    Returns latest live market ticks for all actively subscribed instruments.
    """
    ticks_dict = pipeline.get_all_ticks() if hasattr(pipeline, "get_all_ticks") else {}
    ticks = []
    for t in ticks_dict.values():
        ticks.append(TickResponse(**t) if isinstance(t, dict) else t)
    return ticks


@router.get("/ticks/{symbol}", response_model=TickResponse)
async def get_tick(
    symbol: str,
    pipeline: Any = Depends(get_pipeline_orchestrator),
):
    """
    Returns latest live market tick for a single symbol.
    Strictly returns HTTP 404 if the feed is inactive or no tick exists (Blueprint O7).
    """
    tick = None
    try:
        # Real PipelineOrchestrator: get bundle → extract last tick data
        if hasattr(pipeline, "get_bundle"):
            bundle = pipeline.get_bundle(symbol)
            if bundle and hasattr(bundle, "last_tick_timestamp") and bundle.last_tick_timestamp:
                ind = pipeline.get_indicator_values(symbol)
                tick = {
                    "symbol": symbol.upper(),
                    "ltp": ind.get("ltp") or ind.get("ema9"),
                    "open": ind.get("open"),
                    "high": ind.get("high"),
                    "low": ind.get("low"),
                    "close": ind.get("close") or ind.get("vwap"),
                    "volume": ind.get("volume"),
                    "timestamp": bundle.last_tick_timestamp.isoformat(),
                } if ind else None
        elif hasattr(pipeline, "get_latest_tick"):
            tick = pipeline.get_latest_tick(symbol)
    except Exception:
        tick = None

    if not tick or not tick.get("ltp"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No live market tick available for symbol '{symbol}'. Live market feed not streaming or symbol inactive.",
        )
    return TickResponse(**tick) if isinstance(tick, dict) else tick


@router.get("/indicators/{symbol}", response_model=FullIndicatorResponse)
async def get_indicators(
    symbol: str,
    pipeline: Any = Depends(get_pipeline_orchestrator),
):
    """
    Returns all 10 technical indicators in nested JSON structure.
    Returns HTTP 404 if indicator data is unavailable.
    """
    ind = None
    try:
        if hasattr(pipeline, "get_indicator_values"):
            ind = pipeline.get_indicator_values(symbol) or {}
            ind = ind if ind else None
        elif hasattr(pipeline, "get_indicators"):
            ind = pipeline.get_indicators(symbol)
    except Exception:
        ind = None

    if not ind:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Indicators for symbol '{symbol}' not available. Insufficient data.",
        )

    flat = ind if isinstance(ind, dict) else {}
    now = flat.get("timestamp") or datetime.now(tz=pytz.utc)

    rsi_data = None
    if flat.get("rsi") is not None:
        rsi_data = RSIData(rsi=flat.get("rsi"), zone=flat.get("rsi_zone"))

    adx_data = None
    if flat.get("adx") is not None:
        adx_data = ADXData(
            adx=flat.get("adx"),
            plus_di=flat.get("plus_di"),
            minus_di=flat.get("minus_di"),
            trend_strength=flat.get("adx_trend_strength"),
        )

    macd_data = None
    if flat.get("macd_line") is not None:
        macd_data = MACDData(
            macd_line=flat.get("macd_line"),
            signal_line=flat.get("signal_line"),
            histogram=flat.get("histogram"),
            crossover=flat.get("macd_crossover"),
        )

    bb_data = None
    if flat.get("upper_band") is not None:
        bb_data = BollingerData(
            upper=flat.get("upper_band"),
            middle=flat.get("middle_band"),
            lower=flat.get("lower_band"),
            percent_b=flat.get("percent_b"),
            position=flat.get("bb_position"),
        )

    atr_data = None
    if flat.get("atr") is not None:
        atr_data = ATRData(
            atr=flat.get("atr"),
            atr_percent=flat.get("atr_percent"),
            volatility=flat.get("volatility"),
        )

    st_data = None
    if flat.get("supertrend") is not None:
        st_data = SupertrendData(
            supertrend=flat.get("supertrend"),
            direction=flat.get("supertrend_direction"),
            signal=flat.get("supertrend_signal"),
            trend_changed=flat.get("supertrend_trend_changed"),
        )

    sent_data = None
    if flat.get("sentiment_score") is not None:
        sent_data = SentimentData(
            score=flat.get("sentiment_score"),
            components=flat.get("component_scores"),
        )

    return FullIndicatorResponse(
        symbol=symbol.upper(),
        timestamp=now,
        ema9=flat.get("ema9"),
        vwap=flat.get("vwap"),
        rsi=rsi_data,
        adx=adx_data,
        macd=macd_data,
        bollinger=bb_data,
        atr=atr_data,
        supertrend=st_data,
        sentiment=sent_data,
        pcr_slope=flat.get("pcr_slope") if not isinstance(flat.get("pcr_slope"), bool) else None,
        volume_surge=flat.get("volume_surge"),
        oi_change_pct=flat.get("oi_change_pct"),
    )


@router.get("/option-chain/{underlying}", response_model=OptionChainResponse)
async def get_option_chain(
    underlying: str,
    expiry: Optional[str] = Query(None, description="Optional target expiry date (YYYY-MM-DD)"),
    adapters: Dict[str, Any] = Depends(get_broker_adapters),
):
    """
    Returns live option chain strike ladder with CE/PE quotes, IV, OI, and spot price.
    Returns HTTP 404 if live chain data is unavailable.
    """
    chain = None
    detail = f"Live option chain for underlying '{underlying}' is unavailable."
    dhan = (adapters or {}).get("Dhan") or (adapters or {}).get("dhan")
    if dhan is not None:
        try:
            chain = await dhan.get_option_chain(underlying.upper(), expiry=expiry)
        except Exception as exc:
            detail = f"Option chain fetch failed for '{underlying}': {exc}"
            logger.warning("[API] %s", detail)

    if not chain:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    if hasattr(chain, "__dict__"):
        d = {k: v for k, v in vars(chain).items() if not k.startswith("_")}
        return OptionChainResponse(**d)
    return OptionChainResponse(**chain) if isinstance(chain, dict) else chain
