/**
 * Strike Desk V4.5 — Overview / Command Center
 * Answers within 5 seconds: can the bot trade? What's happening now?
 */
import React, { useState, useEffect, useCallback } from 'react';
import { useOutletContext, Link } from 'react-router-dom';
import { execution, strategy, market } from '../api/services';
import {
  Panel, PanelHeader, Card, SectionHead, StatusBadge,
  KV, Meter, LoadingBlock, EmptyBlock, PageHeader, Freshness, ActionBtn,
} from '../components/ui';

const POLL = 5000;

function ReadinessPanel({ systemStatus }) {
  const { blockers, readyToTrade, isHalted, isLive, brokerOk, riskClear, execMode, riskState, activeBroker } = systemStatus;
  const rd = riskState?.data;
  const mode = execMode?.data;

  const status = isHalted        ? { label: 'HALTED',        color: 'red',   dot: 'pulse' }
               : !brokerOk       ? { label: 'BROKER ERROR',  color: 'red',   dot: 'pulse' }
               : !riskClear      ? { label: 'RISK WARNING',  color: 'amber', dot: '' }
               : readyToTrade    ? { label: 'READY TO TRADE',color: 'green', dot: '' }
               :                   { label: 'STANDBY',       color: 'amber', dot: '' };

  const dotColor = { green: 'bg-green-400', amber: 'bg-amber-400', red: 'bg-red-500 pulse-dot' };

  return (
    <Panel className="mb-5">
      <div className="p-5 flex items-center gap-4">
        <div className={`w-3 h-3 rounded-full flex-shrink-0 ${dotColor[status.color]} ${status.dot}`} />
        <div className="flex-1">
          <div className={`text-[18px] font-bold font-mono tracking-wide ${
            status.color === 'green' ? 'text-green-300' :
            status.color === 'amber' ? 'text-amber-300' : 'text-red-400'
          }`}>
            SYSTEM STATUS — {status.label}
          </div>
          {blockers.length === 0 && (
            <div className="text-[11px] text-[#8D98A8] mt-0.5">
              All systems nominal · {mode ?? '—'} mode · Broker {activeBroker?.data?.toUpperCase() ?? '—'}
            </div>
          )}
        </div>
        <StatusBadge text={mode ?? '—'} color={mode === 'LIVE' ? 'red' : 'amber'} />
      </div>

      {blockers.length > 0 && (
        <div className="px-5 pb-4">
          <div className="text-[11px] font-bold text-[#8D98A8] mb-2">{blockers.length} BLOCKER{blockers.length > 1 ? 'S' : ''}</div>
          <div className="space-y-1.5">
            {blockers.map((b, i) => (
              <div key={i} className={`flex items-center gap-2 px-3 py-1.5 rounded text-[12px] font-mono ${
                b.level === 'error' ? 'bg-red-950/30 border border-red-800/40 text-red-300' : 'bg-amber-950/20 border border-amber-800/30 text-amber-300'
              }`}>
                <span>{b.level === 'error' ? '🔴' : '🟡'}</span>
                {b.msg}
              </div>
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}

function TodayPanel({ systemStatus }) {
  const rd = systemStatus?.riskState?.data;
  const loading = systemStatus?.riskState?.loading && !rd;

  // Prefer backend-reported limits; fall back to known system defaults
  const ceiling   = rd?.max_daily_loss ?? rd?.daily_loss_limit ?? 2500;
  const tradeMax  = rd?.max_trade_count ?? rd?.daily_trade_limit ?? 5;
  const lossUsed = rd ? Math.abs(Math.min(0, rd.realized_pnl ?? 0)) : 0;
  const lossUsedPct = (lossUsed / ceiling) * 100;
  const tradeUsedPct = ((rd?.trade_count ?? 0) / tradeMax) * 100;

  const pnlColor = (v) => {
    if (v == null) return 'text-[#F5F7FA]';
    return v >= 0 ? 'text-green-300' : 'text-red-400';
  };

  return (
    <Panel>
      <PanelHeader title="TODAY" subtitle="From backend risk state" />
      <div className="p-4">
        {loading ? <LoadingBlock /> : (
          <div className="space-y-2">
            <KV label="Realized P&L"     value={rd?.realized_pnl != null ? `₹ ${rd.realized_pnl.toFixed(2)}` : null} valueColor={pnlColor(rd?.realized_pnl)} />
            <KV label="Total P&L"        value={rd?.total_risk_pnl != null ? `₹ ${rd.total_risk_pnl.toFixed(2)}` : null} valueColor={pnlColor(rd?.total_risk_pnl)} />
            <KV label="Trade Count"      value={rd ? `${rd.trade_count ?? 0} / ${tradeMax}` : null} />
            <KV label="Open Positions"   value={rd?.open_positions_count ?? null} />
            <KV label="Consec. Losses"   value={rd?.consecutive_losses ?? null} valueColor={(rd?.consecutive_losses ?? 0) >= 2 ? 'text-red-400' : ''} />
            <KV label="Cooldown"         value={rd?.cooldown_active ? `ACTIVE until ${rd?.cooldown_until ?? '—'}` : 'INACTIVE'} valueColor={rd?.cooldown_active ? 'text-amber-300' : 'text-green-300'} />
            <div className="pt-2 space-y-2">
              <Meter label="Daily Loss Used" pct={lossUsedPct} color={lossUsedPct > 80 ? 'red' : lossUsedPct > 50 ? 'amber' : 'green'}
                value={`₹ ${lossUsed.toFixed(0)} / ₹ ${ceiling}`} />
              <Meter label="Trade Limit"     pct={tradeUsedPct} color={tradeUsedPct >= 100 ? 'red' : tradeUsedPct >= 60 ? 'amber' : 'green'}
                value={`${rd?.trade_count ?? 0} / ${tradeMax}`} />
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}

function MarketPanel({ mktStatus }) {
  const ticks   = mktStatus?.data;
  const loading = mktStatus?.loading && !ticks;
  const tickArr = Array.isArray(ticks) ? ticks
    : (ticks?.ticks ? Object.values(ticks.ticks) : (ticks ? Object.values(ticks) : []));

  // Pull counts from backend fields when available
  const instrumentCount = ticks?.instrument_count ?? ticks?.tracked_count ?? tickArr.length ?? null;
  const feedSource      = ticks?.feed_source ?? ticks?.ws_source ?? null;
  const pcrStale        = ticks?.pcr_stale_seconds ?? ticks?.pcr_staleness_s ?? null;
  const feedStatus      = ticks?.feed_status ?? ticks?.data_feed_status ?? null;

  return (
    <Panel>
      <PanelHeader title="MARKET DATA" />
      <div className="p-4">
        {loading ? <LoadingBlock /> : (
          <div className="space-y-2">
            <KV label="Tracked instruments" value={instrumentCount != null ? instrumentCount : '—'} />
            <KV label="Primary source"      value={feedSource ?? '—'} />
            <KV label="Feed status"         value={feedStatus ?? '—'} />
            <KV label="Active ticks"        value={tickArr.length > 0 ? tickArr.length : null} />
            <KV label="Feed age"            value={<Freshness ageMs={mktStatus?.age} warnMs={5000} errorMs={15000} />} mono={false} />
            <KV label="PCR staleness (s)"   value={pcrStale != null ? String(pcrStale) : '—'} />
          </div>
        )}
      </div>
    </Panel>
  );
}

function ActivePositionPanel() {
  const [positions, setPositions] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);

  const fetch = useCallback(async () => {
    try {
      const r = await execution.positions();
      const arr = r.data?.positions ?? r.data ?? [];
      setPositions(Array.isArray(arr) ? arr : []);
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const pos = positions?.[0];

  const pnlColor = (v) => v >= 0 ? 'text-green-300' : 'text-red-400';

  return (
    <Panel>
      <PanelHeader title="CURRENT POSITION" right={
        <Link to="/positions" className="text-[10px] font-mono text-[#8D98A8] hover:text-teal-300">
          View all →
        </Link>
      } />
      <div className="p-4">
        {loading ? <LoadingBlock /> :
         err     ? <div className="text-[11px] text-red-400 font-mono">{err}</div> :
         !pos    ? (
          <EmptyBlock label="NO OPEN POSITION" sub="The execution engine is currently flat." />
         ) : (
          <div className="space-y-2">
            <KV label="Symbol"    value={pos.symbol} />
            <KV label="Direction" value={pos.direction} valueColor={pos.direction === 'LONG' ? 'text-green-300' : 'text-red-400'} />
            <KV label="Quantity"  value={pos.quantity} />
            <KV label="Entry"     value={pos.entry_price != null ? `₹ ${pos.entry_price}` : null} />
            <KV label="LTP"       value={pos.current_price != null ? `₹ ${pos.current_price}` : null} />
            <KV label="P&L"       value={pos.unrealized_pnl != null ? `₹ ${pos.unrealized_pnl.toFixed(2)}` : null}
              valueColor={pos.unrealized_pnl != null ? pnlColor(pos.unrealized_pnl) : ''} />
            <KV label="Stop Loss" value={pos.stop_loss_price != null ? `₹ ${pos.stop_loss_price}` : null} valueColor="text-red-400" />
            <KV label="Target"    value={pos.target_price != null ? `₹ ${pos.target_price}` : null} valueColor="text-green-300" />
            <KV label="Trail SL"  value={pos.trail_sl_price != null ? `₹ ${pos.trail_sl_price}` : null} />
            <KV label="Duration"  value={pos.opened_at ?? null} />
          </div>
        )}
      </div>
    </Panel>
  );
}

function SignalsPanel() {
  const [signals, setSignals] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetch = useCallback(async () => {
    try {
      const r = await strategy.signals();
      const arr = r.data?.signals ?? r.data ?? [];
      setSignals(Array.isArray(arr) ? arr : []);
    } catch { setSignals([]); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const tierColor = { 'A+': 'green', A: 'blue', B: 'amber', C: 'gray' };

  return (
    <Panel>
      <PanelHeader title="ACTIVE SIGNALS" right={
        <Link to="/signals" className="text-[10px] font-mono text-[#8D98A8] hover:text-teal-300">View all →</Link>
      } />
      <div className="p-4">
        {loading ? <LoadingBlock /> :
         !signals?.length ? (
          <EmptyBlock label="NO VALID SIGNALS" sub="No signal currently satisfies all hard gates." />
         ) : (
          <div className="space-y-2">
            {signals.slice(0, 5).map((s, i) => (
              <div key={i} className="flex items-center gap-2 py-1.5 border-b border-[#1E2A3A]">
                <StatusBadge text={s.confidence_tier ?? '?'} color={tierColor[s.confidence_tier] ?? 'gray'} />
                <span className="font-mono-terminal text-[11px] text-[#F5F7FA] flex-1 truncate">{s.symbol}</span>
                <span className={`text-[11px] font-mono font-bold ${s.direction === 'LONG' ? 'text-green-300' : 'text-red-400'}`}>
                  {s.direction === 'LONG' ? 'CE ↑' : 'PE ↓'}
                </span>
                <span className="text-[10px] font-mono text-[#8D98A8]">{s.confidence_score?.toFixed(0) ?? '—'}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Panel>
  );
}

export default function Overview() {
  const ctx = useOutletContext();
  const { systemStatus } = ctx ?? {};

  return (
    <div>
      <PageHeader title="Command Center" subtitle="Strike Desk V4.5 — System overview and trading readiness" />

      <ReadinessPanel systemStatus={systemStatus} />

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
        <TodayPanel systemStatus={systemStatus} />
        <MarketPanel mktStatus={systemStatus?.mktStatus} />
        <ActivePositionPanel />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
        <SignalsPanel />

        {/* Quick links */}
        <Panel>
          <PanelHeader title="QUICK ACTIONS" subtitle="Operator shortcuts" />
          <div className="p-4 grid grid-cols-2 gap-2">
            {[
              { to: '/brokers',   label: 'Broker Setup',    desc: '02' },
              { to: '/risk',      label: 'Risk Engine',     desc: '05' },
              { to: '/execution', label: 'Execution',       desc: '06' },
              { to: '/checklist', label: 'Daily Checklist', desc: '08' },
              { to: '/positions', label: 'Positions',       desc: '' },
              { to: '/audit',     label: 'Audit Log',       desc: '' },
            ].map(({ to, label, desc }) => (
              <Link
                key={to}
                to={to}
                className="flex items-center gap-2 px-3 py-2.5 bg-[#151C25] border border-[#1E2A3A] rounded hover:border-[#2A3F5A] hover:bg-[#1E2A3A] transition-colors"
              >
                {desc && <span className="text-[9px] font-mono text-[#4A5568] font-bold">{desc}</span>}
                <span className="text-[12px] text-[#F5F7FA]">{label}</span>
                <span className="ml-auto text-[#4A5568] text-[10px]">→</span>
              </Link>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
