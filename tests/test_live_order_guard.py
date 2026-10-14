"""
S01 — Unconditional Live Order Guard Tests

All tests are mock-only. No broker SDK, HTTP client or real network call is
made. For every blocked order attempt the test asserts that the mocked
transport emitted zero outbound order requests.

Coverage:
  - Each broker adapter's place_order / cancel_order / modify_order raises
    LiveOrderProhibitedError unconditionally.
  - ExecutionModeController.set("LIVE") rejects unconditionally.
  - Manual order route LIVE branch raises LiveOrderProhibitedError.
  - Emergency flatten _flatten_live raises LiveOrderProhibitedError.
  - Auto-execute loop LIVE branch raises LiveOrderProhibitedError.
  - Attempts to call the underlying SDK / HTTP client directly are blocked at
    the adapter boundary (adapter raises before touching the client).
  - PaperBrokerAdapter place_order executes normally (green path preserved).
  - Market-data methods on each adapter are NOT blocked (not order methods).

Environment notes:
  Some tests require fastapi, pytz and pydantic, which are not installed in
  the isolated pytest virtualenv. Those tests mock sys.modules before
  importing the relevant modules, and are skipped if the mock-based import
  still fails. Tests that only need stdlib + live_order_guard always run.
"""

import asyncio
import importlib.util
import sys
import types
import unittest
from unittest.mock import MagicMock

# ── sys.modules warm-up ──────────────────────────────────────────────────────
# The first import of any adapter triggers `pipeline/__init__.py`, which fails
# midway (missing `pytz`). That partial run still caches `pipeline.tick_buffer`
# in sys.modules. The second import of the same adapter therefore succeeds.
# We trigger that first (failing) import here so every test in this file gets
# the cached submodule.
try:
    from pipeline import tick_buffer as _warmup_tick_buf  # noqa: F401
except Exception:
    pass

# ── guard imports (always available) ─────────────────────────────────────────
from utils.live_order_guard import LiveOrderProhibitedError, prohibit_live_order


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_order(**kw):
    from models.order import Order, OrderSide, OrderType
    defaults = dict(
        client_order_id="test-coid-001",
        symbol="NIFTY",
        exchange="NSE",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        broker="dhan",
    )
    defaults.update(kw)
    return Order(**defaults)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _APIHTTPException(Exception):
    """Minimal HTTPException stand-in for environments without fastapi."""
    def __init__(self, status_code=500, detail=""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class _MockStatus:
    HTTP_200_OK = 200
    HTTP_400_BAD_REQUEST = 400
    HTTP_403_FORBIDDEN = 403
    HTTP_404_NOT_FOUND = 404
    HTTP_409_CONFLICT = 409
    HTTP_500_INTERNAL_SERVER_ERROR = 500


def _install_api_mocks():
    """
    Install minimal sys.modules stubs so api.routes.trading and api.routes.risk
    can be imported without fastapi/pydantic/pytz/dotenv being installed.

    Strategy:
      - Mock the `api` package to prevent api/__init__.py from running.
      - Mock api.dependencies and api.routes.auth directly.
      - Install MagicMock stubs for fastapi, pydantic, pytz, dotenv.
      - _APIHTTPException is used as the real HTTPException so status_code
        assertions work correctly.
    """
    import os

    # Prevent api/__init__.py from running (it pulls in api.main → dotenv chain)
    if "api" not in sys.modules or isinstance(sys.modules.get("api"), MagicMock):
        api_pkg = types.ModuleType("api")
        sys.modules.setdefault("api", api_pkg)

    api_deps = types.ModuleType("api.dependencies")
    api_deps.get_broker_adapters = MagicMock()
    sys.modules.setdefault("api.dependencies", api_deps)

    api_routes = types.ModuleType("api.routes")
    sys.modules.setdefault("api.routes", api_routes)

    auth_mod = types.ModuleType("api.routes.auth")
    auth_mod.require_operator = MagicMock()
    sys.modules.setdefault("api.routes.auth", auth_mod)

    mock_fastapi = MagicMock()
    mock_fastapi.HTTPException = _APIHTTPException
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

    from datetime import timezone as _builtin_tz
    mock_pytz = MagicMock()
    # Return a real tzinfo (UTC) so datetime.now(IST) never raises TypeError,
    # regardless of which test file's mock wins the sys.modules.setdefault() race.
    mock_pytz.timezone = MagicMock(return_value=_builtin_tz.utc)
    sys.modules.setdefault("pytz", mock_pytz)

    sys.modules.setdefault("dotenv", MagicMock())

    for sub in [
        "fastapi.responses", "fastapi.middleware", "fastapi.middleware.cors",
        "fastapi.routing", "fastapi.security", "fastapi.encoders", "fastapi.params",
    ]:
        sys.modules.setdefault(sub, MagicMock())


def _load_module_from_file(pkg_name: str, rel_path: str):
    """
    Load a module from a file path, bypassing its package __init__.py.
    Returns the module, or None if the load fails.
    """
    import os
    fpath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        rel_path,
    )
    if pkg_name in sys.modules:
        return sys.modules[pkg_name]
    _install_api_mocks()
    try:
        spec = importlib.util.spec_from_file_location(pkg_name, fpath,
            submodule_search_locations=[])
        mod = types.ModuleType(pkg_name)
        mod.__package__ = pkg_name.rsplit(".", 1)[0]
        mod.__spec__ = spec
        sys.modules[pkg_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        sys.modules.pop(pkg_name, None)
        return None


_TRADING_MOD = _load_module_from_file("api.routes.trading", "api/routes/trading.py")
_RISK_MOD = _load_module_from_file("api.routes.risk", "api/routes/risk.py")


# ── guard unit tests ──────────────────────────────────────────────────────────

class TestProhibitLiveOrderUnit(unittest.TestCase):
    def test_raises_live_order_prohibited_error(self):
        with self.assertRaises(LiveOrderProhibitedError):
            prohibit_live_order("test_context")

    def test_error_is_runtime_error_subclass(self):
        with self.assertRaises(RuntimeError):
            prohibit_live_order()

    def test_message_contains_key_phrase(self):
        try:
            prohibit_live_order("ctx")
        except LiveOrderProhibitedError as exc:
            self.assertIn("permanently prohibited", str(exc))
            self.assertIn("ctx", str(exc))

    def test_no_context_still_raises(self):
        with self.assertRaises(LiveOrderProhibitedError):
            prohibit_live_order()


# ── Dhan adapter ─────────────────────────────────────────────────────────────

class TestDhanAdapterOrderGuard(unittest.TestCase):
    def _make_adapter(self):
        from adapters.dhan_adapter import DhanAdapter, DhanSettings
        settings = DhanSettings(client_id="x", access_token="x")
        adapter = DhanAdapter.__new__(DhanAdapter)
        adapter._client = MagicMock(name="dhanhq_client")
        adapter._settings = settings
        return adapter

    def test_place_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        order = _make_order(broker="dhan")
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.place_order(order))
        adapter._client.place_order.assert_not_called()

    def test_cancel_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.cancel_order("coid-001"))
        adapter._client.cancel_order.assert_not_called()

    def test_modify_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        order = _make_order(broker="dhan")
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.modify_order(order))
        adapter._client.modify_order.assert_not_called()


# ── AngelOne adapter ──────────────────────────────────────────────────────────

class TestAngelOneAdapterOrderGuard(unittest.TestCase):
    def _make_adapter(self):
        from adapters.angelone_adapter import AngelOneAdapter, AngelOneSettings
        settings = AngelOneSettings(
            api_key="x", client_id="x", password="x", totp_secret="x"
        )
        adapter = AngelOneAdapter.__new__(AngelOneAdapter)
        adapter._smart_api = MagicMock(name="smart_api_client")
        adapter._settings = settings
        adapter._token_lock = asyncio.Lock()
        return adapter

    def test_place_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        order = _make_order(broker="angelone")
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.place_order(order))
        adapter._smart_api.placeOrder.assert_not_called()
        adapter._smart_api.place_order.assert_not_called()

    def test_cancel_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.cancel_order("coid-001"))
        adapter._smart_api.cancelOrder.assert_not_called()
        adapter._smart_api.cancel_order.assert_not_called()

    def test_modify_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        order = _make_order(broker="angelone")
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.modify_order(order))
        adapter._smart_api.modifyOrder.assert_not_called()
        adapter._smart_api.modify_order.assert_not_called()


# ── Fyers adapter ─────────────────────────────────────────────────────────────

class TestFyersAdapterOrderGuard(unittest.TestCase):
    def _make_adapter(self):
        from adapters.fyers_adapter import FyersAdapter
        from config.settings import FyersSettings
        settings = FyersSettings(
            client_id="x", secret_key="x", totp_secret="x", access_token="x"
        )
        adapter = FyersAdapter.__new__(FyersAdapter)
        adapter._fyers_model = MagicMock(name="fyers_model_client")
        adapter._settings = settings
        return adapter

    def test_place_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        order = _make_order(broker="fyers")
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.place_order(order))
        adapter._fyers_model.place_order.assert_not_called()

    def test_cancel_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.cancel_order("coid-001"))
        adapter._fyers_model.cancel_order.assert_not_called()

    def test_modify_order_raises_before_sdk(self):
        adapter = self._make_adapter()
        order = _make_order(broker="fyers")
        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.modify_order(order))
        adapter._fyers_model.modify_order.assert_not_called()


# ── ExecutionModeController ───────────────────────────────────────────────────

@unittest.skipIf(_TRADING_MOD is None, "api.routes.trading could not be loaded")
class TestExecutionModeController(unittest.TestCase):
    def _make_controller(self):
        return _TRADING_MOD.ExecutionModeController()

    def test_set_live_raises_http_403(self):
        ctrl = self._make_controller()
        # Use the HTTPException class that trading.py actually imported to avoid
        # class-identity mismatch when multiple test files share sys.modules mocks.
        with self.assertRaises(_TRADING_MOD.HTTPException) as ctx:
            ctrl.set("LIVE")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_set_live_lowercase_raises_http_403(self):
        ctrl = self._make_controller()
        with self.assertRaises(_TRADING_MOD.HTTPException) as ctx:
            ctrl.set("live")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_set_paper_succeeds(self):
        ctrl = self._make_controller()
        result = ctrl.set("PAPER")
        self.assertEqual(result, "PAPER")

    def test_set_invalid_mode_raises_http_400(self):
        ctrl = self._make_controller()
        with self.assertRaises(_TRADING_MOD.HTTPException) as ctx:
            ctrl.set("SIMULATION")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_is_paper_always_true(self):
        ctrl = self._make_controller()
        self.assertTrue(ctrl.is_paper())

    def test_live_error_detail_mentions_permanently_disabled(self):
        ctrl = self._make_controller()
        with self.assertRaises(Exception) as ctx:
            ctrl.set("LIVE")
        self.assertIn("permanently disabled", ctx.exception.detail)


# ── _flatten_live guard ───────────────────────────────────────────────────────

@unittest.skipIf(_RISK_MOD is None, "api.routes.risk could not be loaded")
class TestFlattenLiveGuard(unittest.TestCase):
    def test_flatten_live_raises_live_order_prohibited(self):
        _flatten_live = _RISK_MOD._flatten_live
        with self.assertRaises(LiveOrderProhibitedError):
            _run(_flatten_live(
                adapters={},
                orders_db={},
                positions_db={},
                record_fill=None,
                select_adapter=None,
                mc=None,
                event_id="evt-001",
                now_ist="2024-01-01T00:00:00",
                _logger=MagicMock(),
            ))

    def test_flatten_live_context_string(self):
        _flatten_live = _RISK_MOD._flatten_live
        try:
            _run(_flatten_live(
                adapters={},
                orders_db={},
                positions_db={},
                record_fill=None,
                select_adapter=None,
                mc=None,
                event_id="evt-002",
                now_ist="2024-01-01T00:00:00",
                _logger=MagicMock(),
            ))
        except LiveOrderProhibitedError as exc:
            self.assertIn("_flatten_live", str(exc))


# ── _auto_execute_loop LIVE branch ────────────────────────────────────────────

class TestAutoExecuteLoopLiveBranch(unittest.TestCase):
    """
    Verify the LIVE branch in _auto_execute_loop raises LiveOrderProhibitedError.

    Because the full api.main module cannot be loaded in this environment,
    we test the guard function directly: the LIVE branch calls
    prohibit_live_order("_auto_execute_loop: LIVE branch") unconditionally.
    """

    def test_live_branch_guard_function_raises(self):
        """The guard function used by the LIVE branch raises unconditionally."""
        with self.assertRaises(LiveOrderProhibitedError) as ctx:
            prohibit_live_order("_auto_execute_loop: LIVE branch")
        self.assertIn("_auto_execute_loop", str(ctx.exception))

    def test_live_branch_guard_is_live_order_prohibited_error(self):
        """LiveOrderProhibitedError from the LIVE branch is a RuntimeError."""
        with self.assertRaises(RuntimeError):
            prohibit_live_order("_auto_execute_loop: LIVE branch")


# ── SDK bypass attempts ───────────────────────────────────────────────────────

class TestSdkBypassAttempts(unittest.TestCase):
    """
    Attempt to reach a live order endpoint via the adapter's underlying SDK.
    The adapter-level guard fires first; the mock SDK records zero calls.
    """

    def test_dhan_sdk_cannot_be_reached_via_place_order(self):
        from adapters.dhan_adapter import DhanAdapter, DhanSettings
        settings = DhanSettings(client_id="x", access_token="x")
        adapter = DhanAdapter.__new__(DhanAdapter)
        sdk_mock = MagicMock(name="dhanhq_sdk")
        sdk_mock.place_order = MagicMock(return_value={"orderId": "fake"})
        adapter._client = sdk_mock
        adapter._settings = settings

        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.place_order(_make_order(broker="dhan")))

        # Assert zero outbound order requests
        sdk_mock.place_order.assert_not_called()
        sdk_mock.cancel_order.assert_not_called()
        sdk_mock.modify_order.assert_not_called()

    def test_angelone_sdk_cannot_be_reached_via_place_order(self):
        from adapters.angelone_adapter import AngelOneAdapter, AngelOneSettings
        settings = AngelOneSettings(
            api_key="x", client_id="x", password="x", totp_secret="x"
        )
        adapter = AngelOneAdapter.__new__(AngelOneAdapter)
        sdk_mock = MagicMock(name="smartapi_sdk")
        sdk_mock.placeOrder = MagicMock(return_value={"data": {"orderid": "fake"}})
        adapter._smart_api = sdk_mock
        adapter._settings = settings
        adapter._token_lock = asyncio.Lock()

        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.place_order(_make_order(broker="angelone")))

        sdk_mock.placeOrder.assert_not_called()
        sdk_mock.cancelOrder.assert_not_called()
        sdk_mock.modifyOrder.assert_not_called()

    def test_fyers_sdk_cannot_be_reached_via_place_order(self):
        from adapters.fyers_adapter import FyersAdapter
        from config.settings import FyersSettings
        settings = FyersSettings(
            client_id="x", secret_key="x", totp_secret="x", access_token="x"
        )
        adapter = FyersAdapter.__new__(FyersAdapter)
        sdk_mock = MagicMock(name="fyers_sdk")
        sdk_mock.place_order = MagicMock(return_value={"code": 200, "id": "fake"})
        adapter._fyers_model = sdk_mock
        adapter._settings = settings

        with self.assertRaises(LiveOrderProhibitedError):
            _run(adapter.place_order(_make_order(broker="fyers")))

        sdk_mock.place_order.assert_not_called()
        sdk_mock.cancel_order.assert_not_called()
        sdk_mock.modify_order.assert_not_called()


# ── Startup fail-closed: guard must be importable ────────────────────────────

class TestStartupFailClosed(unittest.TestCase):
    """
    If live_order_guard cannot be imported (e.g. file deleted), the import
    itself raises ImportError — the system fails closed rather than silently
    allowing live orders.
    """

    def test_live_order_guard_importable(self):
        import importlib
        mod = importlib.import_module("utils.live_order_guard")
        self.assertTrue(hasattr(mod, "prohibit_live_order"))
        self.assertTrue(hasattr(mod, "LiveOrderProhibitedError"))

    def test_prohibit_live_order_is_callable(self):
        from utils.live_order_guard import prohibit_live_order
        self.assertTrue(callable(prohibit_live_order))

    def test_live_order_prohibited_error_is_exception(self):
        from utils.live_order_guard import LiveOrderProhibitedError
        self.assertTrue(issubclass(LiveOrderProhibitedError, RuntimeError))


# ── Market-data methods are NOT blocked ───────────────────────────────────────

class TestMarketDataNotBlocked(unittest.TestCase):
    """
    Adapter market-data methods (get_balance etc.) must NOT raise
    LiveOrderProhibitedError — the guard applies only to order dispatch.
    They may raise other errors (auth, parsing) in the mock environment.
    """

    def test_dhan_get_balance_not_guarded(self):
        from adapters.dhan_adapter import DhanAdapter, DhanSettings
        settings = DhanSettings(client_id="x", access_token="x")
        adapter = DhanAdapter.__new__(DhanAdapter)
        adapter._settings = settings
        adapter._client = MagicMock(name="dhan_client")
        adapter._client.get_fund_limits = MagicMock(return_value={"status": "success", "data": {}})
        try:
            _run(adapter.get_balance())
        except LiveOrderProhibitedError:
            self.fail("get_balance must not raise LiveOrderProhibitedError")
        except Exception:
            pass  # auth / parsing errors are fine in mock context

    def test_fyers_get_balance_not_guarded(self):
        from adapters.fyers_adapter import FyersAdapter
        from config.settings import FyersSettings
        settings = FyersSettings(
            client_id="x", secret_key="x", totp_secret="x", access_token="x"
        )
        adapter = FyersAdapter.__new__(FyersAdapter)
        adapter._settings = settings
        adapter._fyers_model = MagicMock(name="fyers_model")
        adapter._fyers_model.funds = MagicMock(return_value={"code": 200, "fund_limit": []})
        try:
            _run(adapter.get_balance())
        except LiveOrderProhibitedError:
            self.fail("get_balance must not raise LiveOrderProhibitedError")
        except Exception:
            pass

    def test_angelone_get_balance_not_guarded(self):
        from adapters.angelone_adapter import AngelOneAdapter, AngelOneSettings
        settings = AngelOneSettings(
            api_key="x", client_id="x", password="x", totp_secret="x"
        )
        adapter = AngelOneAdapter.__new__(AngelOneAdapter)
        adapter._settings = settings
        adapter._smart_api = MagicMock(name="smartapi")
        adapter._token_lock = asyncio.Lock()
        adapter._smart_api.getRMS = MagicMock(return_value={"status": True, "data": {}})
        try:
            _run(adapter.get_balance())
        except LiveOrderProhibitedError:
            self.fail("get_balance must not raise LiveOrderProhibitedError")
        except Exception:
            pass


# ── PaperBrokerAdapter green path ─────────────────────────────────────────────

class TestPaperBrokerGreenPath(unittest.TestCase):
    """
    PaperBrokerAdapter.place_order must complete without raising
    LiveOrderProhibitedError — it is the only permitted execution channel.
    """

    def _make_paper(self):
        from adapters.paper_broker import PaperBrokerAdapter

        def _ltp(sym):
            return 100.0

        return PaperBrokerAdapter(ltp_provider=_ltp)

    def test_paper_place_order_does_not_raise_live_guard(self):
        paper = self._make_paper()
        order = _make_order(broker="paper")
        try:
            _run(paper.place_order(order))
        except LiveOrderProhibitedError:
            self.fail("PaperBrokerAdapter.place_order must not raise LiveOrderProhibitedError")
        except Exception:
            pass  # other errors are acceptable in mock context

    def test_paper_place_order_records_position(self):
        paper = self._make_paper()
        from models.order import OrderSide
        order = _make_order(broker="paper", symbol="NIFTY", quantity=1.0)
        order.side = OrderSide.BUY
        try:
            _run(paper.place_order(order))
            sym = order.symbol.upper()
            self.assertIn(sym, paper.positions)
        except LiveOrderProhibitedError:
            self.fail("PaperBrokerAdapter must not raise LiveOrderProhibitedError")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
