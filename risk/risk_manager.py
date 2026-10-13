"""
Strike Desk V4 - Phase 5 Unified Risk Manager
Central Orchestrator for all Risk Controls:
- Hard Limits (Loss Ceiling ₹-2,500, Max 5 Trades)
- Consecutive Loss Cooldown (2 losses -> 45m cooldown)
- Tick-level P&L monitoring & immediate trip
- Partial fills & Position tracking
- 15-Minute Stagnation Exit (Blueprint O5)
- 15:10 IST EOD Hard Square-Off with Retries & Escalation
- Multi-Level Emergency Halt Kill-Switch with Manual Acknowledgment
"""

import time
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pytz
import threading

logger = logging.getLogger("RiskManager")
IST = pytz.timezone("Asia/Kolkata")

from .eod_square_off import (
    EODSquareOffManager,
    EODSquareOffConfig,
    SquareOffStatus,
    SquareOffReason,
    SquareOffOrder,
    SquareOffResult,
)

from .emergency_halt import (
    EmergencyHaltManager,
    HaltLevel,
    HaltEvent,
)


@dataclass
class RiskLimitsConfig:
    daily_loss_ceiling: float = -2500.0   # Hard loss ceiling in INR
    max_daily_trades: int = 5             # Maximum trades per calendar day
    consecutive_loss_threshold: int = 2   # 2 consecutive losses
    cooldown_duration_minutes: int = 45   # 45-minute cooldown
    stagnation_ceiling_minutes: float = 15.0  # 15-minute stagnation exit
    eod_cutoff_ist: str = "15:10:00"      # 15:10 IST hard square-off
    max_square_off_retries: int = 3       # Max retry attempts for square-off


@dataclass
class ActivePosition:
    position_id: str
    symbol: str
    quantity: int
    entry_price: float
    current_price: float
    entry_time: datetime
    high_price: float = 0.0
    low_price: float = 0.0
    is_open: bool = True
    # Risk exit levels — set from signal after entry
    stop_loss_price: Optional[float] = None    # hard SL: exit immediately if price <= SL (long)
    target_price: Optional[float] = None       # profit target: exit immediately if price >= target (long)
    trail_sl_price: Optional[float] = None     # trailing SL: ratchets up as current_price rises

    def calculate_unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.quantity


@dataclass
class OrderAuthResult:
    authorized: bool
    auth_token: Optional[str] = None
    reason: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(IST))


class RiskManager:
    """
    Unified Risk Controller orchestrating all Phase 5 risk enforcement.
    Thread-safe implementation for multi-threaded tick streams and order routing.
    """
    def __init__(
        self,
        config: Optional[RiskLimitsConfig] = None,
        emergency_halt_manager: Optional[EmergencyHaltManager] = None,
        eod_square_off_manager: Optional[EODSquareOffManager] = None,
    ):
        self.config = config or RiskLimitsConfig()
        self.lock = threading.RLock()

        self.halt_manager = emergency_halt_manager or EmergencyHaltManager()
        self.eod_manager = eod_square_off_manager or EODSquareOffManager(
            config=EODSquareOffConfig(
                cutoff_time_str=self.config.eod_cutoff_ist,
                max_retries=self.config.max_square_off_retries,
                escalate_to_emergency_halt_on_failure=True,
            ),
            emergency_halt_manager=self.halt_manager,
        )

        self.current_ist_date = datetime.now(IST).date()
        self.realized_pnl: float = 0.0
        self.trade_count: int = 0
        self.consecutive_losses: int = 0
        self.cooldown_until: Optional[datetime] = None
        self.positions: Dict[str, ActivePosition] = {}
        self.authorized_orders: Dict[str, Dict[str, Any]] = {}
        self.daily_closed_trades: List[Dict[str, Any]] = []

    def _check_and_reset_daily_state(self, current_time: Optional[datetime] = None):
        now = current_time or datetime.now(IST)
        if now.date() != self.current_ist_date:
            self.current_ist_date = now.date()
            self.realized_pnl = 0.0
            self.trade_count = 0
            self.consecutive_losses = 0
            self.cooldown_until = None
            self.positions.clear()
            self.authorized_orders.clear()
            self.daily_closed_trades.clear()

    def get_total_risk_pnl(self) -> float:
        with self.lock:
            unrealized = sum(p.calculate_unrealized_pnl() for p in self.positions.values() if p.is_open)
            return self.realized_pnl + unrealized

    def is_cooldown_active(self, current_time: Optional[datetime] = None) -> bool:
        with self.lock:
            if self.cooldown_until is None:
                return False
            now = current_time or datetime.now(IST)
            if now < self.cooldown_until:
                return True
            self.cooldown_until = None
            return False

    def is_past_eod_cutoff(self, current_time: Optional[datetime] = None) -> bool:
        return self.eod_manager.is_past_cutoff(current_time)

    def authorize_order(
        self,
        symbol: str,
        quantity: int,
        side: str,
        order_id: Optional[str] = None,
        current_time: Optional[datetime] = None,
    ) -> OrderAuthResult:
        """
        Pre-trade risk gating. Validates limits, halts, cooldowns, and cutoffs.
        """
        with self.lock:
            now = current_time or datetime.now(IST)
            self._check_and_reset_daily_state(now)
            oid = order_id or f"ORD_{uuid.uuid4().hex[:8].upper()}"

            # 1. Emergency Halt Check
            if self.halt_manager.is_halted:
                reason = f"Emergency halt active: {self.halt_manager.current_reason}"
                print(f"[OrderAuthorizer] Order {oid} REJECTED: {reason}")
                return OrderAuthResult(authorized=False, reason=reason)

            # 2. 15:10 IST Cutoff Check
            if self.is_past_eod_cutoff(now):
                reason = "Past 15:10 IST cutoff"
                print(f"[OrderAuthorizer] Order {oid} REJECTED: {reason}")
                return OrderAuthResult(authorized=False, reason=reason)

            # 3. Daily Loss Ceiling Check
            total_pnl = self.get_total_risk_pnl()
            if total_pnl <= self.config.daily_loss_ceiling:
                reason = f"Daily loss ceiling breached: ₹{total_pnl:.2f} <= ₹{self.config.daily_loss_ceiling:.2f}"
                print(f"[OrderAuthorizer] Order {oid} REJECTED: {reason}")
                return OrderAuthResult(authorized=False, reason=reason)

            # 4. Daily Trade Cap Check
            if self.trade_count >= self.config.max_daily_trades:
                reason = f"Daily trade cap reached: {self.trade_count}/{self.config.max_daily_trades} trades"
                print(f"[OrderAuthorizer] Order {oid} REJECTED: {reason}")
                return OrderAuthResult(authorized=False, reason=reason)

            # 5. Consecutive Loss Cooldown Check
            if self.is_cooldown_active(now):
                remaining_m = (self.cooldown_until - now).total_seconds() / 60.0
                reason = f"Consecutive loss cooldown active ({remaining_m:.1f}m remaining)"
                print(f"[OrderAuthorizer] Order {oid} REJECTED: {reason}")
                return OrderAuthResult(authorized=False, reason=reason)

            token = f"AUTH_{uuid.uuid4().hex[:10].upper()}"
            self.authorized_orders[oid] = {
                "order_id": oid,
                "symbol": symbol,
                "quantity": quantity,
                "side": side,
                "auth_token": token,
                "authorized_at": now,
            }
            return OrderAuthResult(authorized=True, auth_token=token)

    def record_fill(
        self,
        order_id: str,
        symbol: str,
        filled_qty: int,
        fill_price: float,
        is_complete: bool = True,
        current_time: Optional[datetime] = None,
    ):
        """
        Handles partial and complete trade fills, maintaining weighted average entry price.
        """
        with self.lock:
            now = current_time or datetime.now(IST)
            if symbol not in self.positions or not self.positions[symbol].is_open:
                self.positions[symbol] = ActivePosition(
                    position_id=f"POS_{symbol}_{uuid.uuid4().hex[:6].upper()}",
                    symbol=symbol,
                    quantity=filled_qty,
                    entry_price=fill_price,
                    current_price=fill_price,
                    entry_time=now,
                    high_price=fill_price,
                    low_price=fill_price,
                    is_open=True,
                )
            else:
                pos = self.positions[symbol]
                total_qty = pos.quantity + filled_qty
                if total_qty != 0:
                    pos.entry_price = (
                        (pos.entry_price * pos.quantity) + (fill_price * filled_qty)
                    ) / total_qty
                pos.quantity = total_qty

            if is_complete:
                self.trade_count += 1

    def apply_fill(
        self,
        order_id: str,
        symbol: str,
        signed_units: int,
        fill_price: float,
        current_time: Optional[datetime] = None,
    ) -> float:
        """
        Books a fill in UNITS (lots × lot_size; + buy, − sell), so P&L here matches
        the broker's. A fill against an open position reduces/closes it and
        realises P&L; anything else opens or adds and counts as a new trade.
        Returns the P&L realised by this fill.
        """
        with self.lock:
            sym = symbol.strip().upper()
            pos = self.positions.get(sym)
            if pos is None or not pos.is_open or pos.quantity == 0 or (pos.quantity > 0) == (signed_units > 0):
                self.record_fill(order_id, sym, signed_units, fill_price,
                                 is_complete=True, current_time=current_time)
                return 0.0

            closing = min(abs(signed_units), abs(pos.quantity))
            remainder = signed_units + (closing if signed_units < 0 else -closing)
            if closing == abs(pos.quantity):
                realized = self.close_trade(sym, fill_price, current_time=current_time)
                if remainder:
                    self.record_fill(order_id, sym, remainder, fill_price,
                                     is_complete=True, current_time=current_time)
                return realized

            # Partial close: realise the closed slice, keep the rest open.
            direction = 1 if pos.quantity > 0 else -1
            realized = (fill_price - pos.entry_price) * closing * direction
            pos.quantity -= closing * direction
            self.realized_pnl += realized
            return realized

    def set_position_levels(
        self,
        symbol: str,
        stop_loss_price: Optional[float] = None,
        target_price: Optional[float] = None,
        trailing_sl_pct: Optional[float] = None,
    ) -> None:
        """
        Attach SL / target / trailing-SL to an open position after entry fill.
        trailing_sl_pct: e.g. 0.05 = trail at 5% below the running high for longs.
        """
        with self.lock:
            sym = (symbol or "").strip().upper()
            pos = self.positions.get(sym)
            if pos is None or not pos.is_open:
                return
            if stop_loss_price is not None:
                pos.stop_loss_price = float(stop_loss_price)
            if target_price is not None:
                pos.target_price = float(target_price)
            if trailing_sl_pct is not None and pos.entry_price > 0:
                # Initialise trail SL from entry price
                if pos.quantity > 0:  # long
                    pos.trail_sl_price = round(pos.entry_price * (1.0 - trailing_sl_pct), 2)
                else:  # short
                    pos.trail_sl_price = round(pos.entry_price * (1.0 + trailing_sl_pct), 2)

    def check_sl_target_exits(
        self,
        current_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scans open positions for SL / target / trailing-SL breaches.
        Returns list of dicts: {symbol, reason, exit_price}.
        """
        exits: List[Dict[str, Any]] = []
        with self.lock:
            for sym, pos in self.positions.items():
                if not pos.is_open or pos.quantity == 0:
                    continue
                ltp = pos.current_price
                if ltp <= 0:
                    continue
                is_long = pos.quantity > 0

                # Hard stop-loss
                if pos.stop_loss_price is not None:
                    if is_long and ltp <= pos.stop_loss_price:
                        exits.append({"symbol": sym, "reason": "STOP_LOSS", "exit_price": ltp})
                        continue
                    if not is_long and ltp >= pos.stop_loss_price:
                        exits.append({"symbol": sym, "reason": "STOP_LOSS", "exit_price": ltp})
                        continue

                # Target
                if pos.target_price is not None:
                    if is_long and ltp >= pos.target_price:
                        exits.append({"symbol": sym, "reason": "TARGET", "exit_price": ltp})
                        continue
                    if not is_long and ltp <= pos.target_price:
                        exits.append({"symbol": sym, "reason": "TARGET", "exit_price": ltp})
                        continue

                # Trailing SL — ratchet the floor up as price rises (long)
                if pos.trail_sl_price is not None:
                    # Ratchet: use high_price as the peak for long positions
                    if is_long:
                        if pos.high_price > pos.entry_price:
                            # recompute trail floor: keep 5% below running high
                            # Use the gap between entry and initial trail to infer pct
                            if pos.entry_price > 0:
                                _trail_pct = (pos.entry_price - pos.trail_sl_price) / pos.entry_price
                                new_trail = round(pos.high_price * (1.0 - _trail_pct), 2)
                                if new_trail > pos.trail_sl_price:
                                    pos.trail_sl_price = new_trail
                        if ltp <= pos.trail_sl_price:
                            exits.append({"symbol": sym, "reason": "TRAIL_SL", "exit_price": ltp})
                            continue
                    else:
                        if pos.low_price < pos.entry_price and pos.entry_price > 0:
                            _trail_pct = (pos.trail_sl_price - pos.entry_price) / pos.entry_price
                            new_trail = round(pos.low_price * (1.0 + _trail_pct), 2)
                            if new_trail < pos.trail_sl_price:
                                pos.trail_sl_price = new_trail
                        if ltp >= pos.trail_sl_price:
                            exits.append({"symbol": sym, "reason": "TRAIL_SL", "exit_price": ltp})
                            continue
        return exits

    def update_tick(
        self,
        symbol: str,
        ltp: float,
        current_time: Optional[datetime] = None,
    ) -> float:
        """
        Fast tick-level monitor. Recalculates Total Risk P&L and trips loss ceiling on breach.
        """
        with self.lock:
            if symbol in self.positions and self.positions[symbol].is_open:
                pos = self.positions[symbol]
                pos.current_price = ltp
                pos.high_price = max(pos.high_price, ltp)
                pos.low_price = min(pos.low_price, ltp)

            total_pnl = self.get_total_risk_pnl()
            if total_pnl <= self.config.daily_loss_ceiling:
                print(
                    f"[CircuitBreaker] TICK BREACH: {symbol} price=₹{ltp:.2f} pushed "
                    f"Total Risk P&L to ₹{total_pnl:.2f}. LOSS CEILING TRIPPED!"
                )
                self.halt_manager.trigger_halt(
                    reason=f"Loss ceiling tick breach: P&L ₹{total_pnl:.2f} <= ₹{self.config.daily_loss_ceiling:.2f}",
                    level=HaltLevel.HARD_HALT,
                    triggered_by="TICK_P_AND_L_BREAKER",
                )
            return total_pnl

    def close_trade(
        self,
        symbol: str,
        exit_price: float,
        current_time: Optional[datetime] = None,
    ) -> float:
        """
        Closes active position, calculates realized PnL, and manages streak counters.
        """
        with self.lock:
            now = current_time or datetime.now(IST)
            if symbol not in self.positions or not self.positions[symbol].is_open:
                return 0.0

            pos = self.positions[symbol]
            trade_pnl = (exit_price - pos.entry_price) * pos.quantity
            pos.quantity = 0
            pos.is_open = False
            self.realized_pnl += trade_pnl

            if trade_pnl < 0:
                self.consecutive_losses += 1
                if self.consecutive_losses >= self.config.consecutive_loss_threshold:
                    self.cooldown_until = now + timedelta(minutes=self.config.cooldown_duration_minutes)
                    print(
                        f"[RiskStateManager] {self.consecutive_losses} consecutive losses hit! "
                        f"Triggered {self.config.cooldown_duration_minutes}m cooldown until {self.cooldown_until.isoformat()}"
                    )
            else:
                self.consecutive_losses = 0
                self.cooldown_until = None

            self.daily_closed_trades.append({
                "symbol": symbol,
                "pnl": trade_pnl,
                "closed_at": now,
            })

            if self.realized_pnl <= self.config.daily_loss_ceiling:
                print(f"[CircuitBreaker] Realized loss ₹{self.realized_pnl:.2f} hit ceiling! Tripping breaker.")
                self.halt_manager.trigger_halt(
                    reason=f"Daily loss ceiling breached: ₹{self.realized_pnl:.2f}",
                    level=HaltLevel.HARD_HALT,
                    triggered_by="LOSS_CEILING_BREAKER",
                )

            return trade_pnl

    def check_stagnant_positions(
        self,
        current_time: Optional[datetime] = None,
    ) -> List[str]:
        """
        Scans open positions exceeding holding ceiling (Blueprint O5).
        """
        with self.lock:
            now = current_time or datetime.now(IST)
            stagnant = []
            for sym, pos in self.positions.items():
                if pos.is_open:
                    duration_m = (now - pos.entry_time).total_seconds() / 60.0
                    if duration_m >= self.config.stagnation_ceiling_minutes:
                        print(
                            f"[StagnationMonitor] Stagnation alert triggered: Position stagnant for "
                            f"{duration_m:.1f}m exceeding {self.config.stagnation_ceiling_minutes}m ceiling (Blueprint O5)"
                        )
                        stagnant.append(sym)
            return stagnant

    def run_eod_square_off(
        self,
        broker_router: Any = None,
        current_time: Optional[datetime] = None,
    ) -> Optional[SquareOffResult]:
        """
        Executes automated 15:10 IST hard square-off against all active open positions.
        """
        with self.lock:
            now = current_time or datetime.now(IST)
            open_positions = [p for p in self.positions.values() if p.is_open and p.quantity != 0]
            result = self.eod_manager.check_and_trigger(
                positions=open_positions,
                broker_router=broker_router,
                current_time=now,
            )
            return result

    def get_risk_summary(self) -> Dict[str, Any]:
        with self.lock:
            total_pnl = self.get_total_risk_pnl()
            return {
                "ist_date": str(self.current_ist_date),
                "realized_pnl": self.realized_pnl,
                "total_risk_pnl": total_pnl,
                "daily_loss_ceiling": self.config.daily_loss_ceiling,
                "loss_ceiling_breached": total_pnl <= self.config.daily_loss_ceiling,
                "trade_count": self.trade_count,
                "max_daily_trades": self.config.max_daily_trades,
                "consecutive_losses": self.consecutive_losses,
                "cooldown_active": self.is_cooldown_active(),
                "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
                "emergency_halt_active": self.halt_manager.is_halted,
                "emergency_halt_reason": self.halt_manager.current_reason,
                "open_positions_count": sum(1 for p in self.positions.values() if p.is_open),
            }


risk_manager = RiskManager()
