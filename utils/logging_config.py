import contextlib
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "broker": getattr(record, "broker", "system"),
            "operation": getattr(record, "operation", record.funcName),
            "duration": getattr(record, "duration", None),
            "success": getattr(record, "success", None),
            "error_code": getattr(record, "error_code", None),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


class BrokerFilter(logging.Filter):
    def __init__(self, target_broker: str) -> None:
        super().__init__()
        self.target_broker = target_broker.lower()

    def filter(self, record: logging.LogRecord) -> bool:
        record_broker = getattr(record, "broker", "").lower()
        if not record_broker:
            name_parts = record.name.lower().split(".")
            if self.target_broker in name_parts:
                record_broker = self.target_broker
        return record_broker == self.target_broker


def setup_logging(
    log_dir: Optional[str] = None,
    default_level: int = logging.INFO,
) -> None:
    if log_dir is None:
        candidates = ["/root/strike_desk/logs", "logs", "/tmp/strike_desk_logs"]
        for cand in candidates:
            try:
                os.makedirs(cand, exist_ok=True)
                log_dir = cand
                break
            except (PermissionError, OSError):
                continue
        if log_dir is None:
            log_dir = "/tmp/strike_desk_logs"
            os.makedirs(log_dir, exist_ok=True)
    else:
        os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(default_level)
    formatter = JSONFormatter()

    root_logger.handlers.clear()

    # Main combined log
    main_file = os.path.join(log_dir, "strike_desk.log")
    main_handler = logging.FileHandler(main_file, encoding="utf-8")
    main_handler.setFormatter(formatter)
    main_handler.setLevel(default_level)
    root_logger.addHandler(main_handler)

    # Per-broker logs
    for broker_name in ("dhan", "angelone", "fyers"):
        b_file = os.path.join(log_dir, f"{broker_name}.log")
        b_handler = logging.FileHandler(b_file, encoding="utf-8")
        b_handler.setFormatter(formatter)
        b_handler.setLevel(default_level)
        b_handler.addFilter(BrokerFilter(broker_name))
        root_logger.addHandler(b_handler)

    # Console log
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(default_level)
    root_logger.addHandler(console_handler)


def get_structured_logger(name: str, broker: Optional[str] = None) -> logging.LoggerAdapter:
    base_logger = logging.getLogger(name)
    extra = {"broker": broker or "system", "operation": "general"}
    return logging.LoggerAdapter(base_logger, extra)


@contextlib.asynccontextmanager
async def log_operation_async(logger: Any, operation: str, broker: str = ""):
    start = time.monotonic()
    try:
        yield
        duration = round((time.monotonic() - start) * 1000, 2)
        logger.info(
            f"{operation} succeeded in {duration}ms",
            extra={
                "broker": broker,
                "operation": operation,
                "duration": duration,
                "success": True,
                "error_code": None,
            },
        )
    except Exception as exc:
        duration = round((time.monotonic() - start) * 1000, 2)
        err_code = getattr(exc, "error_code", type(exc).__name__)
        logger.error(
            f"{operation} failed after {duration}ms: {exc}",
            extra={
                "broker": broker,
                "operation": operation,
                "duration": duration,
                "success": False,
                "error_code": str(err_code),
            },
            exc_info=True,
        )
        raise