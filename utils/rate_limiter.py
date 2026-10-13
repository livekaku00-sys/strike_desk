import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Dict, Optional

from utils.exceptions import RateLimitError

logger = logging.getLogger(__name__)


@dataclass
class BucketConfig:
    capacity: float = 10.0      # Maximum tokens
    refill_rate: float = 10.0   # Tokens per second


class TokenBucket:
    def __init__(self, config: BucketConfig) -> None:
        self.capacity = config.capacity
        self.refill_rate = config.refill_rate
        self.tokens = config.capacity
        self.last_refill = time.monotonic()
        self.backoff_until: float = 0.0

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

    def can_consume(self, tokens: float = 1.0, now: Optional[float] = None) -> bool:
        t_now = now or time.monotonic()
        if t_now < self.backoff_until:
            return False
        self._refill(t_now)
        return self.tokens >= tokens

    def consume(self, tokens: float = 1.0, now: Optional[float] = None) -> bool:
        t_now = now or time.monotonic()
        if t_now < self.backoff_until:
            return False
        self._refill(t_now)
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def trigger_backoff(self, duration_seconds: float) -> None:
        self.backoff_until = max(self.backoff_until, time.monotonic() + duration_seconds)
        self.tokens = 0.0


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: Dict[str, TokenBucket] = {}
        self._default_config = BucketConfig(capacity=10.0, refill_rate=10.0)
        self._configs: Dict[str, BucketConfig] = {
            "dhan": BucketConfig(capacity=10.0, refill_rate=10.0),
            "angelone": BucketConfig(capacity=10.0, refill_rate=10.0),
            "fyers": BucketConfig(capacity=10.0, refill_rate=10.0),
        }
        self._lock = asyncio.Lock()

    def _get_bucket(self, broker: str) -> TokenBucket:
        b = broker.lower()
        if b not in self._buckets:
            cfg = self._configs.get(b, self._default_config)
            self._buckets[b] = TokenBucket(cfg)
        return self._buckets[b]

    def configure_broker(self, broker: str, capacity: float, refill_rate: float) -> None:
        b = broker.lower()
        cfg = BucketConfig(capacity=capacity, refill_rate=refill_rate)
        self._configs[b] = cfg
        self._buckets[b] = TokenBucket(cfg)

    async def acquire(self, broker: str, tokens: float = 1.0, timeout: float = 10.0) -> bool:
        b = broker.lower()
        start = time.monotonic()

        while True:
            now = time.monotonic()
            if (now - start) > timeout:
                logger.warning("[%s] Rate limit acquire timed out after %.2fs", broker, timeout)
                raise RateLimitError(
                    f"Rate limit timeout exceeded for broker {broker}",
                    broker=broker,
                    retry_after=1.0,
                )

            async with self._lock:
                bucket = self._get_bucket(b)
                if now < bucket.backoff_until:
                    remaining_backoff = bucket.backoff_until - now
                    logger.warning(
                        "[%s] Rate limit active backoff in effect (%.2fs remaining)",
                        broker,
                        remaining_backoff,
                    )
                    sleep_time = min(remaining_backoff, 0.5)
                elif bucket.consume(tokens, now=now):
                    return True
                else:
                    needed = tokens - bucket.tokens
                    sleep_time = min(max(needed / bucket.refill_rate, 0.05), 0.5)

            await asyncio.sleep(sleep_time)

    def record_rate_limit(self, broker: str, backoff_seconds: float = 5.0) -> None:
        b = broker.lower()
        bucket = self._get_bucket(b)
        bucket.trigger_backoff(backoff_seconds)
        logger.warning(
            "[%s] HTTP 429 / Rate Limit recorded! Triggering backoff for %.1fs",
            broker,
            backoff_seconds,
        )

    def reset(self, broker: Optional[str] = None) -> None:
        if broker:
            b = broker.lower()
            if b in self._buckets:
                cfg = self._configs.get(b, self._default_config)
                self._buckets[b] = TokenBucket(cfg)
        else:
            self._buckets.clear()


rate_limiter = RateLimiter()