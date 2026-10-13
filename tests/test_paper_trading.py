"""
Paper trading: real live market data, virtual money.

The guarantee under test is narrow and absolute — while PAPER is armed, no broker
order API is invoked, by either the manual route or the auto-execution loop, while
fill prices still come from the live tick feed.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient

from adapters.paper_broker import PaperBrokerAdapter
from api.main import create_app
from api.routes.auth import require_operator
from api.routes.trading import EXEC_LIVE, EXEC_PAPER, execution_mode, get_paper_broker
from models.order import Order, OrderSide, OrderType


def mk_order(symbol="NIFTY", side="BUY", qty=50, price=None, cid="c1"):
    return Order(
        client_order_id=cid,
        symbol=symbol,
        exchange="NSE",
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        order_type=OrderType.LIMIT if price else OrderType.MARKET,
        quantity=qty,
        price=price or 0.0,
        broker="paper",
    )


class TestPaperLedger(unittest.TestCase):
    """Virtual money accounting against real prices."""

    def setUp(self):
        self.prices = {"NIFTY": 100.0}
        self.pb = PaperBrokerAdapter(
            ltp_provider=lambda s: self.prices.get(s.upper()), starting_cash=100_000.0
        )

    def test_a_market_order_fills_at_live_ltp(self):
        upd = asyncio.run(self.pb.place_order(mk_order(qty=10)))
        self.assertEqual(upd.state.value, "FILLED")
        self.assertEqual(upd.avg_fill_price, 100.0)
        self.assertTrue(upd.metadata["simulated"])
        self.assertEqual(self.pb.cash, 100_000.0 - 1000.0)

    def test_b_limit_order_fills_at_its_limit(self):
        upd = asyncio.run(self.pb.place_order(mk_order(qty=10, price=95.0)))
        self.assertEqual(upd.avg_fill_price, 95.0)

    def test_c_unpriced_symbol_is_rejected_not_invented(self):
        with self.assertRaises(ValueError):
            asyncio.run(self.pb.place_order(mk_order(symbol="NO_TICKS", qty=1)))
        self.assertEqual(self.pb.cash, 100_000.0)
        self.assertEqual(self.pb.positions, {})

    def test_d_unrealised_pnl_tracks_the_live_feed(self):
        asyncio.run(self.pb.place_order(mk_order(qty=10)))
        self.prices["NIFTY"] = 130.0
        pos = self.pb.get_positions_dict()[0]
        self.assertEqual(pos["pnl"], 300.0)          # (130-100) * 10
        self.assertEqual(pos["current_price"], 130.0)

    def test_e_closing_realises_pnl(self):
        asyncio.run(self.pb.place_order(mk_order(qty=10, cid="a")))
        self.prices["NIFTY"] = 130.0
        asyncio.run(self.pb.place_order(mk_order(side="SELL", qty=10, cid="b")))
        summary = self.pb.get_account_summary()
        self.assertEqual(summary["realized_pnl"], 300.0)
        self.assertEqual(summary["cash"], 100_300.0)
        self.assertEqual(summary["open_positions"], 0)

    def test_f_short_then_cover_realises_pnl(self):
        asyncio.run(self.pb.place_order(mk_order(side="SELL", qty=10, cid="s")))
        self.prices["NIFTY"] = 80.0
        asyncio.run(self.pb.place_order(mk_order(side="BUY", qty=10, cid="c")))
        self.assertEqual(self.pb.get_account_summary()["realized_pnl"], 200.0)

    def test_g_averages_up_on_add(self):
        asyncio.run(self.pb.place_order(mk_order(qty=10, price=100.0, cid="1")))
        asyncio.run(self.pb.place_order(mk_order(qty=10, price=120.0, cid="2")))
        pos = self.pb.positions["NIFTY"]
        self.assertEqual(pos.quantity, 20)
        self.assertEqual(pos.avg_price, 110.0)

    def test_h_paper_broker_imports_no_broker_sdk(self):
        src = open(os.path.join(PROJECT_ROOT, "adapters", "paper_broker.py")).read()
        for banned in ("dhanhq", "SmartConnect", "fyers", "DhanAdapter", "AngelOneAdapter"):
            self.assertNotIn(banned, src, f"paper broker reached for {banned}")


class TestExecutionRouting(unittest.TestCase):
    """The switch that decides whether an order is simulated or real."""

    def setUp(self):
        self.app = create_app()
        self.app.dependency_overrides[require_operator] = lambda: {
            "sub": "monuj", "role": "operator"
        }
        self.client = TestClient(self.app)
        self._saved = execution_mode.get()
        get_paper_broker().reset()

    def tearDown(self):
        execution_mode.set(self._saved)
        self.app.dependency_overrides.clear()
        get_paper_broker().reset()

    def test_a_defaults_to_paper(self):
        # The safe failure is simulating an order, never sending an unintended real one.
        from api.routes.trading import ExecutionModeController
        self.assertTrue(ExecutionModeController().is_paper())

    def test_b_mode_is_readable_and_settable(self):
        # LIVE arming now requires a passing preflight run. Mock it so this test
        # exercises mode switching without depending on live broker/feed services.
        from services.preflight import preflight_engine as _pf, PreflightReport, CheckResult, SEVERITY_CRITICAL, STATUS_PASS
        from unittest.mock import patch as _patch
        all_pass = PreflightReport(
            checks=[CheckResult("broker_auth", SEVERITY_CRITICAL, STATUS_PASS, "ok")],
            ran_at=__import__("datetime").datetime.now(__import__("pytz").timezone("Asia/Kolkata")),
            live_mode_blocked=False, overall="GREEN",
        )
        with _patch.object(_pf, "run_all", return_value=all_pass), \
             _patch("services.token_scheduler.token_scheduler") as mock_ts:
            mock_ts.get_live_mode_block_reason.return_value = (False, "")
            self.assertEqual(
                self.client.post("/api/v1/trading/execution-mode", json={"mode": "LIVE"}).json()["execution_mode"],
                EXEC_LIVE,
            )
        self.assertFalse(self.client.get("/api/v1/trading/execution-mode").json()["is_paper"])
        self.client.post("/api/v1/trading/execution-mode", json={"mode": "PAPER"})
        self.assertTrue(self.client.get("/api/v1/trading/execution-mode").json()["is_paper"])

    def test_c_invalid_mode_rejected(self):
        r = self.client.post("/api/v1/trading/execution-mode", json={"mode": "SEMI_REAL"})
        self.assertEqual(r.status_code, 400)

    def test_d_paper_order_never_touches_a_broker_adapter(self):
        """The guarantee: in PAPER, no adapter's place_order is called."""
        execution_mode.set(EXEC_PAPER)
        live_adapter = MagicMock()
        live_adapter.place_order = AsyncMock()

        from api.dependencies import get_broker_adapters
        self.app.dependency_overrides[get_broker_adapters] = lambda: {
            "angelone": live_adapter, "dhan": live_adapter
        }
        # The ledger binds its price source at construction, so stub that directly.
        pb = get_paper_broker()
        original_provider = pb._ltp_provider
        pb._ltp_provider = lambda _sym: 250.0
        try:
            r = self.client.post("/api/v1/trading/orders", json={
                "symbol": "NIFTY26SEP23100CE", "side": "BUY", "quantity": 1,
                "price": 250.0, "is_manual": True, "category": "AUTO_OPTIONS",
            })
        finally:
            pb._ltp_provider = original_provider

        live_adapter.place_order.assert_not_called()
        if r.status_code == 200:
            body = r.json()
            self.assertTrue(body["broker"].startswith("paper:"), body["broker"])
            self.assertTrue(body["is_simulated"])
            self.assertEqual(body["execution_mode"], EXEC_PAPER)
        else:
            # A risk-gate rejection is fine; the point is the broker was never called.
            self.assertIn(r.status_code, (400, 502))

    def test_e_paper_account_endpoint_reports_the_ledger(self):
        body = self.client.get("/api/v1/trading/paper/account").json()
        for key in ("starting_cash", "cash", "realized_pnl", "unrealized_pnl", "open_positions"):
            self.assertIn(key, body)

    def test_f_paper_reset_clears_the_ledger(self):
        pb = get_paper_broker()
        pb.cash = 1.0
        self.client.post("/api/v1/trading/paper/reset")
        self.assertEqual(pb.cash, pb.starting_cash)

    def test_g_market_data_is_untouched_by_paper_mode(self):
        # Paper affects the order path only; the data pipeline stays fully live.
        execution_mode.set(EXEC_PAPER)
        for path in ("/api/v1/trading/signals", "/api/v1/market/ticks"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


class TestPaperExitFlow(unittest.TestCase):
    """
    Entry → Exit round-trip in Paper Mode.

    Verifies the full loop the user clicks through the dashboard:
      1. Entry order fills and creates an open position.
      2. Exit order closes the position and realises P&L.
      3. The closed position is no longer returned by get_positions_dict().
      4. The realized P&L is visible in get_account_summary().
    """

    def setUp(self):
        self.prices = {"NIFTY": 22000.0, "HDFCBANK": 1600.0}
        self.pb = PaperBrokerAdapter(
            ltp_provider=lambda s: self.prices.get(s.upper()),
            starting_cash=500_000.0,
        )

    # ── core ledger behaviour ─────────────────────────────────────────────

    def test_long_entry_then_full_exit_leaves_flat_position(self):
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "BUY", qty=50, cid="e1")))
        self.assertEqual(self.pb.positions["NIFTY"].quantity, 50)

        self.prices["NIFTY"] = 22500.0
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "SELL", qty=50, cid="x1")))

        # Position is flat — quantity must be zero
        pos = self.pb.positions.get("NIFTY")
        if pos is not None:
            self.assertEqual(pos.quantity, 0)
        # Realized P&L = (22500 - 22000) * 50 = 25000
        summary = self.pb.get_account_summary()
        self.assertAlmostEqual(summary["realized_pnl"], 25_000.0)
        self.assertEqual(summary["open_positions"], 0)

    def test_short_entry_then_cover_leaves_flat_position(self):
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "SELL", qty=50, cid="s1")))
        self.prices["NIFTY"] = 21500.0
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "BUY", qty=50, cid="c1")))

        summary = self.pb.get_account_summary()
        self.assertAlmostEqual(summary["realized_pnl"], 25_000.0)  # shorted at 22k, covered at 21.5k
        self.assertEqual(summary["open_positions"], 0)

    def test_cash_balance_reflects_entry_and_exit(self):
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "BUY", qty=50, cid="e1")))
        self.prices["NIFTY"] = 22500.0
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "SELL", qty=50, cid="x1")))
        # Started 500k, bought 50 @ 22000 (cash −1,100,000), sold 50 @ 22500 (cash +1,125,000)
        # Net: 500k + 25k = 525k
        self.assertAlmostEqual(self.pb.cash, 525_000.0)

    # ── get_positions_dict filtering ──────────────────────────────────────

    def test_closed_position_does_not_appear_in_positions_dict(self):
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "BUY", qty=50, cid="e1")))
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "SELL", qty=50, cid="x1")))

        positions = self.pb.get_positions_dict()
        open_syms = [p["symbol"] for p in positions]
        self.assertNotIn("NIFTY", open_syms)

    def test_partial_exit_keeps_position_in_dict(self):
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "BUY", qty=50, cid="e1")))
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "SELL", qty=20, cid="x1")))

        positions = self.pb.get_positions_dict()
        syms = [p["symbol"] for p in positions]
        self.assertIn("NIFTY", syms)
        nifty = next(p for p in positions if p["symbol"] == "NIFTY")
        self.assertEqual(nifty["quantity"], 30)

    def test_multiple_symbols_exit_one_leaves_other_open(self):
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "BUY", qty=50, cid="n1")))
        asyncio.run(self.pb.place_order(mk_order("HDFCBANK", "BUY", qty=10, cid="h1")))
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "SELL", qty=50, cid="nx1")))

        positions = self.pb.get_positions_dict()
        syms = [p["symbol"] for p in positions]
        self.assertNotIn("NIFTY", syms)
        self.assertIn("HDFCBANK", syms)

    # ── realized P&L survives the position being closed ───────────────────

    def test_realized_pnl_accumulates_across_multiple_trades(self):
        # Trade 1: BUY 50 @ 22000, SELL 50 @ 22500 → +25000
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "BUY", qty=50, cid="e1")))
        self.prices["NIFTY"] = 22500.0
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "SELL", qty=50, cid="x1")))
        # Trade 2: BUY 50 @ 22500, SELL 50 @ 23000 → +25000
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "BUY", qty=50, cid="e2")))
        self.prices["NIFTY"] = 23000.0
        asyncio.run(self.pb.place_order(mk_order("NIFTY", "SELL", qty=50, cid="x2")))

        summary = self.pb.get_account_summary()
        self.assertAlmostEqual(summary["realized_pnl"], 50_000.0)
        self.assertEqual(summary["open_positions"], 0)

    # ── API integration (exit via the trading endpoint) ───────────────────

    def test_exit_via_api_endpoint_closes_paper_position(self):
        """
        Exercises the full HTTP path: entry order via API, then exit via API.
        Asserts the position is flat and account summary shows realized P&L.
        """
        from api.main import create_app
        from api.routes.auth import require_operator
        from api.routes.trading import execution_mode, get_paper_broker, EXEC_PAPER
        from fastapi.testclient import TestClient

        app = create_app()
        app.dependency_overrides[require_operator] = lambda: {"sub": "op", "role": "operator"}
        client = TestClient(app)
        saved = execution_mode.get()
        pb = get_paper_broker()
        pb.reset()

        try:
            execution_mode.set(EXEC_PAPER)
            # Disable tick_provider so get_fill_price falls back to ltp_provider.
            # In tests the pipeline orchestrator has no live ticks; _tick_provider=None
            # makes the paper broker use _ltp_provider directly (backward-compat path).
            saved_tick_provider = pb._tick_provider
            pb._tick_provider = None
            pb._ltp_provider = lambda _: 22000.0

            # Bypass the live feed gate (pipeline not running in unit tests).
            # qty=1 @ 22000 = ₹22,000 — well below the ₹200,000 per-order cap
            with patch("api.routes.trading.require_live_feed", return_value=None):
                # Entry
                r_entry = client.post("/api/v1/trading/orders", json={
                    "symbol": "NIFTY", "side": "BUY", "quantity": 1,
                    "is_manual": True, "price": 22000.0,
                })
                self.assertIn(r_entry.status_code, (200, 201), r_entry.text)

                # Move price and exit
                pb._ltp_provider = lambda _: 22500.0
                r_exit = client.post("/api/v1/trading/orders", json={
                    "symbol": "NIFTY", "side": "SELL", "quantity": 1,
                    "is_manual": True, "price": 22500.0,
                })
                self.assertIn(r_exit.status_code, (200, 201), r_exit.text)

            summary = pb.get_account_summary()
            self.assertEqual(summary["open_positions"], 0)
            self.assertAlmostEqual(summary["realized_pnl"], 500.0)  # (22500-22000)*1

            # Positions endpoint must not list the closed position
            positions_resp = client.get("/api/v1/trading/positions")
            self.assertEqual(positions_resp.status_code, 200)
            open_syms = [p["symbol"] for p in positions_resp.json()]
            self.assertNotIn("NIFTY", open_syms)

        finally:
            pb._tick_provider = saved_tick_provider
            execution_mode.set(saved)
            pb.reset()
            app.dependency_overrides.clear()


class TestPaperLotSizePnL(unittest.TestCase):
    """
    P&L must be multiplied by lot_size for futures; EQ stays at 1:1.
    These tests exercise the ledger layer directly (no API, no live feed).
    """

    def _make_broker(self, ltp_map):
        return PaperBrokerAdapter(ltp_provider=lambda sym: ltp_map.get(sym.upper()))

    # ------------------------------------------------------------------
    # EQ (lot_size=1) — baseline unchanged
    # ------------------------------------------------------------------

    def test_eq_unrealized_pnl_is_one_to_one(self):
        pb = self._make_broker({"HINDUNILVR": 1915.4})
        pb._apply_fill("HINDUNILVR", 10, 1911.3, lot_size=1, instrument_type="EQ")
        pos = pb.positions["HINDUNILVR"]
        expected = (1915.4 - 1911.3) * 10 * 1
        self.assertAlmostEqual(pos.unrealized_pnl(1915.4), expected, places=2)

    def test_eq_realized_pnl_is_one_to_one(self):
        pb = self._make_broker({})
        pb._apply_fill("INFY", 5, 1500.0, lot_size=1, instrument_type="EQ")
        pb._apply_fill("INFY", -5, 1550.0, lot_size=1, instrument_type="EQ")
        pos = pb.positions.get("INFY")
        # Position is flat with realized P&L; qty==0 but realized_pnl != 0 keeps it in dict
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.realized_pnl, (1550.0 - 1500.0) * 5 * 1, places=2)

    def test_eq_position_dict_has_lot_size_1(self):
        pb = self._make_broker({"TCS": 3500.0})
        pb._apply_fill("TCS", 2, 3480.0, lot_size=1, instrument_type="EQ")
        d = pb.positions["TCS"].to_dict(3500.0)
        self.assertEqual(d["lot_size"], 1)
        self.assertEqual(d["instrument_type"], "EQ")

    # ------------------------------------------------------------------
    # FUT (lot_size > 1) — P&L must include multiplier
    # ------------------------------------------------------------------

    def test_fut_unrealized_pnl_multiplied_by_lot_size(self):
        # HINDUNILVR FUT lot_size=500; 1 lot long, +₹4.1/share → ₹2,050 total
        pb = self._make_broker({"HINDUNILVR": 1915.4})
        pb._apply_fill("HINDUNILVR", 1, 1911.3, lot_size=500, instrument_type="FUT")
        pos = pb.positions["HINDUNILVR"]
        expected = (1915.4 - 1911.3) * 1 * 500
        self.assertAlmostEqual(pos.unrealized_pnl(1915.4), expected, places=2)

    def test_fut_short_unrealized_pnl_multiplied(self):
        # TITAN FUT lot_size=75; 1 lot short, -₹12/share → -₹900 total
        pb = self._make_broker({"TITAN": 4834.0})
        pb._apply_fill("TITAN", -1, 4822.0, lot_size=75, instrument_type="FUT")
        pos = pb.positions["TITAN"]
        expected = (4834.0 - 4822.0) * (-1) * 75
        self.assertAlmostEqual(pos.unrealized_pnl(4834.0), expected, places=2)

    def test_fut_realized_pnl_multiplied_by_lot_size(self):
        # BAJFINANCE FUT lot_size=125; buy 1 lot at 977.9, exit at 990 → ₹1,512.5
        pb = self._make_broker({})
        pb._apply_fill("BAJFINANCE", 1, 977.9, lot_size=125, instrument_type="FUT")
        pb._apply_fill("BAJFINANCE", -1, 990.0, lot_size=125, instrument_type="FUT")
        pos = pb.positions.get("BAJFINANCE")
        self.assertIsNotNone(pos)
        expected = (990.0 - 977.9) * 1 * 125
        self.assertAlmostEqual(pos.realized_pnl, expected, places=2)

    def test_fut_position_dict_includes_lot_size_and_type(self):
        pb = self._make_broker({"BANKNIFTY": 48500.0})
        pb._apply_fill("BANKNIFTY", 1, 48000.0, lot_size=15, instrument_type="FUT")
        d = pb.positions["BANKNIFTY"].to_dict(48500.0)
        self.assertEqual(d["lot_size"], 15)
        self.assertEqual(d["instrument_type"], "FUT")
        expected_pnl = (48500.0 - 48000.0) * 1 * 15
        self.assertAlmostEqual(d["pnl"], expected_pnl, places=2)

    def test_fut_pnl_differs_from_eq_pnl_for_same_price_move(self):
        # Sanity: same price move, same qty, different lot_size → different P&L
        pb = self._make_broker({})
        pb._apply_fill("EQ_SYM", 1, 100.0, lot_size=1, instrument_type="EQ")
        pb._apply_fill("FUT_SYM", 1, 100.0, lot_size=500, instrument_type="FUT")
        eq_pnl = pb.positions["EQ_SYM"].unrealized_pnl(110.0)
        fut_pnl = pb.positions["FUT_SYM"].unrealized_pnl(110.0)
        self.assertAlmostEqual(eq_pnl, 10.0, places=2)
        self.assertAlmostEqual(fut_pnl, 5000.0, places=2)

    def test_registry_fallback_uses_lot_size_1_when_not_found(self):
        # When place_order cannot resolve the instrument, lot_size defaults to 1.
        pb = self._make_broker({"UNKNOWN": 100.0})
        order = mk_order(symbol="UNKNOWN", side="BUY", qty=1, price=100.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None  # not in registry
            asyncio.run(pb.place_order(order))
        pos = pb.positions.get("UNKNOWN")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.lot_size, 1)

    def test_registry_lookup_populates_lot_size_on_position(self):
        # When the registry returns an instrument, its lot_size is stored on position.
        from models.instrument import Instrument, InstrumentType, AssetClass, Exchange
        fut_inst = Instrument(
            exchange=Exchange.NSE,
            symbol="HDFCBANK",
            asset_class=AssetClass.STOCK_FUTURES,
            instrument_type=InstrumentType.FUT,
            lot_size=550,
        )
        pb = self._make_broker({"HDFCBANK": 1700.0})
        order = mk_order(symbol="HDFCBANK", side="BUY", qty=1, price=1680.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = fut_inst
            asyncio.run(pb.place_order(order))
        pos = pb.positions.get("HDFCBANK")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.lot_size, 550)
        self.assertEqual(pos.instrument_type, "FUT")
        expected_pnl = (1700.0 - 1680.0) * 1 * 550
        self.assertAlmostEqual(pos.unrealized_pnl(1700.0), expected_pnl, places=2)

    def test_get_positions_dict_reflects_lot_size_pnl(self):
        pb = self._make_broker({"TITAN": 4834.0})
        pb._apply_fill("TITAN", -1, 4822.0, lot_size=75, instrument_type="FUT")
        result = pb.get_positions_dict()
        self.assertEqual(len(result), 1)
        d = result[0]
        self.assertEqual(d["lot_size"], 75)
        expected_pnl = (4834.0 - 4822.0) * (-1) * 75
        self.assertAlmostEqual(d["pnl"], expected_pnl, places=2)


class TestFutLotSizeResolution(unittest.TestCase):
    """
    Prove the full lot-size resolution chain:
      DhanDiscovery CSV → instrument_registry → paper_broker.place_order()
    as well as the two static fallback tiers (CORE_SYMBOLS, STOCK_FUTURES_LOT_SIZES).
    """

    def _make_broker(self, ltp_map):
        return PaperBrokerAdapter(ltp_provider=lambda sym: ltp_map.get(sym.upper()))

    # ------------------------------------------------------------------
    # resolve_fut_underlying helper
    # ------------------------------------------------------------------

    def test_resolve_fut_underlying_strips_day_month_2digit_year(self):
        from services.core_symbols import resolve_fut_underlying
        self.assertEqual(resolve_fut_underlying("HINDUNILVR29OCTFUT"), "HINDUNILVR")

    def test_resolve_fut_underlying_strips_day_month_4digit_year(self):
        from services.core_symbols import resolve_fut_underlying
        self.assertEqual(resolve_fut_underlying("RELIANCE29SEP2025FUT"), "RELIANCE")

    def test_resolve_fut_underlying_strips_index_futures(self):
        from services.core_symbols import resolve_fut_underlying
        self.assertEqual(resolve_fut_underlying("NIFTY29SEP26FUT"), "NIFTY")
        self.assertEqual(resolve_fut_underlying("BANKNIFTY25OCTFUT"), "BANKNIFTY")

    def test_resolve_fut_underlying_plain_symbol_unchanged(self):
        from services.core_symbols import resolve_fut_underlying
        self.assertEqual(resolve_fut_underlying("HINDUNILVR"), "HINDUNILVR")

    # ------------------------------------------------------------------
    # Dhan CSV parser: FUTIDX/FUTSTK rows → registry entries
    # ------------------------------------------------------------------

    def test_dhan_csv_emits_fut_entries_for_universe_futures(self):
        """FUTSTK rows for NIFTY100 underlyings produce UNDERLYING-FUT entries."""
        import io, csv
        from services.broker_discovery.dhan_discovery import DhanDiscovery

        csv_rows = [
            # Header
            "EXCH_ID,INSTRUMENT,UNDERLYING_SYMBOL,SYMBOL_NAME,SECURITY_ID,SERIES,ISIN,LOT_SIZE,TICK_SIZE,DISPLAY_NAME",
            # EQUITY row (should also be included)
            "NSE,EQUITY,HINDUNILVR,HINDUNILVR,1330,EQ,INE030A01027,1,0.05,HUL",
            # Two FUTSTK rows with same underlying — only first should appear
            "NSE,FUTSTK,HINDUNILVR,HINDUNILVR29OCT25FUT,99001,,INE030A01027,500,0.05,HUL OCT FUT",
            "NSE,FUTSTK,HINDUNILVR,HINDUNILVR27NOV25FUT,99002,,INE030A01027,500,0.05,HUL NOV FUT",
            # FUTIDX for NIFTY
            "NSE,FUTIDX,NIFTY,NIFTY29OCT25FUT,88001,,,25,0.05,NIFTY OCT FUT",
            # FUTSTK for a stock NOT in the universe — must be excluded
            "NSE,FUTSTK,UNKNOWN_CO,UNKNOWN29OCT25FUT,99099,,,200,0.05,UNKNOWN FUT",
        ]
        text = "\n".join(csv_rows)

        disc = DhanDiscovery()
        # tickers/isins represent the universe; HINDUNILVR and NIFTY are in it
        tickers = {"HINDUNILVR", "NIFTY"}
        isins = {"INE030A01027"}
        result = disc._parse_master_csv(text, tickers, isins)

        trading_syms = {r["trading_symbol"] for r in result}
        self.assertIn("HINDUNILVR-FUT", trading_syms)
        self.assertIn("NIFTY-FUT", trading_syms)
        self.assertNotIn("UNKNOWN_CO-FUT", trading_syms)
        # Deduplication: only one HINDUNILVR-FUT despite two monthly expiry rows
        hul_fut_rows = [r for r in result if r["trading_symbol"] == "HINDUNILVR-FUT"]
        self.assertEqual(len(hul_fut_rows), 1)
        self.assertEqual(hul_fut_rows[0]["lot_size"], "500")

    def test_dhan_csv_fut_entry_uses_synthetic_security_id(self):
        from services.broker_discovery.dhan_discovery import DhanDiscovery
        csv_rows = [
            "EXCH_ID,INSTRUMENT,UNDERLYING_SYMBOL,SYMBOL_NAME,SECURITY_ID,SERIES,ISIN,LOT_SIZE,TICK_SIZE,DISPLAY_NAME",
            "NSE,FUTSTK,TITAN,TITAN29OCT25FUT,55001,,,75,0.05,TITAN FUT",
        ]
        disc = DhanDiscovery()
        result = disc._parse_master_csv("\n".join(csv_rows), {"TITAN"}, set())
        fut_rows = [r for r in result if r["trading_symbol"] == "TITAN-FUT"]
        self.assertEqual(len(fut_rows), 1)
        self.assertEqual(fut_rows[0]["security_id"], "FUT-TITAN")

    # ------------------------------------------------------------------
    # paper_broker.place_order lot-size resolution — registry path
    # ------------------------------------------------------------------

    def test_place_order_resolves_lot_size_from_registry_fut_key(self):
        """Registry has HINDUNILVR-FUT with lot_size=500; order is HINDUNILVR29OCTFUT."""
        from models.instrument import Instrument, InstrumentType, AssetClass, Exchange
        fut_inst = Instrument(
            exchange=Exchange.NSE, symbol="HINDUNILVR-FUT",
            asset_class=AssetClass.STOCK_FUTURES, instrument_type=InstrumentType.FUT,
            lot_size=500,
        )
        pb = self._make_broker({"HINDUNILVR29OCTFUT": 1920.0})
        order = mk_order(symbol="HINDUNILVR29OCTFUT", side="BUY", qty=1, price=1910.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = fut_inst
            asyncio.run(pb.place_order(order))
        pos = pb.positions.get("HINDUNILVR29OCTFUT")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.lot_size, 500)
        self.assertEqual(pos.instrument_type, "FUT")

    def test_place_order_falls_back_to_core_symbols_for_index_futures(self):
        """Registry miss → CORE_SYMBOLS → NIFTY lot_size=25."""
        pb = self._make_broker({"NIFTY29OCTFUT": 24000.0})
        order = mk_order(symbol="NIFTY29OCTFUT", side="BUY", qty=1, price=23800.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None  # not in registry
            asyncio.run(pb.place_order(order))
        pos = pb.positions.get("NIFTY29OCTFUT")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.lot_size, 25)
        self.assertEqual(pos.instrument_type, "IDX_FUT")

    def test_place_order_falls_back_to_static_table_for_stock_futures(self):
        """Registry miss and not an index → STOCK_FUTURES_LOT_SIZES → TITAN lot_size=75."""
        pb = self._make_broker({"TITAN25OCTFUT": 5000.0})
        order = mk_order(symbol="TITAN25OCTFUT", side="BUY", qty=1, price=4900.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            asyncio.run(pb.place_order(order))
        pos = pb.positions.get("TITAN25OCTFUT")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.lot_size, 75)
        self.assertEqual(pos.instrument_type, "FUT")

    def test_place_order_lot_size_1_for_unknown_futures(self):
        """Completely unknown futures symbol → graceful fallback to lot_size=1."""
        pb = self._make_broker({"OBSCURECO29OCTFUT": 100.0})
        order = mk_order(symbol="OBSCURECO29OCTFUT", side="BUY", qty=1, price=99.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            asyncio.run(pb.place_order(order))
        pos = pb.positions.get("OBSCURECO29OCTFUT")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.lot_size, 1)

    def test_place_order_eq_symbol_unaffected_by_futures_logic(self):
        """A plain equity symbol must still resolve lot_size=1 via registry."""
        from models.instrument import Instrument, InstrumentType, AssetClass, Exchange
        eq_inst = Instrument(
            exchange=Exchange.NSE, symbol="INFY",
            asset_class=AssetClass.STOCK_CASH, instrument_type=InstrumentType.EQ,
            lot_size=1,
        )
        pb = self._make_broker({"INFY": 1550.0})
        order = mk_order(symbol="INFY", side="BUY", qty=10, price=1500.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = eq_inst
            asyncio.run(pb.place_order(order))
        pos = pb.positions.get("INFY")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.lot_size, 1)
        self.assertEqual(pos.instrument_type, "EQ")

    def test_fut_pnl_correct_with_registry_driven_lot_size(self):
        """End-to-end: HINDUNILVR29OCTFUT, lot_size=500, P&L = (ltp-entry)*qty*500."""
        from models.instrument import Instrument, InstrumentType, AssetClass, Exchange
        fut_inst = Instrument(
            exchange=Exchange.NSE, symbol="HINDUNILVR-FUT",
            asset_class=AssetClass.STOCK_FUTURES, instrument_type=InstrumentType.FUT,
            lot_size=500,
        )
        pb = self._make_broker({"HINDUNILVR29OCTFUT": 1920.0})
        order = mk_order(symbol="HINDUNILVR29OCTFUT", side="BUY", qty=1, price=1910.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = fut_inst
            asyncio.run(pb.place_order(order))
        pos = pb.positions["HINDUNILVR29OCTFUT"]
        expected_pnl = (1920.0 - 1910.0) * 1 * 500  # = 5000
        self.assertAlmostEqual(pos.unrealized_pnl(1920.0), expected_pnl, places=2)

    # ------------------------------------------------------------------
    # Static table completeness spot-checks
    # ------------------------------------------------------------------

    def test_static_table_contains_screenshot_symbols(self):
        """The four symbols from the user's screenshot must be in the static table."""
        from services.core_symbols import STOCK_FUTURES_LOT_SIZES
        for sym in ("HINDUNILVR", "TITAN", "BAJFINANCE", "M&M"):
            self.assertIn(sym, STOCK_FUTURES_LOT_SIZES, f"{sym} missing from STOCK_FUTURES_LOT_SIZES")
            self.assertGreater(STOCK_FUTURES_LOT_SIZES[sym], 1)

    def test_index_futures_in_core_symbols_not_static_table(self):
        """Index futures are covered by CORE_SYMBOLS, not STOCK_FUTURES_LOT_SIZES."""
        from services.core_symbols import CORE_SYMBOLS, STOCK_FUTURES_LOT_SIZES
        for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            self.assertIn(idx, CORE_SYMBOLS)
            self.assertNotIn(idx, STOCK_FUTURES_LOT_SIZES)


# ---------------------------------------------------------------------------
# Option lot-size resolution (paper broker + core_symbols helper)
# ---------------------------------------------------------------------------

class TestOptUnderlyingResolution(unittest.TestCase):
    """resolve_opt_underlying must strip expiry+strike+CE/PE to return the ticker."""

    def test_monthly_index_option_ce(self):
        from services.core_symbols import resolve_opt_underlying
        self.assertEqual(resolve_opt_underlying("NIFTY24OCT24250CE"), "NIFTY")

    def test_monthly_index_option_pe(self):
        from services.core_symbols import resolve_opt_underlying
        self.assertEqual(resolve_opt_underlying("BANKNIFTY24OCT50000PE"), "BANKNIFTY")

    def test_weekly_index_option_pure_digit_expiry(self):
        from services.core_symbols import resolve_opt_underlying
        # Weekly Dhan format: YYMMDD + strike + CE/PE
        self.assertEqual(resolve_opt_underlying("NIFTY2410031750CE"), "NIFTY")

    def test_monthly_stock_option(self):
        from services.core_symbols import resolve_opt_underlying
        self.assertEqual(resolve_opt_underlying("HINDUNILVR24OCT1900CE"), "HINDUNILVR")

    def test_plain_symbol_unchanged(self):
        from services.core_symbols import resolve_opt_underlying
        self.assertEqual(resolve_opt_underlying("NIFTY"), "NIFTY")


class TestPaperBrokerOptionLotSize(unittest.TestCase):
    """
    paper_broker.place_order must use lot_size > 1 for option symbols (CE/PE),
    resolving via the same UNDERLYING-FUT registry key used for futures.
    """

    def _make_broker(self, ltp_map):
        return PaperBrokerAdapter(ltp_provider=lambda sym: ltp_map.get(sym.upper()))

    def _make_opt_instrument(self, lot_size: int):
        from models.instrument import Instrument, Exchange, AssetClass, InstrumentType
        return Instrument(
            exchange=Exchange.NSE,
            symbol="NIFTY-FUT",
            asset_class=AssetClass.INDEX_FUTURES,
            instrument_type=InstrumentType.IDX_FUT,
            broker_symbol="NIFTY-FUT",
            token="",
            lot_size=lot_size,
            name="NIFTY Futures",
        )

    def test_option_symbol_uses_underlying_fut_registry_key(self):
        """
        A NIFTY CE order resolves lot_size from the NIFTY-FUT registry entry (25).
        unrealized_pnl = price_move × qty × lot_size.
        """
        opt_inst = self._make_opt_instrument(25)
        pb = self._make_broker({"NIFTY24OCT24250CE": 150.0})
        order = mk_order(symbol="NIFTY24OCT24250CE", side="BUY", qty=1, price=140.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = opt_inst
            asyncio.run(pb.place_order(order))
        pos = pb.positions["NIFTY24OCT24250CE"]
        self.assertEqual(pos.lot_size, 25)
        self.assertAlmostEqual(pos.unrealized_pnl(150.0), (150.0 - 140.0) * 1 * 25, places=2)

    def test_option_metadata_underlying_used_for_registry_lookup(self):
        """
        When order.metadata["underlying"] is set (option-chain path), it is used
        directly for the UNDERLYING-FUT registry lookup instead of the regex.
        """
        opt_inst = self._make_opt_instrument(15)
        pb = self._make_broker({"HINDUNILVR24OCT1900CE": 50.0})
        order = Order(
            client_order_id="c2",
            symbol="HINDUNILVR24OCT1900CE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=1,
            price=0.0,
            broker="paper",
            metadata={"underlying": "HINDUNILVR"},
        )
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = opt_inst
            asyncio.run(pb.place_order(order))
            # Confirm the registry was called with the correct key
            mock_get.assert_called_with(unittest.mock.ANY, "HINDUNILVR-FUT")
        pos = pb.positions["HINDUNILVR24OCT1900CE"]
        self.assertEqual(pos.lot_size, 15)

    def test_option_falls_back_to_core_symbols_when_registry_miss(self):
        """If the registry returns None, fall back to CORE_SYMBOLS lot_size."""
        pb = self._make_broker({"NIFTY24OCT24250CE": 200.0})
        order = mk_order(symbol="NIFTY24OCT24250CE", side="BUY", qty=1, price=190.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            asyncio.run(pb.place_order(order))
        pos = pb.positions["NIFTY24OCT24250CE"]
        from services.core_symbols import CORE_SYMBOLS
        expected_lot = CORE_SYMBOLS["NIFTY"]["lot_size"]
        self.assertEqual(pos.lot_size, expected_lot)
        self.assertGreater(pos.lot_size, 1)

    def test_option_falls_back_to_static_table_for_stock_option(self):
        """Stock options fall back to STOCK_FUTURES_LOT_SIZES if registry misses."""
        from services.core_symbols import STOCK_FUTURES_LOT_SIZES
        # Pick a symbol present in the static table
        underlying = next(iter(STOCK_FUTURES_LOT_SIZES))
        expected_lot = STOCK_FUTURES_LOT_SIZES[underlying]
        opt_sym = f"{underlying}24OCT1000CE"
        pb = self._make_broker({opt_sym: 10.0})
        order = mk_order(symbol=opt_sym, side="BUY", qty=1, price=9.0)
        with patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            asyncio.run(pb.place_order(order))
        pos = pb.positions[opt_sym]
        self.assertEqual(pos.lot_size, expected_lot)


# ---------------------------------------------------------------------------
# Dhan and Fyers: quantity × lot_size in place_order payloads
# ---------------------------------------------------------------------------

class TestDhanLotSizeMultiplication(unittest.IsolatedAsyncioTestCase):
    """
    DhanAdapter.place_order must multiply quantity by lot_size for NSE_FNO orders.
    Sending only the number-of-lots (without multiplying) causes the exchange to
    reject or partially fill the order.
    """

    async def _make_dhan_adapter(self):
        import logging
        from adapters.dhan_adapter import DhanAdapter
        adapter = DhanAdapter.__new__(DhanAdapter)
        adapter.settings = None
        adapter._client = MagicMock()
        adapter._client.place_order = MagicMock(return_value={"orderId": "D123"})
        adapter._equity_ids = {}
        adapter._client_ids_by_tag = {}
        # base_adapter.__init__ attributes required by invalidate_balance_cache
        adapter.broker_name = "dhan"
        adapter.logger = logging.getLogger("test.dhan")
        adapter._cached_balance = None
        adapter._balance_cached_at = 0.0
        return adapter

    async def test_nse_fno_quantity_multiplied_by_lot_size_from_registry(self):
        adapter = await self._make_dhan_adapter()
        order = Order(
            client_order_id="c1",
            symbol="NIFTY24OCT24250CE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=2,
            price=0.0,
            broker="dhan",
            metadata={
                "dhan_security_id": "12345",
                "dhan_segment": "NSE_FNO",
                "underlying": "NIFTY",
            },
        )
        from models.instrument import Instrument, Exchange, AssetClass, InstrumentType
        nifty_inst = Instrument(
            exchange=Exchange.NSE, symbol="NIFTY-FUT",
            asset_class=AssetClass.INDEX_FUTURES,
            instrument_type=InstrumentType.IDX_FUT,
            broker_symbol="NIFTY-FUT", token="", lot_size=25, name="NIFTY",
        )
        with patch("adapters.dhan_adapter.rate_limiter") as mock_rl, \
             patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_rl.acquire = AsyncMock()
            mock_get.return_value = nifty_inst
            await adapter.place_order(order)

        called_payload = adapter._client.place_order.call_args.kwargs
        self.assertEqual(called_payload["quantity"], 2 * 25,  # 50 total contracts
                         "Dhan must multiply quantity by lot_size for NSE_FNO orders")

    async def test_eq_order_quantity_not_multiplied(self):
        """EQ (non-FNO) orders must NOT have their quantity changed."""
        from adapters.dhan_adapter import DhanAdapter, _DHAN_SECURITY_IDS
        adapter = await self._make_dhan_adapter()
        # Use a stock EQ symbol that goes through the equity_ids path
        adapter._equity_ids = {"RELIANCE": (1, 500)}
        order = Order(
            client_order_id="c2",
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=10,
            price=0.0,
            broker="dhan",
        )
        with patch("adapters.dhan_adapter.rate_limiter") as mock_rl, \
             patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_rl.acquire = AsyncMock()
            mock_get.return_value = None
            await adapter.place_order(order)
        called_payload = adapter._client.place_order.call_args.kwargs
        self.assertEqual(called_payload["quantity"], 10,
                         "EQ orders must send raw quantity (lot_size=1)")


class TestFyersLotSizeMultiplication(unittest.IsolatedAsyncioTestCase):
    """
    FyersAdapter.place_order must multiply qty by lot_size for option/futures orders.
    """

    async def _make_fyers_adapter(self):
        import logging
        from adapters.fyers_adapter import FyersAdapter
        adapter = FyersAdapter.__new__(FyersAdapter)
        adapter.settings = None
        adapter._fyers_model = MagicMock()
        adapter._fyers_model.place_order = MagicMock(return_value={"s": "ok", "id": "F456"})
        adapter._fno_by_symbol = {}
        adapter._fno_index = {}
        adapter._fno_loaded_at = None
        adapter._equity_symbols = {}
        # base_adapter.__init__ attributes
        adapter.broker_name = "fyers"
        adapter.logger = logging.getLogger("test.fyers")
        adapter._cached_balance = None
        adapter._balance_cached_at = 0.0
        return adapter

    async def test_option_qty_multiplied_by_lot_size(self):
        from adapters.fyers_adapter import FyersAdapter, _FYERS_SYMBOL_MAP
        adapter = await self._make_fyers_adapter()
        # Use _fno_by_symbol direct lookup to avoid needing _fno_index populated.
        adapter._fno_by_symbol = {"NIFTY24OCT24250CE": {"symbol": "NSE:NIFTY24OCT24250CE"}}
        order = Order(
            client_order_id="c3",
            symbol="NIFTY24OCT24250CE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=2,
            price=0.0,
            broker="fyers",
            metadata={
                "underlying": "NIFTY",
            },
        )
        from models.instrument import Instrument, Exchange, AssetClass, InstrumentType
        nifty_inst = Instrument(
            exchange=Exchange.NSE, symbol="NIFTY-FUT",
            asset_class=AssetClass.INDEX_FUTURES,
            instrument_type=InstrumentType.IDX_FUT,
            broker_symbol="NIFTY-FUT", token="", lot_size=25, name="NIFTY",
        )
        with patch("adapters.fyers_adapter.rate_limiter") as mock_rl, \
             patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_rl.acquire = AsyncMock()
            mock_get.return_value = nifty_inst
            await adapter.place_order(order)

        called_data = adapter._fyers_model.place_order.call_args[0][0]
        self.assertEqual(called_data["qty"], 2 * 25,
                         "Fyers must multiply qty by lot_size for option orders")

    async def test_eq_symbol_qty_not_multiplied(self):
        """EQ orders on Fyers must send raw quantity (lot_size=1)."""
        from adapters.fyers_adapter import FyersAdapter
        adapter = await self._make_fyers_adapter()
        adapter._equity_symbols = {"RELIANCE": "NSE:RELIANCE-EQ"}
        order = Order(
            client_order_id="c4",
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=5,
            price=0.0,
            broker="fyers",
        )
        with patch("adapters.fyers_adapter.rate_limiter") as mock_rl, \
             patch("services.instrument_registry.instrument_registry.get", new_callable=AsyncMock) as mock_get:
            mock_rl.acquire = AsyncMock()
            mock_get.return_value = None
            await adapter.place_order(order)
        called_data = adapter._fyers_model.place_order.call_args[0][0]
        self.assertEqual(called_data["qty"], 5,
                         "EQ orders must send raw quantity (no lot_size multiplication)")


# ---------------------------------------------------------------------------
# Regression: option limit orders must fill when tick_provider is set and
# the option strike is not in the live feed (only the underlying is).
# ---------------------------------------------------------------------------

class TestPaperBrokerOptionFillWithTickProvider(unittest.IsolatedAsyncioTestCase):
    """
    Regression guard for the 502 / ValueError that fired when a limit option
    order was placed in Paper Mode.

    In production, PaperBrokerAdapter is constructed with *both* ltp_provider
    AND tick_provider. Option strikes are NOT subscribed to the WebSocket feed —
    only their underlying (e.g. NIFTY) is. When tick_provider returns None for
    the option symbol, get_fill_price must still honour the limit price rather
    than returning None and raising ValueError → 502.
    """

    def _make_mock_tick(self, ltp: float):
        t = MagicMock()
        t.ltp = ltp
        t.source = "live"
        return t

    def _make_broker_with_tick_provider(self, underlying_ltp_map, opt_tick_map=None):
        """
        ltp_provider returns LTP for any symbol.
        tick_provider returns a tick only for underlyings; returns None for option strikes
        (mimicking real WS feed behaviour).
        """
        if opt_tick_map is None:
            opt_tick_map = {}

        def tick_provider(sym):
            sym_u = sym.upper()
            if sym_u in opt_tick_map:
                return opt_tick_map[sym_u]
            if sym_u in underlying_ltp_map:
                return self._make_mock_tick(underlying_ltp_map[sym_u])
            return None

        def ltp_provider(sym):
            return underlying_ltp_map.get(sym.upper())

        return PaperBrokerAdapter(
            ltp_provider=ltp_provider,
            tick_provider=tick_provider,
        )

    async def test_stock_option_limit_order_fills_at_limit_price(self):
        """
        RELIANCE24OCT2400CE limit order fills at stated price even though the
        tick_provider has no tick for the individual option strike.
        Lot size should be resolved from registry (mocked to 250).
        """
        from models.instrument import Instrument, Exchange, AssetClass, InstrumentType
        inst = Instrument(
            exchange=Exchange.NSE,
            symbol="RELIANCE-FUT",
            asset_class=AssetClass.STOCK_FUTURES,
            instrument_type=InstrumentType.FUT,
            broker_symbol="RELIANCE-FUT",
            token="",
            lot_size=250,
            name="RELIANCE Futures",
        )
        # tick_provider returns None for option; no crash expected
        pb = self._make_broker_with_tick_provider({"RELIANCE": 2500.0})
        order = mk_order(symbol="RELIANCE24OCT2400CE", side="BUY", qty=1, price=55.0)

        with patch(
            "services.instrument_registry.instrument_registry.get",
            new_callable=AsyncMock,
            return_value=inst,
        ):
            update = await pb.place_order(order)

        self.assertEqual(update.state.value, "FILLED")
        self.assertAlmostEqual(update.avg_fill_price, 55.0)
        pos = pb.positions["RELIANCE24OCT2400CE"]
        self.assertEqual(pos.lot_size, 250)

    async def test_index_option_limit_order_fills_at_limit_price(self):
        """
        NIFTY24OCT22000CE limit order fills at stated price even though the
        tick_provider has no tick for the individual option strike.
        Lot size falls back to CORE_SYMBOLS (registry returns None).
        """
        pb = self._make_broker_with_tick_provider({"NIFTY": 22200.0})
        order = mk_order(symbol="NIFTY24OCT22000CE", side="BUY", qty=1, price=120.0)

        with patch(
            "services.instrument_registry.instrument_registry.get",
            new_callable=AsyncMock,
            return_value=None,  # registry miss → falls back to CORE_SYMBOLS
        ):
            update = await pb.place_order(order)

        self.assertEqual(update.state.value, "FILLED")
        self.assertAlmostEqual(update.avg_fill_price, 120.0)
        from services.core_symbols import CORE_SYMBOLS
        pos = pb.positions["NIFTY24OCT22000CE"]
        self.assertEqual(pos.lot_size, CORE_SYMBOLS["NIFTY"]["lot_size"])

    async def test_market_option_order_with_no_tick_raises_value_error(self):
        """
        A market order on an option with no live tick must raise ValueError
        (clean rejection), not crash the worker. The trading route converts
        this ValueError into a 502 with a human-readable detail field.
        """
        pb = self._make_broker_with_tick_provider({})  # no ticks at all
        order = mk_order(symbol="NIFTY24OCT22000CE", side="BUY", qty=1, price=None)

        with self.assertRaises(ValueError):
            with patch(
                "services.instrument_registry.instrument_registry.get",
                new_callable=AsyncMock,
                return_value=None,
            ):
                await pb.place_order(order)

    async def test_non_live_tick_source_still_raises_for_options(self):
        """
        If a tick IS found for the option symbol but its source is not 'live',
        the C1 guard must still raise ValueError (historical data forbidden).
        """
        stale_tick = MagicMock()
        stale_tick.ltp = 130.0
        stale_tick.source = "historical"
        pb = self._make_broker_with_tick_provider(
            {},
            opt_tick_map={"NIFTY24OCT22000CE": stale_tick},
        )
        order = mk_order(symbol="NIFTY24OCT22000CE", side="BUY", qty=1, price=120.0)

        with self.assertRaises(ValueError, msg="Non-live tick source must raise ValueError"):
            with patch(
                "services.instrument_registry.instrument_registry.get",
                new_callable=AsyncMock,
                return_value=None,
            ):
                await pb.place_order(order)


if __name__ == "__main__":
    unittest.main()
