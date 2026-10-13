/**
 * Strike Desk V4.5 — Shared UI Primitives
 * Terminal-style components. Import from here, not from individual files.
 */
import React, { useState, useEffect } from 'react';

// ── Panel / Card ──────────────────────────────────────────────────────────────
export function Panel({ children, className = '' }) {
  return (
    <div className={`bg-[#111821] border border-[#1E2A3A] rounded-lg ${className}`}>
      {children}
    </div>
  );
}

export function Card({ children, className = '' }) {
  return (
    <div className={`bg-[#151C25] border border-[#1E2A3A] rounded-lg p-4 ${className}`}>
      {children}
    </div>
  );
}

export function PanelHeader({ title, subtitle, right, className = '' }) {
  return (
    <div className={`flex items-center justify-between px-4 py-3 border-b border-[#1E2A3A] ${className}`}>
      <div>
        <h2 className="text-[13px] font-semibold text-[#F5F7FA]">{title}</h2>
        {subtitle && <p className="text-[11px] text-[#8D98A8] mt-0.5">{subtitle}</p>}
      </div>
      {right && <div className="flex items-center gap-2">{right}</div>}
    </div>
  );
}

// ── Section header ────────────────────────────────────────────────────────────
export function SectionHead({ label, right }) {
  return (
    <div className="flex items-center justify-between mb-3">
      <h3 className="text-[10px] font-bold uppercase tracking-[0.1em] text-[#4A5568]">{label}</h3>
      {right}
    </div>
  );
}

// ── Status badges ─────────────────────────────────────────────────────────────
export function StatusBadge({ text, color = 'gray', className = '' }) {
  const cls = {
    green:  'bg-green-500/15  text-green-300  border-green-500/40',
    amber:  'bg-amber-500/15  text-amber-300  border-amber-500/40',
    red:    'bg-red-500/15    text-red-400    border-red-500/40',
    teal:   'bg-teal-500/15   text-teal-300   border-teal-500/40',
    blue:   'bg-blue-500/15   text-blue-300   border-blue-500/40',
    purple: 'bg-purple-500/15 text-purple-300 border-purple-500/40',
    gray:   'bg-[#1E2A3A]     text-[#8D98A8]  border-[#2A3F5A]',
  };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-mono font-bold tracking-wider ${cls[color]} ${className}`}>
      <span className={`w-1.5 h-1.5 rounded-full bg-current ${color === 'red' ? 'pulse-dot' : ''}`} />
      {text}
    </span>
  );
}

// ── KV row ────────────────────────────────────────────────────────────────────
export function KV({ label, value, valueColor = '', mono = true }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5 border-b border-[#1E2A3A] last:border-0">
      <span className="text-[11px] text-[#8D98A8] flex-shrink-0">{label}</span>
      <span className={`${mono ? 'font-mono-terminal' : ''} text-[12px] text-right ${valueColor || 'text-[#F5F7FA]'}`}>
        {value ?? <span className="text-[#4A5568]">—</span>}
      </span>
    </div>
  );
}

// ── Data table ────────────────────────────────────────────────────────────────
export function DataTable({ cols, rows, emptyMsg = 'No data', className = '' }) {
  return (
    <div className={`overflow-x-auto ${className}`}>
      <table className="w-full min-w-max text-[12px]">
        <thead>
          <tr className="border-b border-[#1E2A3A]">
            {cols.map(c => (
              <th key={c.key}
                className={`px-3 py-2 text-[10px] font-bold uppercase tracking-wider text-[#4A5568] text-left whitespace-nowrap ${c.right ? 'text-right' : ''}`}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={cols.length} className="px-3 py-8 text-center text-[#4A5568] font-mono text-[11px]">
                {emptyMsg}
              </td>
            </tr>
          ) : rows.map((row, i) => (
            <tr key={i} className="border-b border-[#1E2A3A]/50 hover:bg-[#111821]/50 transition-colors">
              {cols.map(c => (
                <td key={c.key}
                  className={`px-3 py-2.5 text-[#F5F7FA] font-mono-terminal whitespace-nowrap ${c.right ? 'text-right' : ''} ${c.className ?? ''}`}>
                  {c.render ? c.render(row[c.key], row) : (row[c.key] ?? <span className="text-[#4A5568]">—</span>)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Meter bar ─────────────────────────────────────────────────────────────────
export function Meter({ pct, color = 'green', label, value }) {
  const fill = {
    green:  'bg-green-500',
    amber:  'bg-amber-400',
    red:    'bg-red-500',
    teal:   'bg-teal-400',
    blue:   'bg-blue-500',
  };
  const safePct = Math.min(100, Math.max(0, pct ?? 0));
  return (
    <div className="flex items-center gap-3 text-[12px]">
      {label && <span className="text-[#8D98A8] min-w-[160px] text-[11px]">{label}</span>}
      <div className="flex-1 h-1.5 bg-[#1E2A3A] rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${fill[color]}`} style={{ width: `${safePct}%` }} />
      </div>
      {value !== undefined && <span className="text-[#F5F7FA] font-mono-terminal text-right min-w-[80px]">{value}</span>}
    </div>
  );
}

// ── Loading / Error / Empty ───────────────────────────────────────────────────
export function LoadingBlock({ label = 'Loading…' }) {
  return (
    <div className="flex items-center justify-center py-12 text-[#4A5568] font-mono text-[11px]">
      <span className="animate-pulse">{label}</span>
    </div>
  );
}

export function ErrorBlock({ label, detail }) {
  return (
    <div className="p-4 bg-red-950/20 border border-red-800/40 rounded-lg">
      <div className="text-[12px] font-bold text-red-400 font-mono">{label ?? 'BACKEND ERROR'}</div>
      {detail && <div className="mt-1 text-[11px] text-red-300/70 font-mono">{detail}</div>}
    </div>
  );
}

export function EmptyBlock({ label, sub }) {
  return (
    <div className="flex flex-col items-center justify-center py-14 gap-2">
      <div className="text-[13px] font-bold text-[#4A5568] font-mono uppercase tracking-wider">{label}</div>
      {sub && <div className="text-[11px] text-[#4A5568]/70">{sub}</div>}
    </div>
  );
}

export function BackendRequired({ feature }) {
  return (
    <div className="flex items-center gap-2 p-3 bg-[#151C25] border border-[#2A3F5A] rounded text-[11px] font-mono text-[#8D98A8]">
      <span className="text-amber-400">⚠</span>
      BACKEND INTEGRATION REQUIRED
      {feature && <span className="text-[#4A5568]">— {feature}</span>}
    </div>
  );
}

// ── Action button ─────────────────────────────────────────────────────────────
export function ActionBtn({ children, onClick, color = 'default', disabled = false, className = '' }) {
  const cols = {
    default: 'bg-[#1E2A3A] hover:bg-[#2A3F5A] text-[#F5F7FA] border-[#2A3F5A]',
    green:   'bg-green-600/20 hover:bg-green-600/30 text-green-300 border-green-500/50',
    amber:   'bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border-amber-500/50',
    red:     'bg-red-600/20   hover:bg-red-600/30   text-red-400   border-red-500/50',
    danger:  'bg-red-700/30   hover:bg-red-700/40   text-red-300   border-red-600/60',
  };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider border rounded transition-colors font-mono disabled:opacity-40 disabled:cursor-not-allowed ${cols[color]} ${className}`}
    >
      {children}
    </button>
  );
}

// ── Page header ───────────────────────────────────────────────────────────────
export function PageHeader({ title, subtitle, right }) {
  return (
    <div className="flex items-start justify-between mb-5">
      <div>
        <h1 className="text-[18px] font-bold text-[#F5F7FA] tracking-tight">{title}</h1>
        {subtitle && <p className="text-[12px] text-[#8D98A8] mt-1">{subtitle}</p>}
      </div>
      {right && <div className="flex items-center gap-2">{right}</div>}
    </div>
  );
}

// ── Age / freshness indicator ─────────────────────────────────────────────────
export function Freshness({ ageMs, warnMs = 30000, errorMs = 60000 }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!ageMs) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [ageMs]);
  if (!ageMs) return <span className="text-[#4A5568] text-[10px] font-mono">—</span>;
  const age = now - ageMs;
  const sec = Math.round(age / 1000);
  const label = sec < 60 ? `${sec}s ago` : `${Math.round(sec / 60)}m ago`;
  const color = age > errorMs ? 'text-red-400' : age > warnMs ? 'text-amber-400' : 'text-green-400';
  return <span className={`text-[10px] font-mono ${color}`}>● {label}</span>;
}
