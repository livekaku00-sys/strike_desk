import React, { useState, useEffect } from 'react';
import { useNavigate, Navigate } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';

// Execution-broker options shown in the dropdown.
// 'paper' = PAPER mode; the others = LIVE mode via that specific broker.
const BROKER_OPTIONS = [
  {
    id: 'paper',
    label: 'Paper Trading',
    sublabel: 'Real live data · virtual money · zero risk',
    icon: (
      <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path strokeLinecap="round" strokeLinejoin="round"
          d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
      </svg>
    ),
    accent: 'amber',
    accentClasses: 'border-amber-500/70 bg-amber-500/10 text-amber-300',
    submitClasses: 'bg-amber-500 hover:bg-amber-400 text-slate-950',
    badgeClasses: 'bg-amber-900/30 text-amber-400 border border-amber-700/40',
    badgeLabel: 'PAPER',
  },
  {
    id: 'dhan',
    label: 'Dhan',
    sublabel: 'LIVE execution · recommended for F&O',
    icon: (
      <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path strokeLinecap="round" strokeLinejoin="round"
          d="M13 10V3L4 14h7v7l9-11h-7z" />
      </svg>
    ),
    accent: 'emerald',
    accentClasses: 'border-emerald-500/70 bg-emerald-500/10 text-emerald-300',
    submitClasses: 'bg-emerald-500 hover:bg-emerald-400 text-slate-950',
    badgeClasses: 'bg-emerald-900/30 text-emerald-400 border border-emerald-700/40',
    badgeLabel: 'LIVE',
  },
  {
    id: 'angelone',
    label: 'AngelOne',
    sublabel: 'LIVE execution · auto-TOTP refresh',
    icon: (
      <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path strokeLinecap="round" strokeLinejoin="round"
          d="M13 10V3L4 14h7v7l9-11h-7z" />
      </svg>
    ),
    accent: 'emerald',
    accentClasses: 'border-emerald-500/70 bg-emerald-500/10 text-emerald-300',
    submitClasses: 'bg-emerald-500 hover:bg-emerald-400 text-slate-950',
    badgeClasses: 'bg-emerald-900/30 text-emerald-400 border border-emerald-700/40',
    badgeLabel: 'LIVE',
  },
  {
    id: 'fyers',
    label: 'Fyers',
    sublabel: 'LIVE execution · paste daily token',
    icon: (
      <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path strokeLinecap="round" strokeLinejoin="round"
          d="M13 10V3L4 14h7v7l9-11h-7z" />
      </svg>
    ),
    accent: 'emerald',
    accentClasses: 'border-emerald-500/70 bg-emerald-500/10 text-emerald-300',
    submitClasses: 'bg-emerald-500 hover:bg-emerald-400 text-slate-950',
    badgeClasses: 'bg-emerald-900/30 text-emerald-400 border border-emerald-700/40',
    badgeLabel: 'LIVE',
  },
];

function useServerReady() {
  const [ready, setReady] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);

    fetch('/health', { signal: controller.signal })
      .then((r) => { if (!cancelled) setReady(r.ok); })
      .catch(() => { if (!cancelled) setReady(false); })
      .finally(() => clearTimeout(timer));

    return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
  }, []);

  return ready;
}

export default function Login() {
  const navigate   = useNavigate();
  const { login, loginGuest, isAuthenticated } = useAuthStore();
  const serverReady = useServerReady();

  // If already authenticated redirect immediately — no flash of the login form.
  if (isAuthenticated) return <Navigate to="/" replace />;

  const [brokerId, setBrokerId]         = useState('dhan');
  const [username, setUsername]         = useState('');
  const [password, setPassword]         = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading]           = useState(false);
  const [errorMsg, setErrorMsg]         = useState('');

  const selected = BROKER_OPTIONS.find((b) => b.id === brokerId) || BROKER_OPTIONS[1];
  const isLive = brokerId !== 'paper';

  const handleGuestAccess = async () => {
    setErrorMsg('');
    setLoading(true);
    const res = await loginGuest();
    setLoading(false);
    if (res.success) navigate('/guest-dashboard');
    else setErrorMsg(res.error);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setErrorMsg('');
    setLoading(true);
    const res = await login(username, password, brokerId);
    setLoading(false);
    if (res.success) {
      navigate('/broker-setup');
    } else {
      setErrorMsg(res.error);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-[440px] bg-slate-900 border border-slate-800 rounded-xl p-8 shadow-2xl">

        {/* Header */}
        <div className="text-center mb-6">
          <div className="flex items-center justify-center gap-2 mb-1">
            <span className="text-2xl">⚡</span>
            <h1 className="text-2xl font-bold tracking-tight text-white">Strike Desk</h1>
          </div>
          <p className="text-xs text-slate-400 font-medium">Institutional F&amp;O Trading Platform</p>
        </div>

        {/* Server status badge */}
        {serverReady === null && (
          <div className="bg-slate-800/60 border border-slate-700 rounded-lg py-2 px-3 mb-6 flex items-center justify-center gap-2.5">
            <span className="h-2 w-2 rounded-full bg-slate-500 animate-pulse flex-shrink-0" />
            <span className="text-xs font-medium text-slate-400">Connecting to server…</span>
          </div>
        )}
        {serverReady === true && (
          <div className="bg-emerald-950/40 border border-emerald-800/60 rounded-lg py-2 px-3 mb-6 flex items-center justify-center gap-2.5">
            <span className="relative flex h-2 w-2 flex-shrink-0">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
            </span>
            <span className="text-xs font-medium text-emerald-400">System ready for trading</span>
          </div>
        )}
        {serverReady === false && (
          <div className="bg-amber-950/40 border border-amber-800/60 rounded-lg py-2 px-3 mb-6 flex items-center justify-center gap-2.5">
            <span className="h-2 w-2 rounded-full bg-amber-500 animate-pulse flex-shrink-0" />
            <span className="text-xs font-medium text-amber-400">Server starting — login may take a moment</span>
          </div>
        )}

        {/* Error banner */}
        {errorMsg && (
          <div className="bg-red-950/50 border border-red-800/60 rounded-lg p-3 mb-5 text-xs text-red-300">
            {errorMsg}
          </div>
        )}

        {/* ── Execution Broker selector ─────────────────────────────── */}
        <div className="mb-5">
          <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-2.5">
            Select Broker
          </label>

          {/* Custom-styled broker picker */}
          <div className="grid grid-cols-2 gap-2">
            {BROKER_OPTIONS.map((opt) => {
              const active = opt.id === brokerId;
              return (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => { setBrokerId(opt.id); setErrorMsg(''); }}
                  className={[
                    'flex items-start gap-2.5 p-3 rounded-lg border text-left transition-all duration-150',
                    active
                      ? opt.accentClasses
                      : 'bg-slate-950 border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-300',
                  ].join(' ')}
                >
                  <span className={`mt-0.5 ${active ? '' : 'opacity-50'}`}>{opt.icon}</span>
                  <span className="min-w-0">
                    <span className="flex items-center gap-1.5 mb-0.5">
                      <span className="text-xs font-bold leading-tight">{opt.label}</span>
                      <span className={`text-[9px] font-bold px-1 py-0.5 rounded ${active ? opt.badgeClasses : 'bg-slate-800 text-slate-500'}`}>
                        {opt.badgeLabel}
                      </span>
                    </span>
                    <span className="text-[9px] text-slate-500 leading-tight block">{opt.sublabel}</span>
                  </span>
                </button>
              );
            })}
          </div>

          {/* F&O routing note for LIVE brokers */}
          {isLive && (
            <p className="mt-2.5 text-[10px] text-slate-500 leading-relaxed">
              <span className="text-slate-400 font-medium">Note:</span> F&amp;O orders (options &amp; futures) always execute via Dhan regardless of selection — Dhan is the sole F&amp;O execution broker.
            </p>
          )}
        </div>

        {/* ── Operator credentials ──────────────────────────────────── */}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-1.5 uppercase tracking-wider">
              Username
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2.5 text-sm text-slate-100 focus:outline-none focus:border-amber-500 transition-colors"
            />
            <p className="text-[11px] text-slate-500 mt-1">Operator credentials required</p>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-1.5 uppercase tracking-wider">
              Password
            </label>
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                placeholder="••••••••"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2.5 pr-10 text-sm text-slate-100 focus:outline-none focus:border-amber-500 transition-colors"
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                className="absolute inset-y-0 right-0 flex items-center px-3 text-slate-500 hover:text-slate-300 transition-colors"
              >
                {showPassword ? (
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"
                      d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                  </svg>
                ) : (
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"
                      d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"
                      d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                  </svg>
                )}
              </button>
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className={`w-full mt-2 py-3 font-bold rounded-lg text-sm transition-colors duration-150 shadow-lg disabled:opacity-50 ${selected.submitClasses}`}
          >
            {loading
              ? 'Authenticating…'
              : brokerId === 'paper'
              ? 'Sign In — Paper Trading'
              : `Sign In — LIVE via ${selected.label}`}
          </button>
        </form>

        {/* Guest access link */}
        <div className="mt-5 pt-4 border-t border-slate-800/80 flex items-center justify-between">
          <p className="text-[11px] text-slate-500">
            No TOTP stored · JWT auth · Strike Desk V4
          </p>
          <button
            type="button"
            onClick={handleGuestAccess}
            disabled={loading}
            className="text-[11px] text-slate-400 hover:text-slate-200 underline underline-offset-2 transition-colors disabled:opacity-40"
          >
            Guest access →
          </button>
        </div>
      </div>
    </div>
  );
}
