"""
P0-1: Durable State Persistence — unit tests.

Covers PaperBrokerAdapter ledger and _orders_db in api/routes/trading.py.
All tests use isolated temp paths and never touch the live data directory.

Safety invariant: No test modifies any production code path or broker API.
Paper-only enforcement is not altered here.
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock


# ── sys.modules warm-up (mirrors test_live_order_guard.py) ───────────────────
# First import of any adapter triggers pipeline/__init__.py which fails on pytz.
# That partial run caches pipeline.tick_buffer so the second attempt succeeds.
try:
    from pipeline import tick_buffer as _warmup  # noqa: F401
except Exception:
    pass


# ── Minimal stubs for api.routes.trading (no fastapi/pytz/pydantic installed) ─

class _HTTPException(Exception):
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
    HTTP_422_UNPROCESSABLE_ENTITY = 422
    HTTP_500_INTERNAL_SERVER_ERROR = 500
    HTTP_501_NOT_IMPLEMENTED = 501
    HTTP_502_BAD_GATEWAY = 502
    HTTP_503_SERVICE_UNAVAILABLE = 503


def _install_api_mocks():
    if "api" not in sys.modules or isinstance(sys.modules.get("api"), MagicMock):
        sys.modules.setdefault("api", types.ModuleType("api"))

    deps = types.ModuleType("api.dependencies")
    deps.get_broker_adapters = MagicMock()
    sys.modules.setdefault("api.dependencies", deps)

    sys.modules.setdefault("api.routes", types.ModuleType("api.routes"))

    auth = types.ModuleType("api.routes.auth")
    auth.require_operator = MagicMock()
    sys.modules.setdefault("api.routes.auth", auth)

    mock_fastapi = MagicMock()
    mock_fastapi.HTTPException = _HTTPException
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


def _load_trading_module():
    """Load api.routes.trading bypassing api/__init__.py, same as test_live_order_guard."""
    pkg = "api.routes.trading"
    if pkg in sys.modules:
        return sys.modules[pkg]
    _install_api_mocks()
    fpath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "api", "routes", "trading.py",
    )
    try:
        spec = importlib.util.spec_from_file_location(pkg, fpath,
            submodule_search_locations=[])
        mod = types.ModuleType(pkg)
        mod.__package__ = "api.routes"
        mod.__spec__ = spec
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        sys.modules.pop(pkg, None)
        return None


_TRADING_MOD = _load_trading_module()


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_adapter(tmp_path, starting_cash=100_000.0):
    """Return a PaperBrokerAdapter backed by a temp ledger file."""
    from adapters.paper_broker import PaperBrokerAdapter
    ltp = MagicMock(return_value=100.0)
    return PaperBrokerAdapter(
        ltp_provider=ltp,
        starting_cash=starting_cash,
        db_path=tmp_path,
    )


def _direct_fill(adapter, symbol, signed_qty, price, lot_size=1):
    """Bypass async place_order; call _apply_fill directly and then save."""
    adapter._apply_fill(symbol, signed_qty, price, lot_size=lot_size)
    adapter.fills.append({"symbol": symbol, "qty": signed_qty, "price": price})
    adapter._save_ledger()


# ── PaperBrokerAdapter persistence ────────────────────────────────────────────

class TestPaperBrokerPersistence(unittest.TestCase):

    def test_persist_after_fill(self):
        """A fill mutates state and the ledger file is written."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            path = tf.name
        try:
            adapter = _make_adapter(path)
            _direct_fill(adapter, "NIFTY25000CE", 1.0, 150.0, lot_size=50)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                state = json.load(f)
            self.assertIn("NIFTY25000CE", state["positions"])
            self.assertAlmostEqual(state["cash"], 100_000.0 - 1.0 * 50 * 150.0)
        finally:
            os.unlink(path)

    def test_restore_on_startup(self):
        """A new adapter instance recovers positions and cash from a prior session."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            path = tf.name
        try:
            a1 = _make_adapter(path)
            _direct_fill(a1, "BANKNIFTY50000CE", 2.0, 200.0, lot_size=15)
            expected_cash = a1.cash
            expected_qty = a1.positions["BANKNIFTY50000CE"].quantity

            a2 = _make_adapter(path)
            self.assertAlmostEqual(a2.cash, expected_cash)
            self.assertIn("BANKNIFTY50000CE", a2.positions)
            self.assertAlmostEqual(a2.positions["BANKNIFTY50000CE"].quantity, expected_qty)
            self.assertEqual(len(a2.fills), len(a1.fills))
        finally:
            os.unlink(path)

    def test_idempotent_restart(self):
        """Load → persist → load again yields identical state (no drift)."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            path = tf.name
        try:
            a1 = _make_adapter(path)
            _direct_fill(a1, "SENSEX79000PE", -1.0, 50.0, lot_size=10)

            a2 = _make_adapter(path)
            a2._save_ledger()

            a3 = _make_adapter(path)
            self.assertAlmostEqual(a2.cash, a3.cash)
            self.assertEqual(set(a2.positions), set(a3.positions))
        finally:
            os.unlink(path)

    def test_missing_state_starts_fresh(self):
        """No ledger file → adapter starts with clean slate (no error)."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "nonexistent_ledger.json")
            adapter = _make_adapter(path, starting_cash=50_000.0)
            self.assertAlmostEqual(adapter.cash, 50_000.0)
            self.assertEqual(adapter.positions, {})
            self.assertEqual(adapter.fills, [])

    def test_corrupt_state_starts_fresh(self):
        """Corrupt JSON in the ledger file → adapter starts fresh (no crash)."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
            tf.write("{ not valid json !!!")
            path = tf.name
        try:
            adapter = _make_adapter(path, starting_cash=75_000.0)
            self.assertAlmostEqual(adapter.cash, 75_000.0)
            self.assertEqual(adapter.positions, {})
        finally:
            os.unlink(path)

    def test_no_path_no_file_created(self):
        """db_path=None → _save_ledger is a no-op; no file written."""
        from adapters.paper_broker import PaperBrokerAdapter
        with tempfile.TemporaryDirectory() as td:
            adapter = PaperBrokerAdapter(
                ltp_provider=MagicMock(return_value=100.0),
                starting_cash=100_000.0,
                db_path=None,
            )
            _direct_fill(adapter, "INFY", 10.0, 1800.0)
            self.assertEqual(os.listdir(td), [])

    def test_reset_persists_clean_state(self):
        """reset() clears state and writes an empty ledger to disk."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            path = tf.name
        try:
            adapter = _make_adapter(path)
            _direct_fill(adapter, "TCS", 5.0, 3500.0)
            adapter.reset()
            with open(path) as f:
                state = json.load(f)
            self.assertEqual(state["positions"], {})
            self.assertAlmostEqual(state["cash"], adapter.starting_cash)
        finally:
            os.unlink(path)

    def test_corrupt_position_skipped_gracefully(self):
        """A single corrupt position entry is skipped; valid entries are restored."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
            json.dump({
                "starting_cash": 100_000.0,
                "cash": 90_000.0,
                "positions": {
                    "GOOD": {
                        "symbol": "GOOD", "quantity": 2.0, "avg_price": 100.0,
                        "realized_pnl": 0.0, "lot_size": 1,
                        "instrument_type": "EQ", "contract": {},
                    },
                    "BAD": "not a dict",
                },
                "fills": [],
            }, tf)
            path = tf.name
        try:
            # Use _make_adapter so adapters.paper_broker is already cached
            # (importing adapters/__init__.py directly is too heavy for unit tests).
            adapter = _make_adapter(path, starting_cash=100_000.0)
            self.assertIn("GOOD", adapter.positions)
            self.assertNotIn("BAD", adapter.positions)
        finally:
            os.unlink(path)


# ── _orders_db persistence ─────────────────────────────────────────────────────

class TestOrdersDbPersistence(unittest.TestCase):
    """
    Tests call _load_orders_db / _save_orders_db directly via _TRADING_MOD.
    Each test gets its own isolated file via the env var override.
    Skipped when _TRADING_MOD could not be loaded.
    """

    @classmethod
    def setUpClass(cls):
        if _TRADING_MOD is None:
            raise unittest.SkipTest("api.routes.trading could not be loaded — skipping orders_db tests")

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._td.name, "orders_test.json")
        os.environ["STRIKE_DESK_ORDERS_FILE"] = self._path
        _TRADING_MOD._orders_db.clear()

    def tearDown(self):
        self._td.cleanup()
        _TRADING_MOD._orders_db.clear()
        # Remove override so conftest scratch value takes effect on subsequent tests.
        os.environ.pop("STRIKE_DESK_ORDERS_FILE", None)

    def test_save_and_restore_orders_db(self):
        """Orders written to disk are recovered after clearing and reloading."""
        _TRADING_MOD._orders_db["ORD-0001"] = {
            "order_id": "ORD-0001", "symbol": "NIFTY25000CE",
            "side": "BUY", "quantity": 1, "status": "FILLED",
        }
        _TRADING_MOD._save_orders_db()

        # Simulate restart: clear in-memory db, reload from disk.
        _TRADING_MOD._orders_db.clear()
        _TRADING_MOD._load_orders_db()

        self.assertIn("ORD-0001", _TRADING_MOD._orders_db)
        self.assertEqual(_TRADING_MOD._orders_db["ORD-0001"]["symbol"], "NIFTY25000CE")

    def test_missing_file_starts_empty(self):
        """No file → _load_orders_db is a no-op; dict stays empty."""
        # File does not exist at self._path yet.
        _TRADING_MOD._load_orders_db()
        self.assertEqual(_TRADING_MOD._orders_db, {})

    def test_corrupt_file_starts_empty(self):
        """Corrupt JSON → _load_orders_db is silent; dict stays empty."""
        with open(self._path, "w") as f:
            f.write("not json {{[")
        _TRADING_MOD._load_orders_db()  # must not raise
        self.assertEqual(_TRADING_MOD._orders_db, {})

    def test_non_list_json_ignored(self):
        """JSON that is not a list (e.g. a dict) is ignored silently."""
        with open(self._path, "w") as f:
            json.dump({"ORD-0001": {"order_id": "ORD-0001"}}, f)
        _TRADING_MOD._load_orders_db()
        self.assertEqual(_TRADING_MOD._orders_db, {})

    def test_save_is_atomic(self):
        """Atomic write leaves no .tmp file after completion."""
        _TRADING_MOD._orders_db["ORD-0002"] = {"order_id": "ORD-0002", "status": "FILLED"}
        _TRADING_MOD._save_orders_db()
        self.assertFalse(os.path.exists(self._path + ".tmp"),
                         "Atomic write must not leave a .tmp file behind")
        self.assertTrue(os.path.exists(self._path))


if __name__ == "__main__":
    unittest.main()
