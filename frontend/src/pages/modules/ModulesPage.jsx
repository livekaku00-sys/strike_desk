/**
 * Strike Desk V4.5 — Other Modules (07)
 * 6 inactive strategy modules — intentionally disabled, not broken.
 */
import React from 'react';
import { Panel, PanelHeader, StatusBadge, PageHeader } from '../../components/ui';

const MODULES = [
  { id: 'AUTO_OPTIONS',       label: 'Index Options',    underlying: 'NIFTY, BANKNIFTY, SENSEX',
    sl: '−20% of entry', gate: 'PCR staleness 900s', broker: 'Dhan (F&O)', note: 'Needs index PCR integration' },
  { id: 'AUTO_FUTURES',       label: 'Index Futures',    underlying: 'NIFTY-FUT, BANKNIFTY-FUT',
    sl: '0.4% of entry', gate: 'EMA9 + momentum', broker: 'Dhan (F&O)', note: 'Needs futures spread logic' },
  { id: 'AUTO_STOCK_FUTURES', label: 'Stock Futures',    underlying: 'Individual equity FUT',
    sl: '1.0% of entry', gate: 'NSE quarterly lot sizes', broker: 'Dhan (F&O)', note: 'Needs lot size sync' },
  { id: 'AUTO_CASH_INTRADAY', label: 'Cash Intraday',    underlying: 'Equity (MIS margin)',
    sl: '0.75% of entry', gate: 'Volume + EMA9', broker: 'Dhan / AngelOne', note: 'Needs MIS product type' },
  { id: 'AUTO_CASH_SWING',    label: 'Cash Swing',       underlying: '2–10 day holds',
    sl: 'Per swing logic', gate: 'No intraday square-off', broker: 'Dhan / AngelOne', note: 'Needs overnight risk gating' },
  { id: 'AUTO_CASH_DELIVERY', label: 'Cash Delivery',    underlying: 'CNC equity',
    sl: 'Portfolio level', gate: 'Full capital required', broker: 'Dhan / AngelOne', note: 'Needs portfolio P&L tracking' },
];

export default function ModulesPage() {
  return (
    <div>
      <PageHeader
        title="Other Modules"
        subtitle="07 — Inactive strategy modules. Ready to wire. Activate via mode switch when enabled."
      />

      <div className="mb-4 px-3 py-2 bg-[#151C25] border border-[#1E2A3A] rounded text-[11px] font-mono text-[#8D98A8]">
        These modules are coded and tested. They activate by switching the active mode.
        Dashboard panels exist but are intentionally disabled until enabled in a future release.
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {MODULES.map(m => (
          <div key={m.id} className="bg-[#0B0F14] border border-[#1E2A3A]/60 rounded-lg overflow-hidden opacity-75 hover:opacity-90 transition-opacity">
            <div className="px-4 py-3 border-b border-[#1E2A3A]/50 flex items-center justify-between">
              <div>
                <div className="text-[12px] font-mono font-bold text-[#4A5568]">{m.id}</div>
                <div className="text-[11px] text-[#8D98A8] mt-0.5">{m.label}</div>
              </div>
              <StatusBadge text="INACTIVE" color="gray" />
            </div>
            <div className="p-4 space-y-0">
              {[
                ['Underlying',  m.underlying],
                ['Stop Loss',   m.sl],
                ['Key Gate',    m.gate],
                ['Broker',      m.broker],
                ['Status',      `READY · ${m.note}`],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between py-1.5 border-b border-[#1E2A3A]/40 last:border-0 text-[11px]">
                  <span className="text-[#4A5568]">{k}</span>
                  <span className="font-mono text-[#8D98A8] text-right ml-2">{v}</span>
                </div>
              ))}
            </div>
            <div className="px-4 py-2 bg-[#111821]/50 border-t border-[#1E2A3A]/50">
              <span className="text-[9px] font-mono font-bold uppercase tracking-wider text-[#2A3F5A]">
                READY · NOT WIRED
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
