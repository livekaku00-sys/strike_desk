/**
 * Strike Desk V4.5 — Audit Log Page
 */
import React, { useState, useCallback, useEffect } from 'react';
import { risk as riskApi } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, LoadingBlock, EmptyBlock, PageHeader, ActionBtn, ErrorBlock,
} from '../../components/ui';

const ACTION_COLOR = {
  LOGIN: 'teal', LOGOUT: 'gray',
  HALT: 'red', HALT_RELEASE: 'green',
  ORDER: 'blue', ORDER_REJECT: 'red', ORDER_FAILED: 'red',
  POSITION_OPEN: 'green', POSITION_CLOSE: 'amber',
  MODE_CHANGE: 'purple', LIVE_ARM: 'red', LIVE_DISARM: 'amber',
  BROKER_REAUTH: 'teal', TOKEN_UPDATE: 'teal',
  RISK_CHANGE: 'amber', CONFIG_CHANGE: 'amber',
};

export default function AuditPage() {
  const [log, setLog]       = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr]       = useState(null);
  const [filter, setFilter] = useState('');

  const fetch = useCallback(async () => {
    try {
      const r = await riskApi.auditLog({ limit: 100 });
      const arr = r.data?.entries ?? r.data?.log ?? r.data ?? [];
      setLog(Array.isArray(arr) ? arr : []);
      setErr(null);
    } catch(e) {
      setErr(e.response?.data?.detail || e.message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, 15000); return () => clearInterval(t); }, [fetch]);

  const filtered = filter
    ? log.filter(e => JSON.stringify(e).toLowerCase().includes(filter.toLowerCase()))
    : log;

  return (
    <div>
      <PageHeader
        title="Audit Log"
        subtitle="Every important mutation — login, mode changes, orders, halts."
        right={<ActionBtn onClick={fetch} disabled={loading}>Refresh</ActionBtn>}
      />

      <div className="flex gap-2 mb-4">
        <input
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="Filter by action, symbol, operator…"
          className="flex-1 bg-[#111821] border border-[#1E2A3A] rounded px-3 py-1.5 text-[12px] font-mono text-[#F5F7FA] placeholder-[#4A5568] focus:outline-none focus:border-amber-500/60"
        />
        {filter && <ActionBtn onClick={() => setFilter('')}>Clear</ActionBtn>}
      </div>

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="AUDIT LOG UNAVAILABLE" detail={err} /> : (
        <Panel>
          <PanelHeader title={`${filtered.length} ENTRIES`} />
          {filtered.length === 0 ? (
            <EmptyBlock label="NO AUDIT ENTRIES" sub={filter ? 'No entries match filter' : 'Audit log is empty'} />
          ) : (
            <>
              <div className="grid grid-cols-[140px_120px_130px_80px_1fr_100px] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase tracking-wider text-[#4A5568]">
                <span>Timestamp</span><span>Operator</span><span>Action</span>
                <span>Entity</span><span>Detail</span><span>Result</span>
              </div>
              {filtered.map((e, i) => (
                <div key={i} className="grid grid-cols-[140px_120px_130px_80px_1fr_100px] gap-2 px-4 py-2.5 border-b border-[#1E2A3A]/40 hover:bg-[#111821]/50 text-[11px] font-mono items-center">
                  <span className="text-[#4A5568]">{e.timestamp ?? e.created_at ?? '—'}</span>
                  <span className="text-[#8D98A8] truncate">{e.operator ?? e.user ?? '—'}</span>
                  <StatusBadge text={e.action ?? e.event ?? '—'} color={ACTION_COLOR[e.action ?? e.event] ?? 'gray'} />
                  <span className="text-[#8D98A8] truncate">{e.entity ?? e.symbol ?? '—'}</span>
                  <span className="text-[#F5F7FA] truncate">{e.detail ?? e.message ?? e.reason ?? '—'}</span>
                  <StatusBadge text={e.result ?? 'OK'} color={e.result === 'FAILED' || e.result === 'ERROR' ? 'red' : 'green'} />
                </div>
              ))}
            </>
          )}
        </Panel>
      )}
    </div>
  );
}
