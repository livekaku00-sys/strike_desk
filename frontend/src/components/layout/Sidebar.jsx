import React, { useState, useEffect, useCallback } from 'react';
import apiClient from '../../api/client';
import { triggerToast } from '../ToastContainer';
import StatusPill from '../StatusPill';
import { useSystemState } from '../../context/SystemStateContext';
import { REQ, statusFromAxiosError } from '../../utils/requestState';
import {
  activeClassExecutionText,
  brokerConnectionView,
  brokerDisplayName,
  executionModeView,
  formatClock,
  requestStatusText,
} from '../../utils/stateLabels';

export const BROKER_POLL_MS = 30000;

const EXEC_CLASSES = [
  { id: 'AUTO_OPTIONS',       label: 'Index Options',   sub: 'NIFTY · BANKNIFTY · SENSEX' },
  { id: 'AUTO_FUTURES',       label: 'Index Futures',   sub: 'NIFTY · BANKNIFTY futures'  },
  { id: 'AUTO_STOCK_FUTURES', label: 'Stock Futures',   sub: 'Individual equity FUT'       },
  { id: 'AUTO_STOCK_OPTIONS', label: 'Stock Options',   sub: 'Individual equity CE/PE'     },
  { id: 'AUTO_CASH_INTRADAY', label: 'Cash Intraday',   sub: 'Equity MIS'                  },
  { id: 'AUTO_CASH_SWING',    label: 'Cash Swing',      sub: '2-10 day holds'              },
  { id: 'AUTO_CASH_DELIVERY', label: 'Cash Delivery',   sub: 'CNC positions'               },
];

// ── Execution broker card ────────────────────────────────────────────────────
// The broker the server routes orders to (GET /brokers/active), its connection
// state (GET /brokers/tokens/status) and the server's execution mode. A connected
// broker says nothing about PAPER vs LIVE; that line comes from the mode alone.

export function ExecutionBrokerCard({ brokerId, broker, stale, execView, tokens, setTokens, submitting, onSubmit }) {
  const [panelOpen, setPanelOpen] = useState(false);

  const name = brokerDisplayName(brokerId);
  const conn = broker ? brokerConnectionView(broker.status) : { text: 'NO STATUS FOR THIS BROKER', tone: 'muted' };
  const connText = stale ? `${conn.text} (STALE)` : conn.text;
  const isManual = broker && (broker.auth_type === 'MANUAL_REAUTH' || broker.status === 'UNAUTHENTICATED');
  const expiry = broker?.expires_in || null;

  return (
    <div>
      <div className="bg-amber-500/8 border border-amber-500/30 rounded-lg p-3">
        {/* Row 1: broker + connection */}
        <div className="flex items-center justify-between gap-2 mb-2">
          <span className="text-sm font-bold text-white">{name}</span>
          <StatusPill
            className="text-[10px]"
            tone={stale ? 'muted' : conn.tone}
            text={connText}
            ariaLabel={`${name} broker: ${connText.toLowerCase()}`}
          />
        </div>

        {/* Row 2: execution mode (server state, independent of connectivity) */}
        <div className="mb-2" aria-label={execView.aria}>
          <span className={`text-[10px] font-mono font-bold ${
            execView.mode === 'LIVE' ? 'text-rose-300' : execView.mode === 'PAPER' ? 'text-amber-200' : 'text-slate-300'
          }`}>
            {execView.mode === 'PAPER' && 'PAPER — orders simulated, no broker order API is called'}
            {execView.mode === 'LIVE' && `LIVE — orders are sent to ${name}`}
            {!execView.mode && execView.text}
          </span>
        </div>

        {/* Row 3: auth type + expiry */}
        {broker && (
          <div className="flex items-center justify-between text-[10px] mb-2.5">
            {broker.auth_type === 'AUTO_REFRESH' ? (
              <span className="text-emerald-300 font-mono font-semibold">Auto-TOTP · No token needed</span>
            ) : (
              <span className="text-amber-300 font-mono">Manual token</span>
            )}
            {expiry && (
              <span className="font-mono text-slate-300" title="Server's token-lifetime estimate">
                {expiry}
              </span>
            )}
          </div>
        )}

        {/* Re-auth button (manual brokers only) */}
        {isManual && (
          <button
            onClick={() => setPanelOpen((v) => !v)}
            aria-expanded={panelOpen}
            className={`w-full text-[11px] font-bold py-1.5 rounded border transition-all ${
              panelOpen
                ? 'bg-amber-500/25 text-amber-300 border-amber-500/50'
                : 'bg-slate-800 text-amber-300 border-slate-700 hover:border-amber-500/50 hover:bg-amber-500/10'
            }`}
          >
            {panelOpen ? 'Cancel' : '↑ Re-auth Token'}
          </button>
        )}
      </div>

      {/* Re-auth input panel */}
      {isManual && panelOpen && (
        <div className="mt-1.5 bg-slate-900/80 border border-amber-500/20 rounded-lg p-3">
          <p className="text-[10px] text-slate-300 mb-2">
            {brokerId === 'dhan'
              ? 'api.dhan.co → Access Token'
              : 'myapi.fyers.in → Access Token'}
          </p>
          <div className="flex gap-1.5">
            <input
              type="password"
              aria-label={`${name} access token`}
              placeholder="Paste token…"
              value={tokens[brokerId] || ''}
              onChange={(e) => setTokens((p) => ({ ...p, [brokerId]: e.target.value }))}
              className="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1.5 text-[11px] text-slate-100 font-mono placeholder-slate-500 focus:outline-none focus:border-amber-500"
            />
            <button
              onClick={() => { onSubmit(brokerId); setPanelOpen(false); }}
              disabled={!tokens[brokerId]?.trim() || submitting[brokerId]}
              className="px-2.5 py-1.5 bg-amber-500/20 text-amber-300 border border-amber-500/40 rounded text-[11px] font-bold hover:bg-amber-500/30 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
            >
              {submitting[brokerId] ? '…' : 'Submit'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Data feed row ─────────────────────────────────────────────────────────────
// Connectivity only. A connected feed does not imply live execution.

export function DataFeedRow({ broker, stale }) {
  const name = brokerDisplayName(broker.broker_name);
  const conn = brokerConnectionView(broker.status);
  const text = stale ? `${conn.text} (STALE)` : conn.text;
  return (
    <div className="flex items-center justify-between gap-2 px-2 py-1.5 rounded">
      <span className="text-[11px] font-medium text-slate-200">{name}</span>
      <StatusPill
        className="text-[9px]"
        tone={stale ? 'muted' : conn.tone}
        text={text}
        ariaLabel={`${name} data feed: ${text.toLowerCase()}`}
      />
    </div>
  );
}

// ── Main Sidebar ──────────────────────────────────────────────────────────────

export default function Sidebar({ activeMode, onModeChange }) {
  const system = useSystemState();
  const execMode = system?.execMode;
  const classLock = system?.classLock;
  const execView = executionModeView(execMode);

  // Token/feed status (kept on failure, marked STALE) and the active routing
  // broker (never kept on failure: a wrong broker is worse than "unknown").
  const [brokersState, setBrokersState] = useState({ status: REQ.LOADING, list: [], updatedAt: null });
  const [activeBrokerState, setActiveBrokerState] = useState({ status: REQ.LOADING, value: null });
  const [tokens, setTokens]         = useState({});
  const [submitting, setSubmitting] = useState({});

  const fetchBrokers = useCallback(async () => {
    const [rStatus, rActive] = await Promise.allSettled([
      apiClient.get('/api/v1/brokers/tokens/status'),
      apiClient.get('/api/v1/brokers/active'),
    ]);
    const now = Date.now();
    if (rStatus.status === 'fulfilled') {
      setBrokersState({ status: REQ.OK, list: rStatus.value.data?.brokers || [], updatedAt: now });
    } else {
      setBrokersState((prev) => ({ ...prev, status: statusFromAxiosError(rStatus.reason) }));
    }
    if (rActive.status === 'fulfilled' && rActive.value.data?.active_broker) {
      setActiveBrokerState({ status: REQ.OK, value: rActive.value.data.active_broker });
    } else {
      setActiveBrokerState({
        status: rActive.status === 'fulfilled' ? REQ.API_ERROR : statusFromAxiosError(rActive.reason),
        value: null,
      });
    }
  }, []);

  useEffect(() => {
    fetchBrokers();
    const t = setInterval(fetchBrokers, BROKER_POLL_MS);
    return () => clearInterval(t);
  }, [fetchBrokers]);

  const submitReauth = async (brokerName) => {
    const token = (tokens[brokerName] || '').trim();
    if (!token) return;
    setSubmitting((p) => ({ ...p, [brokerName]: true }));
    try {
      await apiClient.post(`/api/v1/brokers/${brokerName}/reauth`, { access_token: token });
      triggerToast({ type: 'ORDER_FILLED', title: 'Auth Success', message: `${brokerName} token accepted` });
      setTokens((p) => ({ ...p, [brokerName]: '' }));
      fetchBrokers();
    } catch (err) {
      const msg = err.response?.data?.message || err.response?.data?.detail || err.message || 'Auth failed';
      triggerToast({ type: 'HALT', title: 'Auth Failed', message: msg });
    } finally {
      setSubmitting((p) => ({ ...p, [brokerName]: false }));
    }
  };

  const activeBroker = activeBrokerState.status === REQ.OK ? activeBrokerState.value : null;
  const brokersStale = brokersState.status !== REQ.OK && brokersState.updatedAt != null;
  const execBroker = activeBroker ? brokersState.list.find((b) => b.broker_name === activeBroker) : null;
  const feedBrokers = brokersState.list.filter((b) => b.broker_name !== activeBroker);

  const lockConfirmed = classLock?.status === REQ.OK;
  const activeClassText = activeClassExecutionText(execMode);

  return (
    <aside className="w-64 flex-shrink-0 bg-slate-950 border-r border-slate-800 flex flex-col overflow-hidden">

      {/* Brand */}
      <div className="p-5 border-b border-slate-800 flex-shrink-0">
        <div className="flex items-center gap-2.5">
          <span className="text-xl" aria-hidden="true">⚡</span>
          <div>
            <h1 className="text-sm font-bold text-white tracking-tight leading-tight">Strike Desk</h1>
            <p className="text-[10px] font-mono text-amber-400 font-semibold tracking-wider">V4.0 · PRODUCTION</p>
          </div>
        </div>
      </div>

      {/* ── Broker Status ──────────────────────────────────────────── */}
      <div className="p-4 border-b border-slate-800 flex-shrink-0">

        {/* Execution Broker — primary section */}
        <p className="text-[10px] font-bold text-slate-300 uppercase tracking-wider mb-2.5">
          Execution Broker
        </p>

        {activeBrokerState.status !== REQ.OK ? (
          <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
            <StatusPill
              className="text-[10px]"
              tone={activeBrokerState.status === REQ.LOADING ? 'muted' : 'error'}
              text={`ACTIVE BROKER: ${requestStatusText(activeBrokerState.status)}`}
            />
          </div>
        ) : (
          <ExecutionBrokerCard
            brokerId={activeBroker}
            broker={execBroker}
            stale={brokersStale}
            execView={execView}
            tokens={tokens}
            setTokens={setTokens}
            submitting={submitting}
            onSubmit={submitReauth}
          />
        )}

        {/* Data Feeds — secondary section */}
        <div className="mt-4">
          <div className="flex items-center justify-between mb-1.5">
            <p className="text-[9px] font-bold text-slate-300 uppercase tracking-wider">Data Feeds</p>
            {brokersState.status !== REQ.OK && (
              <span className="text-[9px] font-mono text-rose-300">
                {brokersStale
                  ? `STALE · ${requestStatusText(brokersState.status)} · last ${formatClock(brokersState.updatedAt)}`
                  : requestStatusText(brokersState.status)}
              </span>
            )}
          </div>
          {feedBrokers.length > 0 && (
            <div className="space-y-0.5">
              {feedBrokers.map((b) => (
                <DataFeedRow key={b.broker_name} broker={b} stale={brokersStale} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── Execution Class Lock ────────────────────────────────────── */}
      <div className="p-4 border-b border-slate-800 flex-1 overflow-y-auto">
        <p className="text-[10px] font-bold text-slate-300 uppercase tracking-wider mb-2.5">
          Execution Class Lock
        </p>
        <div className="space-y-1.5">
          {EXEC_CLASSES.map((cls) => {
            const isActive = activeMode === cls.id;
            const badge = lockConfirmed
              ? `ACTIVE · ${activeClassText}`
              : `ACTIVE · UNCONFIRMED (${requestStatusText(classLock?.status)})`;
            return (
              <button
                key={cls.id}
                onClick={() => onModeChange(cls.id)}
                aria-pressed={isActive}
                aria-label={isActive ? `${cls.label}: ${badge.toLowerCase()}` : `${cls.label}: alert only`}
                className={`w-full text-left px-3 py-2.5 rounded-lg border transition-all duration-150 ${
                  isActive
                    ? 'bg-amber-500/10 border-amber-500/40 text-white'
                    : 'bg-slate-900 border-slate-800/80 text-slate-300 hover:border-slate-700 hover:text-slate-100'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold">{cls.label}</span>
                </div>
                <p className="text-[10px] text-slate-400 mt-0.5">{cls.sub}</p>
                {isActive && (
                  <span className={`mt-1 inline-block text-[9px] px-1.5 py-0.5 rounded font-mono font-bold border ${
                    !lockConfirmed || !execView.mode
                      ? 'bg-slate-800 text-slate-300 border-slate-600'
                      : execView.mode === 'LIVE'
                        ? 'bg-rose-500/20 text-rose-200 border-rose-500/60'
                        : 'bg-amber-500/20 text-amber-200 border-amber-500/50'
                  }`}>
                    {badge}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>

    </aside>
  );
}
