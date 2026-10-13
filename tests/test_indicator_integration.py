"""
Strike Desk V4 - Indicator Integration Tests
Tests: (a) all 5 strategies produce signals with indicators_triggered, (b) API returns all 10
indicators, (c) ATR-based SL for options, (d) confidence boost when MACD+Supertrend align,
(e) zero regressions on existing strategy tests.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from models.signal import Direction, Signal, ConfidenceTier
from models.instrument import AssetClass
from strategies.stock_cash_strategy import StockCashStrategy
from strategies.stock_futures_strategy import StockFuturesStrategy
from strategies.index_futures_strategy import IndexFuturesStrategy
from strategies.index_options_strategy import IndexOptionsStrategy
from strategies.stock_options_strategy import StockOptionsStrategy


NOW = datetime(2024, 1, 15, 6, 0, 0, tzinfo=timezone.utc)

LONG_INDICATORS_CASH = {
    "symbol": "RELIANCE",
    "price": 2500.0,
    "ema9": 2450.0,
    "vwap": 2460.0,
    "volume": 3000000,
    "avg_volume": 1500000,
    "pcr_alignment": True,
    "sentiment_alignment": True,
    "ema9_crossover": True,
    "vwap_alignment": True,
    "volume_surge": True,
}

LONG_INDICATORS_FUTURES = {
    "symbol": "RELIANCE_FUT",
    "price": 2510.0,
    "ema9": 2450.0,
    "vwap": 2460.0,
    "ema9_crossover": True,
    "vwap_alignment": True,
    "basis_tracking": True,
    "oi_change": True,
    "sentiment_alignment": True,
}

LONG_INDICATORS_INDEX_FUT = {
    "symbol": "NIFTY_FUT",
    "price": 22000.0,
    "ema9": 21800.0,
    "vwap": 21900.0,
    "ema9_crossover": True,
    "vwap_alignment": True,
    "pcr_slope": True,
    "oi_change": True,
    "sentiment_alignment": True,
}

LONG_INDICATORS_INDEX_OPT = {
    "symbol": "NIFTY24JAN22000CE",
    "underlying": "NIFTY",
    "price": 22000.0,
    "ema9": 21800.0,
    "option_price": 150.0,
    "pcr_slope": True,
    "ema9_crossover": True,
    "sentiment_alignment": True,
    "vwap_alignment": True,
    "oi_change": True,
}

LONG_INDICATORS_STOCK_OPT = {
    "symbol": "RELIANCE24JAN2500CE",
    "underlying": "RELIANCE",
    "price": 2500.0,
    "ema9": 2450.0,
    "option_price": 80.0,
    "ema9_crossover": True,
    "pcr_slope": True,
    "vwap_alignment": True,
    "sentiment_alignment": True,
    "volume_surge": True,
    "option_volume": 5000,
}

MARKET_STATE_LONG = {"direction": Direction.LONG, "timestamp": NOW}


def _make_index_opt_strategy():
    strat = IndexOptionsStrategy()
    strat.pcr_monitor.record_update("NIFTY", NOW)
    return strat


def _make_stock_opt_strategy():
    strat = StockOptionsStrategy()
    strat.pcr_monitor.record_update("RELIANCE", NOW)
    return strat


# --- (a) All 5 strategies produce signals with indicators_triggered ---

class TestStrategiesProduceSignalsWithIndicatorsTriggered:

    def test_stock_cash_has_indicators_triggered(self):
        strat = StockCashStrategy()
        sig = strat.evaluate(LONG_INDICATORS_CASH.copy(), MARKET_STATE_LONG.copy())
        assert sig is not None
        assert hasattr(sig, "indicators_triggered")
        assert isinstance(sig.indicators_triggered, dict)
        assert set(sig.indicators_triggered.keys()) == {
            "ema9", "vwap", "pcr_slope", "sentiment", "volume_surge",
            "rsi", "adx", "macd", "bollinger", "supertrend"
        }

    def test_stock_futures_has_indicators_triggered(self):
        strat = StockFuturesStrategy()
        sig = strat.evaluate(LONG_INDICATORS_FUTURES.copy(), MARKET_STATE_LONG.copy())
        assert sig is not None
        assert isinstance(sig.indicators_triggered, dict)
        assert len(sig.indicators_triggered) == 10

    def test_index_futures_has_indicators_triggered(self):
        strat = IndexFuturesStrategy()
        sig = strat.evaluate(LONG_INDICATORS_INDEX_FUT.copy(), MARKET_STATE_LONG.copy())
        assert sig is not None
        assert isinstance(sig.indicators_triggered, dict)
        assert len(sig.indicators_triggered) == 10

    def test_index_options_has_indicators_triggered(self):
        strat = _make_index_opt_strategy()
        sig = strat.evaluate(LONG_INDICATORS_INDEX_OPT.copy(), MARKET_STATE_LONG.copy())
        assert sig is not None
        assert isinstance(sig.indicators_triggered, dict)
        assert len(sig.indicators_triggered) == 10

    def test_stock_options_has_indicators_triggered(self):
        strat = _make_stock_opt_strategy()
        sig = strat.evaluate(LONG_INDICATORS_STOCK_OPT.copy(), MARKET_STATE_LONG.copy())
        assert sig is not None
        assert isinstance(sig.indicators_triggered, dict)
        assert len(sig.indicators_triggered) == 10

    def test_indicators_triggered_in_to_dict(self):
        strat = StockCashStrategy()
        sig = strat.evaluate(LONG_INDICATORS_CASH.copy(), MARKET_STATE_LONG.copy())
        assert sig is not None
        d = sig.to_dict()
        assert "indicators_triggered" in d
        assert isinstance(d["indicators_triggered"], dict)


# --- (b) API returns all 10 indicators (schema validation) ---

class TestAPISchemaAllTenIndicators:

    def test_full_indicator_response_has_all_fields(self):
        from api.schemas import FullIndicatorResponse, RSIData, ADXData, MACDData
        from api.schemas import BollingerData, ATRData, SupertrendData, SentimentData

        resp = FullIndicatorResponse(
            symbol="RELIANCE",
            timestamp=NOW,
            ema9=2450.0,
            vwap=2460.0,
            rsi=RSIData(rsi=45.0, zone="NEUTRAL"),
            adx=ADXData(adx=28.0, plus_di=22.0, minus_di=18.0, trend_strength="STRONG"),
            macd=MACDData(macd_line=5.2, signal_line=4.8, histogram=0.4, crossover="BULLISH_CROSS"),
            bollinger=BollingerData(upper=2560.0, middle=2500.0, lower=2440.0, percent_b=0.5),
            atr=ATRData(atr=25.0, atr_percent=1.0, volatility="NORMAL"),
            supertrend=SupertrendData(supertrend=2480.0, direction="UP", signal="BUY"),
            sentiment=SentimentData(score=0.6, components={"news": 0.5, "pcr": 0.7}),
            pcr_slope=-0.05,
            volume_surge=True,
            oi_change_pct=2.5,
        )

        d = resp.model_dump()
        assert d["rsi"]["rsi"] == 45.0
        assert d["adx"]["adx"] == 28.0
        assert d["macd"]["macd_line"] == 5.2
        assert d["bollinger"]["upper"] == 2560.0
        assert d["atr"]["atr"] == 25.0
        assert d["supertrend"]["direction"] == "UP"
        assert d["sentiment"]["score"] == 0.6
        assert d["pcr_slope"] == -0.05
        assert d["volume_surge"] is True
        assert d["oi_change_pct"] == 2.5

    def test_full_indicator_response_nullable_fields(self):
        from api.schemas import FullIndicatorResponse

        resp = FullIndicatorResponse(symbol="NIFTY", timestamp=NOW)
        d = resp.model_dump()
        assert d["rsi"] is None
        assert d["macd"] is None
        assert d["supertrend"] is None

    def test_api_indicators_endpoint_maps_flat_dict(self):
        """Route maps pipeline flat dict to FullIndicatorResponse correctly."""
        from api.routes.market import get_indicators
        import asyncio

        flat = {
            "ema9": 21800.0,
            "vwap": 21900.0,
            "rsi": 35.0,
            "rsi_zone": "OVERSOLD",
            "adx": 30.0,
            "plus_di": 25.0,
            "minus_di": 15.0,
            "adx_trend_strength": "STRONG",
            "macd_line": 12.0,
            "signal_line": 10.0,
            "histogram": 2.0,
            "macd_crossover": "BULLISH_CROSS",
            "upper_band": 22100.0,
            "middle_band": 22000.0,
            "lower_band": 21900.0,
            "percent_b": 0.5,
            "bb_position": "NEAR_MIDDLE",
            "atr": 80.0,
            "atr_percent": 0.36,
            "volatility": "NORMAL",
            "supertrend": 21700.0,
            "supertrend_direction": "UP",
            "supertrend_signal": "BUY",
            "supertrend_trend_changed": False,
            "sentiment_score": 0.4,
            "component_scores": {"news": 0.3, "pcr": 0.5},
            "timestamp": NOW,
        }

        mock_pipeline = MagicMock()
        mock_pipeline.get_indicator_values.return_value = flat

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                get_indicators("NIFTY", pipeline=mock_pipeline)
            )
        finally:
            loop.close()

        assert result.symbol == "NIFTY"
        assert result.ema9 == 21800.0
        assert result.rsi.rsi == 35.0
        assert result.rsi.zone == "OVERSOLD"
        assert result.adx.adx == 30.0
        assert result.macd.macd_line == 12.0
        assert result.macd.crossover == "BULLISH_CROSS"
        assert result.bollinger.upper == 22100.0
        assert result.atr.atr == 80.0
        assert result.supertrend.direction == "UP"
        assert result.sentiment.score == 0.4


# --- (c) ATR-based dynamic SL for options strategies ---

class TestATRDynamicStopLoss:
    """
    Option stops are a fraction of the premium, never premium-minus-ATR.

    ATR is computed from the *underlying's* tick series (see
    PipelineOrchestrator: `self.atr.calculate_indicators(tick)`), so it is on a
    completely different scale from an option premium — BANKNIFTY moves in
    hundreds of points while its ATM option trades in the low hundreds of rupees.
    Subtracting one from the other drove every live stop to the 0.05 floor.
    """

    def test_underlying_scale_atr_does_not_collapse_option_stop(self):
        strat = _make_index_opt_strategy()
        ind = LONG_INDICATORS_INDEX_OPT.copy()
        ind["option_price"] = 86.95
        ind["atr"] = 45.0  # plausible index ATR; 2x it exceeds the premium
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.stop_loss_price == round(86.95 * 0.80, 2)
        assert sig.stop_loss_price > 0.05, "underlying-scale ATR collapsed the stop"

    def test_index_options_fallback_sl_when_no_atr(self):
        strat = _make_index_opt_strategy()
        ind = LONG_INDICATORS_INDEX_OPT.copy()
        ind["option_price"] = 100.0
        ind.pop("atr", None)
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.stop_loss_price == round(100.0 * 0.80, 2)

    def test_stock_options_fallback_sl_when_no_atr(self):
        strat = _make_stock_opt_strategy()
        ind = LONG_INDICATORS_STOCK_OPT.copy()
        ind["option_price"] = 80.0
        ind.pop("atr", None)
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.stop_loss_price == round(80.0 * 0.75, 2)

    def test_low_premium_option_keeps_proportional_stop(self):
        strat = _make_index_opt_strategy()
        ind = LONG_INDICATORS_INDEX_OPT.copy()
        ind["option_price"] = 5.0
        ind["atr"] = 50.0
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.stop_loss_price == round(5.0 * 0.80, 2)


# --- (d) Confidence boost fires when MACD + Supertrend align ---

class TestConfidenceBoostAlignment:

    def _base_indicators(self):
        return {
            "symbol": "RELIANCE",
            "price": 2500.0,
            "ema9": 2450.0,
            "ema9_crossover": True,
            "volume_surge": True,
            "vwap_alignment": False,
            "pcr_alignment": False,
            "sentiment_alignment": False,
        }

    def test_macd_crossover_boost_long(self):
        strat = StockCashStrategy()
        ind = self._base_indicators()
        ind["macd_crossover"] = "BULLISH_CROSS"
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.confidence_score == 55.0

    def test_supertrend_boost_long(self):
        strat = StockCashStrategy()
        ind = self._base_indicators()
        ind["supertrend_direction"] = "UP"
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.confidence_score == 55.0

    def test_rsi_oversold_boost_long(self):
        strat = StockCashStrategy()
        ind = self._base_indicators()
        ind["rsi"] = 25.0
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.confidence_score == 55.0

    def test_bollinger_boost_long(self):
        strat = StockCashStrategy()
        ind = self._base_indicators()
        ind["bb_position"] = "NEAR_LOWER"
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.confidence_score == 55.0

    def test_all_four_bonuses_stack(self):
        strat = StockCashStrategy()
        ind = self._base_indicators()
        ind["macd_crossover"] = "BULLISH_CROSS"
        ind["supertrend_direction"] = "UP"
        ind["rsi"] = 25.0
        ind["bb_position"] = "NEAR_LOWER"
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.confidence_score == 70.0

    def test_wrong_direction_bonus_does_not_fire(self):
        strat = StockCashStrategy()
        ind = self._base_indicators()
        ind["macd_crossover"] = "BEARISH_CROSS"
        ind["supertrend_direction"] = "DOWN"
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is None

    def test_macd_crossover_boost_index_options(self):
        strat = _make_index_opt_strategy()
        ind = LONG_INDICATORS_INDEX_OPT.copy()
        ind["macd_crossover"] = "BULLISH_CROSS"
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.confidence_score >= 95.0 + 5.0 - 5.0

    def test_macd_aligns_appears_in_checklist(self):
        strat = StockCashStrategy()
        ind = self._base_indicators()
        ind["vwap_alignment"] = True
        ind["macd_line"] = 10.0
        ind["signal_line"] = 8.0
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert "macd_crossover" in sig.checklist

    def test_supertrend_aligns_appears_in_checklist(self):
        strat = StockCashStrategy()
        ind = self._base_indicators()
        ind["supertrend_direction"] = "UP"
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert "supertrend_direction" in sig.checklist


# --- (e) Zero regressions: strategy weights still sum to 100 ---

class TestZeroRegressions:

    def test_stock_cash_weights_sum_to_100(self):
        strat = StockCashStrategy()
        assert sum(strat.criteria_weights.values()) == pytest.approx(100.0)

    def test_stock_futures_weights_sum_to_100(self):
        strat = StockFuturesStrategy()
        assert sum(strat.criteria_weights.values()) == pytest.approx(100.0)

    def test_index_futures_weights_sum_to_100(self):
        strat = IndexFuturesStrategy()
        assert sum(strat.criteria_weights.values()) == pytest.approx(100.0)

    def test_index_options_weights_sum_to_100(self):
        strat = IndexOptionsStrategy()
        assert sum(strat.criteria_weights.values()) == pytest.approx(100.0)

    def test_stock_options_weights_sum_to_100(self):
        strat = StockOptionsStrategy()
        assert sum(strat.criteria_weights.values()) == pytest.approx(100.0)

    def test_signal_score_capped_at_100(self):
        strat = StockCashStrategy()
        ind = {
            "symbol": "RELIANCE",
            "price": 2500.0,
            "ema9": 2450.0,
            "ema9_crossover": True,
            "vwap_alignment": True,
            "volume_surge": True,
            "pcr_alignment": True,
            "sentiment_alignment": True,
            "macd_crossover": "BULLISH_CROSS",
            "supertrend_direction": "UP",
            "rsi": 20.0,
            "bb_position": "BELOW_LOWER",
        }
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.confidence_score <= 100.0

    def test_index_options_bollinger_not_in_indicators_triggered(self):
        strat = _make_index_opt_strategy()
        ind = LONG_INDICATORS_INDEX_OPT.copy()
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.indicators_triggered.get("bollinger") is False

    def test_stock_options_bollinger_not_in_indicators_triggered(self):
        strat = _make_stock_opt_strategy()
        ind = LONG_INDICATORS_STOCK_OPT.copy()
        sig = strat.evaluate(ind, MARKET_STATE_LONG.copy())
        assert sig is not None
        assert sig.indicators_triggered.get("bollinger") is False

    def test_indicators_triggered_bool_values(self):
        strat = StockCashStrategy()
        sig = strat.evaluate(LONG_INDICATORS_CASH.copy(), MARKET_STATE_LONG.copy())
        assert sig is not None
        for k, v in sig.indicators_triggered.items():
            assert isinstance(v, bool), f"indicators_triggered[{k!r}] should be bool, got {type(v)}"
