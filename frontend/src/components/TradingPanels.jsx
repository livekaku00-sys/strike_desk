import React, { useState, useCallback } from 'react';
import usePositions from '../hooks/usePositions';
import useOrders from '../hooks/useOrders';
import useRiskState, { RISK_POLL_MS } from '../hooks/useRiskState';
import apiClient from '../api/client';
import StatusPill from './StatusPill';
import { REQ } from '../utils/requestState';
import { apiBadgeView, formatClock, requestStatusText } from '../utils/stateLabels';

const money = (v, opts = {}) =>
  v == null ? '—' : `₹${Number(v).toLocaleString('en-IN', opts)}`;

const signedMoney = (v) =>
  v == null ? '—' : `${v >= 0 ? '+' : '−'}₹${Math.abs(Number(v)).toLocaleString('en-IN')}`;

// Says whether this panel's API call worked — not whether anything is "live".
const ApiBadge = ({ status, what }) => {
  const v = apiBadgeView(status);
  return <StatusPill className="text-[10px]" tone={v.tone} text={v.text} ariaLabel={`${what}: ${v.text.toLowerCase()}`} />;
};

// For panels whose list is empty: distinguish loading/failed from genuinely empty.
const emptyText = (status, what, none) => {
  if (status === REQ.OK) return none;
  if (status === REQ.LOADING) return `Loading ${what}…`;
  return `${what[0].toUpperCase()}${what.slice(1)} unavailable — ${requestStatusText(status)}`;
};

const NOT_EXPOSED = 'NOT EXPOSED';

function cooldownText(r) {
  if (r.cooldown_active == null) return NOT_EXPOSED;
  if (!r.cooldown_active) return 'None active';
  return r.cooldown_until ? `Active until ${formatClock(Date.parse(r.cooldown_until))}` : 'Active';
}

const Panel = ({ title, badge, children, footer }) => (
  <div className="bg-slate-800 rounded-lg p-4 border border-slate-700 flex flex-col justify-between shadow-lg">
    <div>
      <div className="flex items-center justify-between pb-2 mb-3 border-b border-slate-700/80">
        <h3 className="text-xs font-bold uppercase tracking-wider text-sky-400 font-mono">{title}</h3>
        {badge}
      </div>
      {children}
    </div>
    {footer}
  </div>
);

const Empty = ({ children }) => (
  <div className="py-6 text-center text-[11px] text-slate-500 font-mono">{children}</div>
);

export const TradingPanels = () => {
  const { positions, status: posStatus, refresh: refreshPositions } = usePositions();
  const { orders, status: ordStatus } = useOrders();
  const { riskState, status: riskStatus } = useRiskState();
  const riskOnline = riskStatus === REQ.OK;

  const [exitingSymbol, setExitingSymbol] = useState(null);
  const [exitError, setExitError] = useState(null);

  const handleExit = useCallback(async (pos) => {
    const side = pos.direction === 'LONG' ? 'SELL' : 'BUY';
    const qty = pos.quantity; // already absolute from normalize()
    setExitingSymbol(pos.symbol);
    setExitError(null);
    try {
      await apiClient.post('/api/v1/trading/orders', {
        symbol: pos.symbol,
        side,
        quantity: Math.round(qty),
        is_manual: true,
      });
      await refreshPositions();
    } catch (err) {
      // The API's error handler puts the reason in `message`; `detail` is FastAPI's default.
      const detail = err.response?.data?.message ?? err.response?.data?.detail ?? err.message ?? 'Exit order failed';
      setExitError(`${pos.symbol}: ${detail}`);
    } finally {
      setExitingSymbol(null);
    }
  }, [refreshPositions]);

  const lossCeiling = riskState.daily_loss_ceiling;
  const currentPnl = riskState.daily_pnl;

  // Only meaningful once both numbers are known.
  const lossUsagePct = (currentPnl != null && lossCeiling)
    ? (currentPnl < 0 ? Math.min(100, Math.round((Math.abs(currentPnl) / lossCeiling) * 100)) : 0)
    : null;
  const isLossApproaching = lossUsagePct != null && lossUsagePct >= 70;

  const netUnrealized = positions.length
    ? positions.reduce((acc, p) => acc + (p.pnl ?? 0), 0)
    : null;

  return (
    <div className="w-full grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 text-slate-100 select-none">

      {/* PANEL 1: OPEN POSITIONS */}
      <Panel
        title={`Open Positions (${positions.length})`}
        badge={<ApiBadge status={posStatus} what="Positions API" />}
        footer={
          <div className="mt-3 pt-2 border-t border-slate-700/50 flex justify-between text-[11px] text-slate-400 font-mono">
            <span>Unrealized Net:</span>
            <span className={`font-bold ${
              netUnrealized == null ? 'text-slate-500'
                : netUnrealized >= 0 ? 'text-emerald-400' : 'text-rose-400'
            }`}>
              {signedMoney(netUnrealized)}
            </span>
          </div>
        }
      >
        {positions.length === 0 ? (
          <Empty>{emptyText(posStatus, 'positions', 'No open positions')}</Empty>
        ) : (
          <div className="space-y-3">
            {positions.map((pos) => {
              const isProfit = (pos.pnl ?? 0) >= 0;
              return (
                <div key={pos.position_id} className="bg-slate-900/70 p-2.5 rounded border border-slate-700/60">
                  <div className="flex items-center justify-between text-xs font-bold">
                    <span className="text-slate-100 font-mono truncate">
                      {pos.symbol ?? '—'}
                      <span className="text-slate-400 font-normal">
                        {pos.direction ? ` - ${pos.direction}` : ''}{pos.lots ? ` ${pos.lots}` : ''}
                      </span>
                    </span>
                    <span className={`font-mono shrink-0 ${
                      pos.pnl == null ? 'text-slate-500' : isProfit ? 'text-emerald-400' : 'text-rose-400'
                    }`}>
                      {signedMoney(pos.pnl)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between text-[11px] text-slate-400 mt-1 font-mono">
                    <span>Entry: {money(pos.entry_price)}</span>
                    <span>LTP: {money(pos.ltp)}</span>
                  </div>
                  <div className="mt-2 flex justify-end">
                    <button
                      onClick={() => handleExit(pos)}
                      disabled={exitingSymbol === pos.symbol}
                      className={`text-[10px] font-bold font-mono px-2 py-0.5 rounded border transition-colors ${
                        exitingSymbol === pos.symbol
                          ? 'bg-slate-700/50 text-slate-500 border-slate-600 cursor-not-allowed'
                          : 'bg-rose-500/15 text-rose-400 border-rose-500/40 hover:bg-rose-500/30 cursor-pointer'
                      }`}
                    >
                      {exitingSymbol === pos.symbol ? 'EXITING…' : 'EXIT'}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
      {exitError && (
        <div className="col-span-full text-[11px] font-mono text-rose-400 bg-rose-500/10 border border-rose-500/30 rounded px-3 py-1.5">
          Exit failed — {exitError}
        </div>
      )}

      {/* PANEL 2: RECENT ORDERS */}
      <Panel
        title={`Recent Orders (${orders.length})`}
        badge={<ApiBadge status={ordStatus} what="Orders API" />}
        footer={
          <div className="mt-3 pt-2 border-t border-slate-700/50 text-[11px] font-mono text-right">
            {/* Reachability of the orders API only; no broker order-gateway status is exposed. */}
            <span className="text-slate-300">Orders API: </span>
            <span className={ordStatus === REQ.OK ? 'text-emerald-300' : ordStatus === REQ.LOADING ? 'text-slate-300' : 'text-rose-300'}>
              {ordStatus === REQ.OK ? 'Reachable' : requestStatusText(ordStatus)}
            </span>
          </div>
        }
      >
        {orders.length === 0 ? (
          <Empty>{emptyText(ordStatus, 'orders', 'No active orders')}</Empty>
        ) : (
          <div className="space-y-2.5">
            {orders.map((ord) => {
              let badgeColor = 'bg-slate-700 text-slate-300 border-slate-600';
              if (ord.status === 'FILLED' || ord.status === 'COMPLETE') {
                badgeColor = 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30';
              } else if (ord.status === 'PENDING' || ord.status === 'OPEN') {
                badgeColor = 'bg-amber-500/15 text-amber-400 border-amber-500/30';
              } else if (ord.status === 'REJECTED' || ord.status === 'CANCELLED') {
                badgeColor = 'bg-rose-500/15 text-rose-400 border-rose-500/30';
              }

              return (
                <div key={ord.order_id} className="bg-slate-900/70 p-2.5 rounded border border-slate-700/60 text-xs font-mono">
                  <div className="flex items-center justify-between mb-1 gap-2">
                    <span className="font-bold text-slate-200 truncate">{ord.order_id ?? '—'}</span>
                    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded border shrink-0 ${badgeColor}`}>
                      {ord.status}
                    </span>
                  </div>
                  <div className="text-[11px] text-slate-400 flex justify-between gap-2">
                    <span className="truncate">{ord.time} • {ord.symbol ?? '—'}</span>
                    <span className="text-slate-300 shrink-0">
                      Qty: {ord.quantity ?? '—'} | {ord.price == null ? 'MKT' : money(ord.price)}
                    </span>
                  </div>
                  {ord.reject_reason && (
                    <div className="text-[10px] text-rose-400/90 mt-1 break-words">{ord.reject_reason}</div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      {/* PANEL 3: RISK LIMITS */}
      <Panel
        title="Risk Limits"
        badge={<ApiBadge status={riskStatus} what="Risk API" />}
        footer={
          <div className="mt-3 pt-2 border-t border-slate-700/50 text-[11px] text-slate-300 font-mono flex justify-between">
            <span>Position Size:</span>
            <span className="text-slate-200">{riskOnline ? (riskState.position_size ?? NOT_EXPOSED) : '—'}</span>
          </div>
        }
      >
        {!riskOnline ? (
          <Empty>{emptyText(riskStatus, 'risk state', '')}</Empty>
        ) : (
          <div className="space-y-3.5 text-xs font-mono">
            <div>
              <div className="text-slate-400 text-[11px]">Daily P&amp;L:</div>
              <div className="text-sm font-black mt-0.5">
                <span className={
                  currentPnl == null ? 'text-slate-500'
                    : currentPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
                }>
                  {signedMoney(currentPnl)}
                </span>
                <span className="text-slate-400 font-normal text-xs ml-1.5">
                  (Ceiling: {money(lossCeiling)})
                </span>
              </div>
            </div>

            <div>
              <div className="text-slate-400 text-[11px]">Trades Today:</div>
              <div className="text-sm font-bold text-slate-100 mt-0.5">
                {riskState.trade_count ?? '—'} / {riskState.max_daily_trades ?? '—'}
              </div>
            </div>

            <div>
              <div className="flex justify-between text-[11px] text-slate-400 mb-1">
                <span>Loss Ceiling Usage</span>
                <span className={isLossApproaching ? 'text-rose-400 font-bold' : 'text-slate-300'}>
                  {lossUsagePct == null ? '—' : `${lossUsagePct}%`}
                </span>
              </div>
              <div className="w-full bg-slate-900 rounded-full h-2 overflow-hidden border border-slate-700">
                <div
                  className={`h-full transition-all duration-500 ${
                    isLossApproaching ? 'bg-red-500 animate-pulse' : 'bg-emerald-500'
                  }`}
                  style={{ width: `${lossUsagePct ?? 0}%` }}
                />
              </div>
            </div>
          </div>
        )}
      </Panel>

      {/* PANEL 4: SYSTEM STATUS */}
      <Panel
        title="System Status"
        badge={<ApiBadge status={riskStatus} what="Risk API" />}
        footer={
          <div className="mt-3 pt-2 border-t border-slate-700/50 text-[11px] text-slate-300 font-mono flex justify-between">
            <span>Risk state refresh:</span>
            <span className="text-slate-200">every {RISK_POLL_MS / 1000}s</span>
          </div>
        }
      >
        <div className="space-y-2.5 text-xs font-mono">
          <div className="flex items-center justify-between p-2 rounded bg-slate-900/80 border border-slate-700/60">
            <span className="text-slate-400">System State</span>
            {!riskOnline ? (
              <span className="font-bold text-slate-300">UNKNOWN ({requestStatusText(riskStatus)})</span>
            ) : riskState.emergency_halt_active ? (
              <span className="font-bold text-rose-400">🚨 HALTED</span>
            ) : (
              <span className="font-bold text-emerald-400 flex items-center gap-1">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-ping" />
                ACTIVE
              </span>
            )}
          </div>

          <div className="flex items-center justify-between px-1">
            <span className="text-slate-400">Cascade Brake:</span>
            <span className={`font-semibold ${
              riskState.cascade_brake === 'Active' ? 'text-rose-300' : 'text-slate-200'
            }`}>
              {riskOnline ? (riskState.cascade_brake ?? NOT_EXPOSED) : '—'}
            </span>
          </div>

          <div className="flex items-center justify-between px-1">
            <span className="text-slate-400">Cooldown:</span>
            <span className={`font-semibold ${riskState.cooldown_active ? 'text-amber-300' : 'text-slate-200'}`}>
              {riskOnline ? cooldownText(riskState) : '—'}
            </span>
          </div>
        </div>
      </Panel>

    </div>
  );
};

export default TradingPanels;
