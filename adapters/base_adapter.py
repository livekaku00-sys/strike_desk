from abc import ABC, abstractmethod
import logging
from typing import Any, Dict, List, Optional
from models.account import Balance, Position
from models.order import Order, OrderUpdate


class BaseAdapter(ABC):
    def __init__(self, broker_name: str, balance_ttl: float = 60.0) -> None:
        self.broker_name: str = broker_name
        self.is_connected: bool = False
        self.is_authenticated: bool = False
        self.balance_ttl: float = balance_ttl
        self._cached_balance: Optional[Balance] = None
        self._balance_cached_at: float = 0.0
        self.logger = logging.getLogger(f"strike_desk.adapters.{broker_name.lower()}")

    def invalidate_balance_cache(self) -> None:
        self._cached_balance = None
        self._balance_cached_at = 0.0
        self.logger.debug("[%s] Balance cache invalidated.", self.broker_name)

    @abstractmethod
    async def connect(self) -> None:
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        pass

    @abstractmethod
    async def authenticate(self) -> bool:
        pass

    @abstractmethod
    def get_auth_status(self) -> str:
        pass

    @abstractmethod
    def get_connection_state(self) -> str:
        pass

    @abstractmethod
    async def get_balance(self, force_refresh: bool = False) -> Balance:
        pass

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        pass

    @abstractmethod
    async def get_option_chain(
        self, underlying: str, expiry: Optional[str] = None, force_refresh: bool = False
    ) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def subscribe_ticks(self, symbols: List[str], mode: str = "full") -> None:
        pass

    @abstractmethod
    async def unsubscribe_ticks(self, symbols: List[str]) -> None:
        pass

    @abstractmethod
    async def place_order(self, order: Order) -> OrderUpdate:
        pass

    @abstractmethod
    async def cancel_order(self, client_order_id: str, broker_order_id: Optional[str] = None) -> OrderUpdate:
        pass

    @abstractmethod
    async def modify_order(
        self,
        client_order_id: str,
        broker_order_id: Optional[str] = None,
        price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> OrderUpdate:
        pass

    @abstractmethod
    async def get_order_status(
        self, client_order_id: str, broker_order_id: Optional[str] = None
    ) -> Optional[OrderUpdate]:
        pass

    @abstractmethod
    async def get_open_orders(self) -> List[OrderUpdate]:
        pass