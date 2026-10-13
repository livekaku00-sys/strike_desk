# Strike Desk V4 — Project Context for Claude Code

## What this project is
Production-grade algorithmic trading platform for Indian markets.
Multi-broker (Dhan, AngelOne, Fyers), multi-asset (EQ/OPT/FUT).

## Current Status
Phase 1 COMPLETE — All 7 prompts done, verified locally on Windows.
- 285 unit tests passing
- 30/30 Phase 1 verification checks passing (test_brokers.py --mock)
- Python 3.12, asyncio, FastAPI (port 8000), Streamlit (port 8501), SQLite

## Project Structure (36 Python files)
strike_desk/
├── config/settings.py              # Env var loader (8 credentials)
├── adapters/
│   ├── base_adapter.py             # Abstract base (all brokers inherit)
│   ├── dhan_adapter.py             # Dhan: auth, WS, ticks, OC, orders, balance
│   ├── angelone_adapter.py         # AngelOne: same
│   ├── fyers_adapter.py            # Fyers: TOTP auth, same
│   ├── auth_manager.py             # Token lifecycle, retry backoff
│   ├── websocket_handler.py        # State machine, reconnect, heartbeat
│   ├── option_chain_fetcher.py     # Unified OC fetcher (3 broker parsers)
│   └── order_manager.py            # Order lifecycle, idempotency, reconcile
├── models/
│   ├── tick.py                     # NormalizedTick common schema
│   ├── option_chain.py             # OptionChain, StrikeData, StrikeLeg
├── models/order.py                 # UnifiedOrder, 9-state lifecycle
│   └── account.py                  # AccountBalance, Position
├── pipeline/tick_buffer.py         # Async dedup + OOO + multi-consumer
├── utils/
│   ├── token_store.py              # In-memory token storage
│   ├── tick_normalizer.py          # Dhan/AngelOne/Fyers → NormalizedTick
│   ├── pcr_calculator.py           # PCR chain/per-strike/slope
│   ├── cache_manager.py            # 60s TTL cache for option chains
│   ├── order_id_generator.py       # UUID+timestamp+checksum, 5-min dedup
│   ├── connection_monitor.py       # WS health monitor
│   ├── exceptions.py               # StrikeDeskError hierarchy
│   ├── logging_config.py           # JSON structured logging
│   └── rate_limiter.py             # Token bucket, 429 backoff
└── tests/
    ├── test_auth.py                # 18 tests
    ├── test_websocket.py           # 40 tests
    ├── test_tick_normalization.py  # 52 tests
    ├── test_option_chain.py        # 83 tests
    ├── test_order_lifecycle.py     # 92 tests
    └── test_brokers.py             # Phase 1 verification gate (30 checks)

## Immutable Rules (from blueprint)
- Zero mock/synthetic data — real broker data only
- No hardcoded symbols — dynamic discovery only
- No dashboard code until Phase 6 FastAPI is verified
- Paper Mode = real WebSocket + virtual execution
- All phases headless-verified before frontend

## Environment Variables Required
DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN
ANGEL_CLIENT_ID, ANGEL_API_KEY, ANGEL_ACCESS_TOKEN
FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_TOTP_SECRET

## Next Phase
Phase 2: Dynamic Instrument Discovery
- Deliverable: test_instruments.py
- Fetches Nifty 100/50/Pharma/FMCG/BSE constituents live
- Zero hardcoded symbols
- Verified by: python test_instruments.py (prints all discovered symbols)

## Development Roles
- Coder: Gemini Spark (generates code)
- Advisor: Qwen (verifies against blueprint)
- Decision Maker: Monuj (authorizes deployment)
