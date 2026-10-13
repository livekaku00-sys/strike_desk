import React, { useState, useEffect } from 'react';
import { useAuthStore } from '../../store/authStore';
import { useSharedExecutionMode } from '../../hooks/useExecutionMode';
import StatusPill from '../StatusPill';
import { executionModeView } from '../../utils/stateLabels';

export default function Header({ isConnected = false, onAuditTrail }) {
  const { user, logout, role } = useAuthStore();
  const execView = executionModeView(useSharedExecutionMode());
  const [currentTime, setCurrentTime] = useState('');

  useEffect(() => {
    const update = () => {
      setCurrentTime(
        new Date().toLocaleTimeString('en-IN', {
          timeZone: 'Asia/Kolkata',
          hour12: false,
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        }) + ' IST'
      );
    };
    update();
    const t = setInterval(update, 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <header className="h-16 flex-shrink-0 bg-slate-900 border-b border-slate-800 flex items-center justify-between px-6">
      {/* Left */}
      <div>
        <div className="flex items-center gap-2.5">
          <span className="text-sm font-bold text-white tracking-wide">Strike Desk V4</span>
          {/* Dashboard push channel (port 8765). Says nothing about execution mode
              or broker connectivity. */}
          <StatusPill
            className="text-[10px]"
            tone={isConnected ? 'ok' : 'muted'}
            text={isConnected ? 'WS CONNECTED' : 'WS UNAVAILABLE'}
            ariaLabel={`Dashboard WebSocket: ${isConnected ? 'connected' : 'unavailable'}`}
            title="Dashboard push channel. Does not indicate execution mode or broker connectivity."
          />
        </div>
        <p className="text-[11px] text-slate-500 mt-0.5">Algorithmic F&amp;O Trading</p>
      </div>

      {/* Right */}
      <div className="flex items-center gap-4">
        {onAuditTrail && (
          <button
            onClick={onAuditTrail}
            className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg border border-slate-700 text-xs font-mono flex items-center gap-1.5 transition-colors"
          >
            📋 Audit Trail
          </button>
        )}

        {role === 'guest' ? (
          <span
            title="Read-only session: viewing is allowed, trading and settings are not."
            className="text-[10px] font-mono font-bold uppercase tracking-wider px-2 py-1 rounded bg-slate-800 text-slate-400 border border-slate-700"
          >
            👁 Guest — Read-only
          </span>
        ) : (
          // Read from GET /trading/execution-mode only. Loading, error and timeout
          // are shown as such — never as PAPER or LIVE.
          <span role="status" aria-live="polite" className="flex items-center gap-2">
            <span className="text-[10px] font-mono font-bold uppercase tracking-wider text-slate-300">
              Execution mode
            </span>
            <StatusPill
              className="text-xs px-2.5 py-1"
              tone={execView.tone}
              text={execView.text}
              ariaLabel={execView.aria}
              title={execView.mode === 'PAPER'
                ? 'Paper mode: live market data, virtual money. No broker order API is called.'
                : execView.mode === 'LIVE'
                  ? 'LIVE mode: real capital. Orders are sent to the broker.'
                  : 'The execution mode could not be read from the server.'}
            />
          </span>
        )}

        <div className="text-right border-l border-slate-800 pl-4">
          <p className="text-xs font-semibold text-slate-100 capitalize">
            {user?.username || '—'}
          </p>
          <p className="text-[11px] font-mono text-slate-400">{currentTime}</p>
        </div>

        <button
          onClick={logout}
          title="Sign out"
          className="p-2 bg-slate-800/80 hover:bg-red-950/60 hover:text-red-400 border border-slate-700/60 hover:border-red-800/60 rounded-lg text-slate-400 transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="2"
              d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"
            />
          </svg>
        </button>
      </div>
    </header>
  );
}
