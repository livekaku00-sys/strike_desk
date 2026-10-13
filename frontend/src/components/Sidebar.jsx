import React from 'react';
import { useCanTrade } from '../store/authStore';
import AssetClassSelector from './AssetClassSelector';

const BROKER_OPTIONS = [
  { id: 'angelone', label: 'Angel One (SmartAPI)' },
  { id: 'dhan', label: 'DhanHQ' },
  { id: 'fyers', label: 'Fyers API v3' },
];

export const Sidebar = ({
  open,
  activeBroker,
  onBrokerChange,
  activeMode,
  onModeChange,
  activeTab,
  onTabChange,
  backendOnline,
}) => {
  const canTrade = useCanTrade();
  return (
    <aside
      className={`${
        open ? 'w-72' : 'w-0 -translate-x-full'
      } transition-all duration-300 ease-in-out bg-[#0E121A] border-r border-slate-800/80 flex flex-col z-30 shrink-0 select-none overflow-y-auto`}
    >
      {/* Block 1: Brand & Operator Identity */}
      <div className="p-4 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <span className="text-2xl text-sky-400">⚡</span>
          <div>
            <h1 className="text-base font-black tracking-wider text-slate-50 font-mono">
              STRIKE DESK <span className="text-xs text-sky-400 font-normal">v4.0</span>
            </h1>
            <p className="text-[10px] text-slate-400 tracking-tight">Institutional Quant Control</p>
          </div>
        </div>

        <div className="mt-3 p-2.5 bg-[#131824] rounded border border-slate-800 text-xs">
          <div className="text-[10px] uppercase text-slate-400 font-semibold">Active Operator</div>
          <div className="font-bold text-slate-100">Monuj Phukon</div>
          <div className="text-[10px] text-emerald-400 mt-1 flex items-center gap-1 font-mono">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
            AUTHENTICATED (MONUJ)
          </div>
        </div>
      </div>

      {/* Blocks 2 and 3 are execution controls — the class lock and the broker an
          order would route to. An observation-only session does not render them at
          all, rather than showing them disabled. */}
      {canTrade && (
        <>
          {/* Block 2: Asset Class Selector (Blueprint O8) */}
          <div className="p-3 border-b border-slate-800">
            <AssetClassSelector currentMode={activeMode} onModeChange={onModeChange} />
          </div>

          {/* Block 3: Routing Broker Selector */}
          <div className="p-3 border-b border-slate-800">
            <label className="block text-[11px] font-bold uppercase tracking-wider text-sky-400 mb-1 font-mono">
              🎯 Routing Broker
            </label>
            <select
              value={activeBroker}
              onChange={(e) => onBrokerChange(e.target.value)}
              className="w-full bg-[#161D2B] text-slate-200 text-xs font-mono rounded border border-slate-700 p-2 outline-none"
            >
              {BROKER_OPTIONS.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.label}
                </option>
              ))}
            </select>
          </div>
        </>
      )}

      {/* Block 4: View Navigation */}
      <div className="p-3 flex-1">
        <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400 mb-2 font-mono">
          Navigation
        </div>
        <nav className="space-y-1 text-xs font-semibold">
          <button
            onClick={() => onTabChange('alerts')}
            className={`w-full flex items-center gap-2 px-3 py-2 rounded transition ${
              activeTab === 'alerts'
                ? 'bg-sky-500/15 text-sky-400 border border-sky-500/30'
                : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'
            }`}
          >
            <span>⚡</span> 10-Alert Matrix
          </button>
          <button
            onClick={() => onTabChange('risk')}
            className={`w-full flex items-center gap-2 px-3 py-2 rounded transition ${
              activeTab === 'risk'
                ? 'bg-sky-500/15 text-sky-400 border border-sky-500/30'
                : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'
            }`}
          >
            <span>🛡️</span> Risk Limits & Halts
          </button>
          <button
            onClick={() => onTabChange('positions')}
            className={`w-full flex items-center gap-2 px-3 py-2 rounded transition ${
              activeTab === 'positions'
                ? 'bg-sky-500/15 text-sky-400 border border-sky-500/30'
                : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'
            }`}
          >
            <span>💼</span> Positions & Orders
          </button>
        </nav>
      </div>

      {/* Block 5: Heartbeat Footer */}
      <div className="p-3 border-t border-slate-800 text-[11px] text-slate-400 flex items-center justify-between font-mono">
        <div className="flex items-center gap-1.5">
          <span className={`h-2 w-2 rounded-full ${backendOnline ? 'bg-emerald-400' : 'bg-rose-500'}`} />
          <span>{backendOnline ? 'Port 8765 Live' : 'Offline'}</span>
        </div>
        <span>v4.0</span>
      </div>
    </aside>
  );
};

export default Sidebar;
