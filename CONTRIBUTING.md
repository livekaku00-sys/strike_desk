# Contributing to Strike Desk V4

## Architecture Overview

Strike Desk V4 is a FastAPI backend + React frontend. There is no Streamlit component.

```
strike_desk/
├── api/            # FastAPI app and route handlers
├── adapters/       # Broker adapters (AngelOne, Dhan, Fyers, Paper)
├── brokers/        # Token manager and session lifecycle
├── config/         # Env var loading and TOTP generation
├── execution/      # Execution gate router and audit logger
├── models/         # Shared data models (Order, Tick, Signal, Instrument)
├── pipeline/       # WebSocket consumer, indicator calculators, orchestrator
├── risk/           # Risk engine, emergency halt, circuit breaker
├── services/       # Instrument discovery, symbol registry, pre-flight
├── strategies/     # Signal generators per asset class
├── utils/          # Shared utilities (aliases, rate limiter, tick normaliser)
├── tests/          # All tests (run with pytest from repo root)
└── frontend/       # React + Vite (npm run dev from frontend/)
```

## Adding a Broker

1. Create `adapters/<broker>_adapter.py` inheriting from `adapters/base_adapter.py`.
2. Implement `place_order`, `get_positions`, `get_order_status`, and the WebSocket feed handler.
3. Register the adapter in `api/main.py`'s DI container.
4. Add broker token handling in `brokers/token_manager.py`.
5. Write tests in `tests/test_<broker>_adapter.py` covering the live-feed gate mock pattern (see `tests/test_broker_isolation.py` for the pattern).

## Running Tests

```bash
# From repo root, with venv active:
python -m pytest tests/ -v
```

## Non-Negotiable Invariants

Do not modify or bypass these without explicit Decision Maker approval:

- `require_active_broker()` in `api/routes/trading.py` — broker isolation gate
- `_select_active_adapter()` in `api/routes/trading.py` — adapter DI
- `require_live_feed()` in `api/routes/trading.py` — live feed validation gate
- `VALID_TRANSITIONS` in `models/order.py` — order state machine
- `require_operator` dependency in FastAPI routes — RBAC gate
- The 401 vs 403 distinction in auth error handling

## Exchange Classification

The 5 core indices span two exchanges:

| Symbol | Exchange |
|--------|----------|
| NIFTY | NSE |
| BANKNIFTY | NSE |
| FINNIFTY | NSE |
| MIDCPNIFTY | NSE |
| SENSEX | **BSE** |

Adapters already route each symbol to the correct exchange. Do not assume SENSEX is an NSE instrument.
