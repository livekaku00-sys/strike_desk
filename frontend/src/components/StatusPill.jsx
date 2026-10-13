import React from 'react';

// Text always carries the state; colour is a secondary cue only. Shades are the
// -200/-300 range so small text keeps >= 4.5:1 contrast on the dark panels.
const TONES = {
  paper: 'bg-amber-500/15 text-amber-200 border-amber-500/60',
  live: 'bg-rose-500/20 text-rose-200 border-rose-500/70',
  ok: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/40',
  warn: 'bg-amber-500/10 text-amber-300 border-amber-500/40',
  error: 'bg-rose-500/10 text-rose-300 border-rose-500/40',
  muted: 'bg-slate-800 text-slate-300 border-slate-600',
};

export default function StatusPill({ tone = 'muted', text, ariaLabel, className = '', title }) {
  return (
    <span
      aria-label={ariaLabel || text}
      title={title}
      className={`inline-flex items-center gap-1 font-mono font-bold uppercase tracking-wide border rounded px-1.5 py-0.5 ${TONES[tone] || TONES.muted} ${className}`}
    >
      <span aria-hidden="true">●</span>
      <span>{text}</span>
    </span>
  );
}
