"""
P0-2: Accounting Correctness — deterministic unit tests.

Covers:
- ChargeCalculator regulatory formulas (brokerage, STT, exchange, GST, SEBI)
- Trade.net_pnl auto-compute via __post_init__ (realized - charges)
- record_fill_in_risk() charge injection: exit fills get charges, entry fills stay neutral
- RiskStateManager.total_charges accumulation and total_risk_pnl

All tests are isolated and never modify production data paths.
Safety invariant: no live order path is tested or altered.
"""
import sys
import types
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch, call
import os
import tempfile


# ── sys.modules warm-up (mirrors test_p01_persistence.py) ────────────────────
try:
    from pipeline import tick_buffer as _warmup  # noqa: F401
except Exception:
    pass


# ══════════════════════════════════════════════════════════════════════════════
# Part A — ChargeCalculator formula correctness
# ══════════════════════════════════════════════════════════════════════════════

class TestChargeCalculatorBrokerage(unittest.TestCase):

    def setUp(self):
        from risk.charge_calculator import ChargeCalculator
        self.CC = ChargeCalculator

    def test_fno_flat_20_small_turnover(self):
        """F&O brokerage is flat ₹20 regardless of turnover size."""
        self.assertEqual(self.CC.calculate_brokerage(500.0, "FNO", "FNO"), 20.0)

    def test_fno_flat_20_large_turnover(self):
        """F&O brokerage stays ₹20 even for large turnover (no percentage cap)."""
        self.assertEqual(self.CC.calculate_brokerage(10_000_000.0, "FNO", "FNO"), 20.0)

    def test_delivery_equity_zero_brokerage(self):
        """Delivery equity brokerage is ₹0 (free delivery)."""
        self.assertEqual(self.CC.calculate_brokerage(100_000.0, "DELIVERY", "EQUITY"), 0.0)

    def test_intraday_equity_under_threshold(self):
        """Intraday equity brokerage is 0.03% when below ₹20 threshold (turnover < ₹66,666)."""
        turnover = 50_000.0
        expected = round(0.0003 * turnover, 4)  # < ₹20
        self.assertAlmostEqual(self.CC.calculate_brokerage(turnover, "INTRADAY", "EQUITY"), expected)
        self.assertLess(expected, 20.0)

    def test_intraday_equity_capped_at_20(self):
        """Intraday equity brokerage is capped at ₹20 for large turnover."""
        self.assertEqual(self.CC.calculate_brokerage(200_000.0, "INTRADAY", "EQUITY"), 20.0)

    def test_zero_turnover_zero_brokerage(self):
        """Zero turnover yields zero brokerage across all types."""
        self.assertEqual(self.CC.calculate_brokerage(0.0, "FNO", "FNO"), 0.0)
        self.assertEqual(self.CC.calculate_brokerage(0.0, "INTRADAY", "EQUITY"), 0.0)


class TestChargeCalculatorSTT(unittest.TestCase):

    def setUp(self):
        from risk.charge_calculator import ChargeCalculator
        self.CC = ChargeCalculator

    def test_fno_stt_sell_side(self):
        """F&O STT is 0.0625% (0.000625) on sell side."""
        turnover = 100_000.0
        expected = round(0.000625 * turnover, 4)
        self.assertAlmostEqual(self.CC.calculate_stt(turnover, "FNO", "FNO", is_sell=True), expected)

    def test_fno_stt_buy_side_zero(self):
        """F&O STT is ₹0 on buy side."""
        self.assertEqual(self.CC.calculate_stt(100_000.0, "FNO", "FNO", is_sell=False), 0.0)

    def test_intraday_equity_stt_sell_only(self):
        """Intraday equity STT 0.025% applies only on sell."""
        turnover = 100_000.0
        expected = round(0.00025 * turnover, 4)
        self.assertAlmostEqual(
            self.CC.calculate_stt(turnover, "INTRADAY", "EQUITY", is_sell=True), expected
        )
        self.assertEqual(
            self.CC.calculate_stt(turnover, "INTRADAY", "EQUITY", is_sell=False), 0.0
        )

    def test_delivery_equity_stt_both_sides(self):
        """Delivery equity STT 0.1% on both buy and sell."""
        turnover = 100_000.0
        expected = round(0.001 * turnover, 4)
        self.assertAlmostEqual(
            self.CC.calculate_stt(turnover, "DELIVERY", "EQUITY", is_sell=True), expected
        )
        self.assertAlmostEqual(
            self.CC.calculate_stt(turnover, "DELIVERY", "EQUITY", is_sell=False), expected
        )


class TestChargeCalculatorTotalCharges(unittest.TestCase):

    def setUp(self):
        from risk.charge_calculator import ChargeCalculator
        self.CC = ChargeCalculator

    def test_total_equals_sum_of_components(self):
        """Total charges = brokerage + STT + exchange + GST + SEBI."""
        turnover, trade_type, segment = 100_000.0, "FNO", "FNO"
        breakdown = self.CC.get_charges_breakdown(turnover, trade_type, segment, is_sell=True)
        computed_sum = round(
            breakdown["brokerage"] + breakdown["stt"] + breakdown["exchange_charges"]
            + breakdown["gst"] + breakdown["sebi_charges"],
            4,
        )
        self.assertAlmostEqual(breakdown["total_charges"], computed_sum, places=4)

    def test_fno_sell_charges_positive(self):
        """F&O sell-side produces positive total charges."""
        charges = self.CC.calculate_total_charges(100_000.0, "FNO", "FNO", is_sell=True)
        self.assertGreater(charges, 0.0)

    def test_fno_buy_charges_positive(self):
        """F&O buy-side still has brokerage + exchange + GST + SEBI (no STT)."""
        charges = self.CC.calculate_total_charges(100_000.0, "FNO", "FNO", is_sell=False)
        self.assertGreater(charges, 0.0)

    def test_charges_increase_with_turnover(self):
        """Higher turnover yields higher charges (all components are turnover-based)."""
        c1 = self.CC.calculate_total_charges(50_000.0, "FNO", "FNO", is_sell=True)
        c2 = self.CC.calculate_total_charges(200_000.0, "FNO", "FNO", is_sell=True)
        self.assertGreater(c2, c1)


# ══════════════════════════════════════════════════════════════════════════════
# Part B — Trade.net_pnl auto-compute (models/risk_state.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestTradeNetPnl(unittest.TestCase):

    def setUp(self):
        from models.risk_state import Trade
        self.Trade = Trade

    def test_exit_fill_net_pnl_is_realized_minus_charges(self):
        """Net P&L = realized - charges when charges > 0 and no explicit net_pnl."""
        t = self.Trade(
            trade_id="T1", symbol="NIFTY25000CE", side="SELL",
            quantity=50.0, entry_price=150.0,
            realized_pnl=500.0, charges=45.0,
        )
        self.assertAlmostEqual(t.net_pnl, 500.0 - 45.0)

    def test_win_becomes_loss_after_large_charges(self):
        """A small positive realized P&L becomes net loss once charges exceed it."""
        t = self.Trade(
            trade_id="T2", symbol="NIFTY25000CE", side="SELL",
            quantity=50.0, entry_price=150.0,
            realized_pnl=10.0, charges=45.0,
        )
        self.assertLess(t.net_pnl, 0.0)
        self.assertAlmostEqual(t.net_pnl, 10.0 - 45.0)

    def test_entry_fill_neutral_sentinel(self):
        """Entry fill (realized=0, charges=0) yields net_pnl=0 — neutral sentinel."""
        t = self.Trade(
            trade_id="T3", symbol="BANKNIFTY50000CE", side="BUY",
            quantity=15.0, entry_price=200.0,
            realized_pnl=0.0, charges=0.0,
        )
        self.assertEqual(t.net_pnl, 0.0)

    def test_negative_realized_pnl_subtracted_from_charges(self):
        """Loss trade: net_pnl = negative realized - charges (deeper loss)."""
        t = self.Trade(
            trade_id="T4", symbol="SENSEX79000PE", side="SELL",
            quantity=10.0, entry_price=50.0,
            realized_pnl=-200.0, charges=30.0,
        )
        self.assertAlmostEqual(t.net_pnl, -200.0 - 30.0)

    def test_explicit_net_pnl_not_overridden(self):
        """When net_pnl is explicitly passed as non-zero, __post_init__ must not override it."""
        t = self.Trade(
            trade_id="T5", symbol="INFY", side="BUY",
            quantity=10.0, entry_price=1800.0,
            realized_pnl=100.0, charges=25.0, net_pnl=999.0,
        )
        self.assertEqual(t.net_pnl, 999.0)


# ══════════════════════════════════════════════════════════════════════════════
# Part C — record_fill_in_risk() charge injection
# Tests that exit fills receive computed charges, entry fills remain neutral.
# ══════════════════════════════════════════════════════════════════════════════

def _install_trading_mocks():
    """Install minimal stubs so api.routes.trading can be loaded."""
    sys.modules.setdefault("api", types.ModuleType("api"))

    deps = types.ModuleType("api.dependencies")
    deps.get_broker_adapters = MagicMock()
    sys.modules.setdefault("api.dependencies", deps)

    sys.modules.setdefault("api.routes", types.ModuleType("api.routes"))

    auth = types.ModuleType("api.routes.auth")
    auth.require_operator = MagicMock()
    sys.modules.setdefault("api.routes.auth", auth)

    mock_fastapi = MagicMock()

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            self.status_code = status_code
            self.detail = detail
    mock_fastapi.HTTPException = _HTTPException

    class _MockStatus:
        HTTP_400_BAD_REQUEST = 400
        HTTP_403_FORBIDDEN = 403
        HTTP_404_NOT_FOUND = 404
        HTTP_409_CONFLICT = 409
        HTTP_422_UNPROCESSABLE_ENTITY = 422
        HTTP_500_INTERNAL_SERVER_ERROR = 500
        HTTP_501_NOT_IMPLEMENTED = 501
        HTTP_502_BAD_GATEWAY = 502
        HTTP_503_SERVICE_UNAVAILABLE = 503
    mock_fastapi.status = _MockStatus()
    mock_fastapi.APIRouter = MagicMock(return_value=MagicMock())
    mock_fastapi.Depends = lambda x: x
    sys.modules.setdefault("fastapi", mock_fastapi)

    class _BaseModel:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
    mock_pydantic = MagicMock()
    mock_pydantic.BaseModel = _BaseModel
    mock_pydantic.Field = lambda default=None, *a, **kw: default
    sys.modules.setdefault("pydantic", mock_pydantic)

    from datetime import timezone as _tz
    mock_pytz = MagicMock()
    mock_pytz.timezone = MagicMock(return_value=_tz.utc)
    sys.modules.setdefault("pytz", mock_pytz)
    sys.modules.setdefault("dotenv", MagicMock())

    for sub in [
        "fastapi.responses", "fastapi.middleware", "fastapi.middleware.cors",
        "fastapi.routing", "fastapi.security", "fastapi.encoders", "fastapi.params",
    ]:
        sys.modules.setdefault(sub, MagicMock())


import importlib.util

def _load_trading_module():
    pkg = "api.routes.trading"
    if pkg in sys.modules and not isinstance(sys.modules[pkg], MagicMock):
        return sys.modules[pkg]
    _install_trading_mocks()
    fpath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api", "routes", "trading.py",
    )
    try:
        spec = importlib.util.spec_from_file_location(pkg, fpath, submodule_search_locations=[])
        mod = types.ModuleType(pkg)
        mod.__package__ = "api.routes"
        mod.__spec__ = spec
        mod.__file__ = fpath
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        sys.modules.pop(pkg, None)
        return None


_TRADING_MOD = _load_trading_module()


class TestRecordFillInRiskCharges(unittest.TestCase):
    """
    Tests that record_fill_in_risk() injects computed charges into the Trade
    for exit fills and preserves the neutral sentinel (charges=0, net_pnl=0)
    for entry fills.

    We patch the lazy imports inside record_fill_in_risk() so no live risk
    singletons are exercised.
    """

    @classmethod
    def setUpClass(cls):
        if _TRADING_MOD is None:
            raise unittest.SkipTest("api.routes.trading could not be loaded — skipping")

    def _make_order(self, symbol="NIFTY25000CE", quantity=1, side_str="SELL"):
        from models.order import OrderSide
        order = MagicMock()
        order.symbol = symbol
        order.quantity = quantity
        # Use the real enum so comparisons like `order.side == _Side.SELL` work.
        # The enum's .value is already the string ("BUY" / "SELL").
        order.side = OrderSide.SELL if side_str == "SELL" else OrderSide.BUY
        return order

    def _make_update(self, lot_size=50, realized_pnl=None):
        update = MagicMock()
        meta = {"lot_size": lot_size}
        if realized_pnl is not None:
            meta["realized_pnl"] = realized_pnl
        update.metadata = meta
        return update

    def _call_record_fill(self, order, update, realized_from_rm, order_id, fill_price, category):
        """
        Patch the lazy imports inside record_fill_in_risk and capture the Trade
        passed to circuit_breaker.on_trade_completed().
        """
        captured = {}

        mock_rm = MagicMock()
        mock_rm.apply_fill.return_value = realized_from_rm
        mock_rm.get_risk_summary.return_value = {
            "trade_count": 0, "open_positions_count": 0,
            "realized_pnl": 0.0, "total_risk_pnl": 0.0,
        }

        mock_cb = MagicMock()
        def capture_trade(trade):
            captured["trade"] = trade
        mock_cb.on_trade_completed.side_effect = capture_trade

        with patch.dict("sys.modules", {
            "risk.risk_manager": MagicMock(risk_manager=mock_rm),
            "risk.circuit_breaker": MagicMock(circuit_breaker=mock_cb),
        }):
            _TRADING_MOD.record_fill_in_risk(order, update, order_id, fill_price, category)

        return captured.get("trade")

    def test_exit_fill_fno_sell_charges_nonzero(self):
        """Exit fill (realized != 0) for F&O sell gets non-zero charges."""
        order = self._make_order(side_str="SELL")
        update = self._make_update(lot_size=50, realized_pnl=500.0)
        # lot_size=50, qty=1 → units=50, fill_price=150 → turnover=7500
        trade = self._call_record_fill(order, update, 0.0, "ORD-001", 150.0, "OPTIONS")
        self.assertIsNotNone(trade)
        self.assertGreater(trade.charges, 0.0)

    def test_exit_fill_net_pnl_is_realized_minus_charges(self):
        """Exit fill: trade.net_pnl must equal realized - charges."""
        order = self._make_order(side_str="SELL")
        realized = 500.0
        update = self._make_update(lot_size=50, realized_pnl=realized)
        trade = self._call_record_fill(order, update, 0.0, "ORD-002", 150.0, "OPTIONS")
        self.assertIsNotNone(trade)
        self.assertAlmostEqual(trade.net_pnl, trade.realized_pnl - trade.charges, places=4)

    def test_entry_fill_charges_zero_net_pnl_zero(self):
        """Entry fill (realized=0): charges=0 and net_pnl=0 (neutral sentinel preserved)."""
        order = self._make_order(side_str="BUY")
        update = self._make_update(lot_size=50, realized_pnl=None)
        # RiskManager returns 0 for opening a new position
        trade = self._call_record_fill(order, update, 0.0, "ORD-003", 150.0, "OPTIONS")
        self.assertIsNotNone(trade)
        self.assertEqual(trade.charges, 0.0)
        self.assertEqual(trade.net_pnl, 0.0)

    def test_exit_fill_equity_intraday_charges_nonzero(self):
        """Exit fill for EQUITY intraday sell also gets non-zero charges."""
        order = self._make_order(symbol="RELIANCE", quantity=100, side_str="SELL")
        # lot_size=1 for equity, qty=100, price=2500 → turnover=250000
        update = self._make_update(lot_size=1, realized_pnl=1000.0)
        trade = self._call_record_fill(order, update, 0.0, "ORD-004", 2500.0, "EQUITY")
        self.assertIsNotNone(trade)
        self.assertGreater(trade.charges, 0.0)
        self.assertAlmostEqual(trade.net_pnl, trade.realized_pnl - trade.charges, places=4)

    def test_exit_fill_buy_side_no_stt_but_has_brokerage(self):
        """Exit fill on BUY side has no STT but still has brokerage+exchange charges."""
        order = self._make_order(side_str="BUY")
        update = self._make_update(lot_size=50, realized_pnl=-200.0)
        trade = self._call_record_fill(order, update, 0.0, "ORD-005", 100.0, "OPTIONS")
        self.assertIsNotNone(trade)
        self.assertGreater(trade.charges, 0.0)

    def test_charges_not_assigned_when_no_trade_captured(self):
        """Failure path: if Trade is not captured, no exception should propagate."""
        # record_fill_in_risk has a broad except that swallows failures
        # This test verifies the function returns cleanly even when cb raises
        order = self._make_order()
        update = self._make_update(lot_size=50, realized_pnl=500.0)
        mock_rm = MagicMock()
        mock_rm.apply_fill.return_value = 500.0
        mock_rm.get_risk_summary.return_value = {
            "trade_count": 0, "open_positions_count": 0,
            "realized_pnl": 0.0, "total_risk_pnl": 0.0,
        }
        mock_cb = MagicMock()
        mock_cb.on_trade_completed.side_effect = RuntimeError("simulated failure")
        with patch.dict("sys.modules", {
            "risk.risk_manager": MagicMock(risk_manager=mock_rm),
            "risk.circuit_breaker": MagicMock(circuit_breaker=mock_cb),
        }):
            # Must not raise; error is swallowed and logged as CRITICAL
            _TRADING_MOD.record_fill_in_risk(order, update, "ORD-006", 150.0, "OPTIONS")


# ══════════════════════════════════════════════════════════════════════════════
# Part D — RiskStateManager.total_charges accumulation
# ══════════════════════════════════════════════════════════════════════════════

class TestRiskStateChargesAccumulation(unittest.TestCase):
    """
    Directly drives RiskStateManager.record_trade() with Trade objects that
    carry non-zero charges and verifies total_charges accumulates correctly.
    Uses temp files to stay isolated from production state.
    """

    def setUp(self):
        from risk.risk_state_manager import RiskStateManager

        self._td = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._td.name, "risk.db")
        self._json = os.path.join(self._td.name, "risk.json")
        self.rsm = RiskStateManager(
            db_path=self._db,
            json_backup_path=self._json,
            force_reinit=True,
        )
        self.rsm.reset_for_tests(test_date=date(2026, 9, 21))

    def tearDown(self):
        self._td.cleanup()

    def test_exit_trade_charges_accumulate(self):
        """total_charges grows by trade.charges after each exit fill."""
        from models.risk_state import Trade
        t1 = Trade("T1", "NIFTY25000CE", "SELL", 50.0, 150.0,
                   realized_pnl=500.0, charges=45.0)
        self.rsm.record_trade(t1)
        state = self.rsm.get_state()
        self.assertAlmostEqual(state.total_charges, 45.0, places=4)

        t2 = Trade("T2", "BANKNIFTY50000CE", "SELL", 15.0, 200.0,
                   realized_pnl=-200.0, charges=30.0)
        self.rsm.record_trade(t2)
        state = self.rsm.get_state()
        self.assertAlmostEqual(state.total_charges, 75.0, places=4)

    def test_entry_fill_neutral_no_charge_accumulation(self):
        """Entry fill (charges=0, net_pnl=0) does not add to total_charges or losses."""
        from models.risk_state import Trade
        t_entry = Trade("T_ENTRY", "NIFTY25000CE", "BUY", 50.0, 150.0,
                        realized_pnl=0.0, charges=0.0)
        self.rsm.record_trade(t_entry)
        state = self.rsm.get_state()
        self.assertEqual(state.total_charges, 0.0)
        self.assertEqual(state.consecutive_losses, 0)

    def test_total_risk_pnl_subtracts_charges(self):
        """total_risk_pnl = realized + unrealized - total_charges (Review Gap B)."""
        from models.risk_state import Trade
        t = Trade("T3", "SENSEX79000PE", "SELL", 10.0, 50.0,
                  realized_pnl=300.0, charges=60.0)
        self.rsm.record_trade(t)
        state = self.rsm.get_state()
        # unrealized = 0 (no positions set)
        expected_total_risk = state.realized_pnl + state.unrealized_pnl - state.total_charges
        self.assertAlmostEqual(state.total_risk_pnl, expected_total_risk, places=4)
        # Specifically: 300 + 0 - 60 = 240
        self.assertAlmostEqual(state.total_risk_pnl, 240.0, places=4)

    def test_win_after_charges_resets_consecutive_losses(self):
        """A trade with net_pnl > 0 (win after charges) resets consecutive_losses to 0."""
        from models.risk_state import Trade
        # First: a losing trade
        self.rsm.record_trade(Trade("L1", "NIFTY", "SELL", 50.0, 100.0,
                                   realized_pnl=-300.0, charges=20.0))
        state = self.rsm.get_state()
        self.assertEqual(state.consecutive_losses, 1)

        # Win after charges: realized=500, charges=40 → net_pnl=460 > 0
        self.rsm.record_trade(Trade("W1", "NIFTY", "SELL", 50.0, 100.0,
                                   realized_pnl=500.0, charges=40.0))
        state = self.rsm.get_state()
        self.assertEqual(state.consecutive_losses, 0)

    def test_win_turned_loss_by_charges_increments_streak(self):
        """
        A trade where realized > 0 but net_pnl < 0 (charges exceed realized gain)
        must count as a LOSS and increment consecutive_losses.
        This is the Review Gap B scenario.
        """
        from models.risk_state import Trade
        # realized=10, charges=45 → net_pnl=-35 < 0
        t = Trade("WL1", "NIFTY", "SELL", 50.0, 100.0,
                  realized_pnl=10.0, charges=45.0)
        self.rsm.record_trade(t)
        state = self.rsm.get_state()
        self.assertEqual(state.consecutive_losses, 1)
        self.assertEqual(state.last_trade_result, "LOSS")


if __name__ == "__main__":
    unittest.main()
