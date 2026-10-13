/**
 * Observation-only dashboard for guest sessions.
 *
 * Strictly shows: signal alert matrix + live market clock.
 * Strictly excludes: broker status, execution controls, trading panels,
 * halt button, paper/live toggles, sidebar.
 *
 * This is a self-contained layout — it does NOT use DashboardLayout so
 * none of the operator chrome can accidentally leak in.
 */
import React, { useState, useEffect } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuthStore, useIsGuest, ROLE_GUEST } from '../store/authStore';
import AlertMatrix from '../components/AlertMatrix';
import { useWebSocket } from '../hooks/useWebSocket';
import StatusPill from '../components/StatusPill';

// ── Market timing ────────────────────────────────────────────────────────────
// The backend exposes no market/session status (and no holiday calendar), so the
// page does not guess one from the clock. Only the IST time is shown.

function MarketClock() {
  const [currentTime, setCurrentTime] = useState('');

  useEffect(() => {
    const tick = () => {
      setCurrentTime(
        new Date().toLocaleTimeString('en-IN', {
          timeZone: 'Asia/Kolkata', hour12: false,
          hour: '2-digit', minute: '2-digit', second: '2-digit',
        }) + ' IST'
      );
    };
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="flex items-center gap-3 px-4 py-2 rounded-lg bg-slate-800/50 border border-slate-700/60">
      <div title="The backend does not expose market/session status.">
        <p className="text-xs font-bold tracking-wide text-slate-300">MARKET STATUS: NOT EXPOSED</p>
      </div>
      <span className="ml-4 text-xs font-mono text-slate-300 border-l border-slate-700 pl-3">{currentTime}</span>
    </div>
  );
}

// ── Guest header ─────────────────────────────────────────────────────────────

function GuestHeader({ isConnected }) {
  const { user, logout } = useAuthStore();

  return (
    <header className="h-16 flex-shrink-0 bg-slate-950 border-b-2 border-slate-700 flex items-center justify-between px-6">
      {/* Left: brand */}
      <div className="flex items-center gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-bold text-white tracking-wide">Strike Desk V4</span>
            <StatusPill
              className="text-[10px]"
              tone={isConnected ? 'ok' : 'muted'}
              text={isConnected ? 'WS CONNECTED' : 'WS UNAVAILABLE'}
              ariaLabel={`Dashboard WebSocket: ${isConnected ? 'connected' : 'unavailable'}`}
            />
          </div>
          <p className="text-[11px] text-slate-500 mt-0.5">Algorithmic F&amp;O Trading</p>
        </div>
      </div>

      {/* Centre: prominent READ-ONLY badge */}
      <div
        aria-label="Guest read-only session — no trading actions available"
        className="flex items-center gap-2 px-4 py-1.5 rounded-lg border-2 border-slate-600 bg-slate-800 select-none"
      >
        <span className="h-2 w-2 rounded-full bg-slate-400" />
        <span className="text-xs font-black font-mono tracking-widest uppercase text-slate-300">
          GUEST MODE — READ ONLY
        </span>
        <span className="h-2 w-2 rounded-full bg-slate-400" />
      </div>

      {/* Right: username + logout */}
      <div className="flex items-center gap-4">
        <div className="text-right">
          <p className="text-xs font-semibold text-slate-300 capitalize">{user?.username || 'guest'}</p>
          <p className="text-[11px] text-slate-500">observation session</p>
        </div>
        <button
          onClick={logout}
          title="Sign out"
          className="p-2 bg-slate-800/80 hover:bg-red-950/60 hover:text-red-400 border border-slate-700/60 hover:border-red-800/60 rounded-lg text-slate-400 transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"
              d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
          </svg>
        </button>
      </div>
    </header>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function GuestDashboard() {
  const role = useAuthStore((s) => s.role);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const { isConnected } = useWebSocket(['ticks', 'signals']);

  // If an operator somehow lands here, send them to the full dashboard.
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  if (role !== ROLE_GUEST) return <Navigate to="/dashboard" replace />;

  return (
    <div className="min-h-screen bg-[#0B0F19] text-slate-100 flex flex-col">
      <GuestHeader isConnected={isConnected} />

      <main className="flex-1 overflow-y-auto px-6 py-5 max-w-screen-xl mx-auto w-full space-y-5">
        {/* Status bar */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <MarketClock />
          <div className="px-3 py-2 rounded-lg bg-slate-900 border border-slate-700/60 text-[11px] text-slate-500 font-mono">
            Signals refresh every 5 s — live market data only. No orders, positions or broker state in this view.
          </div>
        </div>

        {/* Signal alert matrix — always read-only for guests */}
        {/* No active class: guests do not read the execution lock, and a hard-coded
            one would mark the wrong class as executing. */}
        <AlertMatrix activeMode={null} forceReadOnly />
      </main>

      {/* Minimal footer: just market timing signal, no halt / trade controls */}
      <footer className="h-9 flex-shrink-0 bg-slate-950 border-t border-slate-800 flex items-center px-6 gap-4">
        <span className="text-[10px] font-mono text-slate-600 uppercase tracking-wider">
          Strike Desk V4 — Guest Session
        </span>
        <span className="text-[10px] font-mono text-slate-700">
          All controls require an operator login
        </span>
      </footer>
    </div>
  );
}
