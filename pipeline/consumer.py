import asyncio
from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional, Set

from models.instrument import Exchange
from models.tick import Tick
from pipeline.indicator_calculator import (
    BaseIndicatorCalculator,
    DefaultIndicatorCalculator,
)
from pipeline.market_session import MarketSession, market_session
from pipeline.stale_feed_detector import StaleFeedDetector, stale_feed_detector
from pipeline.tick_buffer import TickBuffer, tick_buffer

logger = logging.getLogger(__name__)


def _current_locked_class() -> Optional[str]:
    """Operator's execution class lock, from the same mode_controller the auto loop reads."""
    try:
        from api.routes.trading import _MODE_TO_ASSET_CLASS, mode_controller
    except Exception:
        # No lock source (e.g. pipeline used without the API): generate unlocked.
        # The auto-execute loop still enforces the lock before any order is placed.
        return None
    return _MODE_TO_ASSET_CLASS.get(mode_controller.active_mode)


class TickConsumer:
    """
    Asynchronous high-throughput tick consumer.
    Subscribes to per-symbol queues in TickBuffer, validates market sessions,
    updates feed freshness, and pipes ticks into stateful indicator calculators.
    """

    def __init__(
        self,
        buffer: Optional[TickBuffer] = None,
        session_validator: Optional[MarketSession] = None,
        stale_detector: Optional[StaleFeedDetector] = None,
    ) -> None:
        self.buffer = buffer or tick_buffer
        self.market_session = session_validator or market_session
        self.stale_detector = stale_detector or stale_feed_detector

        # symbol -> asyncio.Queue
        self._queues: Dict[str, asyncio.Queue] = {}
        # symbol -> asyncio.Task
        self._worker_tasks: Dict[str, asyncio.Task] = {}
        # symbol -> BaseIndicatorCalculator
        self._calculators: Dict[str, BaseIndicatorCalculator] = {}
        # symbol -> last_tick_datetime_utc
        self._last_tick_times: Dict[str, datetime] = {}

        self._consumed_count: int = 0
        self._is_running: bool = False
        self._is_paused: bool = False
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    def register_calculator(
        self, symbol: str, calculator: BaseIndicatorCalculator
    ) -> None:
        self._calculators[symbol.strip().upper()] = calculator

    def get_calculator(self, symbol: str) -> Optional[BaseIndicatorCalculator]:
        return self._calculators.get(symbol.strip().upper())

    def get_consumed_count(self) -> int:
        return self._consumed_count

    def get_last_tick_time(self, symbol: str) -> Optional[datetime]:
        return self._last_tick_times.get(symbol.strip().upper())

    async def start(self, symbols: Optional[List[str]] = None) -> None:
        """
        Subscribes to symbols in TickBuffer and spawns per-symbol consumer tasks.
        """
        async with self._lock:
            if self._is_running:
                return
            self._is_running = True
            self._is_paused = False

            target_symbols = symbols or []
            for sym in target_symbols:
                s_up = sym.strip().upper()
                if s_up not in self._queues:
                    q = self.buffer.subscribe(s_up)
                    self._queues[s_up] = q
                    if s_up not in self._calculators:
                        self._calculators[s_up] = DefaultIndicatorCalculator(s_up)
                    task = asyncio.create_task(
                        self._consume_loop(s_up, q),
                        name=f"tick_consumer_{s_up}",
                    )
                    self._worker_tasks[s_up] = task
                    logger.info("[TickConsumer] Subscribed and started worker for %s.", s_up)

    async def stop(self) -> None:
        """
        Cancels consumer workers and unsubscribes queues from TickBuffer.
        """
        async with self._lock:
            self._is_running = False
            self._is_paused = False

            for sym, task in list(self._worker_tasks.items()):
                if not task.done():
                    task.cancel()
            for sym, q in list(self._queues.items()):
                self.buffer.unsubscribe(sym, q)

            self._worker_tasks.clear()
            self._queues.clear()
            logger.info("[TickConsumer] Stopped all symbol workers and cleaned up subscriptions.")

    def pause(self) -> None:
        self._is_paused = True
        logger.info("[TickConsumer] Paused processing.")

    def resume(self) -> None:
        self._is_paused = False
        logger.info("[TickConsumer] Resumed processing.")

    async def _consume_loop(self, symbol: str, queue: asyncio.Queue) -> None:
        """
        Continuous worker loop pulling ticks from the buffer queue.
        """
        try:
            while self._is_running:
                tick: Tick = await queue.get()
                try:
                    if not self._is_paused:
                        self._process_single_tick(symbol, tick)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[TickConsumer] Fatal error in worker loop for %s: %s", symbol, exc, exc_info=True)

    def _process_single_tick(self, symbol: str, tick: Tick) -> None:
        """
        Validates timestamp, market session, feed freshness, and computes indicators.
        """
        ts = tick.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)

        # 1. Market session check
        if not self.market_session.is_market_open(Exchange.NSE, ts):
            logger.debug("[%s] Received tick outside market session at %s. Skipping.", symbol, ts.isoformat())
            return

        # 2. Update stale feed detector & last tick time
        self.stale_detector.update(symbol, ts)
        self._last_tick_times[symbol] = ts
        self._consumed_count += 1

        # 3. Process indicators
        calc = self._calculators.get(symbol)
        if calc:
            calc.process_tick(tick)

            # 4. Evaluate strategies with updated indicators
            try:
                from strategies.strategy_orchestrator import strategy_orchestrator
                indicators = dict(calc.indicator_values)
                indicators.update({
                    "symbol": symbol,
                    "ltp": tick.ltp,
                    "price": tick.ltp,
                    "timestamp": ts,
                })
                # Route each instrument type to the asset class strategies that
                # should evaluate it.  Index spots feed both futures and options
                # strategies; equity stocks feed all three stock strategies; options
                # and futures contracts route to their specific class only.
                # detect_instrument_type() (tick_normalizer.py) returns "OPT" for
                # all options — never "CE"/"PE" — so we check for "OPT" here and
                # disambiguate by underlying symbol.
                _itype = (tick.instrument_type or "").upper()
                # NSE indices: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50
                # BSE index: SENSEX — exchange routing differs but underlying classification is the same
                _INDEX_UNDERLYINGS = frozenset(
                    ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "NIFTYNXT50")
                )

                if _itype in ("IDX_FUT", "FUTIDX"):
                    # Index spot/futures → index futures AND index options both need this
                    _target_classes = ["INDEX_FUTURES", "INDEX_OPTIONS"]
                elif _itype in ("FUT", "FUTSTK"):
                    _target_classes = ["STOCK_FUTURES"]
                elif _itype in ("IDX_OPT", "OPTIDX"):
                    _target_classes = ["INDEX_OPTIONS"]
                elif _itype == "OPT":
                    # tick_normalizer returns "OPT" for all options contracts;
                    # disambiguate by checking if the underlying is a known index.
                    _is_idx_opt = any(symbol.upper().startswith(idx) for idx in _INDEX_UNDERLYINGS)
                    _target_classes = ["INDEX_OPTIONS"] if _is_idx_opt else ["STOCK_OPTIONS"]
                elif _itype in ("CE", "PE"):
                    # Broker-native CE/PE strings (some brokers skip normalizer path)
                    if symbol.upper() in _INDEX_UNDERLYINGS:
                        _target_classes = ["INDEX_OPTIONS"]
                    else:
                        _target_classes = ["STOCK_OPTIONS"]
                elif _itype == "EQ":
                    # Equity cash → all three stock strategies; index strategies
                    # must NOT evaluate equity symbols
                    _target_classes = ["STOCK_CASH", "STOCK_FUTURES", "STOCK_OPTIONS"]
                else:
                    # Unknown type — fall back to evaluating everything
                    _target_classes = [None]

                _locked = _current_locked_class()
                if _locked is not None:
                    # evaluate_all ignores the asset_class routing filter once a lock
                    # is set, so narrow here: evaluate only the locked class, and only
                    # when this instrument type routes to it (an equity tick must never
                    # drive the index-options strategy).
                    if None in _target_classes or _locked in _target_classes:
                        _target_classes = [_locked]
                    else:
                        logger.debug(
                            "[TickConsumer] %s itype=%s skipped — locked class %s not in %s",
                            symbol, _itype, _locked, _target_classes,
                        )
                        _target_classes = []

                for _ac in _target_classes:
                    logger.debug(
                        "[TickConsumer] %s itype=%s → asset_class=%s locked=%s",
                        symbol, _itype, _ac if _ac else "ALL", _locked,
                    )
                    strategy_orchestrator.evaluate_all(
                        indicators,
                        {
                            "symbol": symbol,
                            "timestamp": ts,
                            "instrument_type": _itype,
                            "asset_class": _ac,
                        },
                        locked_class=_locked,
                    )
            except Exception as _exc:
                logger.critical(
                    "[PIPELINE TRACE] SILENT DROP — strategy eval exception for %s: %s",
                    symbol, _exc, exc_info=True,
                )
