"""
Tests for 8-A (FLATTEN ALL execution) and 8-E (auto-exit halt enforcement).

All order placement is mocked — no real orders are submitted and LIVE execution
is never enabled.
"""
import asyncio
from datetime import datetime, timezone
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.order import Order, OrderSide, OrderState, OrderType, OrderUpdate


# ── shared fixtures ───────────────────────────────────────────────────────────

def _make_update(
    fill_price: float = 100.0,
    lot_size: int = 50,
    realized_pnl: float = 0.0,
    state: OrderState = OrderState.FILLED,
    client_order_id: str = "cid-1",
) -> OrderUpdate:
    return OrderUpdate(
        client_order_id=client_order_id,
        broker_order_id=f"PAPER_{client_order_id}",
        state=state,
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


def _make_paper(positions: dict, ltp: float = 105.0) -> MagicMock:
    paper = MagicMock()
    paper.positions = positions
    paper._ltp_provider = MagicMock(return_value=ltp)
    paper.get_positions_dict = MagicMock(return_value=[])
    return paper


# ── 8-A: PAPER flatten ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flatten_paper_closes_open_position():
    from api.routes.risk import _flatten_paper
    from adapters.paper_broker import PaperPosition

    sym = "NIFTY24500CE"
    pos = PaperPosition(symbol=sym, quantity=2.0, avg_price=80.0, lot_size=50)
    update = _make_update(fill_price=105.0, lot_size=50, realized_pnl=2500.0)

    paper = _make_paper({sym: pos})
    paper.place_order = AsyncMock(return_value=update)

    positions_db: dict = {sym: {"symbol": sym, "quantity": 2.0}}
    record_fill = MagicMock()
    mc = MagicMock(active_mode="AUTO_OPTIONS")
    _logger = MagicMock()

    result = await _flatten_paper(
        paper, positions_db, record_fill, mc, "evt-1", "2026-10-09T10:00:00+05:30", _logger
    )

    assert result["mode"] == "PAPER"
    assert result["closed_count"] == 1
    assert result["failed_count"] == 0
    assert len(result["paper_closes"]) == 1
    close = result["paper_closes"][0]
    assert close["outcome"] == "CLOSED"
    assert close["symbol"] == sym
    assert close["fill_price"] == 105.0
    assert close["realized_pnl"] == 2500.0
    # risk stores received the fill
    record_fill.assert_called_once()
    # positions_db was refreshed (get_positions_dict returned [] → empty)
    assert positions_db == {}


@pytest.mark.asyncio
async def test_flatten_paper_no_positions_returns_empty():
    from api.routes.risk import _flatten_paper

    paper = _make_paper({})
    positions_db: dict = {}
    result = await _flatten_paper(
        paper, positions_db, MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
        "evt-2", "2026-10-09T10:00:00+05:30", MagicMock()
    )

    assert result["mode"] == "PAPER"
    assert result["closed_count"] == 0
    assert result["failed_count"] == 0
    assert result["paper_closes"] == []


@pytest.mark.asyncio
async def test_flatten_paper_no_price_reports_failed():
    from api.routes.risk import _flatten_paper
    from adapters.paper_broker import PaperPosition

    sym = "BANKNIFTY24CE"
    pos = PaperPosition(symbol=sym, quantity=1.0, avg_price=200.0, lot_size=15)

    paper = _make_paper({sym: pos}, ltp=0.0)  # no live price
    paper._ltp_provider = MagicMock(return_value=None)
    # avg_price fallback is also 0 (explicitly tested here by making pos.avg_price < 0.01)
    pos.avg_price = 0.0

    result = await _flatten_paper(
        paper, {}, MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
        "evt-3", "2026-10-09T10:00:00+05:30", MagicMock()
    )

    assert result["failed_count"] == 1
    assert result["closed_count"] == 0
    assert result["paper_closes"][0]["outcome"] == "FAILED"
    assert result["paper_closes"][0]["reason"] == "no_live_price"


@pytest.mark.asyncio
async def test_flatten_paper_broker_exception_reports_failed():
    from api.routes.risk import _flatten_paper
    from adapters.paper_broker import PaperPosition

    sym = "NIFTY24PE"
    pos = PaperPosition(symbol=sym, quantity=-1.0, avg_price=50.0, lot_size=50)
    paper = _make_paper({sym: pos}, ltp=55.0)
    paper.place_order = AsyncMock(side_effect=ValueError("feed unavailable"))

    result = await _flatten_paper(
        paper, {}, MagicMock(), MagicMock(active_mode="AUTO_OPTIONS"),
        "evt-4", "2026-10-09T10:00:00+05:30", MagicMock()
    )

    assert result["failed_count"] == 1
    assert result["paper_closes"][0]["outcome"] == "FAILED"
    assert "feed unavailable" in result["paper_closes"][0]["reason"]


# ── 8-A: LIVE flatten ─────────────────────────────────────────────────────────

def _make_live_update(sym: str, fill_price: float = 200.0) -> OrderUpdate:
    return OrderUpdate(
        client_order_id="live-cid",
        broker_order_id=f"DHAN_live-cid",
        state=OrderState.FILLED,
        filled_qty=1.0,
        remaining_qty=0.0,
        avg_fill_price=fill_price,
        last_fill_qty=1.0,
        last_fill_price=fill_price,
        timestamp=datetime.now(timezone.utc),
        metadata={"lot_size": 15},
    )


@pytest.mark.asyncio
async def test_flatten_live_closes_position_filled():
    from api.routes.risk import _flatten_live

    sym = "BANKNIFTY24FUT"
    adapter = MagicMock()
    adapter.place_order = AsyncMock(return_value=_make_live_update(sym, fill_price=48000.0))
    adapters = {"dhan": adapter}

    positions_db: dict = {sym: {"qty": 1.0, "exchange": "NSE"}}
    orders_db: dict = {}
    record_fill = MagicMock()

    def select_adapter(adapters, broker):
        return adapters[broker]

    mc = MagicMock(active_mode="AUTO_FUTURES")

    with patch("brokers.token_manager.BrokerTokenManager") as mock_tm:
        mock_tm.return_value.get_active_broker.return_value = "dhan"
        result = await _flatten_live(
            adapters, orders_db, positions_db, record_fill,
            select_adapter, mc, "evt-5", "2026-10-09T10:00:00+05:30", MagicMock()
        )

    assert result["mode"] == "LIVE"
    assert result["closed_count"] == 1
    assert result["failed_count"] == 0
    assert result["live_closes"][0]["outcome"] == "FILLED"
    assert result["live_closes"][0]["fill_price"] == 48000.0
    record_fill.assert_called_once()


@pytest.mark.asyncio
async def test_flatten_live_cancels_pending_order():
    from api.routes.risk import _flatten_live

    adapter = MagicMock()
    adapter.cancel_order = AsyncMock(return_value=None)
    adapter.place_order = AsyncMock()  # no positions to close
    adapters = {"dhan": adapter}

    orders_db: dict = {"ORD-0001": {"status": "OPEN", "broker_order_id": "DHAN_001"}}
    positions_db: dict = {}

    def select_adapter(adapters, broker):
        return adapters[broker]

    with patch("brokers.token_manager.BrokerTokenManager") as mock_tm:
        mock_tm.return_value.get_active_broker.return_value = "dhan"
        result = await _flatten_live(
            adapters, orders_db, positions_db, MagicMock(),
            select_adapter, MagicMock(active_mode="AUTO_OPTIONS"),
            "evt-6", "2026-10-09T10:00:00+05:30", MagicMock()
        )

    assert result["live_cancels"][0]["outcome"] == "CANCELLED"
    assert orders_db["ORD-0001"]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_flatten_live_timeout_reports_unknown():
    from api.routes.risk import _flatten_live

    sym = "NIFTY24FUT"
    adapter = MagicMock()
    adapter.place_order = AsyncMock(side_effect=asyncio.TimeoutError())
    adapters = {"dhan": adapter}

    positions_db: dict = {sym: {"qty": 1.0, "exchange": "NSE"}}

    def select_adapter(adapters, broker):
        return adapters[broker]

    with patch("brokers.token_manager.BrokerTokenManager") as mock_tm:
        mock_tm.return_value.get_active_broker.return_value = "dhan"
        result = await _flatten_live(
            adapters, {}, positions_db, MagicMock(),
            select_adapter, MagicMock(active_mode="AUTO_FUTURES"),
            "evt-7", "2026-10-09T10:00:00+05:30", MagicMock()
        )

    assert result["closed_count"] == 0
    assert result["failed_count"] == 1
    close = result["live_closes"][0]
    assert close["outcome"] == "TIMEOUT_UNKNOWN"
    assert sym in result["unresolved_symbols"]


@pytest.mark.asyncio
async def test_flatten_live_no_adapter_returns_error_response():
    from api.routes.risk import _flatten_live

    def select_adapter(adapters, broker):
        raise HTTPException(status_code=503, detail="no adapter for dhan")

    from fastapi import HTTPException

    with patch("brokers.token_manager.BrokerTokenManager") as mock_tm:
        mock_tm.return_value.get_active_broker.return_value = "dhan"
        result = await _flatten_live(
            {}, {}, {}, MagicMock(),
            select_adapter, MagicMock(active_mode="AUTO_OPTIONS"),
            "evt-8", "2026-10-09T10:00:00+05:30", MagicMock()
        )

    assert "error" in result
    assert "unavailable" in result["error"]
    assert result["live_closes"] == []
    assert result["live_cancels"] == []


@pytest.mark.asyncio
async def test_flatten_live_cancel_order_not_in_cancelable_skipped():
    """Terminal-state orders (FILLED, REJECTED) must not be re-cancelled."""
    from api.routes.risk import _flatten_live

    adapter = MagicMock()
    adapter.cancel_order = AsyncMock()
    adapters = {"dhan": adapter}
    orders_db = {
        "ORD-0010": {"status": "FILLED"},
        "ORD-0011": {"status": "REJECTED"},
    }

    def select_adapter(adapters, broker):
        return adapters[broker]

    with patch("brokers.token_manager.BrokerTokenManager") as mock_tm:
        mock_tm.return_value.get_active_broker.return_value = "dhan"
        result = await _flatten_live(
            adapters, orders_db, {}, MagicMock(),
            select_adapter, MagicMock(active_mode="AUTO_OPTIONS"),
            "evt-9", "2026-10-09T10:00:00+05:30", MagicMock()
        )

    adapter.cancel_order.assert_not_called()
    assert result["live_cancels"] == []


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
