"""
Strike Desk V4 - Risk State Manager
Coordinates risk limit enforcement, trade tracking, MTM P&L calculation, and persistence.
Enforces:
- Blueprint O5: ₹2,500 daily loss ceiling; 5-trade cap; 45m cooldown after 2 consecutive losses;
  15m stagnation exit; 15:10 IST hard square-off; emergency halt kill-switch.
- Review Gap A: Pre-trade risk authorization gate.
- Review Gap B: True economic loss limit scope (Realized + Unrealized - Total Charges).
- Review Risk 6: Tick-level P&L monitoring on every market tick.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

from models.account import Position
from models.risk_state import IST, IST_OFFSET, RiskState, Trade
from risk.charge_calculator import ChargeCalculator, charge_calculator
from risk.pnl_calculator import PnLCalculator, pnl_calculator

logger = logging.getLogger(__name__)


def get_default_db_path() -> str:
    """Returns primary DB path with safe fallback if /root is not writable."""
    # Override exists so pytest (tests/conftest.py) never opens the live DB.
    if os.environ.get("STRIKE_DESK_RISK_DB"):
        return os.environ["STRIKE_DESK_RISK_DB"]
    if os.path.exists("/root/strike_desk") and os.access("/root/strike_desk", os.W_OK):
        return "/root/strike_desk/data/risk_state.db"
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base, "data", "risk_state.db")


def get_default_json_path() -> str:
    """Returns primary JSON backup path with safe fallback."""
    if os.environ.get("STRIKE_DESK_RISK_BACKUP"):
        return os.environ["STRIKE_DESK_RISK_BACKUP"]
    if os.path.exists("/root/strike_desk") and os.access("/root/strike_desk", os.W_OK):
        return "/root/strike_desk/data/risk_state_backup.json"
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base, "data", "risk_state_backup.json")


def get_ist_now() -> datetime:
    """Returns current datetime in Indian Standard Time (UTC+5:30)."""
    return datetime.now(IST)


def get_ist_date(dt: Optional[datetime] = None) -> date:
    """Extracts trading date in IST from UTC or naive datetime."""
    if dt is None:
        return get_ist_now().date()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).date()


class RiskStateManager:
    """
    Centralized, thread-safe, and crash-resilient Risk State Manager.
    Persists state to SQLite with an atomic JSON backup for disaster recovery.
    """

    _instance: Optional["RiskStateManager"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "RiskStateManager":
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(
        self,
        db_path: Optional[str] = None,
        json_backup_path: Optional[str] = None,
        pnl_calc: Optional[PnLCalculator] = None,
        force_reinit: bool = False,
    ) -> None:
        is_reconfig = (db_path is not None) or (json_backup_path is not None) or force_reinit

        if getattr(self, "_initialized", False) and not is_reconfig:
            return

        self._thread_lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None

        resolved_db = db_path or getattr(self, "db_path", None) or get_default_db_path()
        resolved_json = json_backup_path or getattr(self, "json_backup_path", None) or get_default_json_path()

        self.db_path = str(resolved_db)
        self.json_backup_path = str(resolved_json)
        self.pnl_calculator = pnl_calc or getattr(self, "pnl_calculator", None) or pnl_calculator

        # Ensure directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.json_backup_path).parent.mkdir(parents=True, exist_ok=True)

        self._state = RiskState(trading_date=get_ist_date())
        self._init_sqlite()
        self.initialize()
        self._initialized = True

    def _get_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _init_sqlite(self) -> None:
        """Creates SQLite tables if they do not exist."""
        with self._thread_lock:
            try:
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS risk_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        trading_date TEXT NOT NULL,
                        realized_pnl REAL NOT NULL,
                        unrealized_pnl REAL NOT NULL,
                        total_charges REAL NOT NULL,
                        total_risk_pnl REAL NOT NULL,
                        trade_count INTEGER NOT NULL,
                        consecutive_losses INTEGER NOT NULL,
                        last_trade_time TEXT,
                        last_trade_result TEXT,
                        cooldown_until TEXT,
                        emergency_halt_active INTEGER NOT NULL,
                        emergency_halt_reason TEXT,
                        emergency_halt_acknowledged INTEGER NOT NULL,
                        last_updated TEXT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        trade_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        entry_price REAL NOT NULL,
                        exit_price REAL NOT NULL,
                        realized_pnl REAL NOT NULL,
                        charges REAL NOT NULL,
                        net_pnl REAL NOT NULL,
                        trade_type TEXT,
                        segment TEXT,
                        timestamp TEXT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS daily_snapshots (
                        trading_date TEXT PRIMARY KEY,
                        realized_pnl REAL NOT NULL,
                        unrealized_pnl REAL NOT NULL,
                        total_charges REAL NOT NULL,
                        total_risk_pnl REAL NOT NULL,
                        trade_count INTEGER NOT NULL,
                        consecutive_losses INTEGER NOT NULL,
                        emergency_halt_triggered INTEGER NOT NULL,
                        snapshot_time TEXT NOT NULL
                    )
                """)
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error("[RiskStateManager] SQLite table initialization failed: %s", e)

    def initialize(self) -> None:
        """
        Loads state from SQLite (or JSON fallback).
        Checks if daily reset is needed.
        """
        with self._thread_lock:
            restored = self.restore_state()
            today_ist = get_ist_date()

            if not restored:
                self._state = RiskState(trading_date=today_ist)
                self.persist_state()
            elif self._state.trading_date < today_ist:
                logger.info(
                    "[RiskStateManager] Stale trading date %s detected (< today %s). Performing daily reset.",
                    self._state.trading_date,
                    today_ist,
                )
                self.daily_reset()

    def get_state(self) -> RiskState:
        """Returns snapshot of current RiskState."""
        with self._thread_lock:
            return RiskState.from_dict(self._state.to_dict())

    def record_trade(self, trade: Trade) -> None:
        """
        Records an executed trade, updates metrics, evaluates consecutive losses, and persists state.
        """
        with self._thread_lock:
            trade_ist_date = get_ist_date(trade.timestamp)
            if trade_ist_date > self._state.trading_date:
                logger.info("[RiskStateManager] Trade arrives on new day %s. Triggering daily reset.", trade_ist_date)
                self.daily_reset()

            self._state.trade_count += 1
            self._state.realized_pnl = round(self._state.realized_pnl + trade.realized_pnl, 4)
            self._state.total_charges = round(self._state.total_charges + trade.charges, 4)
            self._state.last_trade_time = trade.timestamp

            # Consecutive loss tracking for 45-minute cooldown.
            # net_pnl == 0.0 means no P&L data is available yet (e.g. an entry
            # fill recorded at order-placement time before the position is closed).
            # A break-even or pending trade is NEUTRAL: it must not increment
            # consecutive_losses and must not clear the cooldown either.
            if trade.net_pnl > 0.0:
                self._state.last_trade_result = "WIN"
                self._state.consecutive_losses = 0
                self._state.cooldown_until = None
            elif trade.net_pnl < 0.0:
                self._state.last_trade_result = "LOSS"
                self._state.consecutive_losses += 1
                if self._state.consecutive_losses >= 2:
                    self._state.cooldown_until = trade.timestamp + timedelta(minutes=45)
                    logger.warning(
                        "[RiskStateManager] 2 consecutive losses hit! Triggered 45m cooldown until %s",
                        self._state.cooldown_until.isoformat(),
                    )
            # else: net_pnl == 0.0 → neutral, leave consecutive_losses unchanged

            self._state.recalculate_total_risk_pnl()
            self._state.last_updated = datetime.now(timezone.utc)
            self.pnl_calculator.add_trade(trade)

            # Persist trade record to DB
            self._persist_trade_record(trade)
            self.persist_state()

    async def record_trade_async(self, trade: Trade) -> None:
        """Async-safe trade recording."""
        lock = self._get_async_lock()
        async with lock:
            self.record_trade(trade)

    def update_position_pnl(self, symbol: str, current_price: float) -> float:
        """
        Tick-level P&L update (Review Risk 6 compliance).
        Calculates instantaneous MTM and updates RiskState.
        """
        with self._thread_lock:
            sym = symbol.strip().upper()
            total_risk = self.pnl_calculator.update_on_tick(sym, current_price)
            metrics = self.pnl_calculator.get_current_metrics()

            self._state.unrealized_pnl = metrics["unrealized_pnl"]
            self._state.total_risk_pnl = total_risk
            self._state.last_updated = datetime.now(timezone.utc)
            return total_risk

    def check_all_limits(self, current_time: datetime) -> Dict[str, bool]:
        """
        Evaluates all limit metrics. Returns dictionary of booleans (True = OK, False = Breached).
        """
        with self._thread_lock:
            loss_ok = not self._state.is_daily_loss_ceiling_breached(-2500.0)
            trade_cap_ok = not self._state.is_trade_cap_breached(5)
            cooldown_ok = not self._state.is_in_cooldown(current_time)
            stagnation_ok = not self._state.is_stagnant(current_time, 15)
            emergency_halt_ok = not self._state.is_emergency_halt_active()

            return {
                "loss_ceiling_ok": loss_ok,
                "trade_cap_ok": trade_cap_ok,
                "cooldown_ok": cooldown_ok,
                "stagnation_ok": stagnation_ok,
                "emergency_halt_ok": emergency_halt_ok,
            }

    def can_place_order(self, current_time: datetime) -> Tuple[bool, Optional[str]]:
        """
        Enforces pre-trade risk authorization gate (Review Gap A).
        Order of evaluation: Emergency Halt -> Loss Ceiling -> Trade Cap -> Cooldown.
        """
        with self._thread_lock:
            return self._state.can_place_order(current_time)

    def evaluate_signal(
        self,
        signal: Any,
        portfolio_state: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Pre-signal risk gate. Called before a signal is stored as ACTIVE.
        Returns (allowed, rejection_reason_or_None).

        portfolio_state keys (all optional; fall back to live RiskState):
          current_daily_pnl      float  — total P&L today (realized + unrealized - charges)
          open_positions_count   int    — number of currently open positions
          max_capital_per_trade  float  — max ₹ capital deployed per signal (default 50 000)
          daily_loss_ceiling     float  — max daily loss allowed (default -2 500)
          max_open_positions     int    — max concurrent open positions (default 5)
        """
        ps: Dict[str, Any] = portfolio_state or {}

        with self._thread_lock:
            current_pnl: float = float(
                ps.get("current_daily_pnl", self._state.total_risk_pnl)
            )

        open_count: int = int(ps.get("open_positions_count", 0))
        max_trade: float = float(ps.get("max_capital_per_trade", 50_000.0))
        ceil_loss: float = float(ps.get("daily_loss_ceiling", -2_500.0))
        max_pos: int = int(ps.get("max_open_positions", 5))

        entry_price: float = float(getattr(signal, "entry_price", 0.0))

        # 1. Max trade size: entry_price is the per-unit contract price proxy
        if entry_price > max_trade:
            return False, (
                f"Max trade size exceeded "
                f"(Signal={entry_price:.0f}, Limit={max_trade:.0f})"
            )

        # 2. Max daily loss: project worst-case outcome if stop-loss triggers.
        # Requires an explicit `quantity` in portfolio_state — at signal generation
        # time the lot_size is unknown, so we skip this check rather than computing
        # a speculative max_qty that would produce false rejections for cheap contracts.
        stop_loss_price = getattr(signal, "stop_loss_price", None)
        quantity = ps.get("quantity")
        if stop_loss_price is not None and quantity is not None and entry_price > 0:
            from models.signal import Direction
            direction = getattr(signal, "direction", None)
            if direction == Direction.LONG:
                sl_distance = entry_price - float(stop_loss_price)
            else:
                sl_distance = float(stop_loss_price) - entry_price

            if sl_distance > 0:
                trade_risk = float(quantity) * sl_distance
                projected_pnl = current_pnl - trade_risk
                if projected_pnl <= ceil_loss:
                    return False, (
                        f"Max daily loss would be breached "
                        f"(CurrentPnL={current_pnl:.0f}, TradeRisk={trade_risk:.0f}, "
                        f"Ceiling={ceil_loss:.0f})"
                    )

        # 3. Position limits
        if open_count >= max_pos:
            return False, (
                f"Max open positions reached ({open_count}/{max_pos})"
            )

        return True, None

    def activate_emergency_halt(self, reason: str) -> None:
        """
        Engages emergency halt kill-switch. Halts all further trading until manual acknowledgment.
        """
        with self._thread_lock:
            self._state.emergency_halt_active = True
            self._state.emergency_halt_reason = reason
            self._state.emergency_halt_acknowledged = False
            self._state.last_updated = datetime.now(timezone.utc)
            logger.critical("[RiskStateManager] EMERGENCY HALT ACTIVATED: %s", reason)
            self.persist_state()

    def acknowledge_emergency_halt(self) -> None:
        """
        Manually clears emergency halt after operator verification.
        """
        with self._thread_lock:
            self._state.emergency_halt_active = False
            self._state.emergency_halt_acknowledged = True
            self._state.emergency_halt_reason = None
            self._state.last_updated = datetime.now(timezone.utc)
            logger.info("[RiskStateManager] Emergency halt manually acknowledged and cleared.")
            self.persist_state()

    def daily_reset(self, target_date: Optional[date] = None) -> None:
        """
        Resets daily counters at 00:00 IST or upon session boundary.
        Preserves unacknowledged emergency halts.
        """
        with self._thread_lock:
            new_date = target_date or get_ist_date()
            prev_date = self._state.trading_date

            # Snapshot previous day if date changed
            self._save_daily_snapshot()

            emergency_active = self._state.emergency_halt_active
            emergency_reason = self._state.emergency_halt_reason
            emergency_ack = self._state.emergency_halt_acknowledged

            # Reset state for fresh day
            self._state = RiskState(
                trading_date=new_date,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_charges=0.0,
                total_risk_pnl=0.0,
                trade_count=0,
                consecutive_losses=0,
                last_trade_time=None,
                last_trade_result=None,
                cooldown_until=None,
                emergency_halt_active=emergency_active,
                emergency_halt_reason=emergency_reason,
                emergency_halt_acknowledged=emergency_ack,
                last_updated=datetime.now(timezone.utc),
            )

            self.pnl_calculator.clear()
            self.persist_state()
            logger.info("[RiskStateManager] Daily reset executed. Previous: %s -> New: %s", prev_date, new_date)

    def _save_daily_snapshot(self) -> None:
        """Saves end-of-day summary snapshot to SQLite."""
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO daily_snapshots (
                    trading_date, realized_pnl, unrealized_pnl, total_charges,
                    total_risk_pnl, trade_count, consecutive_losses,
                    emergency_halt_triggered, snapshot_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self._state.trading_date.isoformat(),
                self._state.realized_pnl,
                self._state.unrealized_pnl,
                self._state.total_charges,
                self._state.total_risk_pnl,
                self._state.trade_count,
                self._state.consecutive_losses,
                1 if self._state.emergency_halt_active else 0,
                datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("[RiskStateManager] Failed to save daily snapshot: %s", e)

    def _persist_trade_record(self, trade: Trade) -> None:
        """Appends trade execution to SQLite trades audit table."""
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO trades (
                    trade_id, symbol, side, quantity, entry_price, exit_price,
                    realized_pnl, charges, net_pnl, trade_type, segment, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.trade_id,
                trade.symbol,
                trade.side,
                trade.quantity,
                trade.entry_price,
                trade.exit_price,
                trade.realized_pnl,
                trade.charges,
                trade.net_pnl,
                trade.trade_type,
                trade.segment,
                trade.timestamp.isoformat(),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("[RiskStateManager] Failed to write trade record: %s", e)

    def persist_state(self) -> None:
        """
        Persists current RiskState atomically to SQLite and writes JSON backup.
        """
        with self._thread_lock:
            # 1. SQLite atomic write
            try:
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                cur = conn.cursor()
                cur.execute("""
                    INSERT OR REPLACE INTO risk_state (
                        id, trading_date, realized_pnl, unrealized_pnl, total_charges,
                        total_risk_pnl, trade_count, consecutive_losses, last_trade_time,
                        last_trade_result, cooldown_until, emergency_halt_active,
                        emergency_halt_reason, emergency_halt_acknowledged, last_updated
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    self._state.trading_date.isoformat(),
                    self._state.realized_pnl,
                    self._state.unrealized_pnl,
                    self._state.total_charges,
                    self._state.total_risk_pnl,
                    self._state.trade_count,
                    self._state.consecutive_losses,
                    self._state.last_trade_time.isoformat() if self._state.last_trade_time else None,
                    self._state.last_trade_result,
                    self._state.cooldown_until.isoformat() if self._state.cooldown_until else None,
                    1 if self._state.emergency_halt_active else 0,
                    self._state.emergency_halt_reason,
                    1 if self._state.emergency_halt_acknowledged else 0,
                    self._state.last_updated.isoformat(),
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error("[RiskStateManager] Failed to persist state to SQLite: %s", e)

            # 2. JSON backup atomic write (write temp file then atomic replace)
            try:
                Path(self.json_backup_path).parent.mkdir(parents=True, exist_ok=True)
                temp_json = f"{self.json_backup_path}.tmp"
                with open(temp_json, "w", encoding="utf-8") as f:
                    json.dump(self._state.to_dict(), f, indent=2)
                os.replace(temp_json, self.json_backup_path)
            except Exception as e:
                logger.error("[RiskStateManager] Failed to write JSON backup: %s", e)

    def restore_state(self) -> bool:
        """
        Restores state: SQLite first; falls back to JSON backup if SQLite is missing or corrupted.
        Returns True if restored, False if brand new state.
        """
        with self._thread_lock:
            # 1. Try SQLite
            try:
                if os.path.exists(self.db_path):
                    conn = sqlite3.connect(self.db_path, timeout=5.0)
                    cur = conn.cursor()
                    cur.execute("SELECT * FROM risk_state WHERE id = 1")
                    row = cur.fetchone()
                    conn.close()

                    if row:
                        self._state = RiskState(
                            trading_date=date.fromisoformat(row[1]),
                            realized_pnl=float(row[2]),
                            unrealized_pnl=float(row[3]),
                            total_charges=float(row[4]),
                            total_risk_pnl=float(row[5]),
                            trade_count=int(row[6]),
                            consecutive_losses=int(row[7]),
                            last_trade_time=datetime.fromisoformat(row[8]) if row[8] else None,
                            last_trade_result=row[9],
                            cooldown_until=datetime.fromisoformat(row[10]) if row[10] else None,
                            emergency_halt_active=bool(row[11]),
                            emergency_halt_reason=row[12],
                            emergency_halt_acknowledged=bool(row[13]),
                            last_updated=datetime.fromisoformat(row[14]) if row[14] else datetime.now(timezone.utc),
                        )
                        logger.info("[RiskStateManager] State successfully restored from SQLite.")
                        return True
            except Exception as e:
                logger.warning("[RiskStateManager] SQLite restore failed (%s). Attempting JSON recovery.", e)

            # 2. Fallback to JSON backup
            try:
                if os.path.exists(self.json_backup_path):
                    with open(self.json_backup_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._state = RiskState.from_dict(data)
                    logger.info("[RiskStateManager] State successfully restored from JSON backup.")
                    # Re-create SQLite DB cleanly if previously corrupted
                    if os.path.exists(self.db_path):
                        try:
                            os.remove(self.db_path)
                        except Exception:
                            pass
                    self._init_sqlite()
                    self.persist_state()
                    return True
            except Exception as e:
                logger.error("[RiskStateManager] JSON backup recovery failed: %s", e)

            logger.info("[RiskStateManager] No existing risk state found. Initializing fresh session.")
            return False

    def reset_for_tests(self, test_date: Optional[date] = None) -> None:
        """Helper to reset all state for unit tests."""
        with self._thread_lock:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.json_backup_path).parent.mkdir(parents=True, exist_ok=True)
            if os.path.exists(self.db_path):
                try:
                    os.remove(self.db_path)
                except Exception:
                    pass
            if os.path.exists(self.json_backup_path):
                try:
                    os.remove(self.json_backup_path)
                except Exception:
                    pass
            self._init_sqlite()
            self._state = RiskState(trading_date=test_date or get_ist_date())
            self.pnl_calculator.clear()
            self.persist_state()


# Global singleton instance
risk_state_manager = RiskStateManager()