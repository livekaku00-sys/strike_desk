/**
 * Strike Desk V4.5 — System Events Page
 * Real-time event stream from backend.
 */
import React, { useState, useCallback, useEffect, useRef } from 'react';
import { system as sysApi } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, LoadingBlock, EmptyBlock, PageHeader, ActionBtn, ErrorBlock,
} from '../../components/ui';

const SEV_COLOR = { INFO: 'gray', SUCCESS: 'green', WARNING: 'amber', ERROR: 'red', CRITICAL: 'red' };
const POLL = 5000;

export default function EventsPage() {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState('ALL');
  const bottomRef = useRef(null);

  const fetch = useCallback(async () => {
    if (paused) return;
    try {
      const r = await sysApi.events({ limit: 100 });
      const arr = r.data?.events ?? r.data ?? [];
      setEvents(Array.isArray(arr) ? arr : []);
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }, [paused]);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const sevs = ['ALL', 'CRITICAL', 'ERROR', 'WARNING', 'SUCCESS', 'INFO'];
  const filtered = filter === 'ALL' ? events : events.filter(e => (e.severity ?? 'INFO') === filter);

  return (
    <div>
      <PageHeader
        title="System Events"
        subtitle="Real-time event stream. Auto-refreshes every 5s."
        right={
          <div className="flex gap-2">
            <ActionBtn onClick={() => setPaused(v => !v)} color={paused ? 'green' : 'amber'}>
              {paused ? '▶ Resume' : '⏸ Pause'}
            </ActionBtn>
            <ActionBtn onClick={fetch} disabled={loading}>Refresh</ActionBtn>
          </div>
        }
      />

      <div className="flex gap-1 mb-4">
        {sevs.map(s => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-2.5 py-1 text-[10px] font-mono font-bold rounded border transition-colors ${
              filter === s ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' : 'bg-[#151C25] text-[#4A5568] border-[#1E2A3A] hover:text-[#8D98A8]'
            }`}>{s}</button>
        ))}
      </div>

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="EVENTS UNAVAILABLE" detail={err} /> : (
        <Panel>
          <PanelHeader title={`${filtered.length} EVENTS`} subtitle={paused ? '⏸ PAUSED' : '● LIVE'} />
          {filtered.length === 0 ? (
            <EmptyBlock label="NO EVENTS" sub="No events match current filter" />
          ) : (
            <div className="max-h-[600px] overflow-y-auto">
              {[...filtered].reverse().map((e, i) => (
                <div key={i} className={`flex items-start gap-3 px-4 py-2.5 border-b border-[#1E2A3A]/40 hover:bg-[#111821]/30 ${
                  e.severity === 'CRITICAL' || e.severity === 'ERROR' ? 'bg-red-950/10' : ''
                }`}>
                  <span className="text-[10px] font-mono text-[#4A5568] min-w-[85px] mt-0.5">{
                    e.timestamp ? String(e.timestamp).substring(11, 22) : '—'
                  }</span>
                  <StatusBadge text={e.severity ?? 'INFO'} color={SEV_COLOR[e.severity ?? 'INFO']} />
                  <div className="flex-1">
                    <div className="text-[12px] font-mono font-semibold text-[#F5F7FA]">{e.event ?? e.type ?? '—'}</div>
                    {e.detail && <div className="text-[11px] text-[#8D98A8] mt-0.5">{e.detail}</div>}
                  </div>
                </div>
              ))}
              <div ref={bottomRef} />
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}
