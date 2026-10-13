"""
Strike Desk V4 - Real-Time P&L and Risk Value Calculator
Calculates:
- Realized P&L: Sum of closed trades today
- Unrealized P&L: Mark-to-market valuation across all open positions
- Total Charges: Sum of statutory & broker charges incurred
- Total Risk P&L: Realized P&L + Unrealized P&L - Total Charges (True Economic Loss)
- Tick-Level P&L: Instantaneous MTM recalculation on every price tick (Review Risk 6).
"""
import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from models.account import Position
from models.risk_state import Trade

logger = logging.getLogger(__name__)


class PnLCalculator:
    """
    Computes real-time realized, unrealized, and net risk P&L.
    Thread-safe and async-safe with fast cached price lookups.
    """

    def __init__(self) -> None:
        self._positions: Dict[str, Position] = {}
        self._trades: List[Trade] = []
        self._current_prices: Dict[str, float] = {}
        self._thread_lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None

    def _get_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    @classmethod
    def calculate_realized_pnl(cls, trades: List[Trade]) -> float:
        """
        Calculates realized P&L from closed trades.
        """
        total = 0.0
        for t in trades:
            total += t.realized_pnl
        return round(total, 4)

    @classmethod
    def calculate_unrealized_pnl(
        cls,
        positions: List[Position],
        current_prices: Dict[str, float],
    ) -> float:
        """
        Calculates unrealized mark-to-market P&L across open positions.
        """
        total = 0.0
        for pos in positions:
            if not pos.is_open or pos.qty == 0.0:
                continue
            sym = pos.symbol.strip().upper()
            price = current_prices.get(sym, pos.current_price or pos.avg_price)
            if price <= 0.0:
                price = pos.avg_price

            if pos.qty > 0:
                # Long position: (current_price - avg_price) * qty
                total += (price - pos.avg_price) * pos.qty
            else:
                # Short position: (avg_price - current_price) * abs(qty)
                total += (pos.avg_price - price) * abs(pos.qty)

        return round(total, 4)

    @classmethod
    def calculate_total_risk_pnl(
        cls,
        trades: List[Trade],
        positions: List[Position],
        current_prices: Dict[str, float],
    ) -> float:
        """
        Calculates Total Risk P&L (Review Gap B):
        Total Risk P&L = Realized P&L + Unrealized P&L - Total Charges
        """
        realized = cls.calculate_realized_pnl(trades)
        unrealized = cls.calculate_unrealized_pnl(positions, current_prices)
        total_charges = sum(t.charges for t in trades)
        return round(realized + unrealized - total_charges, 4)

    def set_positions(self, positions: List[Position]) -> None:
        """Updates internal position inventory for fast tick-level updates."""
        with self._thread_lock:
            self._positions = {pos.symbol.strip().upper(): pos for pos in positions}

    def set_trades(self, trades: List[Trade]) -> None:
        """Updates internal trade history."""
        with self._thread_lock:
            self._trades = list(trades)

    def add_trade(self, trade: Trade) -> None:
        """Appends a new trade to internal history."""
        with self._thread_lock:
            self._trades.append(trade)

    def update_position(self, position: Position) -> None:
        """Updates or adds an active position."""
        with self._thread_lock:
            self._positions[position.symbol.strip().upper()] = position

    def set_current_price(self, symbol: str, price: float) -> None:
        """Updates cached price for a symbol."""
        with self._thread_lock:
            sym = symbol.strip().upper()
            self._current_prices[sym] = float(price)
            if sym in self._positions:
                self._positions[sym].current_price = float(price)

    def update_on_tick(self, symbol: str, current_price: float) -> float:
        """
        Instantaneous tick-level P&L recalculation (Review Risk 6 compliance).
        Updates current price cache, evaluates open position MTM, and returns new Total Risk P&L.
        """
        with self._thread_lock:
            sym = symbol.strip().upper()
            self._current_prices[sym] = float(current_price)
            if sym in self._positions:
                self._positions[sym].current_price = float(current_price)

            realized = self.calculate_realized_pnl(self._trades)
            unrealized = self.calculate_unrealized_pnl(list(self._positions.values()), self._current_prices)
            charges = sum(t.charges for t in self._trades)
            return round(realized + unrealized - charges, 4)

    async def update_on_tick_async(self, symbol: str, current_price: float) -> float:
        """Async-safe tick update."""
        lock = self._get_async_lock()
        async with lock:
            return self.update_on_tick(symbol, current_price)

    def get_current_metrics(self) -> Dict[str, float]:
        """Returns snapshot of current realized, unrealized, charges, and total risk P&L."""
        with self._thread_lock:
            realized = self.calculate_realized_pnl(self._trades)
            unrealized = self.calculate_unrealized_pnl(list(self._positions.values()), self._current_prices)
            charges = sum(t.charges for t in self._trades)
            total = round(realized + unrealized - charges, 4)
            return {
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "total_charges": round(charges, 4),
                "total_risk_pnl": total,
            }

    def clear(self) -> None:
        """Clears all cached positions, trades, and prices."""
        with self._thread_lock:
            self._positions.clear()
            self._trades.clear()
            self._current_prices.clear()


# Global singleton instance
pnl_calculator = PnLCalculator()