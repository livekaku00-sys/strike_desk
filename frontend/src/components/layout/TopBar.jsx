/**
 * Strike Desk V4.5 — Top Safety Bar
 * Persistent across all pages. Shows live system state from backend only.
 * Never shows state derived from frontend logic.
 */
import React, { useState, useEffect } from 'react';
import { useAuthStore } from '../../store/authStore';

// IST market-hours fallback (same logic as useSystemStatus)
function computeMarketOpen() {
  const ist = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  const day = ist.getDay();
  if (day === 0 || day === 6) return false;
  const hhmm = ist.getHours() * 100 + ist.getMinutes();
  return hhmm >= 915 && hhmm < 1530;
}

function Dot({ color }) {
  const colors = {
    green:  'bg-green-400',
    amber:  'bg-amber-400',
    red:    'bg-red-500 pulse-dot',
    teal:   'bg-teal-400',
    gray:   'bg-slate-500',
  };
  return <span className={`inline-block w-1.5 h-1.5 rounded-full ${colors[color] ?? colors.gray}`} />;
}

function StatusChip({ label, value, dot, urgent, className = '' }) {
  const textColor = urgent ? 'text-red-400' : dot === 'green' ? 'text-green-300' : dot === 'amber' ? 'text-amber-300' : 'text-slate-300';
  return (
    <div className={`flex items-center gap-1.5 px-2.5 py-1 border border-[#1E2A3A] rounded text-[10px] font-mono font-semibold tracking-wider ${urgent ? 'bg-red-950/40 border-red-800/40' : 'bg-[#111821]'} ${className}`}>
      <Dot color={dot} />
      <span className="text-[#8D98A8]">{label}</span>
      <span className={textColor}>{value}</span>
    </div>
  );
}

function ISTClock() {
  const [time, setTime] = useState('');
  useEffect(() => {
    const fmt = () => new Date().toLocaleTimeString('en-IN', {
      timeZone: 'Asia/Kolkata', hour12: false,
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }) + ' IST';
    setTime(fmt());
    const t = setInterval(() => setTime(fmt()), 1000);
    return () => clearInterval(t);
  }, []);
  return <span className="font-mono-terminal text-[11px] text-[#8D98A8]">{time}</span>;
}

export default function TopBar({ systemStatus }) {
  const { user, role, logout } = useAuthStore();
  const {
    execMode,
    riskState,
    activeBroker,
    brokersData,
    mktStatus,
    stratMode,
    isHalted,
    isLive,
  } = systemStatus ?? {};

  const mode        = execMode?.data;
  const modeLoading = execMode?.loading && !execMode?.data;
  const activeB     = activeBroker?.data ?? '—';
  const rd          = riskState?.data;
  const mktData     = mktStatus?.data;

  const brokersList      = brokersData?.data ?? [];
  const activeBrokerInfo = brokersList.find(b => b.broker_name === activeB);
  const brokerOk         = activeBrokerInfo?.is_authenticated;
  const riskClear        = rd && !rd.emergency_halt_active && !rd.loss_ceiling_breached;

  // MARKET: backend field first, then IST time check (re-evaluated on each render via clock)
  const marketOpenBackend = mktData?.market_open ?? mktData?.is_market_open;
  const marketOpen =
    marketOpenBackend !== undefined ? !!marketOpenBackend : computeMarketOpen();
  const marketLabel = marketOpenBackend !== undefined
    ? (marketOpen ? 'OPEN' : 'CLOSED')                     // backend-authoritative
    : (marketOpen ? 'OPEN*' : 'CLOSED*');                  // * = local time estimate
  const marketColor = marketOpen ? 'green' : 'gray';

  // DATA: backend feed_status first, then derive from tick data freshness
  const feedStatus  = mktData?.feed_status ?? mktData?.data_feed_status;
  const ticksFresh  = mktStatus?.age && (Date.now() - mktStatus.age) < 60_000;
  const dataLabel   = feedStatus
    ? String(feedStatus).toUpperCase()
    : mktStatus?.loading && !mktData ? '…'
    : mktStatus?.error ? 'ERROR'
    : ticksFresh ? 'LIVE' : mktData ? 'STALE' : 'UNKNOWN';
  const dataColor   = dataLabel === 'LIVE' ? 'teal'
    : dataLabel === 'STALE' || dataLabel === 'ERROR' ? 'amber' : 'gray';

  // STRATEGY: from backend stratMode
  const stratLabel  = stratMode?.loading && !stratMode?.data ? '…'
    : stratMode?.error ? 'UNKNOWN'
    : stratMode?.data ?? 'UNKNOWN';
  const stratColor  = stratMode?.data ? 'teal' : 'gray';

  const modeColor   = mode === 'LIVE' ? 'red' : mode === 'PAPER' ? 'amber' : 'gray';
  const riskColor   = isHalted ? 'red' : riskClear ? 'green' : 'amber';
  const riskLabel   = isHalted ? 'HALTED' : riskClear ? 'CLEAR' : 'WARNING';
  const brokerColor = brokerOk ? 'green' : 'red';
  const brokerLabel = activeB === '—' ? '—'
    : brokerOk ? activeB.toUpperCase() : `${activeB.toUpperCase()} ⚠`;

  return (
    <header className="flex-shrink-0 h-11 bg-[#0B0F14] border-b border-[#1E2A3A] flex items-center justify-between px-4 gap-3">

      {/* LEFT: Brand */}
      <div className="flex items-center gap-3 flex-shrink-0">
        <div>
          <div className="flex items-center gap-1.5">
            <span className="text-[13px] font-bold text-[#F5F7FA] tracking-tight">STRIKE DESK</span>
            <span className="text-[10px] font-mono text-amber-400 font-semibold">V4.5</span>
          </div>
          <div className="text-[9px] text-[#4A5568] uppercase tracking-widest leading-none">
            Algorithmic Trading Control Terminal
          </div>
        </div>
      </div>

      {/* CENTER: Status chips */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <StatusChip
          label="MARKET"
          value={marketLabel}
          dot={marketColor}
        />
        <StatusChip
          label="DATA"
          value={dataLabel}
          dot={dataColor}
        />
        <StatusChip
          label="BROKER"
          value={brokerLabel}
          dot={brokerColor}
          urgent={!brokerOk}
        />
        <StatusChip
          label="RISK"
          value={riskLabel}
          dot={riskColor}
          urgent={isHalted}
        />
        <StatusChip
          label="EXECUTION"
          value={modeLoading ? '…' : (mode ?? 'UNKNOWN')}
          dot={modeColor}
          urgent={isLive}
        />
        <StatusChip
          label="STRATEGY"
          value={stratLabel}
          dot={stratColor}
        />
      </div>

      {/* RIGHT: Clock + User + Logout */}
      <div className="flex items-center gap-3 flex-shrink-0">
        <ISTClock />
        <div className="border-l border-[#1E2A3A] pl-3 flex items-center gap-2">
          <div className="text-right">
            <div className="text-[11px] font-semibold text-[#F5F7FA] capitalize">{user?.username ?? '—'}</div>
            <div className={`text-[9px] uppercase tracking-wider font-mono font-bold ${role === 'operator' ? 'text-amber-400' : 'text-slate-500'}`}>
              {role === 'operator' ? 'OPERATOR' : 'GUEST'}
            </div>
          </div>
          <button
            onClick={logout}
            title="Sign out"
            className="ml-1 p-1.5 rounded border border-[#1E2A3A] bg-[#111821] hover:bg-red-950/40 hover:border-red-800/50 hover:text-red-400 text-[#8D98A8] transition-colors"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"
                d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
            </svg>
          </button>
        </div>
      </div>
    </header>
  );
}
