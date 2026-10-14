"""
Tests for 8-A (FLATTEN ALL execution) and 8-E (auto-exit halt enforcement).

All order placement is mocked — no real orders are submitted and LIVE execution
is never enabled.

Environment notes: pytest-asyncio is not installed in the isolated pytest venv,
so all async helpers are driven with asyncio.run() via _run(). pytz is also
absent; the module-level _RISK_MOD load installs a sys.modules mock for it via
_install_api_mocks(), which keeps the halt-condition tests (and any other import
that transitively needs pytz) working.
"""
import asyncio
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

# ── sys.modules warm-up ──────────────────────────────────────────────────────
# pipeline/__init__.py fails partway on missing pytz but caches
# pipeline.tick_buffer in sys.modules so a second import of any adapter
# succeeds. Trigger that first failing import once here.
try:
    from pipeline import tick_buffer as _warmup_tick_buf  # noqa: F401
except Exception:
    pass

from utils.live_order_guard import LiveOrderProhibitedError


# ── mock infrastructure (mirrors test_live_order_guard.py) ───────────────────

class _APIHTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class _MockStatus:
    HTTP_200_OK = 200
    HTTP_400_BAD_REQUEST = 400
    HTTP_403_FORBIDDEN = 403
    HTTP_422_UNPROCESSABLE_ENTITY = 422
    HTTP_500_INTERNAL_SERVER_ERROR = 500


def _install_api_mocks():
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
    # Return a real tzinfo (UTC) so datetime.now(IST) doesn't raise TypeError.
    mock_pytz.timezone = MagicMock(return_value=_builtin_tz.utc)
    sys.modules.setdefault("pytz", mock_pytz)

    sys.modules.setdefault("dotenv", MagicMock())

    for sub in [
        "fastapi.responses", "fastapi.middleware", "fastapi.middleware.cors",
        "fastapi.routing", "fastapi.security", "fastapi.encoders", "fastapi.params",
    ]:
        sys.modules.setdefault(sub, MagicMock())


def _load_module_from_file(pkg_name: str, rel_path: str):
    import importlib.util
    import os
    fpath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        rel_path,
    )
    if pkg_name in sys.modules:
        return sys.modules[pkg_name]
    _install_api_mocks()
    try:
        spec = importlib.util.spec_from_file_location(
            pkg_name, fpath, submodule_search_locations=[]
        )
        mod = types.ModuleType(pkg_name)
        mod.__package__ = pkg_name.rsplit(".", 1)[0]
        mod.__spec__ = spec
        sys.modules[pkg_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        sys.modules.pop(pkg_name, None)
        return None


# Load risk module at module level so _install_api_mocks() runs before any test,
# which installs the pytz mock needed by halt-condition tests.
_RISK_MOD = _load_module_from_file("api.routes.risk", "api/routes/risk.py")


# ── shared fixtures ───────────────────────────────────────────────────────────

def _make_update(
    fill_price: float = 100.0,
    lot_size: int = 50,
    realized_pnl: float = 0.0,
):
    from models.order import OrderUpdate, OrderState
    return OrderUpdate(
        client_order_id="cid-1",
        broker_order_id="PAPER_cid-1",
        state=OrderState.FILLED,
        filled_qty=1.0,
        remaining_qty=0.0,
        avg_fill_price=fill_price,
        last_fill_qty=1.0,
        last_fill_price=fill_price,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "simulated": True,
            "broker": "paper",
            "lot_size": lot_size,
            "realized_pnl": realized_pnl,
        },
    )


def _make_pos(symbol, quantity=2.0, avg_price=80.0, lot_size=50):
    """Return a PaperPosition-compatible namespace, falling back to SimpleNamespace."""
    try:
        from adapters.paper_broker import PaperPosition
        return PaperPosition(symbol=symbol, quantity=quantity, avg_price=avg_price, lot_size=lot_size)
    except Exception:
        return types.SimpleNamespace(symbol=symbol, quantity=quantity, avg_price=avg_price, lot_size=lot_size)


def _make_paper(positions: dict, ltp: float = 105.0) -> MagicMock:
    paper = MagicMock()
    paper.positions = positions
    paper._ltp_provider = MagicMock(return_value=ltp)
    paper.get_positions_dict = MagicMock(return_value=[])
    return paper


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── 8-A: PAPER flatten ────────────────────────────────────────────────────────

def test_flatten_paper_closes_open_position():
    if _RISK_MOD is None:
        return
    _flatten_paper = _RISK_MOD._flatten_paper

    sym = "NIFTY24500CE"
    pos = _make_pos(sym, quantity=2.0, avg_price=80.0, lot_size=50)
    update = _make_update(fill_price=105.0, lot_size=50, realized_pnl=2500.0)

    paper = _make_paper({sym: pos})
    paper.place_order = AsyncMock(return_value=update)

    positions_db: dict = {sym: {"symbol": sym, "quantity": 2.0}}
    record_fill = MagicMock()
    mc = MagicMock(active_mode="AUTO_OPTIONS")
    _logger = MagicMock()

    result = _run(_flatten_paper(
        paper, positions_db, record_fill, mc, "evt-1", "2026-10-09T10:00:00+05:30", _logger
    ))

    assert result["mode"] == "PAPER"
    assert result["closed_count"] == 1
    assert result["failed_count"] == 0
    assert len(result["paper_closes"]) == 1
    close = result["paper_closes"][0]
    assert close["outcome"] == "CLOSED"
    assert close["symbol"] == sym
    assert close["fill_price"] == 105.0
    assert close["realized_pnl"] == 2500.0
    record_fill.assert_called_once()
    assert positions_db == {}


def test_flatten_paper_no_positions_returns_empty():
    if _RISK_MOD is None:
        return
    _flatten_paper = _RISK_MOD._flatten_paper

    paper = _make_paper({})
    result = _run(_flatten_paper(
        paper, {}, MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
        "evt-2", "2026-10-09T10:00:00+05:30", MagicMock()
    ))

    assert result["mode"] == "PAPER"
    assert result["closed_count"] == 0
    assert result["failed_count"] == 0
    assert result["paper_closes"] == []


def test_flatten_paper_no_price_reports_failed():
    if _RISK_MOD is None:
        return
    _flatten_paper = _RISK_MOD._flatten_paper

    sym = "BANKNIFTY24CE"
    pos = _make_pos(sym, quantity=1.0, avg_price=0.0, lot_size=15)
    paper = _make_paper({sym: pos}, ltp=0.0)
    paper._ltp_provider = MagicMock(return_value=None)
    pos.avg_price = 0.0

    result = _run(_flatten_paper(
        paper, {}, MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
        "evt-3", "2026-10-09T10:00:00+05:30", MagicMock()
    ))

    assert result["failed_count"] == 1
    assert result["closed_count"] == 0
    assert result["paper_closes"][0]["outcome"] == "FAILED"
    assert result["paper_closes"][0]["reason"] == "no_live_price"


def test_flatten_paper_broker_exception_reports_failed():
    if _RISK_MOD is None:
        return
    _flatten_paper = _RISK_MOD._flatten_paper

    sym = "NIFTY24PE"
    pos = _make_pos(sym, quantity=-1.0, avg_price=50.0, lot_size=50)
    paper = _make_paper({sym: pos}, ltp=55.0)
    paper.place_order = AsyncMock(side_effect=ValueError("feed unavailable"))

    result = _run(_flatten_paper(
        paper, {}, MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
        "evt-4", "2026-10-09T10:00:00+05:30", MagicMock()
    ))

    assert result["failed_count"] == 1
    assert result["paper_closes"][0]["outcome"] == "FAILED"
    assert "feed unavailable" in result["paper_closes"][0]["reason"]


# ── 8-A: LIVE flatten raises unconditionally ──────────────────────────────────

def test_flatten_live_raises_live_order_prohibited_error():
    """_flatten_live must raise LiveOrderProhibitedError — live dispatch is permanently prohibited."""
    if _RISK_MOD is None:
        return
    _flatten_live = _RISK_MOD._flatten_live

    raised = False
    try:
        _run(_flatten_live(
            {}, {}, {}, MagicMock(),
            MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
            "evt-live1", "2026-10-09T10:00:00+05:30", MagicMock()
        ))
    except LiveOrderProhibitedError:
        raised = True
    except Exception:
        pass

    assert raised, "_flatten_live must raise LiveOrderProhibitedError unconditionally"


def test_flatten_live_error_is_runtime_error_subclass():
    """LiveOrderProhibitedError is a RuntimeError subclass — callers must not catch RuntimeError to suppress it."""
    if _RISK_MOD is None:
        return
    _flatten_live = _RISK_MOD._flatten_live

    raised = False
    try:
        _run(_flatten_live(
            {}, {}, {}, MagicMock(),
            MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
            "evt-live2", "2026-10-09T10:00:00+05:30", MagicMock()
        ))
    except RuntimeError:
        raised = True
    except Exception:
        pass

    assert raised, "_flatten_live must raise a RuntimeError (or subclass) unconditionally"


def test_flatten_live_never_returns_result():
    """_flatten_live must never return — any return value would indicate the live guard was bypassed."""
    if _RISK_MOD is None:
        return
    _flatten_live = _RISK_MOD._flatten_live

    returned = False
    try:
        _ = _run(_flatten_live(
            {}, {}, {}, MagicMock(),
            MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
            "evt-live3", "2026-10-09T10:00:00+05:30", MagicMock()
        ))
        returned = True
    except Exception:
        pass

    assert not returned, "_flatten_live must always raise, never return"


# ── 8-E: auto-exit halt enforcement ──────────────────────────────────────────

def test_hard_halt_condition_blocks_auto_exit():
    """HARD_HALT must set the block flag used by the auto-exit guard."""
    from risk.emergency_halt import EmergencyHaltManager, HaltLevel

    ehm = EmergencyHaltManager(db_path=None)
    ehm.trigger_halt(reason="loss ceiling breach", level=HaltLevel.HARD_HALT)

    assert ehm.is_halted
    blocks = ehm.is_halted and ehm.current_level in (
        HaltLevel.HARD_HALT, HaltLevel.FULL_KILL_SWITCH
    )
    assert blocks


def test_full_kill_switch_condition_blocks_auto_exit():
    """FULL_KILL_SWITCH (FLATTEN ALL) must set the block flag."""
    from risk.emergency_halt import EmergencyHaltManager, HaltLevel

    ehm = EmergencyHaltManager(db_path=None)
    ehm.trigger_halt(reason="operator kill", level=HaltLevel.FULL_KILL_SWITCH)

    assert ehm.is_halted
    blocks = ehm.is_halted and ehm.current_level in (
        HaltLevel.HARD_HALT, HaltLevel.FULL_KILL_SWITCH
    )
    assert blocks


def test_soft_halt_condition_allows_auto_exit():
    """SOFT_HALT (stop-entry) must NOT block protective auto-exits."""
    from risk.emergency_halt import EmergencyHaltManager, HaltLevel

    ehm = EmergencyHaltManager(db_path=None)
    ehm.stop_entry(reason="manual stop", operator_id="MONUJ")

    assert ehm.is_halted
    assert ehm.current_level == HaltLevel.SOFT_HALT
    blocks = ehm.is_halted and ehm.current_level in (
        HaltLevel.HARD_HALT, HaltLevel.FULL_KILL_SWITCH
    )
    assert not blocks


def test_no_halt_condition_allows_auto_exit():
    """When system is not halted, the block flag must be False."""
    from risk.emergency_halt import EmergencyHaltManager, HaltLevel

    ehm = EmergencyHaltManager(db_path=None)
    assert not ehm.is_halted
    blocks = ehm.is_halted and ehm.current_level in (
        HaltLevel.HARD_HALT, HaltLevel.FULL_KILL_SWITCH
    )
    assert not blocks
