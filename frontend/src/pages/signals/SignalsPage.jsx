/**
 * Strike Desk V4.5 — Signals Page
 * Live signals table with drawer detail. Tier, score, state, gates.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { strategy } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, DataTable, LoadingBlock, EmptyBlock,
  PageHeader, ActionBtn, ErrorBlock,
} from '../../components/ui';

const POLL = 5000;

const TIER_COLOR = { 'A+': 'green', A: 'blue', B: 'amber', C: 'gray' };
const STATE_COLOR = {
  APPROVED_FOR_EXECUTION: 'green',
  ALERT_ONLY:    'blue',
  QUEUED:        'amber',
  BLOCKED:       'red',
  EXPIRED:       'gray',
  EXECUTED:      'teal',
  CANCELLED:     'gray',
};

function SignalDrawer({ signal, onClose }) {
  if (!signal) return null;
  const gateEntries = signal.gates ? Object.entries(signal.gates) : [];
  const indicators  = signal.indicators ?? signal.indicator_evidence ?? {};

  return (
    <div className="fixed inset-y-0 right-0 w-96 bg-[#111821] border-l border-[#1E2A3A] z-40 flex flex-col shadow-2xl">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#1E2A3A] flex-shrink-0">
        <div>
          <div className="text-[13px] font-bold text-[#F5F7FA] font-mono">{signal.symbol}</div>
          <div className="text-[10px] text-[#8D98A8]">Signal ID: {signal.signal_id ?? '—'}</div>
        </div>
        <button onClick={onClose} className="text-[#4A5568] hover:text-[#F5F7FA] text-xl">×</button>
      </div>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">

        <div className="flex gap-2 flex-wrap">
          <StatusBadge text={signal.confidence_tier ?? '—'} color={TIER_COLOR[signal.confidence_tier] ?? 'gray'} />
          <StatusBadge text={signal.state ?? '—'}           color={STATE_COLOR[signal.state] ?? 'gray'} />
          <StatusBadge text={signal.direction === 'LONG' ? 'CE ↑' : 'PE ↓'} color={signal.direction === 'LONG' ? 'green' : 'red'} />
        </div>

        <div>
          <div className="text-[10px] font-bold uppercase tracking-wider text-[#4A5568] mb-2">SIGNAL DETAILS</div>
          <div className="space-y-0">
            {[
              ['Asset Class',       signal.asset_class],
              ['Entry Price',       signal.entry_price != null ? `₹ ${signal.entry_price}` : null],
              ['Stop Loss',         signal.stop_loss_price != null ? `₹ ${signal.stop_loss_price}` : null],
              ['Target',            signal.target_price != null ? `₹ ${signal.target_price}` : null],
              ['Confidence Score',  signal.confidence_score != null ? `${signal.confidence_score.toFixed(1)} / 100` : null],
              ['Created',           signal.created_at],
              ['Strategy',          signal.strategy ?? signal.mode],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between py-1.5 border-b border-[#1E2A3A] last:border-0 text-[12px]">
                <span className="text-[#8D98A8]">{k}</span>
                <span className="font-mono text-[#F5F7FA]">{v ?? '—'}</span>
              </div>
            ))}
          </div>
        </div>

        {Object.keys(indicators).length > 0 && (
          <div>
            <div className="text-[10px] font-bold uppercase tracking-wider text-[#4A5568] mb-2">INDICATOR EVIDENCE</div>
            <div className="space-y-0">
              {Object.entries(indicators).map(([k, v]) => (
                <div key={k} className="flex justify-between py-1 border-b border-[#1E2A3A] last:border-0 text-[11px]">
                  <span className="text-[#8D98A8]">{k}</span>
                  <span className="font-mono text-[#F5F7FA]">{v != null ? String(v) : '—'}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {gateEntries.length > 0 && (
          <div>
            <div className="text-[10px] font-bold uppercase tracking-wider text-[#4A5568] mb-2">HARD GATES</div>
            <div className="space-y-1">
              {gateEntries.map(([gate, result]) => (
                <div key={gate} className="flex items-center justify-between py-1">
                  <span className="text-[11px] text-[#F5F7FA]">{gate}</span>
                  <StatusBadge text={result ? 'PASS' : 'FAIL'} color={result ? 'green' : 'red'} />
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="p-3 bg-[#151C25] border border-[#1E2A3A] rounded text-[11px] text-[#8D98A8]">
          <div className="font-bold text-[#F5F7FA] mb-1 text-[12px]">WHY THIS SIGNAL EXISTS</div>
          <div>{signal.reason ?? signal.why ?? 'No reason provided by backend'}</div>
        </div>

        <div className="p-3 bg-[#151C25] border border-[#1E2A3A] rounded text-[11px] text-[#8D98A8]">
          <div className="font-bold text-[#F5F7FA] mb-1 text-[12px]">TRADEABILITY</div>
          <div>{signal.state === 'APPROVED_FOR_EXECUTION' ? '✓ Approved for execution — all gates passed' :
               signal.state === 'BLOCKED' ? `✗ Blocked — ${signal.block_reason ?? 'gates failed'}` :
               signal.state ?? 'Unknown'}</div>
        </div>
      </div>
    </div>
  );
}

export default function SignalsPage() {
  const [signals, setSignals]   = useState([]);
  const [loading, setLoading]   = useState(true);
  const [err, setErr]           = useState(null);
  const [selected, setSelected] = useState(null);

  const fetch = useCallback(async () => {
    try {
      const r = await strategy.signals();
      const arr = r.data?.signals ?? r.data ?? [];
      setSignals(Array.isArray(arr) ? arr : []);
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const cols = [
    { key: 'confidence_tier', label: 'Tier', render: v => <StatusBadge text={v ?? '—'} color={TIER_COLOR[v] ?? 'gray'} /> },
    { key: 'confidence_score', label: 'Score', right: true, render: v => v != null ? v.toFixed(0) : '—' },
    { key: 'symbol',     label: 'Symbol' },
    { key: 'asset_class',label: 'Asset Class' },
    { key: 'direction',  label: 'Dir', render: v => <span className={v === 'LONG' ? 'text-green-300' : 'text-red-400'}>{v === 'LONG' ? 'CE ↑' : 'PE ↓'}</span> },
    { key: 'entry_price', label: 'Entry', right: true, render: v => v != null ? `₹${v}` : '—' },
    { key: 'stop_loss_price', label: 'SL',     right: true, render: v => v != null ? `₹${v}` : '—' },
    { key: 'target_price',    label: 'Target', right: true, render: v => v != null ? `₹${v}` : '—' },
    { key: 'state', label: 'State', render: v => <StatusBadge text={v ?? '—'} color={STATE_COLOR[v] ?? 'gray'} /> },
    { key: 'created_at', label: 'Age', render: v => <span className="text-[#8D98A8]">{v ? v.substring(11, 19) : '—'}</span> },
  ];

  return (
    <div>
      <PageHeader
        title="Signals"
        subtitle="Live strategy signals — A+ prioritized. Click row for detail."
        right={<ActionBtn onClick={fetch} disabled={loading}>Refresh</ActionBtn>}
      />

      {selected && <SignalDrawer signal={selected} onClose={() => setSelected(null)} />}

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="SIGNALS UNAVAILABLE" detail={err} /> : (
        <Panel>
          <PanelHeader title={`${signals.length} SIGNAL${signals.length !== 1 ? 'S' : ''}`}
            subtitle="From /api/v1/trading/signals" />
          <div>
            {signals.length === 0 ? (
              <EmptyBlock
                label="NO VALID SIGNALS"
                sub="No signal currently satisfies all hard gates."
              />
            ) : (
              <div className="divide-y divide-[#1E2A3A]">
                {[...signals]
                  .sort((a, b) => (b.confidence_score ?? 0) - (a.confidence_score ?? 0))
                  .map((s, i) => (
                    <button
                      key={s.signal_id ?? i}
                      onClick={() => setSelected(s)}
                      className="w-full text-left hover:bg-[#151C25] transition-colors"
                    >
                      <div className="grid grid-cols-[80px_60px_1fr_80px_60px_80px_80px_120px] gap-2 px-4 py-3 items-center text-[12px] font-mono">
                        <StatusBadge text={s.confidence_tier ?? '?'} color={TIER_COLOR[s.confidence_tier] ?? 'gray'} />
                        <span className="text-right text-[#F5F7FA]">{s.confidence_score?.toFixed(0) ?? '—'}</span>
                        <span className="text-[#F5F7FA] truncate">{s.symbol}</span>
                        <span className={`text-center font-bold ${s.direction === 'LONG' ? 'text-green-300' : 'text-red-400'}`}>
                          {s.direction === 'LONG' ? 'CE ↑' : 'PE ↓'}
                        </span>
                        <span className="text-right text-[#8D98A8]">{s.entry_price != null ? `₹${s.entry_price}` : '—'}</span>
                        <span className="text-right text-red-400">{s.stop_loss_price != null ? `₹${s.stop_loss_price}` : '—'}</span>
                        <span className="text-right text-green-300">{s.target_price != null ? `₹${s.target_price}` : '—'}</span>
                        <StatusBadge text={s.state ?? '?'} color={STATE_COLOR[s.state] ?? 'gray'} />
                      </div>
                    </button>
                  ))}
              </div>
            )}
          </div>
        </Panel>
      )}
    </div>
  );
}
