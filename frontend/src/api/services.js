/**
 * Strike Desk V4.5 — Centralized API Service Layer
 * All backend calls go through here. No fetch() scatter in components.
 * Endpoints match exactly what the FastAPI backend exposes.
 */
import apiClient from './client';

// ── Auth ─────────────────────────────────────────────────────────────────────
export const auth = {
  login:       (body)     => apiClient.post('/api/v1/auth/login', body),
  guest:       ()         => apiClient.post('/api/v1/auth/guest'),
  me:          ()         => apiClient.get('/api/v1/auth/me'),
  logout:      ()         => apiClient.post('/api/v1/auth/logout'),
  tokenStatus: ()         => apiClient.get('/api/v1/auth/token-status'),
};

// ── Brokers ───────────────────────────────────────────────────────────────────
export const brokers = {
  all:           ()                => apiClient.get('/api/v1/brokers'),
  active:        ()                => apiClient.get('/api/v1/brokers/active'),
  setActive:     (broker)          => apiClient.post('/api/v1/brokers/active', { broker }),
  tokensStatus:  ()                => apiClient.get('/api/v1/brokers/tokens/status'),
  health:        ()                => apiClient.get('/api/v1/brokers/health'),
  reauthDhan:    (access_token)    => apiClient.post('/api/v1/brokers/dhan/reauth', { access_token }),
  reauthFyers:   (access_token)    => apiClient.post('/api/v1/brokers/fyers/reauth', { access_token }),
  refreshAngel:  ()                => apiClient.post('/api/v1/brokers/angelone/refresh'),
};

// ── Market Data ───────────────────────────────────────────────────────────────
export const market = {
  ticks:          ()        => apiClient.get('/api/v1/market/ticks'),
  tick:           (symbol)  => apiClient.get(`/api/v1/market/ticks/${symbol}`),
  indicators:     (symbol)  => apiClient.get(`/api/v1/market/indicators/${symbol}`),
  optionChain:    (under, expiry) => apiClient.get(`/api/v1/market/option-chain/${under}`, expiry ? { params: { expiry } } : {}),
  instruments:    ()        => apiClient.get('/api/v1/market/instruments'),
  instrument:     (symbol)  => apiClient.get(`/api/v1/market/instruments/${symbol}`),
  // Pipeline telemetry — monitored_symbols, stale_symbols, pcr_stale_symbols, consumed_ticks, is_running
  pipelineStatus: ()        => apiClient.get('/pipeline/status'),
};

// ── Strategy ──────────────────────────────────────────────────────────────────
export const strategy = {
  mode:       ()          => apiClient.get('/api/v1/trading/mode'),
  setMode:    (mode)      => apiClient.post('/api/v1/trading/mode', { mode }),
  signals:    ()          => apiClient.get('/api/v1/trading/signals'),
  watchlist:  ()          => apiClient.get('/api/v1/trading/watchlist'),
  refreshWL:  ()          => apiClient.post('/api/v1/trading/watchlist/refresh'),
  queue:      ()          => apiClient.get('/api/v1/trading/queue'),
};

// ── Risk ──────────────────────────────────────────────────────────────────────
export const risk = {
  state:               ()       => apiClient.get('/api/v1/risk/state'),
  auditLog:            (params) => apiClient.get('/api/v1/risk/audit-log', { params }),
  halt:                (body)   => apiClient.post('/api/v1/risk/halt', body),
  stopEntry:           (body)   => apiClient.post('/api/v1/risk/stop-entry', body),
  // Halt reset is a two-step flow: request-token → reset
  requestAckToken:     (body)   => apiClient.post('/api/v1/risk/acknowledge/request-token', body),
  resetHalt:           (body)   => apiClient.post('/api/v1/risk/acknowledge/reset', body),
  // Flatten All is a two-step flow: request-token → confirm
  requestFlattenToken: (body)   => apiClient.post('/api/v1/risk/flatten-all/request-token', body),
  flattenAll:          (body)   => apiClient.post('/api/v1/risk/flatten-all/confirm', body),
  reconcile:           ()       => apiClient.post('/api/v1/risk/reconcile'),
  operatorStatus:      ()       => apiClient.get('/api/v1/risk/operator-status'),
};

// ── Execution ─────────────────────────────────────────────────────────────────
export const execution = {
  mode:         ()          => apiClient.get('/api/v1/trading/execution-mode'),
  setMode:      (mode)      => apiClient.post('/api/v1/trading/execution-mode', { mode }),
  placeOrder:   (body)      => apiClient.post('/api/v1/trading/orders', body),
  orders:       ()          => apiClient.get('/api/v1/trading/orders'),
  positions:    ()          => apiClient.get('/api/v1/trading/positions'),
  squareOff:    (symbol)    => apiClient.post(`/api/v1/trading/positions/${symbol}/square-off`),
  paperAccount: ()          => apiClient.get('/api/v1/trading/paper/account'),
};

// ── Trades ────────────────────────────────────────────────────────────────────
export const trades = {
  list:    (params) => apiClient.get('/api/v1/trading/trades', { params }),
  summary: ()       => apiClient.get('/api/v1/trading/trades/summary'),
};

// ── System ────────────────────────────────────────────────────────────────────
export const system = {
  health:   () => apiClient.get('/api/v1/system/health'),
  events:   (params) => apiClient.get('/api/v1/system/events', { params }),
  config:   () => apiClient.get('/api/v1/system/config'),
};

// ── Preflight ─────────────────────────────────────────────────────────────────
export const preflight = {
  status: () => apiClient.get('/api/v1/preflight/status'),
  run:    () => apiClient.post('/api/v1/preflight/run'),
};
