from typing import Any, Optional


class StrikeDeskError(Exception):
    """Base exception class for all Strike Desk errors."""

    def __init__(
        self,
        message: str,
        broker: Optional[str] = None,
        error_code: Optional[str] = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.broker = broker
        self.error_code = error_code
        self.raw_error = raw_error

    def __str__(self) -> str:
        parts = [self.message]
        if self.broker:
            parts.append(f"broker={self.broker}")
        if self.error_code:
            parts.append(f"code={self.error_code}")
        return " | ".join(parts)


class AuthenticationError(StrikeDeskError):
    """Raised when broker authentication fails or tokens expire."""
    pass


class ConnectionError(StrikeDeskError):
    """Raised when network, HTTP, or WebSocket connections drop."""
    pass


class OrderError(StrikeDeskError):
    """Base exception for order lifecycle errors."""
    pass


class OrderRejected(OrderError):
    """Raised when an order is rejected by broker or exchange."""
    pass


class OrderTimeout(OrderError):
    """Raised when an order submission fails to acknowledge within the timeout."""
    pass


class OrderNotFound(OrderError):
    """Raised when an order is missing in local or broker records."""
    pass


class DuplicateOrderError(OrderError):
    """Raised when a duplicate order is detected within the idempotency window."""
    pass


class RateLimitError(StrikeDeskError):
    """Raised when broker API rate limit threshold is exceeded."""

    def __init__(
        self,
        message: str,
        broker: Optional[str] = None,
        retry_after: float = 1.0,
        raw_error: Any = None,
    ) -> None:
        super().__init__(message, broker=broker, error_code="RATE_LIMIT", raw_error=raw_error)
        self.retry_after = retry_after


class DataError(StrikeDeskError):
    """Raised when data parsing, schema normalization, or feeds fail."""
    pass


class ConfigurationError(StrikeDeskError):
    """Raised when required settings or credentials are missing."""
    pass