import React, { useState, useCallback } from 'react';
import useSignals, { ASSET_CLASSES, ASSET_CLASS_LABELS } from '../hooks/useSignals';
import useLTP from '../hooks/useLTP';
import apiClient from '../api/client';
import { useCanTrade } from '../store/authStore';
import { useSharedExecutionMode } from '../hooks/useExecutionMode';
import { useSystemState } from '../context/SystemStateContext';
import { REQ } from '../utils/requestState';
import { activeClassExecutionText, executionModeView, requestStatusText } from '../utils/stateLabels';
import { triggerToast } from './ToastContainer';

// Per-class accent so the groups stay distinguishable at a glance.
const CLASS_ACCENT = {
  AUTO_OPTIONS:        { bar: 'bg-sky-500',     text: 'text-sky-300',     ring: 'border-sky-800/60' },
  AUTO_FUTURES:        { bar: 'bg-indigo-500',  text: 'text-indigo-300',  ring: 'border-indigo-800/60' },
  AUTO_STOCK_FUTURES:  { bar: 'bg-violet-500',  text: 'text-violet-300',  ring: 'border-violet-800/60' },
  AUTO_STOCK_OPTIONS:  { bar: 'bg-fuchsia-500', text: 'text-fuchsia-300', ring: 'border-fuchsia-800/60' },
  AUTO_CASH_INTRADAY:  { bar: 'bg-teal-500',    text: 'text-teal-300',    ring: 'border-teal-800/60' },
  AUTO_CASH_SWING:     { bar: 'bg-cyan-500',    text: 'text-cyan-300',    ring: 'border-cyan-800/60' },
  AUTO_CASH_DELIVERY:  { bar: 'bg-emerald-500', text: 'text-emerald-300', ring: 'border-emerald-800/60' },
};

const money = (v, digits = 1) =>
  v == null ? '—' : `₹${Number(v).toLocaleString('en-IN', { maximumFractionDigits: digits })}`;

const SignalCard = ({ signal, ltpMap, pending, onOrder, canTrade }) => {
  const executing = signal.status === 'EXECUTING';
  const isLong = signal.direction === 'LONG';
  const isAPlus = signal.tier === 'A+';
  const sym = signal.symbol;
  const ltp = sym ? (ltpMap[sym.toUpperCase()] ?? null) : null;
  const ltpVsEntry = ltp != null && signal.entry_price != null && signal.entry_price !== 0
    ? ((ltp - signal.entry_price) / signal.entry_price) * 100
    : null;
  const ltpColor = ltp == null ? 'text-slate-500'
    : ltpVsEntry == null ? 'text-slate-200'
    : ltpVsEntry >= 0 ? 'text-emerald-400' : 'text-rose-400';
  const canOrder = Boolean(sym) && canTrade;

  return (
    <div
      className={`relative bg-[#11151F] p-3 rounded-lg border border-slate-800 transition-all duration-200 hover:border-slate-700 shadow-md border-l-4 ${
        executing ? 'border-l-emerald-500 bg-emerald-950/10' : 'border-l-slate-600 bg-slate-900/40'
      }`}
    >
      <div className="flex items-center justify-between gap-2 mb-2">
        {executing ? (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-ping" />
            EXECUTING
          </span>
        ) : (
          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700/40 text-slate-400 border border-slate-600/40">
            ALERT ONLY
          </span>
        )}
        {canTrade ? (
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => onOrder(signal, 'BUY')}
            disabled={!canOrder || !!pending[`${signal.signal_id}_BUY`]}
            className="px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/40 hover:bg-emerald-500/35 active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
          >
            {pending[`${signal.signal_id}_BUY`] ? '…' : 'BUY'}
          </button>
          {!String(signal.asset_class).includes('OPTION') && (
            <button
              onClick={() => onOrder(signal, 'SELL')}
              disabled={!canOrder || !!pending[`${signal.signal_id}_SELL`]}
              className="px-2 py-0.5 rounded text-[11px] font-bold bg-rose-500/20 text-rose-400 border border-rose-500/40 hover:bg-rose-500/35 active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
            >
              {pending[`${signal.signal_id}_SELL`] ? '…' : 'SELL'}
            </button>
          )}
        </div>
        ) : (
          <span className="text-[10px] font-mono text-slate-500 shrink-0">READ-ONLY</span>
        )}
      </div>

      <div className="flex items-center gap-2 mb-2.5 flex-wrap">
        {signal.direction && (
          <span className={`text-xs font-black px-1.5 py-0.5 rounded shrink-0 ${
            isLong
              ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
              : 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
          }`}>
            {signal.direction}
          </span>
        )}
        <span className="text-sm font-bold text-slate-100 font-mono tracking-tight truncate flex-1 min-w-0">
          {sym ?? '—'}
        </span>
        {ltp != null && (
          <span className={`ml-auto shrink-0 flex items-center gap-1 text-[11px] font-mono font-bold ${ltpColor}`}>
            <span className="text-[9px] text-slate-500">LTP</span>
            {money(ltp, 2)}
            {ltpVsEntry != null && (
              <span className="text-[9px]">
                {ltpVsEntry >= 0 ? '▲' : '▼'}{Math.abs(ltpVsEntry).toFixed(1)}%
              </span>
            )}
          </span>
        )}
      </div>

      <div className="grid grid-cols-3 gap-1 text-[10px] font-mono">
        {[
          { label: 'Entry',  value: signal.entry_price,     color: 'text-slate-200' },
          { label: 'Target', value: signal.target_price,    color: 'text-emerald-400' },
          { label: 'SL',     value: signal.stop_loss_price, color: 'text-rose-400' },
        ].map(({ label, value, color }) => (
          <div key={label} className="bg-slate-950/60 rounded px-1.5 py-1 text-center border border-slate-800/60">
            <div className="text-slate-500 text-[9px] uppercase tracking-wider mb-0.5">{label}</div>
            <div className={`font-bold ${color}`}>{money(value)}</div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between pt-2 border-t border-slate-800/80 text-xs mt-2">
        <div className="text-slate-400 font-mono">
          Score: <span className="font-bold text-slate-200">
            {signal.score == null ? '—' : Number(signal.score).toFixed(1)}
          </span>
        </div>
        {signal.tier && (
          <span className={`font-mono font-bold px-1.5 py-0.5 rounded text-xs ${
            isAPlus
              ? 'text-emerald-400 bg-emerald-950/40 border border-emerald-800/60'
              : 'text-amber-400 bg-amber-950/40 border border-amber-800/60'
          }`}>
            {signal.tier}
          </span>
        )}
      </div>
    </div>
  );
};

export const AlertMatrix = ({ activeMode = 'AUTO_OPTIONS', forceReadOnly = false }) => {
  const { grouped, loading, backendOnline } = useSignals(activeMode);
  const canTrade = useCanTrade() && !forceReadOnly;
  const execModeState = useSharedExecutionMode();
  const execView = executionModeView(execModeState);
  // Class lock confirmation from the shared /trading/mode poll (operator dashboard).
  // The guest page has no shared state and passes no active class.
  const classLock = useSystemState()?.classLock;
  const lockConfirmed = !classLock || classLock.status === REQ.OK;
  const activeBadge = lockConfirmed
    ? `ACTIVE · ${activeClassExecutionText(execModeState)}`
    : `ACTIVE · UNCONFIRMED (${requestStatusText(classLock.status)})`;
  const ltpMap = useLTP();
  const [pending, setPending] = useState({});

  const placeManualOrder = useCallback(async (signal, side) => {
    if (!canTrade) return;   // the API rejects this too; this avoids a pointless round-trip
    const key = `${signal.signal_id}_${side}`;
    setPending((prev) => ({ ...prev, [key]: true }));
    try {
      const { data } = await apiClient.post('/api/v1/trading/orders', {
        symbol: signal.symbol,
        side,
        quantity: 1,
        price: signal.entry_price ?? null,
        is_manual: true,
        category: signal.asset_class,
        // Lets whichever broker is selected find this exact contract in its own
        // master, instead of trying to match on the contract name.
        underlying: signal.metadata?.underlying ?? null,
        selected_expiry: signal.metadata?.selected_expiry ?? null,
        selected_strike: signal.metadata?.selected_strike ?? null,
        option_type: signal.metadata?.option_type ?? null,
        dhan_security_id: signal.metadata?.security_id ?? null,
      });
      // Report what the broker actually returned rather than assuming a fill.
      const st = (data?.status || 'PLACED').toUpperCase();
      triggerToast({
        type: st === 'REJECTED' ? 'RISK_80' : 'ORDER_FILLED',
        title: `Order ${st}`,
        message: `${side} ${signal.symbol} • ${data?.broker ?? 'broker'} • id ${data?.order_id ?? '—'}`,
      });
    } catch (err) {
      const detail = err.response?.data?.message || err.response?.data?.detail || err.message || 'Order placement failed';
      triggerToast({
        type: /halt|frozen/i.test(detail) ? 'HALT' : 'RISK_80',
        title: 'Order Rejected',
        message: detail,
      });
    } finally {
      setPending((prev) => ({ ...prev, [key]: false }));
    }
  }, [canTrade]);

  const totalSignals = ASSET_CLASSES.reduce((n, c) => n + (grouped[c]?.length || 0), 0);

  return (
    <div className="w-full bg-[#0B0E14] text-slate-200 p-4 md:p-6 rounded-xl border border-slate-800/80 shadow-2xl">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between pb-5 mb-5 border-b border-slate-800 gap-2">
        <div>
          <h2 className="text-xl font-bold tracking-wide text-sky-400 flex items-center gap-2">
            <span>⚡</span> Alert Matrix — Top 2 × 7 Classes
          </h2>
          <p className="text-xs text-slate-400 mt-1">
            Blueprint O8 Execution Lock: the selected class is auto-executed in the server's execution mode; others are alert-only.
          </p>
        </div>
        <div className="flex items-center gap-2">
        {canTrade && (
          <span
            aria-label={execView.mode ? execView.aria : `Order mode unknown: ${execView.text.toLowerCase()}`}
            className={`text-[10px] font-mono font-black uppercase tracking-wider px-2 py-1 rounded border ${
              execView.mode === 'PAPER'
                ? 'bg-amber-500/15 text-amber-200 border-amber-500/50'
                : execView.mode === 'LIVE'
                  ? 'bg-rose-500/20 text-rose-200 border-rose-500/60'
                  : 'bg-slate-800 text-slate-300 border-slate-600'
            }`}
          >
            {execView.mode === 'PAPER' && '📄 PAPER — orders are simulated'}
            {execView.mode === 'LIVE' && '⚡ LIVE — orders use real money'}
            {!execView.mode && execView.text}
          </span>
        )}
        <div className="flex items-center gap-1.5 px-2.5 py-1 rounded bg-slate-900 border border-slate-800 text-xs">
          <span aria-hidden="true" className={`h-2 w-2 rounded-full ${backendOnline ? 'bg-emerald-400' : loading ? 'bg-slate-400' : 'bg-rose-500'}`} />
          <span className="text-slate-300 font-mono">
            {loading ? 'SIGNALS: LOADING…' : backendOnline ? `SIGNALS API OK · every 5s · ${totalSignals} signals` : 'SIGNALS: API UNREACHABLE'}
          </span>
        </div>
        </div>
      </div>

      {!loading && backendOnline && totalSignals === 0 && (
        <div className="py-10 text-center text-sm text-slate-500 font-mono">
          No live signals. Waiting for the strategies to qualify a setup.
        </div>
      )}

      <div className="space-y-5">
        {ASSET_CLASSES.map((cls) => {
          const rows = grouped[cls] || [];
          const accent = CLASS_ACCENT[cls] || CLASS_ACCENT.AUTO_OPTIONS;
          const isActive = cls === activeMode;

          return (
            <section
              key={cls}
              className={`rounded-lg border ${isActive ? accent.ring : 'border-slate-800/70'} ${
                isActive ? 'bg-slate-900/40' : 'bg-slate-900/10'
              } p-3`}
            >
              <header className="flex items-center gap-2 mb-3">
                <span className={`h-4 w-1 rounded-full ${accent.bar}`} />
                <h3 className={`text-sm font-bold tracking-wide ${accent.text}`}>
                  {ASSET_CLASS_LABELS[cls] ?? cls}
                </h3>
                <span className="text-[10px] font-mono text-slate-500">({rows.length})</span>
                {isActive && (
                  <span className={`ml-auto text-[10px] font-bold font-mono px-2 py-0.5 rounded border ${
                    !lockConfirmed || !execView.mode
                      ? 'bg-slate-800 text-slate-300 border-slate-600'
                      : execView.mode === 'LIVE'
                        ? 'bg-rose-500/20 text-rose-200 border-rose-500/60'
                        : 'bg-amber-500/15 text-amber-200 border-amber-500/50'
                  }`}>
                    {activeBadge}
                  </span>
                )}
              </header>

              {rows.length === 0 ? (
                <div className="text-[11px] text-slate-600 font-mono py-3 px-1">
                  No qualifying signal in this class.
                </div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {rows.map((signal, i) => (
                    <SignalCard
                      key={signal.signal_id ?? `${cls}-${i}`}
                      signal={signal}
                      ltpMap={ltpMap}
                      pending={pending}
                      onOrder={placeManualOrder}
                      canTrade={canTrade}
                    />
                  ))}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
};

export default AlertMatrix;
